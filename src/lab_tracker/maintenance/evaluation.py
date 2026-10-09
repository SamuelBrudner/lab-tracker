"""Paired synthetic graph evaluation; never opens the production database."""

from __future__ import annotations

import hashlib
import json
import math
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lab_tracker import golden_day
from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import LOCAL_AUTH_USER_ID, AuthContext, Role
from lab_tracker.config import Settings, resolve_graph_draft_provider
from lab_tracker.db import Base
from lab_tracker.golden_day import (
    GOLDEN_DAY_FIXTURE_VERSION,
    GOLDEN_DAY_SCORER_VERSION,
    ScriptedGoldenDayDraftClient,
    golden_day_expected_patch,
    golden_day_link_diagnostics,
    score_golden_day,
    seed_golden_day,
)
from lab_tracker.graph_drafting import (
    BATCH_PROMPT_VERSION,
    GraphDraftClient,
    GraphDraftingError,
    OpenAIGraphDraftClient,
    _batch_instructions,
    make_graph_draft_client,
)
from lab_tracker.models import GraphChangeSetStatus
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker_client.redaction import redact_capture_text


@dataclass(frozen=True)
class EvaluationGates:
    min_precision: float = 0.8
    min_recall: float = 0.8
    max_quality_regression: float = 0.02
    max_duplicate_rate: float = 0.05
    max_latency_ratio: float = 2.0
    min_clarification_recall: float = 1.0
    max_ambiguity_link_rate: float = 0.0
    min_proposal_precision: float = 1.0
    min_proposal_recall: float = 1.0

    def validate(self) -> None:
        for name, value in asdict(self).items():
            if not math.isfinite(value) or value < 0 or (name != "max_latency_ratio" and value > 1):
                raise ValueError("Evaluation gates must be finite and within their metric range.")
        if self.max_latency_ratio <= 0:
            raise ValueError("Latency ratio must be positive.")


class DraftNotReady(ValueError):
    """The provider attempt could not produce a valid, reviewable draft."""

    def __init__(self, details: dict[str, Any]):
        self.details = details
        super().__init__("Synthetic evaluation did not produce a READY draft.")


class EvaluationBatchClient:
    """Record only public usage and reject empty provider patches before day logs."""

    def __init__(self, client: GraphDraftClient):
        self.client = client
        self.provider = getattr(client, "provider", "unknown")
        self.model = getattr(client, "model", "unknown")
        # Generation ownership must cover the real provider timeout even when
        # the client is decorated for telemetry.
        self.timeout_seconds = getattr(client, "timeout_seconds", 60.0)
        self.attempts = 0
        self.operation_count = 0
        self.usage: list[dict[str, int]] = []
        self.response_statuses: list[int] = []
        self.context_sha256: str | None = None
        self.patch_sha256: str | None = None
        if isinstance(client, OpenAIGraphDraftClient):
            client._client.event_hooks["response"].append(self.record_usage)

    def record_usage(self, response: httpx.Response) -> None:
        self.response_statuses.append(response.status_code)
        response.read()
        if not response.is_success:
            return
        try:
            usage = response.json().get("usage")
        except (ValueError, AttributeError):
            return
        if not isinstance(usage, dict):
            return
        selected = {
            name: usage.get(name) for name in ("input_tokens", "output_tokens", "total_tokens")
        }
        for group, name in (
            ("input_tokens_details", "cached_tokens"),
            ("input_tokens_details", "cache_write_tokens"),
            ("output_tokens_details", "reasoning_tokens"),
        ):
            detail = usage.get(group)
            if isinstance(detail, dict) and name in detail:
                selected[name] = detail[name]
        validated: dict[str, int] = {}
        for name, value in selected.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                return
            validated[name] = value
        self.usage.append(validated)

    def draft_from_batch(
        self, *, batch_context: dict[str, Any], user_hint: str | None = None
    ) -> dict[str, Any]:
        self.attempts += 1
        self.context_sha256 = hashlib.sha256(
            json.dumps(batch_context, sort_keys=True).encode()
        ).hexdigest()
        patch = self.client.draft_from_batch(batch_context=batch_context, user_hint=user_hint)
        self.patch_sha256 = hashlib.sha256(json.dumps(patch, sort_keys=True).encode()).hexdigest()
        operations = patch.get("operations")
        if not isinstance(operations, list) or not operations:
            raise GraphDraftingError(
                "An empty provider patch cannot qualify as evaluation evidence."
            )
        self.operation_count = len(operations)
        return patch

    def draft_from_note(self, **kwargs: Any) -> dict[str, Any]:
        raise GraphDraftingError("Evaluation uses only the synthetic batch fixture.")

    def draft_from_analysis_evidence(self, **kwargs: Any) -> dict[str, Any]:
        raise GraphDraftingError("Evaluation uses only the synthetic batch fixture.")

    def transcribe_audio(self, **kwargs: Any) -> dict[str, Any]:
        raise GraphDraftingError("Evaluation uses only the synthetic batch fixture.")

    def close(self) -> None:
        self.client.close()


