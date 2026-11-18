"""Resolve review routing using explicit choices and current project membership."""

from uuid import UUID

from lab_tracker.auth import LOCAL_AUTH_USER_ID, AuthContext
from lab_tracker.capture_routing import (
    AssignmentScope,
    CaptureRoute,
    ReviewOperatorAssignment,
    RoutingStatus,
    UnassignedMachineCapture,
    is_machine_capture,
    machine_capture_source,
    overlapping_sessions,
    review_session_for_capture,
    session_targets,
)
from lab_tracker.errors import ConflictError, NotFoundError, ValidationError
from lab_tracker.models import (
    GraphChangeSet,
    GraphDraftMode,
    Note,
    NoteStatus,
    ProjectMembershipRole,
    utc_now,
)
from lab_tracker.repository import LabTrackerRepository
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.note_observed_at import note_observed_at
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.shared import actor_user_fk


def resolve_capture_routes(
    repository: LabTrackerRepository, project_id: UUID, notes: list[Note]
) -> dict[UUID, CaptureRoute]:
    machine = [note for note in notes if note.project_id == project_id and is_machine_capture(note)]
    if not machine:
        return {}
    explicit = repository.review_operators.for_work("note", {note.note_id for note in machine})
    targets = {note.note_id: session_targets(note) for note in machine}
    nominated_sessions = {note.note_id: review_session_for_capture(note) for note in machine}
    single_session_ids = {
        session_id for session_id in nominated_sessions.values() if session_id is not None
    }
    sessions = repository.review_operators.for_work("session", single_session_ids)
    memberships, _ = repository.query_project_memberships(project_id=project_id)
    eligible = {
        member.user_id
        for member in memberships
        if member.user_id != LOCAL_AUTH_USER_ID
        and member.role in {ProjectMembershipRole.OWNER, ProjectMembershipRole.CONTRIBUTOR}
    }

    routes = {}
    for note in machine:
        session_ids = targets[note.note_id]
        operator = explicit.get(note.note_id)
        if operator is None:
            session_id = nominated_sessions[note.note_id]
            if session_id is None:
                routes[note.note_id] = CaptureRoute(
                    note_id=note.note_id,
                    status="ambiguous_session" if len(session_ids) > 1 else "needs_session",
                    session_ids=session_ids,
                )
                continue
            operator = sessions.get(session_id)
        status: RoutingStatus = "needs_operator"
        if operator is not None and operator.user_id is not None:
            status = "assigned" if operator.user_id in eligible else "operator_unavailable"
        routes[note.note_id] = CaptureRoute(
            note_id=note.note_id, status=status, session_ids=session_ids, operator=operator
        )
    return routes


