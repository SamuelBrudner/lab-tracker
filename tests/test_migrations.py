from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.backup import create_sqlite_backup, restore_sqlite_backup
from lab_tracker.db import get_session_factory
from lab_tracker.models import EntityRef, EntityType, ExplorationNodeType, QuestionType
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

_NOT_NULL_TIGHTENING_COLUMNS_0014_0024 = {
    "graph_change_sets": {
        "source_note_ids",
        "context_packet",
        "summary",
        "uncertain_fields",
        "clarification_requests",
        "error_metadata",
        "created_at",
        "updated_at",
    },
    "graph_change_operations": {
        "payload",
        "rationale",
        "source_refs",
        "error_metadata",
        "created_at",
        "updated_at",
    },
    "question_refactors": {
        "source_snapshot",
        "replacement_snapshot",
        "relationship_changes",
        "created_at",
    },
    "project_memberships": {"created_at", "updated_at"},
    "goals": {"summary", "status", "attributes", "created_at", "updated_at"},
    "goal_links": {"link_status", "created_at"},
    "graph_draft_batch_settings": {"created_at", "updated_at"},
    "graph_draft_batch_runs": {
        "summary",
        "error_metadata",
        "created_at",
        "updated_at",
        "started_at",
    },
}


def _actor() -> AuthContext:
    return AuthContext(user_id=uuid4(), role=Role.ADMIN)


def _alembic_config() -> Config:
    repo_root = Path(__file__).resolve().parent.parent
    return Config(str(repo_root / "alembic.ini"))


def _set_database_url(monkeypatch, database_url: str) -> None:
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)


def _ordered_revisions(config: Config) -> list[str]:
    script = ScriptDirectory.from_config(config)
    revisions = list(script.walk_revisions(base="base", head="heads"))
    revisions.reverse()
    return [revision.revision for revision in revisions]


def _single_script_head(config: Config) -> str:
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    return heads[0]


def _current_revision(database_url: str) -> str | None:
    revisions = _current_revisions(database_url)
    if not revisions:
        return None
    assert len(revisions) == 1
    return next(iter(revisions))


def _current_revisions(database_url: str) -> set[str]:
    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    try:
        inspector = inspect(engine)
        if "alembic_version" not in inspector.get_table_names():
            return set()
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        return {str(row[0]) for row in rows}
    finally:
        engine.dispose()