class EvaluationAttemptFailure(ValueError):
    def __init__(
        self, phase: str, sample_number: int, error_type: str, frames: list[dict[str, Any]]
    ):
        self.phase = phase
        self.sample_number = sample_number
        self.error_type = error_type
        self.frames = frames
        super().__init__("Synthetic model evaluation failed.")


def run_sample(settings: Settings, model: str, *, scripted: bool = False) -> dict[str, Any]:
    provider = resolve_graph_draft_provider(settings.graph_draft_provider)
    field = {
        "openai": "openai_model",
        "anthropic": "anthropic_model",
        "google": "google_model",
    }.get(provider)
    if field is None:
        raise ValueError("Evaluation requires a supported draft provider.")
    candidate = settings.model_copy(update={field: model})
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    client = None
    try:
        api = LabTrackerAPI(repository=SQLAlchemyLabTrackerRepository(session))
        actor = AuthContext(user_id=LOCAL_AUTH_USER_ID, role=Role.ADMIN)
        project = api.create_project("Synthetic maintenance evaluation", actor=actor)
        graph, notes = seed_golden_day(api, project_id=project.project_id, actor=actor)
        client = EvaluationBatchClient(
            ScriptedGoldenDayDraftClient(golden_day_expected_patch(graph, notes))
            if scripted
            else make_graph_draft_client(candidate)
        )
        started = time.monotonic()
        # One provider attempt per sample keeps the explicit ten-call budget and
        # makes invalid first responses visible instead of hiding retries in latency.
        draft = api.create_batch_graph_draft(
            notes, draft_client=client, actor=actor, max_attempts=1
        )
        latency = time.monotonic() - started
        if draft.status != GraphChangeSetStatus.READY:
            category = draft.error_metadata.get("category")
            validation_detail = None
            if category == "validation_error":
                # Only the local validator's feedback is eligible. Provider error
                # bodies stay excluded; explicit in-memory credentials also redact.
                validation_detail = redact_capture_text(
                    str(draft.error_metadata.get("message") or ""),
                    secrets=(
                        candidate.openai_api_key,
                        candidate.anthropic_api_key,
                        candidate.google_api_key,
                    ),
                )[:1024]
            raise DraftNotReady(
                {
                    "provider_attempts": client.attempts,
                    "provider_operation_count": client.operation_count,
                    "usage": client.usage,
                    "provider_response_statuses": client.response_statuses,
                    "latency_seconds": latency,
                    "validation_detail": validation_detail,
                    "failure_category": category
                    if category in {"model_error", "validation_error", "output_truncated"}
                    else "draft_not_ready",
                    "context_sha256": client.context_sha256,
                    "patch_sha256": client.patch_sha256,
                }
            )
        return {
            "completed": True,
            **asdict(score_golden_day(draft, graph, notes)),
            "latency_seconds": latency,
            "provider_attempts": client.attempts,
            "provider_operation_count": client.operation_count,
            "usage": client.usage,
            "provider_response_statuses": client.response_statuses,
            "link_diagnostics": golden_day_link_diagnostics(draft, graph, notes),
            "context_sha256": client.context_sha256,
            "patch_sha256": client.patch_sha256,
        }
    finally:
        if client is not None:
            client.close()
        session.close()
        engine.dispose()


