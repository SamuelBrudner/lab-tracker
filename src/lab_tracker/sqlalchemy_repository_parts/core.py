"""Project and question SQLAlchemy repositories."""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    GroupMembershipModel,
    ProjectGroupModel,
    ProjectMembershipModel,
    ProjectModel,
    QuestionModel,
    QuestionParentModel,
    QuestionRefactorModel,
    UserModel,
)
from lab_tracker.models import (
    GroupMembership,
    Project,
    ProjectGroup,
    ProjectMembership,
    ProjectMembershipRole,
    Question,
    QuestionRefactor,
)
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mapper_parts.common import as_utc, uuid_from_db, uuid_to_db
from lab_tracker.sqlalchemy_mapper_parts.projects import (
    apply_project_group_to_model,
    apply_project_to_model,
    project_from_model,
    project_group_from_model,
    project_group_to_model,
    project_to_model,
)
from lab_tracker.sqlalchemy_mappers import (
    apply_project_membership_to_model,
    apply_question_refactor_to_model,
    apply_question_to_model,
    project_membership_to_model,
    question_from_model,
    question_parent_models,
    question_refactor_from_model,
    question_refactor_to_model,
    question_to_model,
)

from .common import (
    SQLAlchemyModelRepository,
    apply_pagination,
    count_from_statement,
    replace_child_rows,
    substring_pattern,
    uuid_values,
)


