from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path, PurePosixPath

import pluggy
import pytest

from lab_tracker_client import cli as lt_cli
from lab_tracker_client import pipeline_capture
from lab_tracker_client.integrations import kedro as kedro_hooks
from lab_tracker_client.integrations import snakemake
from lab_tracker_client.integrations.dvc import parse_dvc_lock, pipeline_run_from_lock
from lab_tracker_client.integrations.nextflow import parse_trace, pipeline_run_from_trace

_ENV = (
    "LAB_TRACKER_BASE_URL",
    "LAB_TRACKER_MCP_BASE_URL",
    "LAB_TRACKER_PROJECT_ID",
    "LAB_TRACKER_SESSION_ID",
    "LAB_TRACKER_WATCH_CONFIG",
    "LAB_TRACKER_WATCH_OUTBOX",
    "LAB_TRACKER_PIPELINE_CAPTURE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    pipeline_capture._reset_notices_for_tests()


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _checkout(path: Path, *, project: str = "project-1") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "remote", "add", "origin", "git@github.com:Lab/Pipelines.git")
    (path / ".gitignore").write_text(
        ".lab-tracker/\n.snakemake/\nresults/\ndata/\nmodel.pkl\n", encoding="utf-8"
    )
    (path / "lt_ids.json").write_text(json.dumps({"project_id": project}), encoding="utf-8")
    _git(path, "add", ".gitignore", "lt_ids.json")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _events(checkout: Path) -> list[dict]:
    outbox = checkout / ".lab-tracker" / "outbox" / "watch"
    return [json.loads(path.read_text()) for path in sorted(outbox.glob("*.json"))]