def compare_samples(
    baseline: list[dict[str, Any]], candidate: list[dict[str, Any]], gates: EvaluationGates
) -> dict[str, Any]:
    gates.validate()
    if not baseline or len(baseline) != len(candidate):
        raise ValueError("Evaluation requires equal, nonempty baseline and candidate samples.")
    for run in baseline + candidate:
        for metric in (
            "link_precision",
            "link_recall",
            "duplicate_create_rate",
            "latency_seconds",
            "clarification_recall",
            "ambiguity_link_rate",
            "proposal_precision",
            "proposal_recall",
        ):
            value = run.get(metric)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("Evaluation metrics must be finite numbers.")
            if value < 0 or (metric != "latency_seconds" and value > 1):
                raise ValueError("Evaluation metrics are outside their valid range.")
        count = run.get("provider_operation_count", run.get("operation_count"))
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("Empty drafts cannot pass a quality evaluation.")

    def average(samples: list[dict[str, Any]]) -> dict[str, float]:
        return {
            metric: mean(item[metric] for item in samples)
            for metric in (
                "link_precision",
                "link_recall",
                "duplicate_create_rate",
                "latency_seconds",
                "clarification_recall",
                "ambiguity_link_rate",
                "proposal_precision",
                "proposal_recall",
            )
        }

    before, after = average(baseline), average(candidate)
    failures = []
    for metric, minimum in (
        ("link_precision", gates.min_precision),
        ("link_recall", gates.min_recall),
        ("clarification_recall", gates.min_clarification_recall),
        ("proposal_precision", gates.min_proposal_precision),
        ("proposal_recall", gates.min_proposal_recall),
    ):
        if after[metric] < minimum:
            failures.append(f"{metric} below minimum")
        if after[metric] + gates.max_quality_regression < before[metric]:
            failures.append(f"{metric} regressed")
    if after["duplicate_create_rate"] > gates.max_duplicate_rate:
        failures.append("duplicate creation rate above maximum")
    if after["ambiguity_link_rate"] > gates.max_ambiguity_link_rate:
        failures.append("unjustified ambiguous question links")
    if after["latency_seconds"] > max(before["latency_seconds"], 0.001) * gates.max_latency_ratio:
        failures.append("latency ratio above maximum")
    if len({item["prompt_version"] for item in baseline + candidate}) != 1:
        failures.append("prompt versions differ")
    return {
        "passed": not failures,
        "failures": failures,
        "baseline": before,
        "candidate": after,
        "gates": asdict(gates),
        "baseline_runs": baseline,
        "candidate_runs": candidate,
        "cost_verified": False,
        "approval_required": True,
        "scope": "Synthetic golden-day fixture; cost and broader workload quality need review.",
    }


