"""Delegated curation: the one path that records ``auto_accepted``.

Every AI proposal is human-gated by default: only an interactive session may
accept or commit a graph draft. A project owner can delegate part of that
review to the machine, at an interactive session and with an explicit
acknowledgement, through the project-default batch settings row
(``GraphDraftBatchSettings.delegated_curation``). Two principals may then act
under that grant, and only for the operations it admits:

* the server's own drafting pass, right after a draft is generated
  (:class:`DelegatedCurationCoordinator`), and
* a ``graph_curate``-scoped personal access token driving the ordinary
  accept and commit routes from an agent.

Both record every accept as :attr:`AcceptanceMode.AUTO_ACCEPTED`, so the
committed graph never confuses a delegated accept with a person's review.
Editing, rejecting, deferring, and submitting stay a person's verdicts.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol
from uuid import UUID

from lab_tracker.auth import (
    LOCAL_AUTH_USER_ID,
    AuthContext,
    PrincipalType,
    Role,
)
from lab_tracker.errors import AuthError, PermissionDeniedError, ValidationError
from lab_tracker.models import (
    DEFERRED_AT_KEY,
    AcceptanceMode,
    DelegatedCurationPolicy,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftBatchSettings,
    GraphDraftPurpose,
    GraphDraftSemanticType,
    utc_now,
)
from lab_tracker.services.base import BaseService, ServiceContext

logger = logging.getLogger(__name__)

# Proposals that only wire existing records together. They add or adjust
# edges; they never author a record, close a question, or resolve a claim.
ORGANIZE_SEMANTIC_TYPES: Final = frozenset(
    {
        GraphDraftSemanticType.LINK_NOTE_TO_QUESTION,
        GraphDraftSemanticType.LINK_NOTE_TO_SESSION,
        GraphDraftSemanticType.LINK_NOTE_TO_DATASET,
        GraphDraftSemanticType.LINK_NOTE_TO_ANALYSIS,
        GraphDraftSemanticType.LINK_NODE_TO_GOAL,
    }
)
# Under ORGANIZE a link proposal may carry only the field that links: the same
# labels map to generic note and goal updates, so a payload that also sets a
# note status or a goal title is not "organizing" whatever it is called.
ORGANIZE_PAYLOAD_KEYS: Final = {
    GraphDraftSemanticType.LINK_NOTE_TO_QUESTION: frozenset({"targets"}),
    GraphDraftSemanticType.LINK_NOTE_TO_SESSION: frozenset({"targets"}),
    GraphDraftSemanticType.LINK_NOTE_TO_DATASET: frozenset({"targets"}),
    GraphDraftSemanticType.LINK_NOTE_TO_ANALYSIS: frozenset({"targets"}),
    GraphDraftSemanticType.LINK_NODE_TO_GOAL: frozenset({"links"}),
}
# A clarification request exists to ask a person; no grant applies it.
NEVER_DELEGATED_SEMANTIC_TYPES: Final = frozenset({GraphDraftSemanticType.REQUEST_CLARIFICATION})
FULL_SEMANTIC_TYPES: Final = frozenset(GraphDraftSemanticType) - NEVER_DELEGATED_SEMANTIC_TYPES

# Change-set keys: the grant a delegated pass acted under, and why it stopped.
DELEGATED_CURATION_PACKET_KEY: Final = "delegated_curation"
DELEGATED_CURATION_ERROR_KEY: Final = "delegated_curation_error"
DELEGATED_CURATION_PRINCIPAL_LABEL: Final = "delegated-curation"

GRANT_DELEGATED_CURATION_ACTION: Final = "Granting delegated curation"
DELEGATED_CURATION_ACKNOWLEDGEMENT_REQUIRED: Final = (
    "Widening delegated curation requires explicit delegated_curation_acknowledged: true "
    "in the same request."
)
DELEGATED_CURATION_PROJECT_LEVEL_ONLY: Final = (
    "Delegated curation is a project-level grant; set it through the project-default "
    "batch settings."
)


def admitted_semantic_types(
    policy: DelegatedCurationPolicy,
) -> frozenset[GraphDraftSemanticType]:
    """The semantic types a grant lets AI accept and commit on its own."""

    if policy is DelegatedCurationPolicy.ORGANIZE:
        return ORGANIZE_SEMANTIC_TYPES
    if policy is DelegatedCurationPolicy.FULL:
        return FULL_SEMANTIC_TYPES
    return frozenset()


def policy_admits(
    policy: DelegatedCurationPolicy,
    operation: GraphChangeOperation,
) -> bool:
    """Whether a grant covers one proposal; an untyped proposal is never covered.

    ORGANIZE admits a link proposal only when its payload carries nothing but
    the linking field, so a link label cannot smuggle in another update.
    """

    semantic_type = operation.semantic_type
    if semantic_type is None or semantic_type not in admitted_semantic_types(policy):
        return False
    if policy is DelegatedCurationPolicy.ORGANIZE:
        return set(operation.payload) <= ORGANIZE_PAYLOAD_KEYS[semantic_type]
    return True


def is_delegable_principal(actor: AuthContext | None) -> bool:
    """Whether this non-interactive principal may act under a delegation grant.

    The drafting pass (``SYSTEM``) and a ``graph_curate``-scoped token qualify.
    An ``all``-scope token does not: acting on a grant is a separately minted
    permission, not something every automation credential carries.
    """

    if actor is None:
        return False
    return actor.is_system or actor.is_graph_curate_scoped


def delegated_curation_actor(grant: DelegatedCurationGrant | None = None) -> AuthContext:
    """The non-interactive principal the drafting pass accepts and commits as.

    It carries the granting owner's user id, so the records it applies are
    attributed to the person whose grant it acts under (as a person's own
    commit would attribute them), while its ``SYSTEM`` principal type keeps it
    outside every interactive gate.
    """

    return AuthContext(
        user_id=_granting_user_id(grant),
        role=Role.ADMIN,
        principal_type=PrincipalType.SYSTEM,
        principal_label=DELEGATED_CURATION_PRINCIPAL_LABEL,
    )


def _granting_user_id(grant: DelegatedCurationGrant | None) -> UUID:
    if grant is None or not grant.granted_by:
        return LOCAL_AUTH_USER_ID
    try:
        return UUID(grant.granted_by)
    except ValueError:
        return LOCAL_AUTH_USER_ID


@dataclass(frozen=True)
class DelegatedCurationGrant:
    """The project-level grant as recorded on the project-default settings row."""

    policy: DelegatedCurationPolicy
    granted_by: str | None = None
    granted_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.policy is not DelegatedCurationPolicy.OFF

    def admits(self, operation: GraphChangeOperation) -> bool:
        return policy_admits(self.policy, operation)

    def refused(self, operations: Iterable[GraphChangeOperation]) -> list[str]:
        """Sorted labels of the operations this grant does not cover."""

        return sorted(
            {
                operation.semantic_type.value if operation.semantic_type else "untyped"
                for operation in operations
                if not self.admits(operation)
            }
        )

    def as_packet(self) -> dict[str, Any]:
        return {
            "policy": self.policy.value,
            "granted_by": self.granted_by,
            "granted_at": self.granted_at.isoformat() if self.granted_at else None,
        }


OFF_GRANT: Final = DelegatedCurationGrant(policy=DelegatedCurationPolicy.OFF)


class DelegationSettingsReader(Protocol):
    def get_graph_draft_batch_settings_by_project(
        self,
        project_id: UUID,
        *,
        user_id: UUID | None = None,
    ) -> GraphDraftBatchSettings | None: ...


def resolve_delegated_curation(
    reader: DelegationSettingsReader,
    project_id: UUID,
) -> DelegatedCurationGrant:
    """Read the project-default row's grant; personal rows never carry one."""

    settings = reader.get_graph_draft_batch_settings_by_project(project_id, user_id=None)
    if settings is None or settings.delegated_curation is DelegatedCurationPolicy.OFF:
        return OFF_GRANT
    return DelegatedCurationGrant(
        policy=settings.delegated_curation,
        granted_by=settings.delegated_curation_granted_by,
        granted_at=settings.delegated_curation_granted_at,
    )