def _write(path: Path, text: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- Snakemake -------------------------------------------------------------

SNAKEMAKE_SUCCESS_LOG = """\
Building DAG of jobs...
Using shell: /usr/bin/bash
Provided cores: 2
Job stats:
job          count
---------  -------
all              1
clean            2
summarize        1
total            4

Select jobs to execute...

[Mon Sep 28 10:00:00 2026]
rule clean:
    input: data/raw_A.csv
    output: results/clean_A.csv
    jobid: 2
    reason: Missing output files: results/clean_A.csv
    wildcards: sample=A
    resources: tmpdir=/tmp

[Mon Sep 28 10:00:01 2026]
Finished job 2.
1 of 4 steps (25%) done

[Mon Sep 28 10:00:01 2026]
rule clean:
    input: data/raw_B.csv
    output: results/clean_B.csv
    jobid: 3

[Mon Sep 28 10:00:02 2026]
Finished jobid: 3 (Rule: clean)
2 of 4 steps (50%) done

[Mon Sep 28 10:00:02 2026]
rule summarize:
    input: results/clean_A.csv, results/clean_B.csv, config/params.yaml
    output: results/summary.csv, results/summary.png
    jobid: 1

[Mon Sep 28 10:00:03 2026]
Finished job 1.
3 of 4 steps (75%) done

[Mon Sep 28 10:00:03 2026]
localrule all:
    input: results/summary.csv, results/summary.png, results/old_report.pdf
    jobid: 0

[Mon Sep 28 10:00:03 2026]
Finished job 0.
4 of 4 steps (100%) done
Complete log: .snakemake/log/2026-09-28T100000.123456.snakemake.log
"""

SNAKEMAKE_ERROR_LOG = """\
[Mon Sep 28 10:00:00 2026]
rule clean:
    input: data/raw_A.csv
    output: results/clean_A.csv
    jobid: 2

[Mon Sep 28 10:00:01 2026]
Finished job 2.

[Mon Sep 28 10:00:01 2026]
rule clean:
    input: data/raw_B.csv
    output: results/clean_B.csv
    jobid: 3

[Mon Sep 28 10:00:02 2026]
Error in rule clean:
    jobid: 3
    input: data/raw_B.csv
    output: results/clean_B.csv
    shell:
        exit 1
        (one of the commands exited with non-zero exit code)

Removing output files of failed job clean since they might be corrupted:
results/clean_B.csv
Shutting down, this might take some time.
Exiting because a job execution failed. Look above for error message
"""


def test_snakemake_log_parsing_derives_free_inputs_and_finished_outputs() -> None:
    parsed = snakemake.parse_log(SNAKEMAKE_SUCCESS_LOG)

    assert parsed.declared_outputs() == [
        "results/clean_A.csv",
        "results/clean_B.csv",
        "results/summary.csv",
        "results/summary.png",
    ]
    assert parsed.declared_inputs() == ["data/raw_A.csv", "data/raw_B.csv", "config/params.yaml"]
    assert not parsed.workflow_failed

    failed = snakemake.parse_log(SNAKEMAKE_ERROR_LOG)
    assert failed.declared_outputs() == ["results/clean_A.csv"]
    assert failed.failed_rules == ["clean"]
    assert failed.workflow_failed


def test_snakemake_onsuccess_report_records_one_run(tmp_path) -> None:
    checkout = _checkout(tmp_path / "wf")
    for name in ("data/raw_A.csv", "data/raw_B.csv", "results/clean_A.csv", "results/summary.csv"):
        _write(checkout / name)
    log = _write(
        checkout / ".snakemake" / "log" / "2026-09-28T100000.123456.snakemake.log",
        SNAKEMAKE_SUCCESS_LOG,
    )

    class Workflow:
        main_snakefile = str(checkout / "Snakefile")

    result = snakemake.report(str(log), status="success", workflow=Workflow(), cwd=checkout)

    assert result["action"] == "captured"
    assert result["project_source"] == "checkout"
    [event] = _events(checkout)
    metadata = event["payload"]["metadata"]
    assert metadata["pipeline_engine"] == "snakemake"
    assert metadata["pipeline_run_id"] == "snakemake-2026-09-28T100000.123456"
    assert metadata["pipeline_snakemake_jobs_finished"] == 4
    assert metadata["pipeline_snakemake_snakefile"] == "Snakefile"
    assert metadata["pipeline_snakemake_declared_from"] == "log"
    assert metadata["pipeline_started_at"].endswith("+00:00")
    roles = {item["title"]: item["role"] for item in event["artifacts"]}
    assert roles["results/summary.csv"] == "output"
    assert roles["config/params.yaml"] == "input"
    hashed = {item["title"]: item.get("content_hash") for item in event["artifacts"]}
    assert hashed["results/clean_A.csv"].startswith("sha256:")
    assert "Jobs finished: 4" in event["payload"]["body"]
    assert "Finished job 0." in event["log_excerpt"]

    again = snakemake.report(str(log), status="success", cwd=checkout)
    assert again["action"] == "already_captured"


def test_snakemake_onerror_report_marks_the_failed_rule(tmp_path) -> None:
    checkout = _checkout(tmp_path / "wf")
    log = _write(
        checkout / ".snakemake" / "log" / "2026-09-28T110000.000001.snakemake.log",
        SNAKEMAKE_ERROR_LOG,
    )

    snakemake.report(log, status="error", cwd=checkout)

    [event] = _events(checkout)
    assert event["payload"]["metadata"]["pipeline_status"] == "error"
    assert event["payload"]["metadata"]["pipeline_snakemake_jobs_failed"] == 1
    assert "Failed rules: clean" in event["payload"]["body"]
    assert [item["title"] for item in event["artifacts"] if item["role"] == "output"] == [
        "results/clean_A.csv"
    ]


def test_snakemake_metadata_fallback_when_the_log_names_no_jobs(tmp_path) -> None:
    checkout = _checkout(tmp_path / "wf")
    log = _write(
        checkout / ".snakemake" / "log" / "2026-09-28T120000.000000.snakemake.log",
        "Building DAG of jobs...\n",
    )
    started = snakemake.run_started_at(log)
    assert started is not None
    start = pipeline_capture.datetime.fromisoformat(started).timestamp()
    metadata_dir = checkout / ".snakemake" / "metadata"
    metadata_dir.mkdir(parents=True)

    def record(output: str, *, ended: float, chunk: bool = False) -> None:
        encoded = base64.urlsafe_b64encode(output.encode()).decode()
        target = (
            metadata_dir / ("@" + encoded[:8]) / encoded[8:] if chunk else metadata_dir / encoded
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {"rule": "a", "input": ["data/in.csv"], "starttime": ended - 1, "endtime": ended}
            ),
            encoding="utf-8",
        )

    record("results/new.csv", ended=start + 5)
    record("results/deep/also_new.csv", ended=start + 6, chunk=True)
    record("results/old.csv", ended=start - 3600)

    run = snakemake.snakemake_run(log, status="success", cwd=checkout)

    assert sorted(str(item) for item in run.outputs) == [
        "results/deep/also_new.csv",
        "results/new.csv",
    ]
    assert run.inputs == ["data/in.csv"]
    assert run.metadata["pipeline_snakemake_declared_from"] == "metadata"