def evaluate_pair(
    settings: Settings,
    baseline: str,
    candidate: str,
    *,
    repeat: int = 3,
    scripted: bool = False,
    gates: EvaluationGates | None = None,
    candidate_settings: Settings | None = None,
) -> dict[str, Any]:
    if not 1 <= repeat <= 5:
        raise ValueError("Evaluation repeat must be between 1 and 5.")
    gates = gates or EvaluationGates()
    gates.validate()
    before: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    # Alternate runs so the two choices see similar provider conditions.
    for index in range(repeat):
        for phase, model, samples, configuration in (
            ("baseline", baseline, before, settings),
            ("candidate", candidate, after, candidate_settings or settings),
        ):
            try:
                samples.append(run_sample(configuration, model, scripted=scripted))
            except DraftNotReady as exc:
                # A model failure is a scored trial outcome, not a reason to hide
                # remaining trials or skip the other arm of the comparison.
                samples.append(
                    {
                        "completed": False,
                        "model": model,
                        "error_type": type(exc).__name__,
                        **exc.details,
                    }
                )
            except Exception as exc:
                frames = [
                    {
                        "module": Path(frame.filename).name,
                        "function": frame.name,
                        "line": frame.lineno,
                    }
                    for frame in traceback.extract_tb(exc.__traceback__)[-8:]
                ]
                raise EvaluationAttemptFailure(
                    phase, index + 1, type(exc).__name__, frames
                ) from None
    identity = {
        "requested_baseline": baseline,
        "requested_candidate": candidate,
        "scripted": scripted,
        "provider": resolve_graph_draft_provider(settings.graph_draft_provider),
        "reasoning_effort": settings.openai_reasoning_effort,
        "reasoning_mode": settings.openai_reasoning_mode,
        "candidate_reasoning_effort": (candidate_settings or settings).openai_reasoning_effort,
        "candidate_reasoning_mode": (candidate_settings or settings).openai_reasoning_mode,
        "sampling_policy": "One provider attempt per sample; no validation retries.",
        "prompt_version": BATCH_PROMPT_VERSION,
        "gates": asdict(gates),
        "fixture_sha256": hashlib.sha256(Path(golden_day.__file__).read_bytes()).hexdigest(),
        "fixture_version": GOLDEN_DAY_FIXTURE_VERSION,
        "scorer_version": GOLDEN_DAY_SCORER_VERSION,
        "instructions_sha256": hashlib.sha256(_batch_instructions().encode()).hexdigest(),
        "scorer_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    failed = [
        f"{phase} sample {index + 1} did not produce a valid draft"
        for phase, samples in (("baseline", before), ("candidate", after))
        for index, sample in enumerate(samples)
        if sample.get("completed") is False
    ]
    if failed:
        return {
            **identity,
            "completed": False,
            "passed": False,
            "failures": failed,
            "baseline_runs": before,
            "candidate_runs": after,
            "cost_verified": False,
            "approval_required": True,
        }
    return {"completed": True, **compare_samples(before, after, gates), **identity}


def main() -> int:
    """Contained worker; receive only public options and a private settings filename."""
    try:
        parameters = json.loads(sys.argv[1])
        if not parameters["scripted"] and parameters.get("live") is not True:
            raise ValueError("Live evaluation requires explicit opt-in.")
        settings = Settings(_env_file=parameters["provider_env"])  # type: ignore[call-arg]
        overrides = {
            name: parameters[value]
            for name, value in (
                ("openai_reasoning_effort", "reasoning_effort"),
                ("openai_reasoning_mode", "reasoning_mode"),
            )
            if parameters.get(value) is not None
        }
        baseline_settings = settings.model_copy(update=overrides)
        candidate_overrides = {
            name: parameters[value]
            for name, value in (
                ("openai_reasoning_effort", "candidate_reasoning_effort"),
                ("openai_reasoning_mode", "candidate_reasoning_mode"),
            )
            if parameters.get(value) is not None
        }
        report = evaluate_pair(
            baseline_settings,
            parameters["baseline"],
            parameters["candidate"],
            repeat=parameters["repeat"],
            scripted=parameters["scripted"],
            gates=EvaluationGates(**parameters["gates"]),
            candidate_settings=baseline_settings.model_copy(update=candidate_overrides),
        )
        print(json.dumps({"completed": True, **report}))
        return 0
    except Exception as exc:
        failure = {}
        if isinstance(exc, EvaluationAttemptFailure):
            failure = {
                "phase": exc.phase,
                "sample_number": exc.sample_number,
                "attempt_error_type": exc.error_type,
                "failure_locations": exc.frames,
            }
        print(
            json.dumps(
                {
                    "completed": False,
                    "passed": False,
                    "error_type": type(exc).__name__,
                    "approval_required": True,
                    "cost_verified": False,
                    **failure,
                }
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