@dataclass(frozen=True)
class AcceptanceStamp:
    """What one admitted accept records: the mode and the person of record.

    A person's accept is attributed to that person. A delegated accept is
    ``auto_accepted`` and attributed to the person whose authority it acted
    under: the token's owner for a ``graph_curate`` token, the granting
    project owner for the drafting pass.
    """

    mode: AcceptanceMode
    accepted_by: str | None


def _interactive_required(action: str, detail: str) -> PermissionDeniedError:
    return PermissionDeniedError(
        f"{action} requires an interactive human session; service tokens and "
        f"automation principals may draft graph proposals but not accept or commit "
        f"them. {detail}"
    )


class DelegatedCurationGate(BaseService):
    """Decide whether a non-interactive accept or commit is covered by a grant.

    Interactive principals never reach the grant: their accept is recorded as
    they asked (``human_selected`` or ``bulk_accepted``). Everything else is
    fail-closed: a principal that cannot act on grants, a project without one,
    a member-onboarding draft, or one proposal outside the grant is refused
    with the same 403 the ungated code path always raised.
    """

    def grant_for_project(self, project_id: UUID) -> DelegatedCurationGrant:
        return resolve_delegated_curation(self.repository, project_id)

    def admit_accept(
        self,
        actor: AuthContext | None,
        *,
        change_set: GraphChangeSet,
        operation: GraphChangeOperation,
        requested: AcceptanceMode,
        action: str = "Accepting graph operations",
    ) -> AcceptanceStamp:
        """What to record for one accept, or raise when it is not admitted."""

        if requested is AcceptanceMode.AUTO_ACCEPTED:
            raise ValidationError(
                "auto_accepted is recorded only by delegated curation; a person's "
                "accept is human_selected or bulk_accepted."
            )
        if actor is None:
            raise AuthError("Authentication required.")
        if actor.is_interactive:
            return AcceptanceStamp(mode=requested, accepted_by=str(actor.user_id))
        grant = self.require_grant(actor, change_set=change_set, action=action)
        refused = grant.refused([operation])
        if refused:
            raise PermissionDeniedError(
                f"{action} under delegated curation ({grant.policy.value}) does not "
                f"admit {', '.join(refused)}; a person must review that proposal."
            )
        return AcceptanceStamp(
            mode=AcceptanceMode.AUTO_ACCEPTED,
            accepted_by=_person_of_record(actor, grant),
        )

    def require_grant(
        self,
        actor: AuthContext | None,
        *,
        change_set: GraphChangeSet,
        action: str,
    ) -> DelegatedCurationGrant:
        """The active grant a non-interactive principal acts under, or raise."""

        if actor is None:
            raise AuthError("Authentication required.")
        if actor.is_interactive:
            raise ValueError("require_grant is for non-interactive principals only.")
        if change_set.purpose == GraphDraftPurpose.MEMBER_CHECKPOINT_ALIGNMENT:
            raise _interactive_required(
                action, "Member onboarding proposals are never delegated."
            )
        if not is_delegable_principal(actor):
            raise _interactive_required(
                action,
                "Only the drafting pass or a token minted at the Curate graph "
                "(delegated) level may act under a project owner's delegated-curation "
                "grant.",
            )
        grant = self.grant_for_project(change_set.project_id)
        if not grant.active:
            raise _interactive_required(
                action,
                "Delegated curation is off for this project, so a person must accept "
                "or commit.",
            )
        return grant

    def admit_commit(
        self,
        actor: AuthContext | None,
        *,
        change_set: GraphChangeSet,
        action: str = "Committing graph changes",
    ) -> str:
        """The person of record for this commit, or raise when it is not admitted.

        A person commits as themselves. A non-interactive principal commits
        only under the grant, only when every accepted proposal is within it,
        and only once no proposal is left for a person.
        """

        if actor is None:
            raise AuthError("Authentication required.")
        if actor.is_interactive:
            return str(actor.user_id)
        grant = self.require_grant(actor, change_set=change_set, action=action)
        accepted = [
            operation
            for operation in change_set.operations
            if operation.status == GraphChangeOperationStatus.ACCEPTED
        ]
        refused = grant.refused(accepted)
        if refused:
            raise PermissionDeniedError(
                f"{action} under delegated curation ({grant.policy.value}) does not "
                f"admit {', '.join(refused)}; a person must commit this draft."
            )
        undecided = sum(
            operation.status == GraphChangeOperationStatus.PROPOSED
            for operation in change_set.operations
        )
        if undecided:
            raise PermissionDeniedError(
                f"{action} under delegated curation requires every proposal to be "
                f"decided; {undecided} still need a person's review."
            )
        return _person_of_record(actor, grant)


