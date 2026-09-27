"""Pure policy tests for batch identity, external-context policy, and host facts."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from lab_tracker.auth import AuthContext, Role
from lab_tracker.models import ExternalContextPolicy, GraphDraftBatchSettings
from lab_tracker.services import graph_draft_batch_policy as batch_policy


class _SettingsReader:
    def __init__(self, rows: dict[tuple[UUID, UUID | None], GraphDraftBatchSettings]) -> None:
        self.rows = rows

    def get_graph_draft_batch_settings_by_project(
        self,
        project_id: UUID,
        *,
        user_id: UUID | None = None,
    ) -> GraphDraftBatchSettings | None:
        return self.rows.get((project_id, user_id))


def _row(
    project_id: UUID, user_id: UUID | None, policy: ExternalContextPolicy
) -> GraphDraftBatchSettings:
    return GraphDraftBatchSettings(
        settings_id=uuid4(),
        project_id=project_id,
        user_id=user_id,
        external_context_policy=policy,
    )


def test_resolve_external_context_policy_prefers_personal_then_project_default_then_own() -> None:
    project_id = uuid4()
    user_id = uuid4()
    empty = _SettingsReader({})
    assert (
        batch_policy.resolve_external_context_policy(empty, project_id, user_id)
        is ExternalContextPolicy.OWN_NOTES_ONLY
    )
    project_default = _SettingsReader(
        {(project_id, None): _row(project_id, None, ExternalContextPolicy.PROJECT_NOTES)}
    )
    assert (
        batch_policy.resolve_external_context_policy(project_default, project_id, user_id)
        is ExternalContextPolicy.PROJECT_NOTES
    )
    assert (
        batch_policy.resolve_external_context_policy(project_default, project_id, None)
        is ExternalContextPolicy.PROJECT_NOTES
    )
    personal_wins = _SettingsReader(
        {
            (project_id, None): _row(project_id, None, ExternalContextPolicy.PROJECT_NOTES),
            (project_id, user_id): _row(project_id, user_id, ExternalContextPolicy.OWN_NOTES_ONLY),
        }
    )
    assert (
        batch_policy.resolve_external_context_policy(personal_wins, project_id, user_id)
        is ExternalContextPolicy.OWN_NOTES_ONLY
    )


def test_batch_context_policy_is_the_strictest_across_projects() -> None:
    open_project = uuid4()
    closed_project = uuid4()
    owner = batch_policy.BatchReviewer(reviewer="u", reviewer_user_id=uuid4())
    reader = _SettingsReader(
        {(open_project, None): _row(open_project, None, ExternalContextPolicy.PROJECT_NOTES)}
    )
    assert (
        batch_policy.resolve_batch_context_policy(reader, set(), owner)
        is ExternalContextPolicy.OWN_NOTES_ONLY
    )
    assert (
        batch_policy.resolve_batch_context_policy(reader, {open_project}, owner)
        is ExternalContextPolicy.PROJECT_NOTES
    )
    assert (
        batch_policy.resolve_batch_context_policy(reader, {open_project}, None)
        is ExternalContextPolicy.PROJECT_NOTES
    )
    # One closed project keeps colleagues' notes out of the whole packet.
    assert (
        batch_policy.resolve_batch_context_policy(reader, {open_project, closed_project}, owner)
        is ExternalContextPolicy.OWN_NOTES_ONLY
    )


def test_context_owner_for_prefers_reviewer_over_actor() -> None:
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    assignee_id = uuid4()
    assigned = batch_policy.context_owner_for("alice", assignee_id, actor)
    assert assigned == batch_policy.BatchReviewer(reviewer="alice", reviewer_user_id=assignee_id)
    legacy = batch_policy.context_owner_for("alice", None, actor)
    assert legacy == batch_policy.BatchReviewer(reviewer="alice", reviewer_user_id=None)
    from_actor = batch_policy.context_owner_for(None, None, actor)
    assert from_actor == batch_policy.BatchReviewer(
        reviewer=str(actor.user_id), reviewer_user_id=actor.user_id
    )
    assert batch_policy.context_owner_for(None, None, None) is None


def test_external_provider_acknowledgement_is_stamped_only_when_given() -> None:
    actor = AuthContext(user_id=uuid4(), role=Role.EDITOR)
    packet: dict[str, object] = {"mode": "graph_context"}
    batch_policy.stamp_external_provider_acknowledgement(packet, actor, acknowledged=False)
    assert batch_policy.EXTERNAL_PROVIDER_ACKNOWLEDGEMENT_KEY not in packet
    batch_policy.stamp_external_provider_acknowledgement(packet, actor, acknowledged=True)
    stamped = packet[batch_policy.EXTERNAL_PROVIDER_ACKNOWLEDGEMENT_KEY]
    assert isinstance(stamped, dict)
    assert stamped["acknowledged"] is True
    assert stamped["actor_user_id"] == str(actor.user_id)
    assert isinstance(stamped["acknowledged_at"], str)


def test_default_batch_settings_inherit_policy_but_never_the_acknowledgement() -> None:
    project_id = uuid4()
    template = _row(project_id, None, ExternalContextPolicy.PROJECT_NOTES)
    template.external_provider_acknowledged_by = "owner"
    personal = batch_policy.default_batch_settings(
        project_id=project_id, user_id=uuid4(), inherit_from=template
    )
    assert personal.external_context_policy is ExternalContextPolicy.PROJECT_NOTES
    assert personal.external_provider_acknowledged_at is None
    assert personal.external_provider_acknowledged_by is None


def test_drafting_host_facts_are_frozen_value_objects() -> None:
    facts = batch_policy.DraftingHostFacts(review_email_available=True, external_provider=False)
    assert facts == batch_policy.DraftingHostFacts(
        review_email_available=True, external_provider=False
    )
    with pytest.raises(AttributeError):
        facts.external_provider = True  # type: ignore[misc]