class CaptureRoutingService(BaseService):
    def __init__(
        self, context: ServiceContext, *, authorization: ProjectAuthorizationPolicy
    ) -> None:
        super().__init__(context)
        self.authorization = authorization

    def unassigned(
        self, project_id: UUID, *, actor: AuthContext | None
    ) -> list[UnassignedMachineCapture]:
        self.authorization.require_owner(project_id, actor=actor)
        if self.repository.projects.get(project_id) is None:
            raise NotFoundError("Project does not exist.")
        notes, _ = self.repository.query_notes(
            project_id=project_id, status=NoteStatus.STAGED.value
        )
        routes = resolve_capture_routes(self.repository, project_id, notes)
        clock_sessions, _ = (
            self.repository.query_sessions(project_id=project_id)
            if any(route.status == "needs_session" for route in routes.values())
            else ([], 0)
        )
        return [
            UnassignedMachineCapture(
                note_id=note.note_id,
                captured_at=note.created_at,
                source=machine_capture_source(note),
                title=(
                    str(note.metadata.get("evidence_title") or "")
                    or (note.raw_asset.filename if note.raw_asset else note.raw_content)
                )[:160],
                route=route,
                clock_sessions=[
                    session.session_id
                    for session in overlapping_sessions(note_observed_at(note)[0], clock_sessions)
                ]
                if route.status == "needs_session"
                else [],
            )
            for note in notes
            if (route := routes.get(note.note_id)) is not None and route.status != "assigned"
        ]

    def _project_for(self, scope: AssignmentScope, work_id: UUID) -> UUID:
        if scope == "note":
            note = self.repository.notes.get(work_id)
            if note is None or not is_machine_capture(note):
                raise NotFoundError("Machine capture does not exist.")
            return note.project_id
        session = self.repository.sessions.get(work_id)
        if session is None:
            raise NotFoundError("Session does not exist.")
        return session.project_id

    def get(
        self, scope: AssignmentScope, work_id: UUID, *, actor: AuthContext | None
    ) -> ReviewOperatorAssignment | None:
        project_id = self._project_for(scope, work_id)
        self.authorization.require_read(project_id, actor=actor)
        return self.repository.review_operators.for_work(scope, {work_id}).get(work_id)

    def assign(
        self,
        scope: AssignmentScope,
        work_id: UUID,
        user_id: UUID | None,
        *,
        actor: AuthContext | None,
    ) -> ReviewOperatorAssignment:
        self.authorization.require_interactive(actor, action="Assigning review operators")
        with self.application_transaction():
            project_id = self._project_for(scope, work_id)
            self.authorization.require_owner(project_id, actor=actor)
            self.repository.lock_graph_draft_batch_settings(project_id)
            if user_id is not None:
                membership = self.repository.get_project_membership(
                    project_id=project_id, user_id=user_id
                )
                if (
                    user_id == LOCAL_AUTH_USER_ID
                    or membership is None
                    or membership.role
                    not in {ProjectMembershipRole.OWNER, ProjectMembershipRole.CONTRIBUTOR}
                ):
                    raise ValidationError(
                        "Choose a project owner or contributor to review captures."
                    )
            current = self.repository.review_operators.for_work(scope, {work_id}).get(work_id)
            if current is not None and current.user_id == user_id:
                return current
            assignment = ReviewOperatorAssignment(
                scope=scope,
                work_id=work_id,
                user_id=user_id,
                assigned_by_user_id=actor_user_fk(actor, self.repository),
                assigned_at=utc_now(),
            )
            self.repository.review_operators.save(assignment)
            return assignment


CAPTURE_ROUTES_KEY = "capture_review_routes"


def capture_route_revision(note: Note, route: CaptureRoute) -> str:
    """The assignment and the explicit association under which a note was picked up."""
    if route.operator is None:
        return ""
    operator = route.operator
    linked_at = note.review_session_linked_at if operator.scope == "session" else None
    return "|".join(
        (
            operator.scope,
            str(operator.work_id),
            str(operator.user_id),
            operator.assigned_at.isoformat(),
            linked_at.isoformat() if linked_at is not None else "",
        )
    )


def snapshot_capture_routes(
    repository: LabTrackerRepository,
    project_id: UUID,
    notes: list[Note],
    reviewer_id: UUID | None,
) -> dict[str, str]:
    routes = resolve_capture_routes(repository, project_id, notes)
    expected = {}
    for note in notes:
        route = routes.get(note.note_id)
        if route is None:
            continue
        if (
            route.status != "assigned"
            or route.operator is None
            or route.operator.user_id != reviewer_id
        ):
            raise ConflictError(
                "Capture routing changed. Generate a fresh review for the operator."
            )
        expected[str(note.note_id)] = capture_route_revision(note, route)
    return expected


def require_current_capture_routes(repository: LabTrackerRepository, draft: GraphChangeSet) -> None:
    if draft.draft_mode != GraphDraftMode.GRAPH_BATCH:
        return
    expected = draft.review_metadata.get(CAPTURE_ROUTES_KEY)
    if expected == {}:
        return
    notes, _ = repository.query_notes(
        project_id=draft.project_id,
        note_ids=set(draft.source_note_ids) | {draft.source_note_id},
    )
    current = snapshot_capture_routes(
        repository, draft.project_id, notes, draft.review_assignee_user_id
    )
    if expected is not None and expected != current:
        raise ConflictError("Capture routing changed. Generate a fresh review for the operator.")
