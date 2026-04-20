"""SQLAlchemy-backed repository scaffold for Lab Tracker domain entities."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Generic, TypeVar
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    AcquisitionOutputModel,
    AnalysisDatasetModel,
    AnalysisModel,
    ClaimAnalysisModel,
    ClaimDatasetModel,
    ClaimModel,
    DatasetFileModel,
    DatasetModel,
    DatasetQuestionLinkModel,
    DatasetReviewModel,
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
    DatasetFile,
    DatasetReview,
    Note,
    Project,
    Question,
    Session,
    Visualization,
)
from lab_tracker.repository import EntityRepository, LabTrackerRepository
from lab_tracker.sqlalchemy_mappers import (
    acquisition_output_from_model,
    acquisition_output_to_model,
    analysis_dataset_models,
    analysis_from_model,
    analysis_to_model,
    apply_acquisition_output_to_model,
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
    dataset_review_from_model,
    dataset_review_to_model,
    apply_dataset_review_to_model,
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


def _count_from_statement(session: OrmSession, statement: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)


def _apply_pagination(statement: Any, *, limit: int | None, offset: int) -> Any:
    if offset > 0:
        statement = statement.offset(offset)
    if limit is not None:
        statement = statement.limit(limit)
    return statement


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
        self._session.flush()
        row = self._session.get(self._model_type, str(entity_id))
        if row is None:
            return None
        return self._from_model(row)

    def list(self) -> list[EntityT]:
        self._session.flush()
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
        self._session.flush()
        row = self._session.get(QuestionModel, str(entity_id))
        if row is None:
            return None
        parent_ids = self._parent_map([row.question_id]).get(row.question_id, [])
        return question_from_model(row, parent_question_ids=parent_ids)

    def list(self) -> list[Question]:
        self._session.flush()
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
        self._session.flush()
        row = self._session.get(DatasetModel, str(entity_id))
        if row is None:
            return None
        links = self._link_map([row.dataset_id]).get(row.dataset_id, [])
        question_links = [dataset_question_link_from_model(link) for link in links]
        return dataset_from_model(row, question_links=question_links)

    def list(self) -> list[Dataset]:
        self._session.flush()
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


class SQLAlchemyDatasetReviewRepository(
    _SQLAlchemyModelRepository[DatasetReview, DatasetReviewModel]
):
    def __init__(self, session: OrmSession) -> None:
        super().__init__(
            session,
            model_type=DatasetReviewModel,
            id_column=DatasetReviewModel.requested_at,
            entity_id_getter=lambda entity: entity.review_id,
            to_model=dataset_review_to_model,
            from_model=dataset_review_from_model,
            apply_to_model=apply_dataset_review_to_model,
        )


class SQLAlchemyNoteRepository(EntityRepository[Note]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def _children_map(
        self,
        note_ids: list[str],
    ) -> tuple[dict[str, list[Any]], dict[str, list[Any]], dict[str, list[Any]]]:
        extracted_map: dict[str, list[Any]] = defaultdict(list)
        suggestion_map: dict[str, list[Any]] = defaultdict(list)
        target_map: dict[str, list[Any]] = defaultdict(list)
        if not note_ids:
            return extracted_map, suggestion_map, target_map
        extracted_rows = list(
            self._session.scalars(
                select(NoteExtractedEntityModel).where(NoteExtractedEntityModel.note_id.in_(note_ids))
            )
        )
        suggestion_rows = list(
            self._session.scalars(
                select(NoteTagSuggestionModel).where(NoteTagSuggestionModel.note_id.in_(note_ids))
            )
        )
        target_rows = list(
            self._session.scalars(select(NoteTargetModel).where(NoteTargetModel.note_id.in_(note_ids)))
        )
        for row in extracted_rows:
            extracted_map[row.note_id].append(row)
        for row in suggestion_rows:
            suggestion_map[row.note_id].append(row)
        for row in target_rows:
            target_map[row.note_id].append(row)
        return extracted_map, suggestion_map, target_map

    def _notes_from_rows(self, rows: list[NoteModel]) -> list[Note]:
        note_ids = [row.note_id for row in rows]
        extracted_map, suggestion_map, target_map = self._children_map(note_ids)
        return [
            note_from_model(
                row,
                extracted_entities=[
                    extracted_entity_from_model(item)
                    for item in extracted_map.get(row.note_id, [])
                ],
                tag_suggestions=[
                    tag_suggestion_from_model(item)
                    for item in suggestion_map.get(row.note_id, [])
                ],
                targets=[entity_ref_from_model(item) for item in target_map.get(row.note_id, [])],
            )
            for row in rows
        ]

    def get(self, entity_id: UUID) -> Note | None:
        self._session.flush()
        row = self._session.get(NoteModel, str(entity_id))
        if row is None:
            return None
        return self._notes_from_rows([row])[0]

    def list(self) -> list[Note]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(NoteModel).order_by(NoteModel.created_at, NoteModel.note_id)
            )
        )
        return self._notes_from_rows(rows)

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


class SQLAlchemyAcquisitionOutputRepository(
    _SQLAlchemyModelRepository[AcquisitionOutput, AcquisitionOutputModel]
):
    def __init__(self, session: OrmSession) -> None:
        super().__init__(
            session,
            model_type=AcquisitionOutputModel,
            id_column=AcquisitionOutputModel.created_at,
            entity_id_getter=lambda entity: entity.output_id,
            to_model=acquisition_output_to_model,
            from_model=acquisition_output_from_model,
            apply_to_model=apply_acquisition_output_to_model,
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
        self._session.flush()
        row = self._session.get(AnalysisModel, str(entity_id))
        if row is None:
            return None
        dataset_ids = self._dataset_map([row.analysis_id]).get(row.analysis_id, [])
        return analysis_from_model(row, dataset_ids=dataset_ids)

    def list(self) -> list[Analysis]:
        self._session.flush()
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
        self._session.flush()
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
        self._session.flush()
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
        self._session.flush()
        row = self._session.get(VisualizationModel, str(entity_id))
        if row is None:
            return None
        claim_ids = self._claim_map([row.viz_id]).get(row.viz_id, [])
        return visualization_from_model(row, related_claim_ids=claim_ids)

    def list(self) -> list[Visualization]:
        self._session.flush()
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


class SQLAlchemyLabTrackerRepository(LabTrackerRepository):
    """Repository scaffold backed by a SQLAlchemy ORM session."""

    def __init__(self, session: OrmSession) -> None:
        self._session = session
        self.projects = SQLAlchemyProjectRepository(session)
        self.questions = SQLAlchemyQuestionRepository(session)
        self.datasets = SQLAlchemyDatasetRepository(session)
        self.dataset_reviews = SQLAlchemyDatasetReviewRepository(session)
        self.notes = SQLAlchemyNoteRepository(session)
        self.sessions = SQLAlchemySessionRepository(session)
        self.acquisition_outputs = SQLAlchemyAcquisitionOutputRepository(session)
        self.analyses = SQLAlchemyAnalysisRepository(session)
        self.claims = SQLAlchemyClaimRepository(session)
        self.visualizations = SQLAlchemyVisualizationRepository(session)

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()

    def _question_entities_from_rows(self, rows: list[QuestionModel]) -> list[Question]:
        question_ids = [row.question_id for row in rows]
        parent_map = self.questions._parent_map(question_ids)
        return [
            question_from_model(row, parent_question_ids=parent_map.get(row.question_id, []))
            for row in rows
        ]

    def _dataset_entities_from_rows(self, rows: list[DatasetModel]) -> list[Dataset]:
        dataset_ids = [row.dataset_id for row in rows]
        link_map = self.datasets._link_map(dataset_ids)
        return [
            dataset_from_model(
                row,
                question_links=[
                    dataset_question_link_from_model(link)
                    for link in link_map.get(row.dataset_id, [])
                ],
            )
            for row in rows
        ]

    def _analysis_entities_from_rows(self, rows: list[AnalysisModel]) -> list[Analysis]:
        analysis_ids = [row.analysis_id for row in rows]
        dataset_map = self.analyses._dataset_map(analysis_ids)
        return [
            analysis_from_model(row, dataset_ids=dataset_map.get(row.analysis_id, []))
            for row in rows
        ]

    def _claim_entities_from_rows(self, rows: list[ClaimModel]) -> list[Claim]:
        claim_ids = [row.claim_id for row in rows]
        dataset_map = self.claims._dataset_map(claim_ids)
        analysis_map = self.claims._analysis_map(claim_ids)
        return [
            claim_from_model(
                row,
                supported_by_dataset_ids=dataset_map.get(row.claim_id, []),
                supported_by_analysis_ids=analysis_map.get(row.claim_id, []),
            )
            for row in rows
        ]

    def _visualization_entities_from_rows(
        self,
        rows: list[VisualizationModel],
    ) -> list[Visualization]:
        viz_ids = [row.viz_id for row in rows]
        claim_map = self.visualizations._claim_map(viz_ids)
        return [
            visualization_from_model(
                row,
                related_claim_ids=claim_map.get(row.viz_id, []),
            )
            for row in rows
        ]

    def fetch_questions(self, question_ids: list[UUID]) -> list[Question]:
        self._session.flush()
        if not question_ids:
            return []
        rows = list(
            self._session.scalars(
                select(QuestionModel).where(
                    QuestionModel.question_id.in_(
                        [str(question_id) for question_id in question_ids]
                    )
                )
            )
        )
        by_id = {
            question.question_id: question
            for question in self._question_entities_from_rows(rows)
        }
        return [by_id[question_id] for question_id in question_ids if question_id in by_id]

    def fetch_notes(self, note_ids: list[UUID]) -> list[Note]:
        self._session.flush()
        if not note_ids:
            return []
        rows = list(
            self._session.scalars(
                select(NoteModel).where(
                    NoteModel.note_id.in_([str(note_id) for note_id in note_ids])
                )
            )
        )
        by_id = {note.note_id: note for note in self.notes._notes_from_rows(rows)}
        return [by_id[note_id] for note_id in note_ids if note_id in by_id]

    def query_projects(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Project], int]:
        self._session.flush()
        stmt = select(ProjectModel)
        count_stmt = select(ProjectModel.project_id)
        if status is not None:
            stmt = stmt.where(ProjectModel.status == status)
            count_stmt = count_stmt.where(ProjectModel.status == status)
        stmt = stmt.order_by(ProjectModel.created_at, ProjectModel.project_id)
        total = _count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(_apply_pagination(stmt, limit=limit, offset=offset)))
        return [project_from_model(row) for row in rows], total

    def _descendant_question_cte(self, ancestor_question_id: UUID):
        descendants = (
            select(QuestionParentModel.question_id.label("question_id"))
            .where(QuestionParentModel.parent_question_id == str(ancestor_question_id))
            .cte(name="descendant_questions", recursive=True)
        )
        descendants = descendants.union_all(
            select(QuestionParentModel.question_id).join(
                descendants,
                QuestionParentModel.parent_question_id == descendants.c.question_id,
            )
        )
        return descendants

    def query_questions(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        question_type: str | None = None,
        created_from: str | None = None,
        parent_question_id: UUID | None = None,
        ancestor_question_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Question], int]:
        self._session.flush()
        stmt = select(QuestionModel)
        count_stmt = select(QuestionModel.question_id)
        if project_id is not None:
            stmt = stmt.where(QuestionModel.project_id == str(project_id))
            count_stmt = count_stmt.where(QuestionModel.project_id == str(project_id))
        if status is not None:
            stmt = stmt.where(QuestionModel.status == status)
            count_stmt = count_stmt.where(QuestionModel.status == status)
        if question_type is not None:
            stmt = stmt.where(QuestionModel.question_type == question_type)
            count_stmt = count_stmt.where(QuestionModel.question_type == question_type)
        if created_from is not None:
            stmt = stmt.where(QuestionModel.created_from == created_from)
            count_stmt = count_stmt.where(QuestionModel.created_from == created_from)
        if parent_question_id is not None:
            stmt = stmt.join(
                QuestionParentModel,
                QuestionParentModel.question_id == QuestionModel.question_id,
            ).where(QuestionParentModel.parent_question_id == str(parent_question_id))
            count_stmt = count_stmt.join(
                QuestionParentModel,
                QuestionParentModel.question_id == QuestionModel.question_id,
            ).where(QuestionParentModel.parent_question_id == str(parent_question_id))
        if ancestor_question_id is not None:
            descendants = self._descendant_question_cte(ancestor_question_id)
            stmt = stmt.where(QuestionModel.question_id.in_(select(descendants.c.question_id)))
            count_stmt = count_stmt.where(
                QuestionModel.question_id.in_(select(descendants.c.question_id))
            )
        stmt = stmt.order_by(QuestionModel.created_at, QuestionModel.question_id)
        total = _count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(_apply_pagination(stmt, limit=limit, offset=offset)))
        return self._question_entities_from_rows(rows), total

    def query_datasets(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Dataset], int]:
        self._session.flush()
        stmt = select(DatasetModel)
        count_stmt = select(DatasetModel.dataset_id)
        if project_id is not None:
            stmt = stmt.where(DatasetModel.project_id == str(project_id))
            count_stmt = count_stmt.where(DatasetModel.project_id == str(project_id))
        if status is not None:
            stmt = stmt.where(DatasetModel.status == status)
            count_stmt = count_stmt.where(DatasetModel.status == status)
        stmt = stmt.order_by(DatasetModel.created_at, DatasetModel.dataset_id)
        total = _count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(_apply_pagination(stmt, limit=limit, offset=offset)))
        return self._dataset_entities_from_rows(rows), total

    def query_dataset_reviews(
        self,
        *,
        dataset_id: UUID | None = None,
        status: str | None = None,
        reviewer_user_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[DatasetReview], int]:
        self._session.flush()
        stmt = select(DatasetReviewModel)
        count_stmt = select(DatasetReviewModel.review_id)
        if dataset_id is not None:
            stmt = stmt.where(DatasetReviewModel.dataset_id == str(dataset_id))
            count_stmt = count_stmt.where(DatasetReviewModel.dataset_id == str(dataset_id))
        if status is not None:
            stmt = stmt.where(DatasetReviewModel.status == status)
            count_stmt = count_stmt.where(DatasetReviewModel.status == status)
        if reviewer_user_id is not None:
            stmt = stmt.where(DatasetReviewModel.reviewer_user_id == str(reviewer_user_id))
            count_stmt = count_stmt.where(
                DatasetReviewModel.reviewer_user_id == str(reviewer_user_id)
            )
        stmt = stmt.order_by(DatasetReviewModel.requested_at, DatasetReviewModel.review_id)
        total = _count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(_apply_pagination(stmt, limit=limit, offset=offset)))
        return [dataset_review_from_model(row) for row in rows], total

    def query_notes(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Note], int]:
        self._session.flush()
        stmt = select(NoteModel)
        count_stmt = select(NoteModel.note_id)
        if project_id is not None:
            stmt = stmt.where(NoteModel.project_id == str(project_id))
            count_stmt = count_stmt.where(NoteModel.project_id == str(project_id))
        if status is not None:
            stmt = stmt.where(NoteModel.status == status)
            count_stmt = count_stmt.where(NoteModel.status == status)
        if target_entity_type is not None and target_entity_id is not None:
            stmt = stmt.join(NoteTargetModel, NoteTargetModel.note_id == NoteModel.note_id).where(
                NoteTargetModel.entity_type == target_entity_type,
                NoteTargetModel.entity_id == str(target_entity_id),
            )
            count_stmt = count_stmt.join(
                NoteTargetModel,
                NoteTargetModel.note_id == NoteModel.note_id,
            ).where(
                NoteTargetModel.entity_type == target_entity_type,
                NoteTargetModel.entity_id == str(target_entity_id),
            )
        stmt = stmt.order_by(NoteModel.created_at, NoteModel.note_id)
        total = _count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(_apply_pagination(stmt, limit=limit, offset=offset)))
        return self.notes._notes_from_rows(rows), total

    def query_sessions(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        session_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Session], int]:
        self._session.flush()
        stmt = select(SessionModel)
        count_stmt = select(SessionModel.session_id)
        if project_id is not None:
            stmt = stmt.where(SessionModel.project_id == str(project_id))
            count_stmt = count_stmt.where(SessionModel.project_id == str(project_id))
        if status is not None:
            stmt = stmt.where(SessionModel.status == status)
            count_stmt = count_stmt.where(SessionModel.status == status)
        if session_type is not None:
            stmt = stmt.where(SessionModel.session_type == session_type)
            count_stmt = count_stmt.where(SessionModel.session_type == session_type)
        stmt = stmt.order_by(SessionModel.started_at, SessionModel.session_id)
        total = _count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(_apply_pagination(stmt, limit=limit, offset=offset)))
        return [session_from_model(row) for row in rows], total

    def query_acquisition_outputs(
        self,
        *,
        session_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[AcquisitionOutput], int]:
        self._session.flush()
        stmt = select(AcquisitionOutputModel)
        count_stmt = select(AcquisitionOutputModel.output_id)
        if session_id is not None:
            stmt = stmt.where(AcquisitionOutputModel.session_id == str(session_id))
            count_stmt = count_stmt.where(AcquisitionOutputModel.session_id == str(session_id))
        stmt = stmt.order_by(AcquisitionOutputModel.created_at, AcquisitionOutputModel.output_id)
        total = _count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(_apply_pagination(stmt, limit=limit, offset=offset)))
        return [acquisition_output_from_model(row) for row in rows], total

    def query_dataset_files(
        self,
        *,
        dataset_id: UUID,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[DatasetFile], int]:
        self._session.flush()
        stmt = select(DatasetFileModel).where(DatasetFileModel.dataset_id == str(dataset_id))
        count_stmt = select(DatasetFileModel.file_id).where(
            DatasetFileModel.dataset_id == str(dataset_id)
        )
        stmt = stmt.order_by(DatasetFileModel.created_at, DatasetFileModel.file_id)
        total = _count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(_apply_pagination(stmt, limit=limit, offset=offset)))
        return (
            [
                DatasetFile(
                    file_id=UUID(row.file_id),
                    path=row.path,
                    checksum=row.checksum,
                    size_bytes=row.size_bytes,
                )
                for row in rows
            ],
            total,
        )

    def query_analyses(
        self,
        *,
        project_id: UUID | None = None,
        dataset_id: UUID | None = None,
        question_id: UUID | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Analysis], int]:
        self._session.flush()
        stmt = select(AnalysisModel)
        count_stmt = select(AnalysisModel.analysis_id)
        distinct_required = False
        if project_id is not None:
            stmt = stmt.where(AnalysisModel.project_id == str(project_id))
            count_stmt = count_stmt.where(AnalysisModel.project_id == str(project_id))
        if dataset_id is not None:
            stmt = stmt.join(
                AnalysisDatasetModel,
                AnalysisDatasetModel.analysis_id == AnalysisModel.analysis_id,
            ).where(AnalysisDatasetModel.dataset_id == str(dataset_id))
            count_stmt = count_stmt.join(
                AnalysisDatasetModel,
                AnalysisDatasetModel.analysis_id == AnalysisModel.analysis_id,
            ).where(AnalysisDatasetModel.dataset_id == str(dataset_id))
        if question_id is not None:
            distinct_required = True
            stmt = stmt.join(
                AnalysisDatasetModel,
                AnalysisDatasetModel.analysis_id == AnalysisModel.analysis_id,
            ).join(
                DatasetQuestionLinkModel,
                DatasetQuestionLinkModel.dataset_id == AnalysisDatasetModel.dataset_id,
            ).where(DatasetQuestionLinkModel.question_id == str(question_id))
            count_stmt = count_stmt.join(
                AnalysisDatasetModel,
                AnalysisDatasetModel.analysis_id == AnalysisModel.analysis_id,
            ).join(
                DatasetQuestionLinkModel,
                DatasetQuestionLinkModel.dataset_id == AnalysisDatasetModel.dataset_id,
            ).where(DatasetQuestionLinkModel.question_id == str(question_id))
        if status is not None:
            stmt = stmt.where(AnalysisModel.status == status)
            count_stmt = count_stmt.where(AnalysisModel.status == status)
        if distinct_required:
            stmt = stmt.distinct()
            count_stmt = count_stmt.distinct()
        stmt = stmt.order_by(AnalysisModel.created_at, AnalysisModel.analysis_id)
        total = _count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(_apply_pagination(stmt, limit=limit, offset=offset)))
        return self._analysis_entities_from_rows(rows), total

    def query_claims(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        dataset_id: UUID | None = None,
        analysis_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Claim], int]:
        self._session.flush()
        stmt = select(ClaimModel)
        count_stmt = select(ClaimModel.claim_id)
        distinct_required = False
        if project_id is not None:
            stmt = stmt.where(ClaimModel.project_id == str(project_id))
            count_stmt = count_stmt.where(ClaimModel.project_id == str(project_id))
        if status is not None:
            stmt = stmt.where(ClaimModel.status == status)
            count_stmt = count_stmt.where(ClaimModel.status == status)
        if dataset_id is not None:
            distinct_required = True
            stmt = stmt.join(
                ClaimDatasetModel,
                ClaimDatasetModel.claim_id == ClaimModel.claim_id,
            ).where(ClaimDatasetModel.dataset_id == str(dataset_id))
            count_stmt = count_stmt.join(
                ClaimDatasetModel,
                ClaimDatasetModel.claim_id == ClaimModel.claim_id,
            ).where(ClaimDatasetModel.dataset_id == str(dataset_id))
        if analysis_id is not None:
            distinct_required = True
            stmt = stmt.join(
                ClaimAnalysisModel,
                ClaimAnalysisModel.claim_id == ClaimModel.claim_id,
            ).where(ClaimAnalysisModel.analysis_id == str(analysis_id))
            count_stmt = count_stmt.join(
                ClaimAnalysisModel,
                ClaimAnalysisModel.claim_id == ClaimModel.claim_id,
            ).where(ClaimAnalysisModel.analysis_id == str(analysis_id))
        if distinct_required:
            stmt = stmt.distinct()
            count_stmt = count_stmt.distinct()
        stmt = stmt.order_by(ClaimModel.created_at, ClaimModel.claim_id)
        total = _count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(_apply_pagination(stmt, limit=limit, offset=offset)))
        return self._claim_entities_from_rows(rows), total

    def query_visualizations(
        self,
        *,
        project_id: UUID | None = None,
        analysis_id: UUID | None = None,
        claim_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Visualization], int]:
        self._session.flush()
        stmt = select(VisualizationModel)
        count_stmt = select(VisualizationModel.viz_id)
        distinct_required = False
        if project_id is not None:
            stmt = stmt.join(
                AnalysisModel,
                AnalysisModel.analysis_id == VisualizationModel.analysis_id,
            ).where(AnalysisModel.project_id == str(project_id))
            count_stmt = count_stmt.join(
                AnalysisModel,
                AnalysisModel.analysis_id == VisualizationModel.analysis_id,
            ).where(AnalysisModel.project_id == str(project_id))
        if analysis_id is not None:
            stmt = stmt.where(VisualizationModel.analysis_id == str(analysis_id))
            count_stmt = count_stmt.where(VisualizationModel.analysis_id == str(analysis_id))
        if claim_id is not None:
            distinct_required = True
            stmt = stmt.join(
                VisualizationClaimModel,
                VisualizationClaimModel.viz_id == VisualizationModel.viz_id,
            ).where(VisualizationClaimModel.claim_id == str(claim_id))
            count_stmt = count_stmt.join(
                VisualizationClaimModel,
                VisualizationClaimModel.viz_id == VisualizationModel.viz_id,
            ).where(VisualizationClaimModel.claim_id == str(claim_id))
        if distinct_required:
            stmt = stmt.distinct()
            count_stmt = count_stmt.distinct()
        stmt = stmt.order_by(VisualizationModel.created_at, VisualizationModel.viz_id)
        total = _count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(_apply_pagination(stmt, limit=limit, offset=offset)))
        return self._visualization_entities_from_rows(rows), total
