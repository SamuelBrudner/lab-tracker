"""Deterministic fixture behind docs/examples/dataset.prov.jsonld.

The committed example sidecar is generated from this fixture through the same
builder that backs ``GET /datasets/{id}/provenance`` and ``lt export``.
``test_provenance_example.py`` regenerates it on every run and fails if the
committed file drifts from what the builder produces.

Regenerate after an intentional vocabulary or builder change:

    uv run python tests/provenance_example_fixture.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from lab_tracker.models import (
    Dataset,
    DatasetCommitManifest,
    DatasetFile,
    DatasetStatus,
    EntityOrigin,
    OutcomeStatus,
    QuestionLink,
    QuestionLinkRole,
    SupervisionEdge,
)
from lab_tracker.provenance import build_dataset_provenance_document

BASE_URL = "https://lab.example.org"
EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "docs" / "examples" / "dataset.prov.jsonld"

_DATASET_ID = UUID("6fce1866-5432-4b70-b582-3f342a9f4f13")
_PROJECT_ID = UUID("0b6a5f77-6a10-4f2b-9f5e-2b41a3a4f0aa")
_PRIMARY_QUESTION_ID = UUID("9c1f6c33-9d1e-4b2e-9d55-73f4dbb0f2c1")
_SECONDARY_QUESTION_ID = UUID("d0e2b7c8-4d0f-4a3b-8f61-0f4f3f81c9d2")
_NOTE_ID = UUID("4a3b2c1d-0e9f-4d8c-b7a6-5f4e3d2c1b0a")
_SESSION_ID = UUID("7e8f9a0b-1c2d-4e3f-a5b6-c7d8e9f0a1b2")
_CHANGE_SET_ID = UUID("2f3e4d5c-6b7a-4980-9102-a3b4c5d6e7f8")
_CREATOR_USER_ID = UUID("57a1c8b2-8d3e-4f50-a617-b8c9d0e1f2a3")
_SUPERVISOR_USER_ID = UUID("c4d5e6f7-a8b9-4a0b-8c1d-2e3f4a5b6c7d")

_COMMITTED_AT = datetime(2026, 6, 19, 17, 40, tzinfo=timezone.utc)


def example_dataset() -> Dataset:
    """A committed two-photon dataset with the full provenance story attached."""
    question_links = [
        QuestionLink(
            question_id=_PRIMARY_QUESTION_ID,
            role=QuestionLinkRole.PRIMARY,
            outcome_status=OutcomeStatus.SUPPORTS,
        ),
        QuestionLink(
            question_id=_SECONDARY_QUESTION_ID,
            role=QuestionLinkRole.SECONDARY,
            outcome_status=OutcomeStatus.INCONCLUSIVE,
        ),
    ]
    return Dataset(
        dataset_id=_DATASET_ID,
        project_id=_PROJECT_ID,
        commit_hash="sha256:1f8ac10f23c5b5bc1167bda84b833e5c057a77d2",
        primary_question_id=_PRIMARY_QUESTION_ID,
        question_links=question_links,
        commit_manifest=DatasetCommitManifest(
            files=[
                DatasetFile(
                    path="raw/2026_06_19_rig2_session001.nwb",
                    checksum="9b74c9897bac770ffc029102a200c5de",
                    size_bytes=734003200,
                ),
                DatasetFile(
                    path="raw/2026_06_19_rig2_session001_stim.csv",
                    checksum="4e07408562bedb8b60ce05c1decfe3ad",
                    size_bytes=52341,
                ),
            ],
            metadata={"rig": "rig2", "condition": "baseline"},
            nwb_metadata={"Session Description": "PV-cre baseline odor panel"},
            note_ids=[_NOTE_ID],
            question_links=question_links,
            source_session_id=_SESSION_ID,
        ),
        status=DatasetStatus.COMMITTED,
        created_at=_COMMITTED_AT,
        updated_at=_COMMITTED_AT,
        created_by_user_id=_CREATOR_USER_ID,
        origin=EntityOrigin.AI_SUGGESTED,
        change_set_id=_CHANGE_SET_ID,
        origin_provider="anthropic",
        origin_model="claude-sonnet-5",
        origin_prompt_version="drafting-v3",
    )


def example_supervision_edges() -> list[SupervisionEdge]:
    return [
        SupervisionEdge(
            edge_id=UUID("e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b"),
            supervisor_user_id=_SUPERVISOR_USER_ID,
            supervisee_user_id=_CREATOR_USER_ID,
            started_at=datetime(2024, 9, 1, tzinfo=timezone.utc),
        )
    ]


def build_example_document() -> dict[str, object]:
    return build_dataset_provenance_document(
        BASE_URL,
        example_dataset(),
        supervision_edges=example_supervision_edges(),
    )


def render_example() -> str:
    # Same serialization as an `lt export` sidecar, plus a trailing newline.
    return json.dumps(build_example_document(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    EXAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXAMPLE_PATH.write_text(render_example(), encoding="utf-8")
    print(f"wrote {EXAMPLE_PATH}")
