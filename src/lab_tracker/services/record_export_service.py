"""Per-person and group-scoped record export service."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Protocol
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, Role, require_role
from lab_tracker.errors import NotFoundError, OpaqueTargetNotFoundError, ValidationError
from lab_tracker.models import (
    Analysis,
    Claim,
    Dataset,
    EntityRef,
    EntityType,
    ExplorationNode,
    Goal,
    Note,
    ProvenanceLink,
    ProvenanceLinkStatus,
    Question,
    RecordExport,
    RecordExportEvent,
    RecordExportRecords,
    Visualization,
    utc_now,
)
from lab_tracker.provenance import (
    ARA_LAYER_NAMES,
    AraArtifactRecords,
    build_ara_artifact_document,
    build_record_export_provenance_document,
)
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.shared import actor_user_fk, actor_user_id

ADMIN_ROLES = {Role.ADMIN}


class GoalReadAccess(Protocol):
    """Opaque goal reads needed by Ara artifact exports."""

    def get_goal(self, goal_id: UUID) -> Goal: ...

    def require_goal_read(
        self,
        goal: Goal,
        *,
        actor: AuthContext | None,
    ) -> set[UUID]: ...


class QuestionReadAccess(Protocol):
    """Opaque question reads needed by Ara artifact exports."""

    def get_question_for_read(
        self,
        question_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Question: ...


class RecordExportService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        goals: GoalReadAccess,
        questions: QuestionReadAccess,
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.goals = goals
        self.questions = questions
        self.authorization = authorization

    def export_user_records(
        self,
        *,
        user_id: UUID,
        base_url: str,
        group_id: UUID | None = None,
        actor: AuthContext | None = None,
    ) -> RecordExport:
        project_ids = self._authorized_project_scope(group_id=group_id, actor=actor)
        repository = self.repository
        if not repository.user_exists(user_id):
            raise NotFoundError("User does not exist.")
        records = self._collect_records(user_id=user_id, project_ids=project_ids)
        exported_project_ids = self._project_ids_for_records(records, fallback=project_ids)
        supervision_edges, _ = repository.query_supervision_edges(limit=None, offset=0)
        provenance = build_record_export_provenance_document(
            base_url,
            records,
            supervision_edges=supervision_edges,
        )
        generated_at = utc_now()
        event = RecordExportEvent(
            export_id=uuid4(),
            user_id=user_id,
            group_id=group_id,
            project_ids=exported_project_ids,
            record_counts=self._record_counts(records),
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, repository),
            created_at=generated_at,
        )
        with self.unit_of_work() as unit_repository:
            unit_repository.record_export_events.save(event)
        return RecordExport(
            export_event_id=event.export_id,
            user_id=user_id,
            group_id=group_id,
            project_ids=exported_project_ids,
            generated_at=generated_at,
            records=records,
            provenance=provenance,
        )

    def export_goal_artifact(
        self,
        *,
        goal_id: UUID,
        base_url: str,
        layer_name: str | None = None,
        actor: AuthContext | None = None,
    ) -> dict[str, object]:
        self._validate_layer_name(layer_name)
        try:
            goal = self.goals.get_goal(goal_id)
        except NotFoundError as exc:
            raise OpaqueTargetNotFoundError("Goal does not exist.") from exc
        scope_project_ids = self.goals.require_goal_read(goal, actor=actor)
        try:
            records = self._collect_goal_artifact_records(goal, scope_project_ids)
        except NotFoundError as exc:
            # Under READ COMMITTED, a linked target can disappear after scope
            # authorization but before artifact assembly re-reads it.
            raise NotFoundError("Goal does not exist.") from exc
        supervision_edges, _ = self.repository.query_supervision_edges(limit=None, offset=0)
        return build_ara_artifact_document(
            base_url,
            scope_type=EntityType.GOAL,
            scope_id=goal.goal_id,
            records=records,
            generated_at=utc_now(),
            layer_name=layer_name,
            supervision_edges=supervision_edges,
        )

    def export_question_subtree(
        self,
        *,
        root_id: UUID,
        base_url: str,
        layer_name: str | None = None,
        actor: AuthContext | None = None,
    ) -> dict[str, object]:
        self._validate_layer_name(layer_name)
        root = self.questions.get_question_for_read(root_id, actor=actor)
        records = self._collect_question_subtree_records(root)
        supervision_edges, _ = self.repository.query_supervision_edges(limit=None, offset=0)
        return build_ara_artifact_document(
            base_url,
            scope_type=EntityType.QUESTION,
            scope_id=root.question_id,
            records=records,
            generated_at=utc_now(),
            layer_name=layer_name,
            supervision_edges=supervision_edges,
        )

    def _validate_layer_name(self, layer_name: str | None) -> None:
        if layer_name is None:
            return
        if layer_name not in ARA_LAYER_NAMES:
            raise ValidationError(
                f"Unknown Ara layer {layer_name!r}; expected one of {', '.join(ARA_LAYER_NAMES)}."
            )

    def _collect_goal_artifact_records(
        self,
        goal: Goal,
        scope_project_ids: set[UUID],
    ) -> AraArtifactRecords:
        questions: dict[UUID, Question] = {}
        datasets: dict[UUID, Dataset] = {}
        analyses: dict[UUID, Analysis] = {}
        claims: dict[UUID, Claim] = {}
        exploration_nodes: dict[UUID, ExplorationNode] = {}
        notes: dict[UUID, Note] = {}
        visualizations: dict[UUID, Visualization] = {}
        project_full_exports: set[UUID] = set()

        for link in goal.links:
            target = link.target
            if target.entity_type == EntityType.PROJECT:
                project_full_exports.add(target.entity_id)
            elif target.entity_type == EntityType.QUESTION:
                self._add_question_subtree(target.entity_id, questions)
            elif target.entity_type == EntityType.DATASET:
                datasets[target.entity_id] = self._get_dataset(target.entity_id)
            elif target.entity_type == EntityType.ANALYSIS:
                analyses[target.entity_id] = self._get_analysis(target.entity_id)
            elif target.entity_type == EntityType.CLAIM:
                claims[target.entity_id] = self._get_claim(target.entity_id)
            elif target.entity_type == EntityType.NOTE:
                notes[target.entity_id] = self._get_note(target.entity_id)
            elif target.entity_type == EntityType.VISUALIZATION:
                visualizations[target.entity_id] = self._get_visualization(target.entity_id)

        if not goal.links and goal.project_id is not None:
            project_full_exports.add(goal.project_id)

        for project_id in project_full_exports:
            project_records = self._collect_project_records(project_id)
            questions.update({item.question_id: item for item in project_records.questions})
            datasets.update({item.dataset_id: item for item in project_records.datasets})
            analyses.update({item.analysis_id: item for item in project_records.analyses})
            claims.update({item.claim_id: item for item in project_records.claims})
            exploration_nodes.update(
                {item.node_id: item for item in project_records.exploration_nodes}
            )
            notes.update({item.note_id: item for item in project_records.notes})
            visualizations.update({item.viz_id: item for item in project_records.visualizations})

        records = self._close_artifact_scope(
            project_ids=scope_project_ids,
            questions=questions,
            datasets=datasets,
            analyses=analyses,
            claims=claims,
            exploration_nodes=exploration_nodes,
            notes=notes,
            visualizations=visualizations,
        )
        return replace(records, goal=goal, goal_links=list(goal.links))

    def _collect_question_subtree_records(self, root: Question) -> AraArtifactRecords:
        questions: dict[UUID, Question] = {root.question_id: root}
        self._add_question_subtree(root.question_id, questions)
        project_records = self._collect_project_records(root.project_id)
        subtree_question_ids = set(questions)
        datasets = {
            dataset.dataset_id: dataset
            for dataset in project_records.datasets
            if any(link.question_id in subtree_question_ids for link in dataset.question_links)
        }
        analyses = {
            analysis.analysis_id: analysis
            for analysis in project_records.analyses
            if any(dataset_id in datasets for dataset_id in analysis.dataset_ids)
        }
        claims = {
            claim.claim_id: claim
            for claim in project_records.claims
            if (
                any(
                    question_id in subtree_question_ids
                    for question_id in claim.answers_question_ids
                )
                or any(dataset_id in datasets for dataset_id in claim.supported_by_dataset_ids)
                or any(analysis_id in analyses for analysis_id in claim.supported_by_analysis_ids)
            )
        }
        visualizations = {
            visualization.viz_id: visualization
            for visualization in project_records.visualizations
            if (
                visualization.analysis_id in analyses
                or any(claim_id in claims for claim_id in visualization.related_claim_ids)
            )
        }
        notes = {
            note.note_id: note
            for note in project_records.notes
            if self._note_targets_any(
                note,
                {
                    EntityType.QUESTION: subtree_question_ids,
                    EntityType.DATASET: set(datasets),
                    EntityType.ANALYSIS: set(analyses),
                    EntityType.CLAIM: set(claims),
                    EntityType.VISUALIZATION: set(visualizations),
                },
            )
        }
        return self._close_artifact_scope(
            project_ids={root.project_id},
            questions=questions,
            datasets=datasets,
            analyses=analyses,
            claims=claims,
            exploration_nodes={},
            notes=notes,
            visualizations=visualizations,
        )

    def _close_artifact_scope(
        self,
        *,
        project_ids: set[UUID],
        questions: dict[UUID, Question],
        datasets: dict[UUID, Dataset],
        analyses: dict[UUID, Analysis],
        claims: dict[UUID, Claim],
        exploration_nodes: dict[UUID, ExplorationNode],
        notes: dict[UUID, Note],
        visualizations: dict[UUID, Visualization],
    ) -> AraArtifactRecords:
        changed = True
        while changed:
            changed = False
            for analysis in list(analyses.values()):
                for dataset_id in analysis.dataset_ids:
                    changed |= self._put_if_missing(datasets, dataset_id, self._get_dataset)
            for claim in list(claims.values()):
                for dataset_id in claim.supported_by_dataset_ids:
                    changed |= self._put_if_missing(datasets, dataset_id, self._get_dataset)
                for analysis_id in claim.supported_by_analysis_ids:
                    changed |= self._put_if_missing(analyses, analysis_id, self._get_analysis)
                for question_id in claim.answers_question_ids:
                    changed |= self._put_if_missing(questions, question_id, self._get_question)
            for visualization in list(visualizations.values()):
                changed |= self._put_if_missing(
                    analyses,
                    visualization.analysis_id,
                    self._get_analysis,
                )
                for claim_id in visualization.related_claim_ids:
                    changed |= self._put_if_missing(claims, claim_id, self._get_claim)
            for exploration_node in list(exploration_nodes.values()):
                changed |= self._put_entity_ref_if_missing(
                    exploration_node.target,
                    questions=questions,
                    datasets=datasets,
                    analyses=analyses,
                    claims=claims,
                    notes=notes,
                    visualizations=visualizations,
                )
                for evidence_ref in exploration_node.evidence_refs:
                    changed |= self._put_entity_ref_if_missing(
                        evidence_ref,
                        questions=questions,
                        datasets=datasets,
                        analyses=analyses,
                        claims=claims,
                        notes=notes,
                        visualizations=visualizations,
                    )
                if exploration_node.invalidates_claim_id is not None:
                    changed |= self._put_if_missing(
                        claims,
                        exploration_node.invalidates_claim_id,
                        self._get_claim,
                    )
                if exploration_node.invalidates_node_id is not None:
                    changed |= self._put_if_missing(
                        exploration_nodes,
                        exploration_node.invalidates_node_id,
                        self._get_exploration_node,
                    )
                for related_node_id in [
                    *exploration_node.parent_node_ids,
                    *exploration_node.also_depends_on_node_ids,
                ]:
                    changed |= self._put_if_missing(
                        exploration_nodes,
                        related_node_id,
                        self._get_exploration_node,
                    )
            for dataset in list(datasets.values()):
                for link in dataset.question_links:
                    changed |= self._put_if_missing(questions, link.question_id, self._get_question)
                for note_id in dataset.commit_manifest.note_ids:
                    changed |= self._put_if_missing(notes, note_id, self._get_note)
            for related_node in self._exploration_nodes_for_records(
                project_ids=project_ids,
                questions=questions,
                datasets=datasets,
                analyses=analyses,
                claims=claims,
                notes=notes,
                visualizations=visualizations,
                exploration_nodes=exploration_nodes,
            ):
                changed |= self._put_if_missing(
                    exploration_nodes,
                    related_node.node_id,
                    lambda _node_id, item=related_node: item,
                )

        if project_ids:
            self._drop_out_of_scope(
                project_ids,
                questions,
                datasets,
                analyses,
                claims,
                exploration_nodes,
                notes,
            )
        project_visualizations = self._visualizations_for_records(project_ids, analyses, claims)
        visualizations.update({item.viz_id: item for item in project_visualizations})
        notes.update(
            {
                note.note_id: note
                for note in self._notes_for_records(
                    project_ids=project_ids,
                    questions=questions,
                    datasets=datasets,
                    analyses=analyses,
                    claims=claims,
                    visualizations=visualizations,
                    notes=notes,
                )
            }
        )
        claim_edges = self._claim_edges_for_claims(project_ids, claims)
        entity_versions = self._entity_versions_for_records(questions, claims)
        return AraArtifactRecords(
            questions=self._sorted(questions.values(), "created_at", "question_id"),
            datasets=self._sorted(datasets.values(), "created_at", "dataset_id"),
            analyses=self._sorted(analyses.values(), "executed_at", "analysis_id"),
            claims=self._sorted(claims.values(), "created_at", "claim_id"),
            claim_edges=self._sorted(claim_edges, "created_at", "edge_id"),
            notes=self._sorted(notes.values(), "created_at", "note_id"),
            visualizations=self._sorted(visualizations.values(), "created_at", "viz_id"),
            entity_versions=self._sorted(entity_versions, "created_at", "version_id"),
            exploration_nodes=self._sorted(
                exploration_nodes.values(),
                "created_at",
                "node_id",
            ),
            provenance_links=self._accepted_provenance_links(project_ids=project_ids),
        )

    def _collect_project_records(self, project_id: UUID) -> AraArtifactRecords:
        questions, _ = self.repository.query_questions(
            project_id=project_id,
            limit=None,
            offset=0,
        )
        datasets, _ = self.repository.query_datasets(
            project_id=project_id,
            limit=None,
            offset=0,
        )
        analyses, _ = self.repository.query_analyses(
            project_id=project_id,
            limit=None,
            offset=0,
        )
        claims, _ = self.repository.query_claims(
            project_id=project_id,
            limit=None,
            offset=0,
        )
        notes, _ = self.repository.query_notes(
            project_id=project_id,
            limit=None,
            offset=0,
        )
        visualizations, _ = self.repository.query_visualizations(
            project_id=project_id,
            limit=None,
            offset=0,
        )
        claim_edges, _ = self.repository.query_claim_edges(
            project_id=project_id,
            limit=None,
            offset=0,
        )
        exploration_nodes, _ = self.repository.query_exploration_nodes(
            project_id=project_id,
            limit=None,
            offset=0,
        )
        return AraArtifactRecords(
            questions=questions,
            datasets=datasets,
            analyses=analyses,
            claims=claims,
            claim_edges=claim_edges,
            notes=notes,
            visualizations=visualizations,
            exploration_nodes=exploration_nodes,
            entity_versions=self._entity_versions_for_records(
                {question.question_id: question for question in questions},
                {claim.claim_id: claim for claim in claims},
            ),
            provenance_links=self._accepted_provenance_links(project_id=project_id),
        )

    def _accepted_provenance_links(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
    ) -> list[ProvenanceLink]:
        links, _ = self.repository.query_provenance_links(
            project_id=project_id,
            project_ids=project_ids,
            status=ProvenanceLinkStatus.ACCEPTED.value,
            limit=None,
            offset=0,
        )
        return links

    def _add_question_subtree(
        self,
        question_id: UUID,
        questions: dict[UUID, Question],
    ) -> None:
        questions[question_id] = self._get_question(question_id)
        descendants, _ = self.repository.query_questions(
            ancestor_question_id=question_id,
            limit=None,
            offset=0,
        )
        questions.update({question.question_id: question for question in descendants})

    def _put_if_missing(
        self,
        target: dict[UUID, object],
        entity_id: UUID,
        loader,
    ) -> bool:
        if entity_id in target:
            return False
        target[entity_id] = loader(entity_id)
        return True

    def _put_entity_ref_if_missing(
        self,
        ref: EntityRef,
        *,
        questions: dict[UUID, Question],
        datasets: dict[UUID, Dataset],
        analyses: dict[UUID, Analysis],
        claims: dict[UUID, Claim],
        notes: dict[UUID, Note],
        visualizations: dict[UUID, Visualization],
    ) -> bool:
        if ref.entity_type == EntityType.QUESTION:
            return self._put_if_missing(questions, ref.entity_id, self._get_question)
        if ref.entity_type == EntityType.DATASET:
            return self._put_if_missing(datasets, ref.entity_id, self._get_dataset)
        if ref.entity_type == EntityType.ANALYSIS:
            return self._put_if_missing(analyses, ref.entity_id, self._get_analysis)
        if ref.entity_type == EntityType.CLAIM:
            return self._put_if_missing(claims, ref.entity_id, self._get_claim)
        if ref.entity_type == EntityType.NOTE:
            return self._put_if_missing(notes, ref.entity_id, self._get_note)
        if ref.entity_type == EntityType.VISUALIZATION:
            return self._put_if_missing(
                visualizations,
                ref.entity_id,
                self._get_visualization,
            )
        return False

    def _drop_out_of_scope(
        self,
        project_ids: set[UUID],
        questions: dict[UUID, Question],
        datasets: dict[UUID, Dataset],
        analyses: dict[UUID, Analysis],
        claims: dict[UUID, Claim],
        exploration_nodes: dict[UUID, ExplorationNode],
        notes: dict[UUID, Note],
    ) -> None:
        for mapping in (questions, datasets, analyses, claims, exploration_nodes, notes):
            for entity_id, entity in list(mapping.items()):
                if getattr(entity, "project_id", None) not in project_ids:
                    del mapping[entity_id]

    def _visualizations_for_records(
        self,
        project_ids: set[UUID],
        analyses: dict[UUID, Analysis],
        claims: dict[UUID, Claim],
    ) -> list[Visualization]:
        visualizations: dict[UUID, Visualization] = {}
        for analysis_id in analyses:
            items, _ = self.repository.query_visualizations(
                analysis_id=analysis_id,
                limit=None,
                offset=0,
            )
            visualizations.update({item.viz_id: item for item in items})
        for claim_id in claims:
            items, _ = self.repository.query_visualizations(
                claim_id=claim_id,
                limit=None,
                offset=0,
            )
            visualizations.update({item.viz_id: item for item in items})
        if project_ids:
            visualizations = {
                viz_id: viz
                for viz_id, viz in visualizations.items()
                if self._visualization_project_id(viz) in project_ids
            }
        return list(visualizations.values())

    def _notes_for_records(
        self,
        *,
        project_ids: set[UUID],
        questions: dict[UUID, Question],
        datasets: dict[UUID, Dataset],
        analyses: dict[UUID, Analysis],
        claims: dict[UUID, Claim],
        visualizations: dict[UUID, Visualization],
        notes: dict[UUID, Note],
    ) -> list[Note]:
        if not project_ids:
            return list(notes.values())
        scoped_notes: dict[UUID, Note] = dict(notes)
        target_map = {
            EntityType.QUESTION: set(questions),
            EntityType.DATASET: set(datasets),
            EntityType.ANALYSIS: set(analyses),
            EntityType.CLAIM: set(claims),
            EntityType.VISUALIZATION: set(visualizations),
        }
        for project_id in project_ids:
            project_notes, _ = self.repository.query_notes(
                project_id=project_id,
                limit=None,
                offset=0,
            )
            for note in project_notes:
                if self._note_targets_any(note, target_map):
                    scoped_notes[note.note_id] = note
        return list(scoped_notes.values())

    def _exploration_nodes_for_records(
        self,
        *,
        project_ids: set[UUID],
        questions: dict[UUID, Question],
        datasets: dict[UUID, Dataset],
        analyses: dict[UUID, Analysis],
        claims: dict[UUID, Claim],
        notes: dict[UUID, Note],
        visualizations: dict[UUID, Visualization],
        exploration_nodes: dict[UUID, ExplorationNode],
    ) -> list[ExplorationNode]:
        if not project_ids:
            return list(exploration_nodes.values())
        target_map = {
            EntityType.QUESTION: set(questions),
            EntityType.DATASET: set(datasets),
            EntityType.ANALYSIS: set(analyses),
            EntityType.CLAIM: set(claims),
            EntityType.NOTE: set(notes),
            EntityType.VISUALIZATION: set(visualizations),
        }
        known_node_ids = set(exploration_nodes)
        related: dict[UUID, ExplorationNode] = {}
        for project_id in project_ids:
            project_nodes, _ = self.repository.query_exploration_nodes(
                project_id=project_id,
                limit=None,
                offset=0,
            )
            for node in project_nodes:
                if _exploration_node_matches_records(
                    node,
                    target_map=target_map,
                    known_node_ids=known_node_ids,
                ):
                    related[node.node_id] = node
        return list(related.values())

    def _note_targets_any(
        self,
        note: Note,
        target_map: dict[EntityType, set[UUID]],
    ) -> bool:
        return any(
            target.entity_id in target_map.get(target.entity_type, set()) for target in note.targets
        )

    def _claim_edges_for_claims(
        self,
        project_ids: set[UUID],
        claims: dict[UUID, Claim],
    ):
        if not claims:
            return []
        claim_ids = set(claims)
        edges: dict[UUID, object] = {}
        if project_ids:
            for project_id in project_ids:
                items, _ = self.repository.query_claim_edges(
                    project_id=project_id,
                    limit=None,
                    offset=0,
                )
                for edge in items:
                    if edge.claim_id in claim_ids or edge.target_claim_id in claim_ids:
                        edges[edge.edge_id] = edge
        else:
            for claim_id in claim_ids:
                outgoing, _ = self.repository.query_claim_edges(
                    claim_id=claim_id,
                    limit=None,
                    offset=0,
                )
                incoming, _ = self.repository.query_claim_edges(
                    target_claim_id=claim_id,
                    limit=None,
                    offset=0,
                )
                for edge in [*outgoing, *incoming]:
                    edges[edge.edge_id] = edge
        return list(edges.values())

    def _entity_versions_for_records(
        self,
        questions: dict[UUID, Question],
        claims: dict[UUID, Claim],
    ):
        versions = []
        for entity_type, entity_ids in (
            (EntityType.QUESTION, questions),
            (EntityType.CLAIM, claims),
        ):
            for entity_id in entity_ids:
                items, _ = self.repository.query_entity_versions(
                    entity_type=entity_type.value,
                    entity_id=entity_id,
                    limit=None,
                    offset=0,
                )
                versions.extend(items)
        return versions

    def _get_question(self, question_id: UUID) -> Question:
        question = self.repository.questions.get(question_id)
        if question is None:
            raise NotFoundError("Question does not exist.")
        return question

    def _get_dataset(self, dataset_id: UUID) -> Dataset:
        dataset = self.repository.datasets.get(dataset_id)
        if dataset is None:
            raise NotFoundError("Dataset does not exist.")
        return dataset

    def _get_analysis(self, analysis_id: UUID) -> Analysis:
        analysis = self.repository.analyses.get(analysis_id)
        if analysis is None:
            raise NotFoundError("Analysis does not exist.")
        return analysis

    def _get_claim(self, claim_id: UUID) -> Claim:
        claim = self.repository.claims.get(claim_id)
        if claim is None:
            raise NotFoundError("Claim does not exist.")
        return claim

    def _get_exploration_node(self, node_id: UUID) -> ExplorationNode:
        node = self.repository.exploration_nodes.get(node_id)
        if node is None:
            raise NotFoundError("Exploration node does not exist.")
        return node

    def _get_note(self, note_id: UUID) -> Note:
        note = self.repository.notes.get(note_id)
        if note is None:
            raise NotFoundError("Note does not exist.")
        return note

    def _get_visualization(self, viz_id: UUID) -> Visualization:
        visualization = self.repository.visualizations.get(viz_id)
        if visualization is None:
            raise NotFoundError("Visualization does not exist.")
        return visualization

    def _visualization_project_id(self, visualization: Visualization) -> UUID:
        analysis = self._get_analysis(visualization.analysis_id)
        return analysis.project_id

    def _sorted(self, items: Iterable[object], *field_names: str):
        return sorted(
            items,
            key=lambda item: tuple(str(getattr(item, field_name)) for field_name in field_names),
        )

    def _authorized_project_scope(
        self,
        *,
        group_id: UUID | None,
        actor: AuthContext | None,
    ) -> set[UUID] | None:
        if group_id is None:
            require_role(actor, ADMIN_ROLES)
            return None
        self.authorization.require_group_owner(group_id, actor=actor)
        projects, _ = self.repository.query_projects(
            group_id=group_id,
            limit=None,
            offset=0,
        )
        return {project.project_id for project in projects}

    def _collect_records(
        self,
        *,
        user_id: UUID,
        project_ids: set[UUID] | None,
    ) -> RecordExportRecords:
        return self.repository.records_attributed_to_user(
            user_id=user_id,
            project_ids=project_ids,
        )

    def _project_ids_for_records(
        self,
        records: RecordExportRecords,
        *,
        fallback: set[UUID] | None,
    ) -> list[UUID]:
        if fallback is not None:
            return sorted(fallback, key=str)
        project_ids = {
            *[item.project_id for item in records.questions],
            *[item.project_id for item in records.datasets],
            *[item.project_id for item in records.sessions],
            *[item.project_id for item in records.analyses],
            *[item.project_id for item in records.claims],
            *[item.project_id for item in records.exploration_nodes],
            *[item.project_id for item in records.notes],
            *[self._visualization_project_id(item) for item in records.visualizations],
        }
        return sorted(project_ids, key=str)

    def _record_counts(self, records: RecordExportRecords) -> dict[str, int]:
        return {
            "questions": len(records.questions),
            "datasets": len(records.datasets),
            "sessions": len(records.sessions),
            "analyses": len(records.analyses),
            "claims": len(records.claims),
            "claim_edges": len(records.claim_edges),
            "exploration_nodes": len(records.exploration_nodes),
            "notes": len(records.notes),
            "visualizations": len(records.visualizations),
        }


def _exploration_node_matches_records(
    node: ExplorationNode,
    *,
    target_map: dict[EntityType, set[UUID]],
    known_node_ids: set[UUID],
) -> bool:
    if node.node_id in known_node_ids:
        return True
    if node.target.entity_id in target_map.get(node.target.entity_type, set()):
        return True
    if any(
        ref.entity_id in target_map.get(ref.entity_type, set())
        for ref in node.evidence_refs
    ):
        return True
    if node.invalidates_claim_id in target_map.get(EntityType.CLAIM, set()):
        return True
    related_node_ids = {*node.parent_node_ids, *node.also_depends_on_node_ids}
    if node.invalidates_node_id is not None:
        related_node_ids.add(node.invalidates_node_id)
    return bool(related_node_ids & known_node_ids)