def test_snakemake_explicit_outputs_win_and_report_never_raises(tmp_path, monkeypatch) -> None:
    checkout = _checkout(tmp_path / "wf")
    run = snakemake.snakemake_run(None, status="success", outputs=["results/x.csv"], cwd=checkout)
    assert run.outputs == ["results/x.csv"]
    assert run.metadata["pipeline_snakemake_declared_from"] == "arguments"

    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline_capture, "report_pipeline_run", explode)
    assert snakemake.report(None, status="success", cwd=checkout)["action"] == "failed"


# --- Kedro -----------------------------------------------------------------


class _Dataset:
    def __init__(self, filepath: str, protocol: str = "file") -> None:
        self._filepath = PurePosixPath(filepath)
        self._protocol = protocol
        self._version = None


class _VersionedDataset(_Dataset):
    def __init__(self, filepath: str, version: str) -> None:
        super().__init__(filepath)
        self._version = version
        self._save_version = version

    def _get_save_path(self) -> PurePosixPath:
        return self._filepath / self._save_version / self._filepath.name


class _MemoryDataset:
    pass


class _Catalog019:
    def __init__(self, datasets: dict) -> None:
        self._datasets = datasets

    def _get_dataset(self, name: str):
        return self._datasets[name]


class _Catalog1:
    def __init__(self, datasets: dict) -> None:
        self._store = datasets

    def get(self, name: str):
        return self._store.get(name)


class _Pipeline:
    def inputs(self) -> set[str]:
        return {"raw", "params:alpha", "parameters", "remote_raw"}

    def outputs(self) -> set[str]:
        return {"model", "scratch"}


def _kedro_specs() -> pluggy.PluginManager:
    spec = pluggy.HookspecMarker("kedro")

    class PipelineSpecs:
        @spec
        def before_pipeline_run(self, run_params, pipeline, catalog): ...

        @spec
        def after_pipeline_run(self, run_params, run_result, pipeline, catalog): ...

        @spec
        def on_pipeline_error(self, error, run_params, pipeline, catalog): ...

    manager = pluggy.PluginManager("kedro")
    manager.add_hookspecs(PipelineSpecs)
    return manager


@pytest.mark.parametrize("catalog_cls", [_Catalog019, _Catalog1])
def test_kedro_hooks_register_with_pluggy_and_record_free_datasets(tmp_path, catalog_cls):
    project = _checkout(tmp_path / "kedro-project")
    raw = _write(project / "data" / "01_raw" / "raw.csv", "a\n1\n")
    model_dir = project / "data" / "06_models" / "model.pkl" / "2026-09-28T10.00.00.000Z"
    _write(model_dir / "model.pkl", "model")
    catalog = catalog_cls(
        {
            "raw": _Dataset(str(raw)),
            "remote_raw": _Dataset("lab-bucket/raw/remote.parquet", protocol="s3"),
            "model": _VersionedDataset(
                str(project / "data" / "06_models" / "model.pkl"), "2026-09-28T10.00.00.000Z"
            ),
            "scratch": _MemoryDataset(),
        }
    )
    hooks = kedro_hooks.LabTrackerHooks(drain=False, question="q-1")
    manager = _kedro_specs()
    manager.register(hooks)
    run_params = {
        "session_id": "2026-09-28T10.00.00.000Z",
        "project_path": str(project),
        "env": "local",
        "kedro_version": "0.19.9",
        "pipeline_name": "training",
    }

    manager.hook.before_pipeline_run(run_params=run_params, pipeline=_Pipeline(), catalog=catalog)
    manager.hook.after_pipeline_run(
        run_params=run_params, run_result={}, pipeline=_Pipeline(), catalog=catalog
    )

    assert hooks.last_result is not None and hooks.last_result["action"] == "captured"
    [event] = _events(project)
    metadata = event["payload"]["metadata"]
    assert metadata["pipeline_engine"] == "kedro"
    assert metadata["pipeline_status"] == "success"
    assert metadata["pipeline_run_id"] == "2026-09-28T10.00.00.000Z"
    assert metadata["pipeline_kedro_pipeline"] == "training"
    assert metadata["pipeline_kedro_datasets_without_files"] == 3
    assert metadata["pipeline_started_at"]
    assert event["context"]["question_id"] == "q-1"
    by_title = {item["title"]: item for item in event["artifacts"]}
    assert by_title["raw (data/01_raw/raw.csv)"]["content_hash"].startswith("sha256:")
    assert by_title["remote_raw (s3://lab-bucket/raw/remote.parquet)"]["kind"] == "remote"
    model = by_title["model (data/06_models/model.pkl/2026-09-28T10.00.00.000Z/model.pkl)"]
    assert model["role"] == "output"
    assert model["content_hash"].startswith("sha256:")