class SQLAlchemyProjectRepository(SQLAlchemyModelRepository[Project, ProjectModel]):
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

    def query(
        self,
        *,
        group_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Project], int]:
        self._session.flush()
        if project_ids is not None and not project_ids:
            return [], 0
        stmt = select(ProjectModel)
        count_stmt = select(ProjectModel.project_id)
        if group_id is not None:
            stmt = stmt.where(ProjectModel.group_id == str(group_id))
            count_stmt = count_stmt.where(ProjectModel.group_id == str(group_id))
        if project_ids is not None:
            project_values = uuid_values(project_ids)
            stmt = stmt.where(ProjectModel.project_id.in_(project_values))
            count_stmt = count_stmt.where(ProjectModel.project_id.in_(project_values))
        if status is not None:
            stmt = stmt.where(ProjectModel.status == status)
            count_stmt = count_stmt.where(ProjectModel.status == status)
        stmt = stmt.order_by(ProjectModel.created_at, ProjectModel.project_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [project_from_model(row) for row in rows], total


class SQLAlchemyProjectGroupRepository(
    SQLAlchemyModelRepository[ProjectGroup, ProjectGroupModel]
):
    def __init__(self, session: OrmSession) -> None:
        super().__init__(
            session,
            model_type=ProjectGroupModel,
            id_column=ProjectGroupModel.group_id,
            entity_id_getter=lambda entity: entity.group_id,
            to_model=project_group_to_model,
            from_model=project_group_from_model,
            apply_to_model=apply_project_group_to_model,
        )

    def query(
        self,
        *,
        kind: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[ProjectGroup], int]:
        self._session.flush()
        stmt = select(ProjectGroupModel)
        count_stmt = select(ProjectGroupModel.group_id)
        if kind is not None:
            stmt = stmt.where(ProjectGroupModel.kind == kind)
            count_stmt = count_stmt.where(ProjectGroupModel.kind == kind)
        stmt = stmt.order_by(ProjectGroupModel.created_at, ProjectGroupModel.group_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [project_group_from_model(row) for row in rows], total


class SQLAlchemyProjectMembershipRepository(EntityRepository[ProjectMembership]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def _from_row(
        self,
        row: ProjectMembershipModel,
        user: UserModel | None = None,
    ) -> ProjectMembership:
        return ProjectMembership(
            membership_id=UUID(row.membership_id),
            project_id=UUID(row.project_id),
            user_id=UUID(row.user_id),
            role=ProjectMembershipRole(row.role),
            username=user.username if user is not None else None,
            user_global_role=user.role if user is not None else None,
            created_by=row.created_by,
            created_by_user_id=(
                uuid_from_db(row.created_by_user_id)
                if row.created_by_user_id is not None
                else None
            ),
            created_at=as_utc(row.created_at),
            updated_at=as_utc(row.updated_at),
        )

    def get(self, entity_id: UUID) -> ProjectMembership | None:
        self._session.flush()
        row = self._session.get(ProjectMembershipModel, str(entity_id))
        if row is None:
            return None
        user = self._session.get(UserModel, row.user_id)
        return self._from_row(row, user)

    def list(self) -> list[ProjectMembership]:
        self._session.flush()
        rows = list(
            self._session.execute(
                select(ProjectMembershipModel, UserModel)
                .join(UserModel, UserModel.user_id == ProjectMembershipModel.user_id)
                .order_by(ProjectMembershipModel.project_id, UserModel.username)
            )
        )
        return [self._from_row(row, user) for row, user in rows]

    def save(self, entity: ProjectMembership) -> None:
        entity_id = str(entity.membership_id)
        row = self._session.get(ProjectMembershipModel, entity_id)
        if row is None:
            self._session.add(project_membership_to_model(entity))
            return
        apply_project_membership_to_model(row, entity)

    def delete(self, entity_id: UUID) -> ProjectMembership | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(ProjectMembershipModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def query(
        self,
        *,
        project_id: UUID | None = None,
        user_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[ProjectMembership], int]:
        self._session.flush()
        stmt = select(ProjectMembershipModel, UserModel).join(
            UserModel,
            UserModel.user_id == ProjectMembershipModel.user_id,
        )
        count_stmt = select(ProjectMembershipModel.membership_id)
        if project_id is not None:
            stmt = stmt.where(ProjectMembershipModel.project_id == str(project_id))
            count_stmt = count_stmt.where(ProjectMembershipModel.project_id == str(project_id))
        if user_id is not None:
            stmt = stmt.where(ProjectMembershipModel.user_id == str(user_id))
            count_stmt = count_stmt.where(ProjectMembershipModel.user_id == str(user_id))
        stmt = stmt.order_by(ProjectMembershipModel.project_id, UserModel.username)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.execute(apply_pagination(stmt, limit=limit, offset=offset)))
        return [self._from_row(row, user) for row, user in rows], total

    def get_by_project_user(
        self,
        *,
        project_id: UUID,
        user_id: UUID,
    ) -> ProjectMembership | None:
        self._session.flush()
        result = self._session.execute(
            select(ProjectMembershipModel, UserModel)
            .join(UserModel, UserModel.user_id == ProjectMembershipModel.user_id)
            .where(
                ProjectMembershipModel.project_id == str(project_id),
                ProjectMembershipModel.user_id == str(user_id),
            )
        ).first()
        if result is None:
            return None
        row, user = result
        return self._from_row(row, user)


class SQLAlchemyGroupMembershipRepository(EntityRepository[GroupMembership]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def _from_row(
        self,
        row: GroupMembershipModel,
        user: UserModel | None = None,
    ) -> GroupMembership:
        return GroupMembership(
            membership_id=uuid_from_db(row.membership_id),
            group_id=uuid_from_db(row.group_id),
            user_id=uuid_from_db(row.user_id),
            role=ProjectMembershipRole(row.role),
            username=user.username if user is not None else None,
            user_global_role=user.role if user is not None else None,
            created_by=row.created_by,
            created_by_user_id=(
                uuid_from_db(row.created_by_user_id)
                if row.created_by_user_id is not None
                else None
            ),
            created_at=as_utc(row.created_at),
            updated_at=as_utc(row.updated_at),
        )

    def _to_model(self, membership: GroupMembership) -> GroupMembershipModel:
        return GroupMembershipModel(
            membership_id=uuid_to_db(membership.membership_id),
            group_id=uuid_to_db(membership.group_id),
            user_id=uuid_to_db(membership.user_id),
            role=membership.role.value,
            created_by=membership.created_by,
            created_by_user_id=(
                uuid_to_db(membership.created_by_user_id)
                if membership.created_by_user_id is not None
                else None
            ),
            created_at=membership.created_at,
            updated_at=membership.updated_at,
        )

    def _apply_to_model(self, row: GroupMembershipModel, membership: GroupMembership) -> None:
        row.group_id = uuid_to_db(membership.group_id)
        row.user_id = uuid_to_db(membership.user_id)
        row.role = membership.role.value
        row.created_by = membership.created_by
        row.created_by_user_id = (
            uuid_to_db(membership.created_by_user_id)
            if membership.created_by_user_id is not None
            else None
        )
        row.created_at = membership.created_at
        row.updated_at = membership.updated_at

    def get(self, entity_id: UUID) -> GroupMembership | None:
        self._session.flush()
        row = self._session.get(GroupMembershipModel, str(entity_id))
        if row is None:
            return None
        user = self._session.get(UserModel, row.user_id)
        return self._from_row(row, user)

    def list(self) -> list[GroupMembership]:
        self._session.flush()
        rows = list(
            self._session.execute(
                select(GroupMembershipModel, UserModel)
                .join(UserModel, UserModel.user_id == GroupMembershipModel.user_id)
                .order_by(GroupMembershipModel.group_id, UserModel.username)
            )
        )
        return [self._from_row(row, user) for row, user in rows]

    def save(self, entity: GroupMembership) -> None:
        entity_id = str(entity.membership_id)
        row = self._session.get(GroupMembershipModel, entity_id)
        if row is None:
            self._session.add(self._to_model(entity))
            return
        self._apply_to_model(row, entity)

    def delete(self, entity_id: UUID) -> GroupMembership | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(GroupMembershipModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def query(
        self,
        *,
        group_id: UUID | None = None,
        user_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[GroupMembership], int]:
        self._session.flush()
        stmt = select(GroupMembershipModel, UserModel).join(
            UserModel,
            UserModel.user_id == GroupMembershipModel.user_id,
        )
        count_stmt = select(GroupMembershipModel.membership_id)
        if group_id is not None:
            stmt = stmt.where(GroupMembershipModel.group_id == str(group_id))
            count_stmt = count_stmt.where(GroupMembershipModel.group_id == str(group_id))
        if user_id is not None:
            stmt = stmt.where(GroupMembershipModel.user_id == str(user_id))
            count_stmt = count_stmt.where(GroupMembershipModel.user_id == str(user_id))
        stmt = stmt.order_by(GroupMembershipModel.group_id, UserModel.username)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.execute(apply_pagination(stmt, limit=limit, offset=offset)))
        return [self._from_row(row, user) for row, user in rows], total

    def get_by_group_user(
        self,
        *,
        group_id: UUID,
        user_id: UUID,
    ) -> GroupMembership | None:
        self._session.flush()
        result = self._session.execute(
            select(GroupMembershipModel, UserModel)
            .join(UserModel, UserModel.user_id == GroupMembershipModel.user_id)
            .where(
                GroupMembershipModel.group_id == str(group_id),
                GroupMembershipModel.user_id == str(user_id),
            )
        ).first()
        if result is None:
            return None
        row, user = result
        return self._from_row(row, user)


class SQLAlchemyQuestionRepository(EntityRepository[Question]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def parent_map(self, question_ids: list[str]) -> dict[str, list[UUID]]:
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
        return self.questions_from_rows([row])[0]

    def list(self) -> list[Question]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(QuestionModel).order_by(QuestionModel.created_at, QuestionModel.question_id)
            )
        )
        return self.questions_from_rows(rows)

    def questions_from_rows(self, rows: list[QuestionModel]) -> list[Question]:
        question_ids = [row.question_id for row in rows]
        parent_map = self.parent_map(question_ids)
        return [
            question_from_model(row, parent_question_ids=parent_map.get(row.question_id, []))
            for row in rows
        ]

    def descendant_question_cte(self, ancestor_question_id: UUID):
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

    def query(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        question_type: str | None = None,
        search: str | None = None,
        created_by: str | None = None,
        parent_question_id: UUID | None = None,
        ancestor_question_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Question], int]:
        self._session.flush()
        if project_ids is not None and not project_ids:
            return [], 0
        stmt = select(QuestionModel)
        count_stmt = select(QuestionModel.question_id)
        if project_id is not None:
            stmt = stmt.where(QuestionModel.project_id == str(project_id))
            count_stmt = count_stmt.where(QuestionModel.project_id == str(project_id))
        if project_ids is not None:
            project_values = uuid_values(project_ids)
            stmt = stmt.where(QuestionModel.project_id.in_(project_values))
            count_stmt = count_stmt.where(QuestionModel.project_id.in_(project_values))
        if status is not None:
            stmt = stmt.where(QuestionModel.status == status)
            count_stmt = count_stmt.where(QuestionModel.status == status)
        if question_type is not None:
            stmt = stmt.where(QuestionModel.question_type == question_type)
            count_stmt = count_stmt.where(QuestionModel.question_type == question_type)
        if created_by is not None:
            stmt = stmt.where(QuestionModel.created_by_user_id == created_by)
            count_stmt = count_stmt.where(QuestionModel.created_by_user_id == created_by)
        pattern = substring_pattern(search)
        if pattern is not None:
            search_clause = or_(
                QuestionModel.text.ilike(pattern, escape="\\"),
                QuestionModel.hypothesis.ilike(pattern, escape="\\"),
            )
            stmt = stmt.where(search_clause)
            count_stmt = count_stmt.where(search_clause)
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
            descendants = self.descendant_question_cte(ancestor_question_id)
            stmt = stmt.where(QuestionModel.question_id.in_(select(descendants.c.question_id)))
            count_stmt = count_stmt.where(
                QuestionModel.question_id.in_(select(descendants.c.question_id))
            )
        stmt = stmt.order_by(QuestionModel.created_at, QuestionModel.question_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self.questions_from_rows(rows), total

    def save(self, entity: Question) -> None:
        entity_id = str(entity.question_id)
        row = self._session.get(QuestionModel, entity_id)
        if row is None:
            self._session.add(question_to_model(entity))
        else:
            apply_question_to_model(row, entity)
        self._session.flush()
        replace_child_rows(
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


class SQLAlchemyQuestionRefactorRepository(
    SQLAlchemyModelRepository[QuestionRefactor, QuestionRefactorModel]
):
    def __init__(self, session: OrmSession) -> None:
        super().__init__(
            session,
            model_type=QuestionRefactorModel,
            id_column=QuestionRefactorModel.refactor_id,
            entity_id_getter=lambda entity: entity.refactor_id,
            to_model=question_refactor_to_model,
            from_model=question_refactor_from_model,
            apply_to_model=apply_question_refactor_to_model,
        )

    def query(
        self,
        *,
        question_id: UUID,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[QuestionRefactor], int]:
        self._session.flush()
        stmt = select(QuestionRefactorModel).where(
            or_(
                QuestionRefactorModel.source_question_id == str(question_id),
                QuestionRefactorModel.replacement_question_id == str(question_id),
            )
        )
        count_stmt = select(QuestionRefactorModel.refactor_id).where(
            or_(
                QuestionRefactorModel.source_question_id == str(question_id),
                QuestionRefactorModel.replacement_question_id == str(question_id),
            )
        )
        stmt = stmt.order_by(
            QuestionRefactorModel.created_at,
            QuestionRefactorModel.refactor_id,
        )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [question_refactor_from_model(row) for row in rows], total