def _person_of_record(actor: AuthContext, grant: DelegatedCurationGrant) -> str:
    """Whose authority a delegated accept or commit acts under."""

    if actor.is_system and grant.granted_by:
        return grant.granted_by
    return str(actor.user_id)


class DelegationReview(Protocol):
    def bulk_accept_graph_change_operations(
        self,
        change_set_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet: ...


class DelegationCommit(Protocol):
    def commit_graph_change_set(
        self,
        change_set_id: UUID,
        *,
        message: str,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet: ...


class DelegationRecords(Protocol):
    def get_graph_change_set(self, change_set_id: UUID) -> GraphChangeSet: ...

    def save_graph_change_set(self, change_set: GraphChangeSet) -> None: ...


class DelegationAuthorization(Protocol):
    def delegated_curation_grant(self, project_id: UUID) -> DelegatedCurationGrant: ...


class DelegatedCurationCoordinator(BaseService):
    """Apply a project's grant to a draft the server just generated.

    Runs once per generated draft, after the draft is ``READY``: accepts every
    valid proposal the grant admits, then commits only when nothing is left
    for a person. A draft that mixes admitted and unadmitted proposals stays
    in the review queue with its admitted proposals pre-accepted as
    ``auto_accepted``. A refused or failed pass never fails the draft: the
    reason is stamped on the change set and the draft waits for a person.
    """

    def __init__(
        self,
        context: ServiceContext,
        *,
        records: DelegationRecords,
        review: DelegationReview,
        commit: DelegationCommit,
        authorization: DelegationAuthorization,
    ) -> None:
        super().__init__(context)
        self.records = records
        self.review = review
        self.commit = commit
        self.authorization = authorization

    def apply_delegated_curation(self, change_set: GraphChangeSet) -> GraphChangeSet:
        if not is_fresh_draft(change_set):
            return change_set
        grant = self.authorization.delegated_curation_grant(change_set.project_id)
        if not grant.active:
            return change_set
        actor = delegated_curation_actor(grant)
        try:
            # One unit in every context: the outer boundary is a no-op under a
            # request (the request owns the transaction) and opens one in the
            # background worker; the savepoint inside isolates a failed pass
            # so its accepts never outlive the commit they were made for.
            with self.application_transaction(), self.recoverable_unit_of_work():
                return self._apply_under_grant(change_set.change_set_id, grant, actor)
        except Exception as exc:
            # The draft outlives its pass: the reason is stamped on the change
            # set and a person reviews it exactly as the model left it.
            logger.exception(
                "Delegated curation left draft %s for a person", change_set.change_set_id
            )
            return self._record_failure(change_set.change_set_id, grant, str(exc))

    def _apply_under_grant(
        self,
        change_set_id: UUID,
        grant: DelegatedCurationGrant,
        actor: AuthContext,
    ) -> GraphChangeSet:
        change_set = self.review.bulk_accept_graph_change_operations(change_set_id, actor=actor)
        accepted = [
            operation
            for operation in change_set.operations
            if operation.status == GraphChangeOperationStatus.ACCEPTED
        ]
        undecided = [
            operation
            for operation in change_set.operations
            if operation.status == GraphChangeOperationStatus.PROPOSED
        ]
        packet = {
            **grant.as_packet(),
            "applied_at": utc_now().isoformat(),
            "accepted_operation_ids": [str(operation.operation_id) for operation in accepted],
            "left_for_review": len(undecided),
            "committed": False,
        }
        change_set.context_packet = {
            **change_set.context_packet,
            DELEGATED_CURATION_PACKET_KEY: packet,
        }
        change_set.error_metadata = {
            key: value
            for key, value in change_set.error_metadata.items()
            if key != DELEGATED_CURATION_ERROR_KEY
        }
        self.records.save_graph_change_set(change_set)
        if not accepted or undecided:
            return change_set
        committed = self.commit.commit_graph_change_set(
            change_set_id,
            message=_commit_message(grant, len(accepted)),
            actor=actor,
        )
        committed.context_packet = {
            **committed.context_packet,
            DELEGATED_CURATION_PACKET_KEY: {**packet, "committed": True},
        }
        self.records.save_graph_change_set(committed)
        return committed

    def _record_failure(
        self,
        change_set_id: UUID,
        grant: DelegatedCurationGrant,
        message: str,
    ) -> GraphChangeSet:
        change_set = self.records.get_graph_change_set(change_set_id)
        change_set.error_metadata = {
            **change_set.error_metadata,
            DELEGATED_CURATION_ERROR_KEY: {
                **grant.as_packet(),
                "message": message,
                "failed_at": utc_now().isoformat(),
            },
        }
        self.records.save_graph_change_set(change_set)
        return change_set


def is_fresh_draft(change_set: GraphChangeSet) -> bool:
    """Whether the pass may run: a ready, untouched draft it has not seen.

    A draft a person has already started deciding (any operation accepted,
    rejected, or deferred), one the pass already ran on, or a member-onboarding
    draft is never touched, so re-requesting a draft never re-runs the pass
    over someone's review.
    """

    if change_set.status != GraphChangeSetStatus.READY:
        return False
    if change_set.purpose == GraphDraftPurpose.MEMBER_CHECKPOINT_ALIGNMENT:
        return False
    if DELEGATED_CURATION_PACKET_KEY in change_set.context_packet:
        return False
    if DELEGATED_CURATION_ERROR_KEY in change_set.error_metadata:
        return False
    return all(
        operation.status == GraphChangeOperationStatus.PROPOSED
        and DEFERRED_AT_KEY not in operation.error_metadata
        for operation in change_set.operations
    )


def _commit_message(grant: DelegatedCurationGrant, accepted_count: int) -> str:
    noun = "proposal" if accepted_count == 1 else "proposals"
    return (
        f"Delegated curation ({grant.policy.value}): applied {accepted_count} {noun} "
        "automatically under the project owner's grant."
    )
