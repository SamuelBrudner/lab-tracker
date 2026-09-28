"""Kedro project hooks: one staged Lab Tracker note per pipeline run.

Register in the Kedro project's ``src/<package>/settings.py``::

    from lab_tracker_client.integrations.kedro import LabTrackerHooks

    HOOKS = (LabTrackerHooks(),)

``LabTrackerHooks`` implements Kedro's pipeline hook specs
(``before_pipeline_run``, ``after_pipeline_run``, ``on_pipeline_error``).
After a run, or when it fails, it records the pipeline's *free* inputs
(``pipeline.inputs()``, the datasets no node produces) and free outputs
(``pipeline.outputs()``, the datasets no node consumes) as pointers resolved
from the catalog's dataset file paths — local files are fingerprinted, remote
(``s3://``, ``gs://``, ...) paths are pointer-only, and in-memory datasets and
parameters are counted but not recorded. Intermediate datasets are Kedro's
mechanical lineage and are deliberately left to Kedro.

Kedro is not imported: the hook methods carry the same ``kedro_impl`` marker
``kedro.framework.hooks.hook_impl`` (a ``pluggy.HookimplMarker("kedro")``)
would set, so this module imports without Kedro installed. The hooks never
raise into the Kedro run.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, TypeVar

from lab_tracker_client.pipeline_capture import PipelineRun, capture_pipeline_run, utc_now

_F = TypeVar("_F", bound=Callable[..., Any])
HOOK_NAMESPACE = "kedro"
_PARAMETER_NAMES = frozenset({"parameters"})
_REMOTE_LOCAL_PROTOCOLS = frozenset({"", "file", "local"})


def _hook_impl(function: _F) -> _F:
    """Mark ``function`` as a Kedro hook implementation without importing pluggy.

    Mirrors ``pluggy.HookimplMarker("kedro")()``: pluggy's plugin manager
    finds implementations through the ``kedro_impl`` attribute.
    """

    setattr(
        function,
        f"{HOOK_NAMESPACE}_impl",
        {
            "wrapper": False,
            "hookwrapper": False,
            "optionalhook": False,
            "tryfirst": False,
            "trylast": False,
            "specname": None,
        },
    )
    return function


class LabTrackerHooks:
    """Kedro hooks that record each pipeline run as one staged note.

    Keyword arguments mirror ``lt pipeline report``: ``project``, ``question``
    and ``session`` declare where the note belongs (the project otherwise comes
    from ``LAB_TRACKER_PROJECT_ID`` or the project's ``lt_ids.json``);
    ``drain=False`` only queues the event.
    """

    def __init__(
        self,
        *,
        project: str | None = None,
        question: str | None = None,
        session: str | None = None,
        label: str | None = None,
        drain: bool = True,
        request_draft: bool = False,
        hash_max_bytes: int | None = None,
        max_artifacts: int | None = None,
    ) -> None:
        self.project = project
        self.question = question
        self.session = session
        self.label = label
        self.drain = drain
        self.request_draft = request_draft
        self.hash_max_bytes = hash_max_bytes
        self.max_artifacts = max_artifacts
        self.last_result: dict[str, Any] | None = None
        self._started: dict[str, str] = {}

    @_hook_impl
    def before_pipeline_run(
        self, run_params: Mapping[str, Any], pipeline: Any, catalog: Any
    ) -> None:
        """Remember when the run started."""

        try:
            self._started[_run_id(run_params)] = utc_now()
        except Exception:  # noqa: BLE001 - hooks never break a Kedro run.
            return

    @_hook_impl
    def after_pipeline_run(
        self,
        run_params: Mapping[str, Any],
        run_result: Any,
        pipeline: Any,
        catalog: Any,
    ) -> None:
        """Record a successful run."""

        self._record("success", run_params, pipeline, catalog, error=None)

    @_hook_impl
    def on_pipeline_error(
        self,
        error: BaseException,
        run_params: Mapping[str, Any],
        pipeline: Any,
        catalog: Any,
    ) -> None:
        """Record a failed run with the error message."""

        self._record("error", run_params, pipeline, catalog, error=error)

    def _record(
        self,
        status: str,
        run_params: Mapping[str, Any],
        pipeline: Any,
        catalog: Any,
        *,
        error: BaseException | None,
    ) -> None:
        try:
            params = dict(run_params or {})
            run_id = _run_id(params)
            run = kedro_run(
                params,
                pipeline,
                catalog,
                status=status,
                error=error,
                started_at=self._started.pop(run_id, None),
                label=self.label,
            )
            self.last_result = capture_pipeline_run(
                run,
                cwd=params.get("project_path") or None,
                project_id=self.project,
                session=self.session,
                question_id=self.question,
                drain=self.drain,
                request_draft=self.request_draft,
                hash_max_bytes=self.hash_max_bytes,
                max_artifacts=self.max_artifacts,
            )
        except Exception as exc:  # noqa: BLE001 - hooks never break a Kedro run.
            self.last_result = {"action": "failed", "error": str(exc)}


def kedro_run(
    run_params: Mapping[str, Any],
    pipeline: Any,
    catalog: Any,
    *,
    status: str,
    error: BaseException | None = None,
    started_at: str | None = None,
    label: str | None = None,
) -> PipelineRun:
    """Build the :class:`PipelineRun` for one Kedro pipeline run."""

    inputs, input_skipped = _declared(_names(pipeline, "inputs"), catalog, role="input")
    outputs, output_skipped = _declared(_names(pipeline, "outputs"), catalog, role="output")
    pipeline_name = _pipeline_name(run_params)
    details = [f"- Pipeline: `{pipeline_name}`"]
    if run_params.get("env"):
        details.append(f"- Kedro environment: `{run_params['env']}`")
    for key in ("tags", "node_names", "from_nodes", "to_nodes", "from_inputs", "to_outputs"):
        value = run_params.get(key)
        if value:
            shown = ", ".join(str(item) for item in list(value)[:20])
            details.append(f"- {key.replace('_', ' ').capitalize()}: {shown}")
    skipped = input_skipped + output_skipped
    if skipped:
        details.append(
            f"- Datasets without a file path (in memory or parameters), not recorded: {skipped}"
        )
    metadata: dict[str, str | int | float | bool] = {
        "pipeline_kedro_pipeline": pipeline_name,
        "pipeline_kedro_datasets_without_files": skipped,
    }
    for key, name in (("env", "pipeline_kedro_env"), ("kedro_version", "pipeline_kedro_version")):
        if run_params.get(key):
            metadata[name] = str(run_params[key])
    error_text = f"{type(error).__name__}: {error}" if error is not None else None
    return PipelineRun(
        engine="kedro",
        status=status,
        run_id=_run_id(run_params) or None,
        started_at=started_at,
        ended_at=utc_now(),
        inputs=inputs,
        outputs=outputs,
        error_text=error_text,
        label=label or (None if pipeline_name == "__default__" else pipeline_name),
        details=details,
        metadata=metadata,
    )


def dataset_location(dataset: Any, *, role: str) -> str | None:
    """The file path or URI a Kedro dataset reads or writes, or ``None``.

    Versioned datasets resolve to the concrete version path
    (``_get_save_path`` for outputs, ``_get_load_path`` for inputs); others use
    ``_filepath`` (or ``_describe()["filepath"]``), prefixed with the dataset's
    protocol when it is not local.
    """

    if dataset is None:
        return None
    path: Any = None
    if getattr(dataset, "_version", None) is not None:
        method = getattr(dataset, "_get_save_path" if role == "output" else "_get_load_path", None)
        if callable(method):
            try:
                path = method()
            except Exception:  # noqa: BLE001 - fall back to the base path.
                path = None
    if path is None:
        path = getattr(dataset, "_filepath", None) or getattr(dataset, "filepath", None)
    if path is None:
        describe = getattr(dataset, "_describe", None)
        if callable(describe):
            try:
                described = describe()
            except Exception:  # noqa: BLE001 - not every dataset describes itself.
                described = None
            if isinstance(described, Mapping):
                path = described.get("filepath") or described.get("path")
    if path is None or not str(path).strip():
        return None
    text = str(path)
    protocol = str(getattr(dataset, "_protocol", "") or "").lower()
    if protocol not in _REMOTE_LOCAL_PROTOCOLS and "://" not in text:
        text = f"{protocol}://{text.lstrip('/')}"
    return text


def _declared(
    names: Iterable[str],
    catalog: Any,
    *,
    role: str,
) -> tuple[list[str | Mapping[str, Any]], int]:
    declared: list[str | Mapping[str, Any]] = []
    skipped = 0
    for name in names:
        if name in _PARAMETER_NAMES or name.startswith("params:"):
            skipped += 1
            continue
        try:
            location = dataset_location(_dataset(catalog, name), role=role)
        except Exception:  # noqa: BLE001 - one odd dataset must not lose the run.
            location = None
        if location is None:
            skipped += 1
            continue
        if "://" in location and not location.lower().startswith("file://"):
            declared.append(
                {
                    "uri": location,
                    "title": f"{name} ({location})",
                    "kind": "remote",
                    "summary": "Remote pointer; not dereferenced or hashed.",
                }
            )
        else:
            declared.append({"path": location, "title": name})
    return declared, skipped


def _dataset(catalog: Any, name: str) -> Any:
    for getter in ("_get_dataset", "get"):
        method = getattr(catalog, getter, None)
        if not callable(method):
            continue
        try:
            dataset = method(name)
        except Exception:  # noqa: BLE001 - try the next catalog API.
            continue
        if dataset is not None:
            return dataset
    for attribute in ("_datasets", "_data_sets", "datasets"):
        mapping = getattr(catalog, attribute, None)
        if isinstance(mapping, Mapping) and name in mapping:
            return mapping[name]
    try:
        return catalog[name]
    except Exception:  # noqa: BLE001 - unknown catalog shape.
        return None


def _names(pipeline: Any, method: str) -> list[str]:
    try:
        values = getattr(pipeline, method)()
    except Exception:  # noqa: BLE001 - an unusual pipeline object declares nothing.
        return []
    return sorted(str(value) for value in values)


def _run_id(run_params: Mapping[str, Any]) -> str:
    return str(run_params.get("session_id") or run_params.get("run_id") or "")


def _pipeline_name(run_params: Mapping[str, Any]) -> str:
    names = run_params.get("pipeline_names")
    if isinstance(names, (list, tuple)) and names:
        return ",".join(str(name) for name in names)
    return str(run_params.get("pipeline_name") or "__default__")


__all__ = ["LabTrackerHooks", "dataset_location", "kedro_run"]