def _upgrade_head(database_url: str, monkeypatch) -> None:
    _set_database_url(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")


def test_alembic_upgrade_chain_from_empty_to_head(monkeypatch, tmp_path):
    db_path = tmp_path / "migrations-chain.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    config = _alembic_config()
    _set_database_url(monkeypatch, database_url)

    assert _current_revision(database_url) is None

    revisions = _ordered_revisions(config)
    assert revisions
    for revision in revisions:
        command.upgrade(config, revision)
        assert revision in _current_revisions(database_url)
    assert _current_revision(database_url) == _single_script_head(config)


def test_alembic_has_single_head() -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    heads = script.get_heads()
    assert len(heads) == 1, (
        f"Expected exactly one Alembic head, found {sorted(heads)}. "
        "Migration filename prefixes (NNNN_) are decorative; Alembic chains on the "
        "revision/down_revision strings, not the number. A new migration must set "
        "down_revision to the current head (run `uv run alembic heads`). If two "
        "branches each added a head, reconcile them with `uv run alembic merge`."
    )


def test_alembic_upgrade_head_creates_expected_tables(monkeypatch, tmp_path):
    db_path = tmp_path / "migrations-smoke.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    _upgrade_head(database_url, monkeypatch)

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    expected = {
        "projects",
        "questions",
        "datasets",
        "dataset_files",
        "notes",
        "sessions",
        "acquisition_outputs",
        "analyses",
        "claims",
        "visualizations",
        "dataset_question_links",
        "question_parents",
        "analysis_datasets",
        "claim_datasets",
        "claim_analyses",
        "claim_questions",
        "claim_edges",
        "exploration_nodes",
        "exploration_node_edges",
        "entity_versions",
        "goals",
        "goal_links",
        "visualization_claims",
        "graph_change_sets",
        "graph_change_operations",
        "graph_draft_batch_settings",
        "graph_draft_batch_runs",
        "project_memberships",
        "project_groups",
        "group_memberships",
        "supervision_edges",
        "ownership_reassignments",
        "record_export_events",
        "usage_events",
        "usage_event_rollups",
        "invitations",
        "personal_access_tokens",
    }
    assert expected.issubset(table_names)
    assert "dataset_reviews" not in table_names
    assert "daily_graph_reviews" not in table_names
    assert "daily_graph_review_change_sets" not in table_names
    assert "note_extracted_entities" not in table_names
    assert "note_tag_suggestions" not in table_names

    project_columns = {column["name"] for column in inspector.get_columns("projects")}
    project_group_columns = {
        column["name"] for column in inspector.get_columns("project_groups")
    }
    question_columns = {column["name"] for column in inspector.get_columns("questions")}
    dataset_columns = {column["name"] for column in inspector.get_columns("datasets")}
    analysis_columns = {column["name"] for column in inspector.get_columns("analyses")}
    graph_change_columns = {
        column["name"] for column in inspector.get_columns("graph_change_sets")
    }
    membership_columns = {
        column["name"] for column in inspector.get_columns("project_memberships")
    }
    group_membership_columns = {
        column["name"] for column in inspector.get_columns("group_memberships")
    }
    supervision_columns = {
        column["name"] for column in inspector.get_columns("supervision_edges")
    }
    ownership_columns = {
        column["name"] for column in inspector.get_columns("ownership_reassignments")
    }
    record_export_columns = {
        column["name"] for column in inspector.get_columns("record_export_events")
    }
    usage_event_columns = {
        column["name"]: column for column in inspector.get_columns("usage_events")
    }
    usage_event_rollup_columns = {
        column["name"]: column for column in inspector.get_columns("usage_event_rollups")
    }
    invitation_columns = {column["name"] for column in inspector.get_columns("invitations")}
    personal_access_token_columns = {
        column["name"] for column in inspector.get_columns("personal_access_tokens")
    }
    entity_version_columns = {
        column["name"] for column in inspector.get_columns("entity_versions")
    }
    visualization_columns = {
        column["name"] for column in inspector.get_columns("visualizations")
    }
    goal_link_columns = {
        column["name"]: column for column in inspector.get_columns("goal_links")
    }
    goal_columns = {
        column["name"]: column for column in inspector.get_columns("goals")
    }

    assert "review_policy" not in project_columns
    assert "group_id" in project_columns
    _assert_created_by_user_fk(inspector, "project_groups")
    _assert_created_by_user_fk(inspector, "projects")
    _assert_created_by_user_fk(inspector, "questions")
    _assert_created_by_user_fk(inspector, "question_refactors")
    _assert_created_by_user_fk(inspector, "datasets")
    _assert_created_by_user_fk(inspector, "notes")
    _assert_created_by_user_fk(inspector, "graph_change_sets")
    _assert_created_by_user_fk(inspector, "graph_draft_batch_runs")
    _assert_created_by_user_fk(inspector, "sessions")
    _assert_created_by_user_fk(inspector, "claims")
    _assert_created_by_user_fk(inspector, "exploration_nodes")
    _assert_created_by_user_fk(inspector, "entity_versions")
    _assert_created_by_user_fk(inspector, "goals")
    _assert_created_by_user_fk(inspector, "goal_links")
    _assert_created_by_user_fk(inspector, "visualizations")
    _assert_created_by_user_fk(inspector, "project_memberships")
    _assert_created_by_user_fk(inspector, "group_memberships")
    for table_name in (
        "questions",
        "datasets",
        "notes",
        "sessions",
        "analyses",
        "claims",
        "exploration_nodes",
        "goals",
        "visualizations",
    ):
        _assert_origin_backlink_columns(inspector, table_name)
    assert "executed_by_user_id" in analysis_columns
    assert "external_artifacts" in analysis_columns
    _assert_index(inspector, "analyses", "ix_analyses_executed_by_user_id")
    _assert_fk(
        inspector,
        "analyses",
        column="executed_by_user_id",
        referred_table="users",
    )
    assert {
        "group_id",
        "name",
        "description",
        "kind",
        "group_read_all",
    }.issubset(project_group_columns)
    assert "created_from" not in question_columns
    assert "source_provenance" not in question_columns
    assert "terminal_reason" in question_columns
    assert "manifest_extraction_provenance" not in dataset_columns
    assert "manifest_external_artifacts" in dataset_columns
    assert "terminal_reason" in dataset_columns
    assert "terminal_reason" in analysis_columns
    claim_columns = {column["name"] for column in inspector.get_columns("claims")}
    assert "terminal_reason" in claim_columns
    assert "external_citations" in claim_columns
    assert {
        "falsification_criteria",
        "verification_plan",
        "refuting_outcome",
    }.issubset(claim_columns)
    claim_edge_columns = {column["name"] for column in inspector.get_columns("claim_edges")}
    assert {
        "edge_id",
        "claim_id",
        "target_claim_id",
        "relation",
        "created_by",
        "created_by_user_id",
        "created_at",
    }.issubset(claim_edge_columns)
    _assert_index(inspector, "claim_edges", "ix_claim_edges_claim_id")
    _assert_index(inspector, "claim_edges", "ix_claim_edges_target_claim_id")
    _assert_index(inspector, "claim_edges", "ix_claim_edges_created_by_user_id")
    _assert_fk(inspector, "claim_edges", column="claim_id", referred_table="claims")
    _assert_fk(
        inspector,
        "claim_edges",
        column="target_claim_id",
        referred_table="claims",
    )
    _assert_fk(
        inspector,
        "claim_edges",
        column="created_by_user_id",
        referred_table="users",
    )
    exploration_columns = {
        column["name"] for column in inspector.get_columns("exploration_nodes")
    }
    assert {
        "node_id",
        "project_id",
        "node_type",
        "title",
        "target_entity_type",
        "target_entity_id",
        "status",
        "choice",
        "alternatives_considered",
        "rationale",
        "evidence_refs",
        "hypothesis",
        "failure_mode",
        "lesson",
        "tooling_context",
        "trigger",
        "invalidates_node_id",
        "invalidates_claim_id",
        "created_by",
        "created_by_user_id",
        "origin",
        "change_set_id",
        "origin_provider",
        "origin_model",
        "origin_prompt_version",
        "created_at",
        "updated_at",
    }.issubset(exploration_columns)
    exploration_edge_columns = {
        column["name"] for column in inspector.get_columns("exploration_node_edges")
    }
    assert {
        "edge_id",
        "source_node_id",
        "target_node_id",
        "relation",
        "created_at",
    }.issubset(exploration_edge_columns)
    _assert_index(inspector, "exploration_nodes", "ix_exploration_nodes_project_created_at")
    _assert_index(inspector, "exploration_nodes", "ix_exploration_nodes_target")
    _assert_index(inspector, "exploration_node_edges", "ix_exploration_node_edges_source")
    _assert_index(inspector, "exploration_node_edges", "ix_exploration_node_edges_target")
    _assert_fk(inspector, "exploration_nodes", column="project_id", referred_table="projects")
    _assert_fk(
        inspector,
        "exploration_nodes",
        column="invalidates_node_id",
        referred_table="exploration_nodes",
    )
    _assert_fk(
        inspector,
        "exploration_nodes",
        column="invalidates_claim_id",
        referred_table="claims",
    )
    _assert_fk(
        inspector,
        "exploration_node_edges",
        column="source_node_id",
        referred_table="exploration_nodes",
    )
    _assert_fk(
        inspector,
        "exploration_node_edges",
        column="target_node_id",
        referred_table="exploration_nodes",
    )
    assert {
        "version_id",
        "entity_type",
        "entity_id",
        "version_number",
        "snapshot",
        "change_set_id",
        "committed_at",
        "created_at",
        "created_by",
        "created_by_user_id",
    }.issubset(entity_version_columns)
    _assert_index(
        inspector,
        "entity_versions",
        "ix_entity_versions_entity_created_at",
    )
    _assert_index(inspector, "entity_versions", "ix_entity_versions_change_set_id")
    _assert_fk(
        inspector,
        "entity_versions",
        column="change_set_id",
        referred_table="graph_change_sets",
    )
    assert {
        "submitted_at",
        "submitted_by",
        "reviewed_at",
        "reviewed_by",
        "review_note",
        "source_note_ids",
        "batch_key",
        "batch_window_start",
        "batch_window_end",
    }.issubset(graph_change_columns)
    _assert_unique_constraint(
        inspector,
        "graph_change_sets",
        "uq_graph_change_sets_batch_key",
        {"batch_key"},
    )
    _assert_no_index(inspector, "graph_change_sets", "ix_graph_change_sets_batch_key")
    operation_columns = {
        column["name"] for column in inspector.get_columns("graph_change_operations")
    }
    assert {"project_id", "user_id", "role"}.issubset(membership_columns)
    assert {"group_id", "user_id", "role"}.issubset(group_membership_columns)
    assert {
        "supervisor_user_id",
        "supervisee_user_id",
        "started_at",
        "ended_at",
    }.issubset(supervision_columns)
    _assert_index(
        inspector,
        "supervision_edges",
        "ix_supervision_edges_supervisor_started",
    )
    _assert_index(
        inspector,
        "supervision_edges",
        "ix_supervision_edges_supervisee_started",
    )
    _assert_index(inspector, "supervision_edges", "uq_supervision_edges_active_pair")
    _assert_fk(
        inspector,
        "supervision_edges",
        column="supervisor_user_id",
        referred_table="users",
    )
    _assert_fk(
        inspector,
        "supervision_edges",
        column="supervisee_user_id",
        referred_table="users",
    )
    assert {
        "reassignment_id",
        "from_user_id",
        "to_user_id",
        "reason",
        "record_counts",
        "created_by",
        "created_by_user_id",
        "created_at",
    }.issubset(ownership_columns)
    _assert_fk(
        inspector,
        "ownership_reassignments",
        column="from_user_id",
        referred_table="users",
    )
    _assert_fk(
        inspector,
        "ownership_reassignments",
        column="to_user_id",
        referred_table="users",
    )
    _assert_fk(
        inspector,
        "ownership_reassignments",
        column="created_by_user_id",
        referred_table="users",
    )
    _assert_index(
        inspector,
        "ownership_reassignments",
        "ix_ownership_reassignments_from_created",
    )
    _assert_index(
        inspector,
        "ownership_reassignments",
        "ix_ownership_reassignments_to_created",
    )
    _assert_index(
        inspector,
        "ownership_reassignments",
        "ix_ownership_reassignments_created_by_user_id",
    )
    assert {
        "export_id",
        "user_id",
        "group_id",
        "project_ids",
        "record_counts",
        "created_by",
        "created_by_user_id",
        "created_at",
    }.issubset(record_export_columns)
    _assert_fk(
        inspector,
        "record_export_events",
        column="user_id",
        referred_table="users",
    )
    _assert_fk(
        inspector,
        "record_export_events",
        column="group_id",
        referred_table="project_groups",
    )
    _assert_fk(
        inspector,
        "record_export_events",
        column="created_by_user_id",
        referred_table="users",
    )
    _assert_index(
        inspector,
        "record_export_events",
        "ix_record_export_events_user_created",
    )
    _assert_index(
        inspector,
        "record_export_events",
        "ix_record_export_events_group_created",
    )
    _assert_index(
        inspector,
        "record_export_events",
        "ix_record_export_events_created_by_user_id",
    )
    assert {
        "event_id",
        "occurred_at",
        "verb",
        "resource_type",
        "resource_id",
        "actor_user_id",
        "actor_role",
        "principal_type",
        "surface",
        "project_id",
        "outcome",
        "duration_ms",
        "result_count",
    }.issubset(usage_event_columns)
    assert {
        "rollup_id",
        "day",
        "verb",
        "resource_type",
        "project_id",
        "actor_role",
        "principal_type",
        "surface",
        "outcome",
        "event_count",
        "total_duration_ms",
        "total_result_count",
        "created_at",
    }.issubset(usage_event_rollup_columns)
    _assert_fk(
        inspector,
        "usage_events",
        column="project_id",
        referred_table="projects",
    )
    _assert_fk(
        inspector,
        "usage_event_rollups",
        column="project_id",
        referred_table="projects",
    )
    for index_name in {
        "ix_usage_events_occurred_at",
        "ix_usage_events_verb",
        "ix_usage_events_resource_type",
        "ix_usage_events_project_id",
        "ix_usage_events_actor_user_id",
        "ix_usage_event_rollups_day",
        "ix_usage_event_rollups_bucket",
    }:
        table_name = "usage_event_rollups" if "rollups" in index_name else "usage_events"
        _assert_index(inspector, table_name, index_name)
    for columns in (usage_event_columns, usage_event_rollup_columns):
        for column in columns.values():
            column_type = str(column["type"]).upper()
            assert "TEXT" not in column_type
            assert "JSON" not in column_type
    assert {
        "invitation_id",
        "email",
        "role",
        "token_hash",
        "expires_at",
        "created_at",
        "consumed_at",
        "consumed_by_user_id",
        "revoked_at",
    }.issubset(invitation_columns)
    _assert_fk(
        inspector,
        "invitations",
        column="consumed_by_user_id",
        referred_table="users",
    )
    _assert_index(inspector, "invitations", "ix_invitations_email_created")
    _assert_index(inspector, "invitations", "ix_invitations_expires_at")
    assert {
        "token_id",
        "user_id",
        "label",
        "token_hash",
        "role",
        "read_only",
        "expires_at",
        "created_at",
        "last_used_at",
        "revoked_at",
    }.issubset(personal_access_token_columns)
    _assert_fk(
        inspector,
        "personal_access_tokens",
        column="user_id",
        referred_table="users",
    )
    _assert_index(
        inspector,
        "personal_access_tokens",
        "ix_personal_access_tokens_user_created",
    )
    _assert_index(
        inspector,
        "personal_access_tokens",
        "ix_personal_access_tokens_expires_at",
    )
    assert {
        "asset_storage_id",
        "asset_filename",
        "asset_content_type",
        "asset_size_bytes",
        "asset_checksum",
    }.issubset(visualization_columns)
    assert "review_note" in operation_columns
    assert goal_columns["project_id"]["nullable"] is True
    assert goal_link_columns["slot"]["nullable"] is False
    _assert_columns_not_nullable(
        inspector,
        _NOT_NULL_TIGHTENING_COLUMNS_0014_0024,
    )
    engine.dispose()


def test_not_null_tightening_migration_backfills_existing_nulls(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "not-null-tightening.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    config = _alembic_config()
    _set_database_url(monkeypatch, database_url)

    command.upgrade(config, "0038_entity_versions")

    user_id = str(uuid4())
    project_id = str(uuid4())
    note_id = str(uuid4())
    source_question_id = str(uuid4())
    replacement_question_id = str(uuid4())
    change_set_id = str(uuid4())
    operation_id = str(uuid4())
    refactor_id = str(uuid4())
    membership_id = str(uuid4())
    goal_id = str(uuid4())
    link_id = str(uuid4())
    batch_settings_id = str(uuid4())
    batch_run_id = str(uuid4())

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(user_id, username, password_hash, role, created_at) "
                "VALUES (:user_id, 'migration-user', 'unused', 'admin', "
                "CURRENT_TIMESTAMP)"
            ),
            {"user_id": user_id},
        )
        connection.execute(
            text(
                "INSERT INTO projects "
                "(project_id, name, description, status, created_at, updated_at) "
                "VALUES (:project_id, 'Migration project', '', 'active', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"project_id": project_id},
        )
        connection.execute(
            text(
                "INSERT INTO notes "
                "(note_id, project_id, raw_content, status, origin, created_at, "
                "updated_at) VALUES (:note_id, :project_id, 'raw', 'staged', "
                "'user', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"note_id": note_id, "project_id": project_id},
        )
        for question_id, text_value in (
            (source_question_id, "Source question?"),
            (replacement_question_id, "Replacement question?"),
        ):
            connection.execute(
                text(
                    "INSERT INTO questions "
                    "(question_id, project_id, text, question_type, status, "
                    "origin, created_at, updated_at) "
                    "VALUES (:question_id, :project_id, :text_value, "
                    "'descriptive', 'staged', 'user', CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP)"
                ),
                {
                    "question_id": question_id,
                    "project_id": project_id,
                    "text_value": text_value,
                },
            )
        connection.execute(
            text(
                "INSERT INTO graph_change_sets "
                "(change_set_id, project_id, source_note_id, provider, model, "
                "prompt_version, draft_mode, status) "
                "VALUES (:change_set_id, :project_id, :note_id, 'openai', "
                "'test-model', 'v1', 'graph_context', 'drafting')"
            ),
            {
                "change_set_id": change_set_id,
                "project_id": project_id,
                "note_id": note_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO graph_change_operations "
                "(operation_id, change_set_id, sequence, op, entity_type, status) "
                "VALUES (:operation_id, :change_set_id, 1, 'create', "
                "'question', 'proposed')"
            ),
            {
                "operation_id": operation_id,
                "change_set_id": change_set_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO question_refactors "
                "(refactor_id, project_id, source_question_id, "
                "replacement_question_id, reason) "
                "VALUES (:refactor_id, :project_id, :source_question_id, "
                ":replacement_question_id, 'cleanup')"
            ),
            {
                "refactor_id": refactor_id,
                "project_id": project_id,
                "source_question_id": source_question_id,
                "replacement_question_id": replacement_question_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO project_memberships "
                "(membership_id, project_id, user_id, role) "
                "VALUES (:membership_id, :project_id, :user_id, 'owner')"
            ),
            {
                "membership_id": membership_id,
                "project_id": project_id,
                "user_id": user_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO goals "
                "(goal_id, project_id, goal_type, title, origin) "
                "VALUES (:goal_id, :project_id, 'answer_question', "
                "'Migration goal', 'user')"
            ),
            {"goal_id": goal_id, "project_id": project_id},
        )
        connection.execute(
            text(
                "INSERT INTO goal_links "
                "(link_id, goal_id, entity_type, entity_id, relation, slot) "
                "VALUES (:link_id, :goal_id, 'question', :entity_id, "
                "'milestone', '')"
            ),
            {
                "link_id": link_id,
                "goal_id": goal_id,
                "entity_id": source_question_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO graph_draft_batch_settings "
                "(settings_id, project_id, enabled, cadence_minutes, "
                "run_at_local_time, timezone_name) "
                "VALUES (:settings_id, :project_id, 1, 1440, '06:00', 'UTC')"
            ),
            {"settings_id": batch_settings_id, "project_id": project_id},
        )
        connection.execute(
            text(
                "INSERT INTO graph_draft_batch_runs "
                "(run_id, project_id, trigger, status, window_start, window_end, "
                "note_count, batch_key) "
                "VALUES (:run_id, :project_id, 'manual', 'running', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0, :batch_key)"
            ),
            {
                "run_id": batch_run_id,
                "project_id": project_id,
                "batch_key": f"batch-{uuid4()}",
            },
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    inspector = inspect(engine)
    _assert_columns_not_nullable(
        inspector,
        _NOT_NULL_TIGHTENING_COLUMNS_0014_0024,
    )
    with engine.connect() as connection:
        for table_name, id_column, id_value in (
            ("graph_change_sets", "change_set_id", change_set_id),
            ("graph_change_operations", "operation_id", operation_id),
            ("question_refactors", "refactor_id", refactor_id),
            ("project_memberships", "membership_id", membership_id),
            ("goals", "goal_id", goal_id),
            ("goal_links", "link_id", link_id),
            ("graph_draft_batch_settings", "settings_id", batch_settings_id),
            ("graph_draft_batch_runs", "run_id", batch_run_id),
        ):
            row_count = connection.execute(
                text(
                    f"SELECT COUNT(*) FROM {table_name} "
                    f"WHERE {id_column} = :id_value"
                ),
                {"id_value": id_value},
            ).scalar_one()
            assert row_count == 1, f"{table_name} row was lost during migration"
        for table_name, column_names in _NOT_NULL_TIGHTENING_COLUMNS_0014_0024.items():
            for column_name in column_names:
                null_count = connection.execute(
                    text(
                        f"SELECT COUNT(*) FROM {table_name} "
                        f"WHERE {column_name} IS NULL"
                    )
                ).scalar_one()
                assert null_count == 0, f"{table_name}.{column_name} remained NULL"
        goal_row = connection.execute(
            text("SELECT summary, status FROM goals WHERE goal_id = :goal_id"),
            {"goal_id": goal_id},
        ).one()
        assert goal_row.summary == ""
        assert goal_row.status == "planned"
        link_status = connection.execute(
            text("SELECT link_status FROM goal_links WHERE goal_id = :goal_id"),
            {"goal_id": goal_id},
        ).scalar_one()
        assert link_status == "candidate"
    engine.dispose()


def test_goal_link_slot_migration_normalizes_unslotted_links(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "goal-link-slot-dedupe.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    config = _alembic_config()
    _set_database_url(monkeypatch, database_url)

    command.upgrade(config, "0022_goals")

    project_id = str(uuid4())
    goal_id = str(uuid4())
    entity_id = str(uuid4())
    kept_link_id = str(uuid4())
    deduped_link_id = str(uuid4())
    slotted_link_id = str(uuid4())

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects "
                "(project_id, name, description, status, created_at, updated_at) "
                "VALUES (:project_id, 'Goal migration project', '', 'active', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"project_id": project_id},
        )
        connection.execute(
            text(
                "INSERT INTO goals "
                "(goal_id, project_id, goal_type, title, created_at, updated_at) "
                "VALUES (:goal_id, :project_id, 'answer_question', "
                "'Goal migration', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"goal_id": goal_id, "project_id": project_id},
        )
        for link_id, slot, created_at in (
            (kept_link_id, None, "2026-01-01 00:00:00"),
            (deduped_link_id, None, "2026-01-02 00:00:00"),
            (slotted_link_id, "Figure 1", "2026-01-04 00:00:00"),
        ):
            connection.execute(
                text(
                    "INSERT INTO goal_links "
                    "(link_id, goal_id, entity_type, entity_id, relation, slot, "
                    "created_at) VALUES (:link_id, :goal_id, 'question', "
                    ":entity_id, 'milestone', :slot, :created_at)"
                ),
                {
                    "link_id": link_id,
                    "goal_id": goal_id,
                    "entity_id": entity_id,
                    "slot": slot,
                    "created_at": created_at,
                },
            )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    inspector = inspect(engine)
    goal_link_columns = {
        column["name"]: column for column in inspector.get_columns("goal_links")
    }
    assert goal_link_columns["slot"]["nullable"] is False
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT link_id, slot FROM goal_links "
                "WHERE goal_id = :goal_id AND entity_id = :entity_id "
                "ORDER BY slot, link_id"
            ),
            {"goal_id": goal_id, "entity_id": entity_id},
        ).mappings().all()

    assert rows == [
        {"link_id": kept_link_id, "slot": ""},
        {"link_id": slotted_link_id, "slot": "Figure 1"},
    ]
    engine.dispose()


def test_schema_cleanup_migration_preserves_graph_change_children(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "schema-cleanup.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    config = _alembic_config()
    _set_database_url(monkeypatch, database_url)

    command.upgrade(config, "0039_not_null_tightening_0014_0024")

    project_id = str(uuid4())
    note_id = str(uuid4())
    change_set_id = str(uuid4())
    operation_id = str(uuid4())
    batch_run_id = str(uuid4())
    review_id = str(uuid4())
    batch_key = f"batch-{uuid4()}"

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects "
                "(project_id, name, description, status, created_at, updated_at) "
                "VALUES (:project_id, 'Schema cleanup project', '', 'active', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"project_id": project_id},
        )
        connection.execute(
            text(
                "INSERT INTO notes "
                "(note_id, project_id, raw_content, status, origin, created_at, "
                "updated_at) VALUES (:note_id, :project_id, 'raw', 'staged', "
                "'user', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"note_id": note_id, "project_id": project_id},
        )
        connection.execute(
            text(
                "INSERT INTO graph_change_sets "
                "(change_set_id, project_id, source_note_id, source_note_ids, "
                "provider, model, prompt_version, draft_mode, context_packet, "
                "summary, uncertain_fields, clarification_requests, status, "
                "error_metadata, created_at, updated_at, batch_key) "
                "VALUES (:change_set_id, :project_id, :note_id, '[]', "
                "'openai', 'test-model', 'v1', 'graph_context', '{}', '', "
                "'[]', '[]', 'drafting', '{}', CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP, :batch_key)"
            ),
            {
                "change_set_id": change_set_id,
                "project_id": project_id,
                "note_id": note_id,
                "batch_key": batch_key,
            },
        )
        connection.execute(
            text(
                "INSERT INTO graph_change_operations "
                "(operation_id, change_set_id, sequence, op, entity_type, "
                "payload, rationale, source_refs, status, error_metadata, "
                "created_at, updated_at) VALUES (:operation_id, :change_set_id, "
                "1, 'create', 'question', '{}', '', '[]', 'proposed', '{}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "operation_id": operation_id,
                "change_set_id": change_set_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO graph_draft_batch_runs "
                "(run_id, project_id, trigger, status, window_start, window_end, "
                "note_count, batch_key, change_set_id, summary, error_metadata, "
                "created_at, updated_at, started_at) "
                "VALUES (:run_id, :project_id, 'manual', 'ready', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1, :batch_key, "
                ":change_set_id, '', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)"
            ),
            {
                "run_id": batch_run_id,
                "project_id": project_id,
                "batch_key": f"run-{batch_key}",
                "change_set_id": change_set_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO daily_graph_reviews "
                "(review_id, project_id, window_start, window_end, status) "
                "VALUES (:review_id, :project_id, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP, 'ready')"
            ),
            {"review_id": review_id, "project_id": project_id},
        )
        connection.execute(
            text(
                "INSERT INTO daily_graph_review_change_sets "
                "(review_id, change_set_id, sequence) "
                "VALUES (:review_id, :change_set_id, 1)"
            ),
            {"review_id": review_id, "change_set_id": change_set_id},
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert "daily_graph_reviews" not in table_names
    assert "daily_graph_review_change_sets" not in table_names
    _assert_unique_constraint(
        inspector,
        "graph_change_sets",
        "uq_graph_change_sets_batch_key",
        {"batch_key"},
    )
    _assert_no_index(inspector, "graph_change_sets", "ix_graph_change_sets_batch_key")
    with engine.connect() as connection:
        operation_count = connection.execute(
            text(
                "SELECT COUNT(*) FROM graph_change_operations "
                "WHERE operation_id = :operation_id"
            ),
            {"operation_id": operation_id},
        ).scalar_one()
        batch_run_change_set_id = connection.execute(
            text(
                "SELECT change_set_id FROM graph_draft_batch_runs "
                "WHERE run_id = :batch_run_id"
            ),
            {"batch_run_id": batch_run_id},
        ).scalar_one()

    assert operation_count == 1
    assert batch_run_change_set_id == change_set_id
    engine.dispose()


def test_database_at_daily_review_branch_upgrades_to_current_head(monkeypatch, tmp_path):
    db_path = tmp_path / "daily-review-branch.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    config = _alembic_config()
    _set_database_url(monkeypatch, database_url)

    command.upgrade(config, "0017_daily_graph_reviews")
    assert _current_revision(database_url) == "0017_daily_graph_reviews"

    command.upgrade(config, "head")
    assert _current_revision(database_url) == _single_script_head(config)

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert {
        "device_tokens",
        "device_enrollments",
        "project_memberships",
        "graph_draft_batch_settings",
        "graph_draft_batch_runs",
        "project_groups",
        "group_memberships",
        "supervision_edges",
        "ownership_reassignments",
        "record_export_events",
        "usage_events",
        "usage_event_rollups",
    }.issubset(table_names)
    assert "daily_graph_reviews" not in table_names
    assert "daily_graph_review_change_sets" not in table_names
    engine.dispose()


def test_attribution_user_fk_backfill_is_guarded(monkeypatch, tmp_path):
    db_path = tmp_path / "attribution-backfill.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    config = _alembic_config()
    _set_database_url(monkeypatch, database_url)

    command.upgrade(config, "0027_attribution_user_fks")
    valid_user_id = str(uuid4())
    missing_user_id = str(uuid4())
    local_user_id = "00000000-0000-4000-8000-000000000001"
    valid_project_id = str(uuid4())
    missing_project_id = str(uuid4())
    local_project_id = str(uuid4())
    legacy_project_id = str(uuid4())
    analysis_id = str(uuid4())
    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (user_id, username, password_hash, role, created_at) "
                "VALUES (:user_id, :username, :password_hash, :role, CURRENT_TIMESTAMP)"
            ),
            {
                "user_id": valid_user_id,
                "username": "valid-attribution-user",
                "password_hash": "unused",
                "role": "admin",
            },
        )
        for project_id, created_by in (
            (valid_project_id, valid_user_id),
            (missing_project_id, missing_user_id),
            (local_project_id, local_user_id),
            (legacy_project_id, "legacy-operator"),
        ):
            connection.execute(
                text(
                    "INSERT INTO projects "
                    "(project_id, name, status, created_by, created_at, updated_at) "
                    "VALUES (:project_id, :name, 'active', :created_by, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "project_id": project_id,
                    "name": f"Project {project_id}",
                    "created_by": created_by,
                },
            )
        connection.execute(
            text(
                "INSERT INTO analyses "
                "(analysis_id, project_id, method_hash, code_version, executed_by, "
                "executed_at, status, created_at, updated_at) "
                "VALUES (:analysis_id, :project_id, 'method', 'code', :executed_by, "
                "CURRENT_TIMESTAMP, 'staged', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "analysis_id": analysis_id,
                "project_id": valid_project_id,
                "executed_by": valid_user_id,
            },
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        project_rows = dict(
            connection.execute(
                text("SELECT project_id, created_by_user_id FROM projects")
            ).all()
        )
        analysis_user_id = connection.execute(
            text("SELECT executed_by_user_id FROM analyses WHERE analysis_id = :analysis_id"),
            {"analysis_id": analysis_id},
        ).scalar_one()

    assert project_rows[valid_project_id] == valid_user_id
    assert project_rows[missing_project_id] is None
    assert project_rows[local_project_id] is None
    assert project_rows[legacy_project_id] is None
    assert analysis_user_id == valid_user_id
    engine.dispose()


def _assert_created_by_user_fk(inspector, table_name: str) -> None:
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    assert "created_by_user_id" in columns
    _assert_index(inspector, table_name, f"ix_{table_name}_created_by_user_id")
    _assert_fk(
        inspector,
        table_name,
        column="created_by_user_id",
        referred_table="users",
    )


def _assert_columns_not_nullable(
    inspector,
    table_columns: dict[str, set[str]],
) -> None:
    for table_name, column_names in table_columns.items():
        columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        for column_name in column_names:
            assert columns[column_name]["nullable"] is False, (
                f"{table_name}.{column_name} should be NOT NULL"
            )


def _assert_origin_backlink_columns(inspector, table_name: str) -> None:
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    assert {
        "origin",
        "change_set_id",
        "origin_provider",
        "origin_model",
        "origin_prompt_version",
    }.issubset(columns)
    _assert_index(inspector, table_name, f"ix_{table_name}_change_set_id")
    _assert_fk(
        inspector,
        table_name,
        column="change_set_id",
        referred_table="graph_change_sets",
    )


def _assert_index(inspector, table_name: str, index_name: str) -> None:
    indexes = {index["name"] for index in inspector.get_indexes(table_name)}
    assert index_name in indexes


def _assert_no_index(inspector, table_name: str, index_name: str) -> None:
    indexes = {index["name"] for index in inspector.get_indexes(table_name)}
    assert index_name not in indexes


def _assert_unique_constraint(
    inspector,
    table_name: str,
    constraint_name: str,
    column_names: set[str],
) -> None:
    unique_constraints = inspector.get_unique_constraints(table_name)
    assert any(
        constraint["name"] == constraint_name
        and set(constraint["column_names"]) == column_names
        for constraint in unique_constraints
    )


def _assert_fk(
    inspector,
    table_name: str,
    *,
    column: str,
    referred_table: str,
) -> None:
    foreign_keys = inspector.get_foreign_keys(table_name)
    assert any(
        column in foreign_key["constrained_columns"]
        and foreign_key["referred_table"] == referred_table
        for foreign_key in foreign_keys
    )


def test_migrated_database_supports_api_round_trip(monkeypatch, tmp_path):
    db_path = tmp_path / "migrations-roundtrip.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    _upgrade_head(database_url, monkeypatch)
    actor = _actor()

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    session_factory = get_session_factory(engine=engine)

    with session_factory() as session:
        api = LabTrackerAPI(repository=SQLAlchemyLabTrackerRepository(session))
        project = api.create_project("Migrated DB", actor=actor)
        question = api.create_question(
            project_id=project.project_id,
            text="Is Alembic wiring valid?",
            question_type=QuestionType.DESCRIPTIVE,
            actor=actor,
        )
        dataset = api.create_dataset(
            project_id=project.project_id,
            primary_question_id=question.question_id,
            actor=actor,
        )
        claim = api.create_claim(
            project_id=project.project_id,
            statement="The migrated database preserves claim links.",
            confidence=50,
            supported_by_dataset_ids=[dataset.dataset_id],
            actor=actor,
        )
        decision = api.create_exploration_node(
            project_id=project.project_id,
            node_type=ExplorationNodeType.DECISION,
            title="Choose migrated exploration path",
            target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id),
            choice="Use migrated API path",
            alternatives_considered=["Direct SQL fixture"],
            rationale="The API round trip verifies mapper wiring.",
            actor=actor,
        )
        pivot = api.create_exploration_node(
            project_id=project.project_id,
            node_type=ExplorationNodeType.PIVOT,
            title="Pivot migrated path",
            target=EntityRef(entity_type=EntityType.CLAIM, entity_id=claim.claim_id),
            trigger="Migration smoke needs edge data.",
            rationale="Persist invalidation and dependency fields.",
            invalidates_node_id=decision.node_id,
            parent_node_ids=[decision.node_id],
            actor=actor,
        )

    with session_factory() as session:
        api = LabTrackerAPI(repository=SQLAlchemyLabTrackerRepository(session))
        assert api.get_project(project.project_id).name == "Migrated DB"
        assert api.get_question(question.question_id).project_id == project.project_id
        assert api.get_dataset(dataset.dataset_id).primary_question_id == question.question_id
        assert api.get_claim(claim.claim_id).supported_by_dataset_ids == [dataset.dataset_id]
        reloaded_decision = api.get_exploration_node(decision.node_id)
        reloaded_pivot = api.get_exploration_node(pivot.node_id)
        assert reloaded_decision.target.entity_id == question.question_id
        assert reloaded_pivot.invalidates_node_id == decision.node_id
        assert reloaded_pivot.parent_node_ids == [decision.node_id]
        exploration_node_ids = {
            node.node_id
            for node in api.list_exploration_nodes(project_id=project.project_id)
        }
        assert exploration_node_ids == {
            decision.node_id,
            pivot.node_id,
        }

    engine.dispose()


def test_restored_migrated_database_supports_api_round_trip(monkeypatch, tmp_path):
    db_path = tmp_path / "migrations-restore-source.db"
    restored_path = tmp_path / "migrations-restore-target.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    restored_database_url = f"sqlite+pysqlite:///{restored_path}"
    _upgrade_head(database_url, monkeypatch)
    actor = _actor()

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    session_factory = get_session_factory(engine=engine)

    with session_factory() as session:
        api = LabTrackerAPI(repository=SQLAlchemyLabTrackerRepository(session))
        project = api.create_project("Restored migrated DB", actor=actor)
        question = api.create_question(
            project_id=project.project_id,
            text="Does restore preserve the migrated API database?",
            question_type=QuestionType.DESCRIPTIVE,
            actor=actor,
        )
        dataset = api.create_dataset(
            project_id=project.project_id,
            primary_question_id=question.question_id,
            actor=actor,
        )

    backup = create_sqlite_backup(database_url, backup_dir=tmp_path / "backups")
    assert backup.backup_path is not None
    restore_sqlite_backup(backup.backup_path, restored_database_url)
    engine.dispose()

    restored_engine = create_engine(
        restored_database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    restored_session_factory = get_session_factory(engine=restored_engine)
    with restored_session_factory() as session:
        api = LabTrackerAPI(repository=SQLAlchemyLabTrackerRepository(session))
        assert api.get_project(project.project_id).name == "Restored migrated DB"
        assert api.get_question(question.question_id).project_id == project.project_id
        assert api.get_dataset(dataset.dataset_id).primary_question_id == question.question_id

    restored_engine.dispose()
