"""Score a graph-draft client against the golden day. Opt-in; never run by CI.

Seeds the golden-day project into a throwaway in-memory database, asks one
draft client for a batch draft of its fourteen captures, and prints link
precision/recall, duplicate-question rate and clarification rate so prompt
versions can be compared on the same day.

``--provider scripted`` (the default) uses the package's scripted client and
costs nothing. ``--provider live`` builds the configured provider client from
the operator's graph-draft settings (see docs/configuration.md) and CALLS A
PAID MODEL once per ``--repeat``. Exit codes: 0 scored, 2 the live client
could not be built, 3 the draft did not reach READY.

Usage::

    uv run python scripts/eval-drafts.py --provider scripted --json
    uv run python scripts/eval-drafts.py --provider live --repeat 3
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from statistics import mean
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import LOCAL_AUTH_USER_ID, AuthContext, Role
from lab_tracker.config import get_settings
from lab_tracker.db import Base
from lab_tracker.golden_day import (
    GoldenDayScore,
    ScriptedGoldenDayDraftClient,
    golden_day_expected_patch,
    score_golden_day,
    seed_golden_day,
)
from lab_tracker.graph_drafting import GraphDraftClient, GraphDraftingError, make_graph_draft_client
from lab_tracker.models import GraphChangeSetStatus
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

SCRIPTED_PROVIDER = "scripted"
LIVE_PROVIDER = "live"
EXIT_OK = 0
EXIT_CLIENT_UNAVAILABLE = 2
EXIT_DRAFT_NOT_READY = 3


class DraftNotReadyError(RuntimeError):
    """The batch draft ended in a non-READY status."""


def build_in_memory_api() -> LabTrackerAPI:
    """A LabTrackerAPI over a fresh in-memory SQLite database (mirrors tests/api_helpers)."""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return LabTrackerAPI(repository=SQLAlchemyLabTrackerRepository(session_factory()))


def make_client(provider: str, patch: dict[str, Any]) -> GraphDraftClient:
    if provider == SCRIPTED_PROVIDER:
        return ScriptedGoldenDayDraftClient(patch)
    if provider == LIVE_PROVIDER:
        return make_graph_draft_client(get_settings())
    raise ValueError(f"Unknown provider {provider!r}.")


def run_once(provider: str) -> GoldenDayScore:
    api = build_in_memory_api()
    actor = AuthContext(user_id=LOCAL_AUTH_USER_ID, role=Role.ADMIN)
    project = api.create_project("Golden day evaluation", actor=actor)
    graph, notes = seed_golden_day(api, project_id=project.project_id, actor=actor)
    client = make_client(provider, golden_day_expected_patch(graph, notes))
    try:
        change_set = api.create_batch_graph_draft(notes, draft_client=client, actor=actor)
    finally:
        client.close()
    if change_set.status != GraphChangeSetStatus.READY:
        raise DraftNotReadyError(
            f"draft ended {change_set.status.value}: "
            f"{json.dumps(change_set.error_metadata, sort_keys=True, default=str)}"
        )
    return score_golden_day(change_set, graph, notes)


def summarize(scores: list[GoldenDayScore]) -> dict[str, Any]:
    first = scores[0]
    return {
        "provider": first.provider,
        "model": first.model,
        "prompt_version": first.prompt_version,
        "runs": [asdict(score) for score in scores],
        "mean_link_precision": mean(score.link_precision for score in scores),
        "mean_link_recall": mean(score.link_recall for score in scores),
        "mean_duplicate_create_rate": mean(score.duplicate_create_rate for score in scores),
        "mean_clarification_rate": mean(score.clarification_rate for score in scores),
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--provider",
        choices=(SCRIPTED_PROVIDER, LIVE_PROVIDER),
        default=SCRIPTED_PROVIDER,
        help="scripted: the package's fixed patch (free); live: the configured paid model.",
    )
    parser.add_argument(
        "--repeat", type=int, default=1, help="Number of independent runs (default 1)."
    )
    parser.add_argument(
        "--json", action="store_true", help="Print only the JSON summary (no progress lines)."
    )
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1.")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    scores: list[GoldenDayScore] = []
    for run_index in range(1, args.repeat + 1):
        if not args.json:
            print(f"run {run_index}/{args.repeat} ({args.provider})", file=sys.stderr)
        try:
            scores.append(run_once(args.provider))
        except GraphDraftingError as exc:
            print(f"Could not build the {args.provider} draft client: {exc}", file=sys.stderr)
            return EXIT_CLIENT_UNAVAILABLE
        except DraftNotReadyError as exc:
            print(f"Run {run_index} failed: {exc}", file=sys.stderr)
            return EXIT_DRAFT_NOT_READY
    print(json.dumps(summarize(scores), indent=2, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