def test_kedro_error_hook_records_the_error_and_survives_a_broken_catalog(tmp_path) -> None:
    project = _checkout(tmp_path / "kedro-project")

    class _BrokenCatalog:
        def __getattr__(self, name):
            raise RuntimeError("catalog exploded")

    class _BrokenPipeline:
        def inputs(self):
            raise RuntimeError("no inputs")

        def outputs(self):
            return {"model"}

    hooks = kedro_hooks.LabTrackerHooks(drain=False)
    hooks.on_pipeline_error(
        error=ValueError("bad column"),
        run_params={"session_id": "s-err", "project_path": str(project)},
        pipeline=_BrokenPipeline(),
        catalog=_BrokenCatalog(),
    )

    [event] = _events(project)
    assert event["payload"]["metadata"]["pipeline_status"] == "error"
    assert event["payload"]["metadata"]["pipeline_kedro_datasets_without_files"] == 1
    assert "ValueError: bad column" in event["log_excerpt"]
    assert kedro_hooks.LabTrackerHooks.after_pipeline_run.kedro_impl["hookwrapper"] is False


# --- Nextflow --------------------------------------------------------------

_TRACE_ROWS = [
    ("1", "FASTQC (s1)", "COMPLETED", "0", "10:00:00", "10:01:00"),
    ("2", "FASTQC (s2)", "FAILED", "1", "10:00:01", "10:00:06"),
    ("3", "FASTQC (s2)", "COMPLETED", "0", "10:00:07", "10:01:07"),
    ("4", "ALIGN (s1)", "CACHED", "0", "09:00:00", "09:02:00"),
    ("5", "REPORT", "FAILED", "137", "10:02:00", "10:02:09"),
]
TRACE = "\n".join(
    [
        "task_id\thash\tnative_id\tname\tstatus\texit\tsubmit\tduration\tcomplete",
        *(
            f"{task}\tab/{task}\t1{task}\t{name}\t{status}\t{code}"
            f"\t2026-09-28 {submit}.000\t1m\t2026-09-28 {complete}.000"
            for task, name, status, code, submit, complete in _TRACE_ROWS
        ),
    ]
)


def test_nextflow_trace_parsing_uses_each_tasks_last_attempt() -> None:
    trace = parse_trace(TRACE)

    assert trace.status_counts == {"COMPLETED": 2, "CACHED": 1, "FAILED": 1}
    assert [task.name for task in trace.failed_tasks] == ["REPORT"]
    assert trace.derived_status() == "error"
    assert trace.started_at == "2026-09-28 09:00:00.000"
    assert trace.ended_at == "2026-09-28 10:02:09.000"
    with pytest.raises(ValueError):
        parse_trace("task_id\thash\n1\tab\n")


