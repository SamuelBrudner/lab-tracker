"""First-class Experiment lifecycle and membership service."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    Dataset,
    EntityOrigin,
    Experiment,
    ExperimentStatus,
    QuestionStatus,
    Session,
    SessionType,
    utc_now,
)
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.project_authorization import (
    ProjectAuthorizationPolicy,
)
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.question_service import QuestionService
from lab_tracker.services.shared import (
    actor_user_fk,
    actor_user_id,
    ensure_non_empty,
)

if TYPE_CHECKING:
    from lab_tracker.services.dataset_service import DatasetService
    from lab_tracker.services.session_service import SessionService


class _UnsetDescription:
    """Distinguish an omitted PATCH field from an explicit null."""


_UNSET_DESCRIPTION = _UnsetDescription()

EXPERIMENT_STATUS_TRANSITIONS = {
    ExperimentStatus.ACTIVE: {
        ExperimentStatus.ACTIVE,
        ExperimentStatus.CLOSED,
    },
    ExperimentStatus.CLOSED: {
        ExperimentStatus.CLOSED,
        ExperimentStatus.ARCHIVED,
    },
    ExperimentStatus.ARCHIVED: {ExperimentStatus.ARCHIVED},
}


class ExperimentService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        questions: QuestionService,
        sessions_provider: Callable[[], SessionService],
        datasets_provider: Callable[[], DatasetService],
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.questions = questions
        self._sessions_provider = sessions_provider
        self._datasets_provider = datasets_provider
        self.authorization = authorization

    @property
    def sessions(self) -> SessionService:
        return self._sessions_provider()

    @property
    def datasets(self) -> DatasetService:
        return self._datasets_provider()

    def create_experiment(
        self,
        *,
        project_id: UUID,
        name: str,
        primary_question_id: UUID,
        description: str | None = None,
        actor: AuthContext | None = None,
        origin: EntityOrigin = EntityOrigin.USER,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Experiment:
        self.authorization.require_contributor(project_id, actor=actor)
        self.projects.get_project(project_id)
        ensure_non_empty(name, "name")
        question = self.questions.get_question(primary_question_id)
        if question.project_id != project_id:
            raise ValidationError(
                "Primary question must belong to the same project."
            )
        if question.status != QuestionStatus.ACTIVE:
            raise ValidationError(
                "Primary question must be active for Experiments."
            )
        experiment = Experiment(
            experiment_id=uuid4(),
            project_id=project_id,
            name=name.strip(),
            description=(description or "").strip(),
            primary_question_id=primary_question_id,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
            origin=origin,
            change_set_id=change_set_id,
            origin_provider=origin_provider,
            origin_model=origin_model,
            origin_prompt_version=origin_prompt_version,
        )
        with self.unit_of_work() as repository:
            repository.experiments.save(experiment)
        return experiment

    def get_experiment(self, experiment_id: UUID) -> Experiment:
        return self.get_from_repository(
            entity_id=experiment_id,
            label="Experiment",
            loader=lambda repository: repository.experiments.get(
                experiment_id
            ),
        )

    def query_experiments(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        primary_question_id: UUID | None = None,
        status: ExperimentStatus | None = None,
        search: str | None = None,
        session_id: UUID | None = None,
        dataset_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Experiment], int]:
        return self.repository.query_experiments(
            project_id=project_id,
            project_ids=project_ids,
            primary_question_id=primary_question_id,
            status=status.value if status is not None else None,
            search=search,
            session_id=session_id,
            dataset_id=dataset_id,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )

    def update_experiment(
        self,
        experiment_id: UUID,
        *,
        name: str | None = None,
        description: str | None | _UnsetDescription = _UNSET_DESCRIPTION,
        status: ExperimentStatus | None = None,
        actor: AuthContext | None = None,
    ) -> Experiment:
        experiment = self.get_experiment(experiment_id)
        self.authorization.require_contributor(
            experiment.project_id,
            actor=actor,
        )
        if (
            name is None
            and isinstance(description, _UnsetDescription)
            and status is None
        ):
            return experiment
        with self.application_transaction(), self.unit_of_work() as repository:
            repository.lock_experiment_updates((experiment_id,))
            experiment = self.get_experiment(experiment_id)
            self.authorization.require_contributor(
                experiment.project_id,
                actor=actor,
            )
            if experiment.status == ExperimentStatus.ARCHIVED:
                raise ValidationError("Archived Experiments are immutable.")
            if status is not None:
                self._ensure_status_transition(experiment.status, status)
            if name is not None:
                ensure_non_empty(name, "name")
                experiment.name = name.strip()
            if not isinstance(description, _UnsetDescription):
                experiment.description = (description or "").strip()
            now = utc_now()
            if (
                status == ExperimentStatus.CLOSED
                and experiment.status == ExperimentStatus.ACTIVE
            ):
                experiment.closed_at = now
            if (
                status == ExperimentStatus.ARCHIVED
                and experiment.status == ExperimentStatus.CLOSED
            ):
                experiment.archived_at = now
            if status is not None:
                experiment.status = status
            experiment.updated_at = now
            repository.experiments.save(experiment)
        return experiment

    def add_session(
        self,
        experiment_id: UUID,
        session_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Experiment:
        experiment = self.get_experiment(experiment_id)
        self.authorization.require_contributor(
            experiment.project_id,
            actor=actor,
        )
        with self.application_transaction(), self.unit_of_work() as repository:
            # Promotion takes this same Session lock before it snapshots
            # memberships, so a PUT is wholly before or after inheritance.
            repository.lock_session_acquisition_state(session_id)
            repository.lock_experiment_updates((experiment_id,))
            experiment = self.get_experiment(experiment_id)
            self.authorization.require_contributor(
                experiment.project_id,
                actor=actor,
            )
            session = self.sessions.get_session(session_id)
            if repository.experiment_has_session(
                experiment_id=experiment_id,
                session_id=session_id,
            ):
                return experiment
            if experiment.status != ExperimentStatus.ACTIVE:
                raise ValidationError(
                    "Only active Experiments accept Session members."
                )
            self._validate_session(experiment, session)
            now = utc_now()
            repository.add_experiment_session(
                experiment_id=experiment_id,
                session_id=session_id,
                created_by=actor_user_id(actor),
                created_by_user_id=actor_user_fk(actor, repository),
                created_at=now,
            )
            experiment.updated_at = now
            repository.experiments.save(experiment)
        return experiment

    def remove_session(
        self,
        experiment_id: UUID,
        session_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Experiment:
        experiment = self.get_experiment(experiment_id)
        self.authorization.require_contributor(
            experiment.project_id,
            actor=actor,
        )
        with self.application_transaction(), self.unit_of_work() as repository:
            # Keep DELETE ordered with promotion's membership snapshot.
            repository.lock_session_acquisition_state(session_id)
            repository.lock_experiment_updates((experiment_id,))
            experiment = self.get_experiment(experiment_id)
            self.authorization.require_contributor(
                experiment.project_id,
                actor=actor,
            )
            if not repository.experiment_has_session(
                experiment_id=experiment_id,
                session_id=session_id,
            ):
                return experiment
            if experiment.status == ExperimentStatus.ARCHIVED:
                raise ValidationError("Archived Experiments are immutable.")
            now = utc_now()
            repository.remove_experiment_session(
                experiment_id=experiment_id,
                session_id=session_id,
            )
            experiment.updated_at = now
            repository.experiments.save(experiment)
        return experiment

    def add_dataset(
        self,
        experiment_id: UUID,
        dataset_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Experiment:
        experiment = self.get_experiment(experiment_id)
        self.authorization.require_contributor(
            experiment.project_id,
            actor=actor,
        )
        with self.application_transaction():
            self.repository.lock_experiment_updates((experiment_id,))
            return self._add_dataset_locked(
                experiment_id,
                dataset_id,
                actor=actor,
            )

    def _add_dataset_locked(
        self,
        experiment_id: UUID,
        dataset_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Experiment:
        """Add a Dataset after the caller has locked this Experiment."""

        experiment = self.get_experiment(experiment_id)
        self.authorization.require_contributor(
            experiment.project_id,
            actor=actor,
        )
        dataset = self.datasets.get_dataset(dataset_id)
        if self.repository.experiment_has_dataset(
            experiment_id=experiment_id,
            dataset_id=dataset_id,
        ):
            return experiment
        if experiment.status == ExperimentStatus.ARCHIVED:
            raise ValidationError(
                "Archived Experiments do not accept Dataset members."
            )
        self._validate_dataset(experiment, dataset)
        now = utc_now()
        with self.unit_of_work() as repository:
            repository.add_experiment_dataset(
                experiment_id=experiment_id,
                dataset_id=dataset_id,
                created_by=actor_user_id(actor),
                created_by_user_id=actor_user_fk(actor, repository),
                created_at=now,
            )
            experiment.updated_at = now
            repository.experiments.save(experiment)
        return experiment

    def remove_dataset(
        self,
        experiment_id: UUID,
        dataset_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Experiment:
        experiment = self.get_experiment(experiment_id)
        self.authorization.require_contributor(
            experiment.project_id,
            actor=actor,
        )
        with self.application_transaction(), self.unit_of_work() as repository:
            repository.lock_experiment_updates((experiment_id,))
            experiment = self.get_experiment(experiment_id)
            self.authorization.require_contributor(
                experiment.project_id,
                actor=actor,
            )
            if not repository.experiment_has_dataset(
                experiment_id=experiment_id,
                dataset_id=dataset_id,
            ):
                return experiment
            if experiment.status == ExperimentStatus.ARCHIVED:
                raise ValidationError("Archived Experiments are immutable.")
            now = utc_now()
            repository.remove_experiment_dataset(
                experiment_id=experiment_id,
                dataset_id=dataset_id,
            )
            experiment.updated_at = now
            repository.experiments.save(experiment)
        return experiment

    def query_sessions(
        self,
        experiment_id: UUID,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Session], int]:
        self.get_experiment(experiment_id)
        return self.repository.query_experiment_sessions(
            experiment_id=experiment_id,
            limit=limit,
            offset=offset,
        )

    def query_datasets(
        self,
        experiment_id: UUID,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Dataset], int]:
        self.get_experiment(experiment_id)
        return self.repository.query_experiment_datasets(
            experiment_id=experiment_id,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def _ensure_status_transition(
        current: ExperimentStatus,
        next_status: ExperimentStatus,
    ) -> None:
        if next_status not in EXPERIMENT_STATUS_TRANSITIONS[current]:
            raise ValidationError(
                "Experiment status cannot transition "
                f"from {current.value} to {next_status.value}."
            )

    @staticmethod
    def _validate_session(
        experiment: Experiment,
        session: Session,
    ) -> None:
        if session.project_id != experiment.project_id:
            raise ValidationError(
                "Session must belong to the same project as the Experiment."
            )
        if (
            session.session_type == SessionType.SCIENTIFIC
            and session.primary_question_id != experiment.primary_question_id
        ):
            raise ValidationError(
                "Scientific Session primary question must match the "
                "Experiment primary question."
            )

    @staticmethod
    def _validate_dataset(
        experiment: Experiment,
        dataset: Dataset,
    ) -> None:
        if dataset.project_id != experiment.project_id:
            raise ValidationError(
                "Dataset must belong to the same project as the Experiment."
            )
        if not any(
            link.question_id == experiment.primary_question_id
            for link in dataset.question_links
        ):
            raise ValidationError(
                "Dataset must link the Experiment primary question."
            )
