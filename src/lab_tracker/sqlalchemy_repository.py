"""SQLAlchemy-backed repository scaffold for Lab Tracker domain entities."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Generic, TypeVar
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    AnalysisDatasetModel,
    AnalysisModel,
    ClaimAnalysisModel,
    ClaimDatasetModel,
    ClaimModel,
    DatasetModel,
    DatasetQuestionLinkModel,
    NoteExtractedEntityModel,
    NoteModel,
    NoteTagSuggestionModel,
    NoteTargetModel,
    ProjectModel,
    QuestionModel,
    QuestionParentModel,
    SessionModel,
    VisualizationClaimModel,
    VisualizationModel,
)
from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    Claim,
    Dataset,
    Note,
    Project,
    Question,
    Session,
    Visualization,
)
from lab_tracker.repository import EntityRepository, LabTrackerRepository
from lab_tracker.sqlalchemy_mappers import (
    analysis_dataset_models,
    analysis_from_model,
    analysis_to_model,
    apply_analysis_to_model,
    apply_claim_to_model,
    apply_dataset_to_model,
    apply_note_to_model,
    apply_project_to_model,
    apply_question_to_model,
    apply_session_to_model,
    apply_visualization_to_model,
    claim_analysis_models,
    claim_dataset_models,
    claim_from_model,
    claim_to_model,
    dataset_from_model,
    dataset_question_link_from_model,
    dataset_question_link_models,
    dataset_to_model,
    entity_ref_from_model,
    extracted_entity_from_model,
    note_extracted_entity_models,
    note_from_model,
    note_tag_suggestion_models,
    note_target_models,
    note_to_model,
    project_from_model,
    project_to_model,
    question_from_model,
    question_parent_models,
    question_to_model,
    session_from_model,
    session_to_model,
    tag_suggestion_from_model,
    visualization_claim_models,
    visualization_from_model,
    visualization_to_model,
)

EntityT = TypeVar("EntityT")
ModelT = TypeVar("ModelT")


def _replace_child_rows(
    session: OrmSession,
    model_type: type[Any],
    column: Any,
    owner_id: str,
    replacement_rows: list[Any],
) -> None:
    session.execute(delete(model_type).where(column == owner_id))
    if replacement_rows:
        session.add_all(replacement_rows)


class _SQLAlchemyModelRepository(Generic[EntityT, ModelT], EntityRepository[EntityT]):
    def __init__(
        self,
        session: OrmSession,
        *,
        model_type: type[ModelT],
        id_column: Any,
        entity_id_getter: Callable[[EntityT], UUID],
        to_model: Callable[[EntityT], ModelT],
        from_model: Callable[[ModelT], EntityT],
        apply_to_model: Callable[[ModelT, EntityT], None],
    ) -> None:
        self._session = session
        self._model_type = model_type
        self._id_column = id_column
        self._entity_id_getter = entity_id_getter
        self._to_model = to_model
        self._from_model = from_model
        self._apply_to_model = apply_to_model

    def get(self, entity_id: UUID) -> EntityT | None:
        row = self._session.get(self._model_type, str(entity_id))
        if row is None:
            return None
        return self._from_model(row)

    def list(self) -> list[EntityT]:
        rows = list(self._session.scalars(select(self._model_type).order_by(self._id_column)))
        return [self._from_model(row) for row in rows]

    def save(self, entity: EntityT) -> None:
        entity_id = self._entity_id_getter(entity)
        row = self._session.get(self._model_type, str(entity_id))
        if row is None:
            self._session.add(self._to_model(entity))
            return
        self._apply_to_model(row, entity)

    def delete(self, entity_id: UUID) -> EntityT | None:
        row = self._session.get(self._model_type, str(entity_id))
        if row is None:
            return None
        entity = self._from_model(row)
        self._session.delete(row)
        return entity


class SQLAlchemyProjectRepository(_SQLAlchemyModelRepository[Project, ProjectModel]):
    def __init__(self, session: OrmSession) -> None:
        super().__init__(
            session,
            model_type=ProjectModel,
            id_column=ProjectModel.project_id,
            entity_id_getter=lambda entity: entity.project_id,
            to_model=project_to_model,
            from_model=project_from_model,
            apply_to_model=apply_project_to_model,
        )


class SQLAlchemyQuestionRepository(EntityRepository[Question]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def _parent_map(self, question_ids: list[str]) -> dict[str, list[UUID]]:
        parent_map: dict[str, list[UUID]] = defaultdict(list)
        if not question_ids:
            return parent_map
        rows = self._session.scalars(
            select(QuestionParentModel).where(QuestionParentModel.question_id.in_(question_ids))
        )
        for row in rows:
            parent_map[row.question_id].append(UUID(row.parent_question_id))
        return parent_map

    def get(self, entity_id: UUID) -> Question | None:
        row = self._session.get(QuestionModel, str(entity_id))
        if row is None:
            return None
        parent_ids = self._parent_map([row.question_id]).get(row.question_id, [])
        return question_from_model(row, parent_question_ids=parent_ids)

    def list(self) -> list[Question]:
        rows = list(
            self._session.scalars(
                select(QuestionModel).order_by(QuestionModel.created_at, QuestionModel.question_id)
            )
        )
        question_ids = [row.question_id for row in rows]
        parent_map = self._parent_map(question_ids)
        return [
            question_from_model(row, parent_question_ids=parent_map.get(row.question_id, []))
            for row in rows
        ]

    def save(self, entity: Question) -> None:
        entity_id = str(entity.question_id)
        row = self._session.get(QuestionModel, entity_id)
        if row is None:
            self._session.add(question_to_model(entity))
        else:
            apply_question_to_model(row, entity)
        _replace_child_rows(
            self._session,
            QuestionParentModel,
            QuestionParentModel.question_id,
            entity_id,
            question_parent_models(entity),
        )

    def delete(self, entity_id: UUID) -> Question | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(QuestionModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity


class SQLAlchemyDatasetRepository(EntityRepository[Dataset]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def _link_map(self, dataset_ids: list[str]) -> dict[str, list[DatasetQuestionLinkModel]]:
        link_map: dict[str, list[DatasetQuestionLinkModel]] = defaultdict(list)
        if not dataset_ids:
            return link_map
        rows = self._session.scalars(
            select(DatasetQuestionLinkModel).where(
                DatasetQuestionLinkModel.dataset_id.in_(dataset_ids)
            )
        )
        for row in rows:
            link_map[row.dataset_id].append(row)
        return link_map

    def get(self, entity_id: UUID) -> Dataset | None:
        row = self._session.get(DatasetModel, str(entity_id))
        if row is None:
            return None
        links = self._link_map([row.dataset_id]).get(row.dataset_id, [])
        question_links = [dataset_question_link_from_model(link) for link in links]
        return dataset_from_model(row, question_links=question_links)

    def list(self) -> list[Dataset]:
        rows = list(
            self._session.scalars(
                select(DatasetModel).order_by(DatasetModel.created_at, DatasetModel.dataset_id)
            )
        )
        dataset_ids = [row.dataset_id for row in rows]
        link_map = self._link_map(dataset_ids)
        entities: list[Dataset] = []
        for row in rows:
            link_rows = link_map.get(row.dataset_id, [])
            question_links = [dataset_question_link_from_model(link) for link in link_rows]
            entities.append(dataset_from_model(row, question_links=question_links))
        return entities

    def save(self, entity: Dataset) -> None:
        entity_id = str(entity.dataset_id)
        row = self._session.get(DatasetModel, entity_id)
        if row is None:
            self._session.add(dataset_to_model(entity))
        else:
            apply_dataset_to_model(row, entity)
        _replace_child_rows(
            self._session,
            DatasetQuestionLinkModel,
            DatasetQuestionLinkModel.dataset_id,
            entity_id,
            dataset_question_link_models(entity),
        )

    def delete(self, entity_id: UUID) -> Dataset | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(DatasetModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity


class SQLAlchemyNoteRepository(EntityRepository[Note]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def _children(self, note_id: str) -> tuple[list[Any], list[Any], list[Any]]:
        extracted_rows = list(
            self._session.scalars(
                select(NoteExtractedEntityModel).where(NoteExtractedEntityModel.note_id == note_id)
            )
        )
        suggestion_rows = list(
            self._session.scalars(
                select(NoteTagSuggestionModel).where(NoteTagSuggestionModel.note_id == note_id)
            )
        )
        target_rows = list(
            self._session.scalars(select(NoteTargetModel).where(NoteTargetModel.note_id == note_id))
        )
        return extracted_rows, suggestion_rows, target_rows

    def get(self, entity_id: UUID) -> Note | None:
        row = self._session.get(NoteModel, str(entity_id))
        if row is None:
            return None
        extracted_rows, suggestion_rows, target_rows = self._children(row.note_id)
        return note_from_model(
            row,
            extracted_entities=[extracted_entity_from_model(item) for item in extracted_rows],
            tag_suggestions=[tag_suggestion_from_model(item) for item in suggestion_rows],
            targets=[entity_ref_from_model(item) for item in target_rows],
        )

    def list(self) -> list[Note]:
        rows = list(
            self._session.scalars(
                select(NoteModel).order_by(NoteModel.created_at, NoteModel.note_id)
            )
        )
        entities: list[Note] = []
        for row in rows:
            extracted_rows, suggestion_rows, target_rows = self._children(row.note_id)
            entities.append(
                note_from_model(
                    row,
                    extracted_entities=[
                        extracted_entity_from_model(item) for item in extracted_rows
                    ],
                    tag_suggestions=[tag_suggestion_from_model(item) for item in suggestion_rows],
                    targets=[entity_ref_from_model(item) for item in target_rows],
                )
            )
        return entities

    def save(self, entity: Note) -> None:
        entity_id = str(entity.note_id)
        row = self._session.get(NoteModel, entity_id)
        if row is None:
            self._session.add(note_to_model(entity))
        else:
            apply_note_to_model(row, entity)
        _replace_child_rows(
            self._session,
            NoteExtractedEntityModel,
            NoteExtractedEntityModel.note_id,
            entity_id,
            note_extracted_entity_models(entity),
        )
        _replace_child_rows(
            self._session,
            NoteTagSuggestionModel,
            NoteTagSuggestionModel.note_id,
            entity_id,
            note_tag_suggestion_models(entity),
        )
        _replace_child_rows(
            self._session,
            NoteTargetModel,
            NoteTargetModel.note_id,
            entity_id,
            note_target_models(entity),
        )

    def delete(self, entity_id: UUID) -> Note | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(NoteModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity


class SQLAlchemySessionRepository(_SQLAlchemyModelRepository[Session, SessionModel]):
    def __init__(self, session: OrmSession) -> None:
        super().__init__(
            session,
            model_type=SessionModel,
            id_column=SessionModel.session_id,
            entity_id_getter=lambda entity: entity.session_id,
            to_model=session_to_model,
            from_model=session_from_model,
            apply_to_model=apply_session_to_model,
        )


class SQLAlchemyAnalysisRepository(EntityRepository[Analysis]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def _dataset_map(self, analysis_ids: list[str]) -> dict[str, list[UUID]]:
        dataset_map: dict[str, list[UUID]] = defaultdict(list)
        if not analysis_ids:
            return dataset_map
        rows = self._session.scalars(
            select(AnalysisDatasetModel).where(AnalysisDatasetModel.analysis_id.in_(analysis_ids))
        )
        for row in rows:
            dataset_map[row.analysis_id].append(UUID(row.dataset_id))
        return dataset_map

    def get(self, entity_id: UUID) -> Analysis | None:
        row = self._session.get(AnalysisModel, str(entity_id))
        if row is None:
            return None
        dataset_ids = self._dataset_map([row.analysis_id]).get(row.analysis_id, [])
        return analysis_from_model(row, dataset_ids=dataset_ids)

    def list(self) -> list[Analysis]:
        rows = list(
            self._session.scalars(
                select(AnalysisModel).order_by(AnalysisModel.created_at, AnalysisModel.analysis_id)
            )
        )
        analysis_ids = [row.analysis_id for row in rows]
        dataset_map = self._dataset_map(analysis_ids)
        return [
            analysis_from_model(row, dataset_ids=dataset_map.get(row.analysis_id, []))
            for row in rows
        ]

    def save(self, entity: Analysis) -> None:
        entity_id = str(entity.analysis_id)
        row = self._session.get(AnalysisModel, entity_id)
        if row is None:
            self._session.add(analysis_to_model(entity))
        else:
            apply_analysis_to_model(row, entity)
        _replace_child_rows(
            self._session,
            AnalysisDatasetModel,
            AnalysisDatasetModel.analysis_id,
            entity_id,
            analysis_dataset_models(entity),
        )

    def delete(self, entity_id: UUID) -> Analysis | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(AnalysisModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity


class SQLAlchemyClaimRepository(EntityRepository[Claim]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def _dataset_map(self, claim_ids: list[str]) -> dict[str, list[UUID]]:
        dataset_map: dict[str, list[UUID]] = defaultdict(list)
        if not claim_ids:
            return dataset_map
        rows = self._session.scalars(
            select(ClaimDatasetModel).where(ClaimDatasetModel.claim_id.in_(claim_ids))
        )
        for row in rows:
            dataset_map[row.claim_id].append(UUID(row.dataset_id))
        return dataset_map

    def _analysis_map(self, claim_ids: list[str]) -> dict[str, list[UUID]]:
        analysis_map: dict[str, list[UUID]] = defaultdict(list)
        if not claim_ids:
            return analysis_map
        rows = self._session.scalars(
            select(ClaimAnalysisModel).where(ClaimAnalysisModel.claim_id.in_(claim_ids))
        )
        for row in rows:
            analysis_map[row.claim_id].append(UUID(row.analysis_id))
        return analysis_map

    def get(self, entity_id: UUID) -> Claim | None:
        row = self._session.get(ClaimModel, str(entity_id))
        if row is None:
            return None
        claim_ids = [row.claim_id]
        dataset_ids = self._dataset_map(claim_ids).get(row.claim_id, [])
        analysis_ids = self._analysis_map(claim_ids).get(row.claim_id, [])
        return claim_from_model(
            row,
            supported_by_dataset_ids=dataset_ids,
            supported_by_analysis_ids=analysis_ids,
        )

    def list(self) -> list[Claim]:
        rows = list(
            self._session.scalars(
                select(ClaimModel).order_by(ClaimModel.created_at, ClaimModel.claim_id)
            )
        )
        claim_ids = [row.claim_id for row in rows]
        dataset_map = self._dataset_map(claim_ids)
        analysis_map = self._analysis_map(claim_ids)
        return [
            claim_from_model(
                row,
                supported_by_dataset_ids=dataset_map.get(row.claim_id, []),
                supported_by_analysis_ids=analysis_map.get(row.claim_id, []),
            )
            for row in rows
        ]

    def save(self, entity: Claim) -> None:
        entity_id = str(entity.claim_id)
        row = self._session.get(ClaimModel, entity_id)
        if row is None:
            self._session.add(claim_to_model(entity))
        else:
            apply_claim_to_model(row, entity)
        _replace_child_rows(
            self._session,
            ClaimDatasetModel,
            ClaimDatasetModel.claim_id,
            entity_id,
            claim_dataset_models(entity),
        )
        _replace_child_rows(
            self._session,
            ClaimAnalysisModel,
            ClaimAnalysisModel.claim_id,
            entity_id,
            claim_analysis_models(entity),
        )

    def delete(self, entity_id: UUID) -> Claim | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(ClaimModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity


class SQLAlchemyVisualizationRepository(EntityRepository[Visualization]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def _claim_map(self, visualization_ids: list[str]) -> dict[str, list[UUID]]:
        claim_map: dict[str, list[UUID]] = defaultdict(list)
        if not visualization_ids:
            return claim_map
        rows = self._session.scalars(
            select(VisualizationClaimModel).where(
                VisualizationClaimModel.viz_id.in_(visualization_ids)
            )
        )
        for row in rows:
            claim_map[row.viz_id].append(UUID(row.claim_id))
        return claim_map

    def get(self, entity_id: UUID) -> Visualization | None:
        row = self._session.get(VisualizationModel, str(entity_id))
        if row is None:
            return None
        claim_ids = self._claim_map([row.viz_id]).get(row.viz_id, [])
        return visualization_from_model(row, related_claim_ids=claim_ids)

    def list(self) -> list[Visualization]:
        rows = list(
            self._session.scalars(
                select(VisualizationModel).order_by(
                    VisualizationModel.created_at,
                    VisualizationModel.viz_id,
                )
            )
        )
        visualization_ids = [row.viz_id for row in rows]
        claim_map = self._claim_map(visualization_ids)
        return [
            visualization_from_model(row, related_claim_ids=claim_map.get(row.viz_id, []))
            for row in rows
        ]

    def save(self, entity: Visualization) -> None:
        entity_id = str(entity.viz_id)
        row = self._session.get(VisualizationModel, entity_id)
        if row is None:
            self._session.add(visualization_to_model(entity))
        else:
            apply_visualization_to_model(row, entity)
        _replace_child_rows(
            self._session,
            VisualizationClaimModel,
            VisualizationClaimModel.viz_id,
            entity_id,
            visualization_claim_models(entity),
        )

    def delete(self, entity_id: UUID) -> Visualization | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(VisualizationModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity


class UnsupportedAcquisitionOutputRepository(EntityRepository[AcquisitionOutput]):
    _error = (
        "Acquisition outputs are not yet mapped to SQLAlchemy because there is no "
        "acquisition_outputs table in the current migration set."
    )

    def get(self, entity_id: UUID) -> AcquisitionOutput | None:
        raise NotImplementedError(self._error)

    def list(self) -> list[AcquisitionOutput]:
        raise NotImplementedError(self._error)

    def save(self, entity: AcquisitionOutput) -> None:
        raise NotImplementedError(self._error)

    def delete(self, entity_id: UUID) -> AcquisitionOutput | None:
        raise NotImplementedError(self._error)


class SQLAlchemyLabTrackerRepository(LabTrackerRepository):
    """Repository scaffold backed by a SQLAlchemy ORM session."""

    def __init__(self, session: OrmSession) -> None:
        self._session = session
        self.projects = SQLAlchemyProjectRepository(session)
        self.questions = SQLAlchemyQuestionRepository(session)
        self.datasets = SQLAlchemyDatasetRepository(session)
        self.notes = SQLAlchemyNoteRepository(session)
        self.sessions = SQLAlchemySessionRepository(session)
        self.acquisition_outputs = UnsupportedAcquisitionOutputRepository()
        self.analyses = SQLAlchemyAnalysisRepository(session)
        self.claims = SQLAlchemyClaimRepository(session)
        self.visualizations = SQLAlchemyVisualizationRepository(session)

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()