def test_nextflow_cli_records_the_trace_summary(tmp_path, capsys) -> None:
    checkout = _checkout(tmp_path / "nf")
    _write(checkout / "results" / "multiqc_report.html")
    trace = _write(checkout / "results" / "pipeline_info" / "trace.txt", TRACE)

    lt_cli.main(
        [
            "pipeline",
            "nextflow",
            "--trace",
            str(trace),
            "--output",
            "results",
            "--run-id",
            "nf-session-1",
            "--cwd",
            str(checkout),
            "--no-drain",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "pipeline-nextflow"
    assert payload["status"] == "error"
    [event] = _events(checkout)
    metadata = event["payload"]["metadata"]
    assert metadata["pipeline_nextflow_task_count"] == 4
    assert metadata["pipeline_nextflow_failed_count"] == 1
    assert metadata["pipeline_nextflow_cached_count"] == 1
    body = event["payload"]["body"]
    assert "- Process `FASTQC`: 2 completed" in body
    assert "- Failed task `REPORT` (failed, exit 137)" in body
    assert "- Retried attempts: 1" in body
    [results] = event["artifacts"]
    assert results["kind"] == "directory"

    run = pipeline_run_from_trace(trace, status="success")
    assert run.status == "success"


# --- DVC -------------------------------------------------------------------

DVC_LOCK = """\
schema: '2.0'
stages:
  prepare:
    cmd: python src/prepare.py data/data.xml --token abc123
    deps:
    - path: data/data.xml
      hash: md5
      md5: 22a1a2931c8370d3aeedd7183606fd7f
      size: 14445097
    - path: s3://lab-bucket/reference.fa
      etag: '0x8DB1C2D3E4F5'
      size: 1024
    - path: src/prepare.py
      md5: f09ea0c15980b43010257ccb9f0055e2
      size: 1576
    params:
      params.yaml:
        prepare.seed: 20170428
        prepare.split: 0.2
        db.password: hunter2
        api_token: abc123
        max_tokens: 100
    outs:
    - path: data/prepared
      md5: 153aad06d376b6595932470e459ef42a.dir
      size: 8437363
      nfiles: 2
  train:
    cmd:
    - python src/train.py data/prepared model.pkl
    deps:
    - path: data/prepared
      md5: 153aad06d376b6595932470e459ef42a.dir
      size: 8437363
      nfiles: 2
    - path: src/train.py
      md5: 3ab8d2a0f40bbbc9f79a2d6a1b1d4b41
      size: 967
    outs:
    - path: model.pkl
      md5: 9ab9e3fa4c41e7b8fec1dc0bd0c5e4f3
      size: 6100
"""


def test_dvc_lock_parsing_and_legacy_layout() -> None:
    stages = parse_dvc_lock(DVC_LOCK)

    assert [stage.name for stage in stages] == ["prepare", "train"]
    prepare = stages[0]
    assert prepare.cmd == ("python src/prepare.py data/data.xml --token abc123",)
    assert prepare.deps[1].hash_name == "etag"
    assert prepare.outs[0].is_directory
    assert prepare.params["params.yaml"]["prepare.seed"] == 20170428
    assert prepare.params["params.yaml"]["max_tokens"] == 100

    legacy = parse_dvc_lock(
        "train:\n  cmd: python train.py\n  outs:\n  - path: m.pkl\n    md5: abc\n"
    )
    assert legacy[0].outs[0].path == "m.pkl"


def test_dvc_cli_records_stage_pointers_with_md5(tmp_path, capsys) -> None:
    checkout = _checkout(tmp_path / "dvc")
    lock = _write(checkout / "dvc.lock", DVC_LOCK)

    lt_cli.main(["pipeline", "dvc", "--cwd", str(checkout), "--status", "success", "--no-drain"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "pipeline-dvc"
    assert payload["input_count"] == 4
    assert payload["output_count"] == 2
    [event] = _events(checkout)
    by_title = {item["title"]: item for item in event["artifacts"]}
    assert by_title["data/prepared"]["content_hash"] == "md5:153aad06d376b6595932470e459ef42a.dir"
    assert by_title["data/prepared"]["kind"] == "directory"
    assert by_title["data/prepared"]["role"] == "output"
    assert by_title["model.pkl"]["size_bytes"] == 6100
    assert by_title["data/data.xml"]["role"] == "input"
    assert by_title["s3://lab-bucket/reference.fa"]["content_hash"] == "etag:0x8DB1C2D3E4F5"
    assert by_title["s3://lab-bucket/reference.fa"]["kind"] == "remote"
    body = event["payload"]["body"]
    assert "Stage `prepare`" in body
    assert "abc123" not in body
    assert "hunter2" not in body
    assert "db.password=[REDACTED]" in body
    assert "api_token=[REDACTED]" in body
    assert "max_tokens=100" in body
    assert "prepare.seed=20170428" in body
    metadata = event["payload"]["metadata"]
    assert metadata["pipeline_dvc_stage_count"] == 2
    assert metadata["pipeline_run_id"].startswith("dvc-lock-")

    lt_cli.main(["pipeline", "dvc", "--lock", str(lock), "--cwd", str(checkout), "--no-drain"])
    assert json.loads(capsys.readouterr().out)["action"] == "already_captured"


def test_dvc_run_id_follows_the_lock_content(tmp_path) -> None:
    lock = _write(tmp_path / "dvc.lock", DVC_LOCK)
    first = pipeline_run_from_lock(lock)
    _write(lock, DVC_LOCK.replace("size: 6100", "size: 6101"))
    second = pipeline_run_from_lock(lock)

    assert first.engine == "dvc"
    assert first.status == "unknown"
    assert first.run_id != second.run_id
    assert len(first.outputs) == 2
