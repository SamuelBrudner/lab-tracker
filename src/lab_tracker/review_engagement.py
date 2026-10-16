"""Read the review funnel from human views and durable review decisions."""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from lab_tracker.db_models import GraphChangeSetModel, UsageEventModel, UserModel
from lab_tracker.sqlalchemy_mapper_parts.common import as_utc


def review_engagement(session: Session, project_id: UUID, since: datetime) -> dict[str, Any]:
    session.flush()
    rows = list(
        session.execute(
            select(
                GraphChangeSetModel.change_set_id,
                GraphChangeSetModel.review_assignee_user_id,
                GraphChangeSetModel.created_by_user_id,
                GraphChangeSetModel.status,
                GraphChangeSetModel.submitted_at,
                GraphChangeSetModel.committed_at,
                GraphChangeSetModel.reviewed_at,
                GraphChangeSetModel.review_metadata,
            ).where(
                GraphChangeSetModel.project_id == str(project_id),
                GraphChangeSetModel.draft_mode == "graph_batch",
                GraphChangeSetModel.created_at >= since,
            )
        ).mappings()
    )
    ids = [UUID(str(row["change_set_id"])) for row in rows]
    views: dict[tuple[str, str], datetime] = {}
    if ids:
        events = session.execute(
            select(
                UsageEventModel.resource_id,
                UsageEventModel.actor_user_id,
                UsageEventModel.occurred_at,
            ).where(
                UsageEventModel.project_id == project_id,
                UsageEventModel.resource_id.in_(ids),
                UsageEventModel.verb == "view",
                UsageEventModel.resource_type == "graph_change_set",
                UsageEventModel.outcome == "ok",
                UsageEventModel.principal_type.in_(["user", "device"]),
                UsageEventModel.surface == "http",
            )
        )
        for resource_id, actor_id, occurred_at in events:
            key = (str(resource_id), str(actor_id))
            instant = as_utc(occurred_at)
            views[key] = min(views.get(key, instant), instant)
    reviewer_ids = {row["review_assignee_user_id"] or row["created_by_user_id"] for row in rows}
    reviewer_ids.discard(None)
    people = {
        str(user_id): username
        for user_id, username in session.execute(
            select(UserModel.user_id, UserModel.username).where(UserModel.user_id.in_(reviewer_ids))
        )
    }
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        actor_id = str(row["review_assignee_user_id"] or row["created_by_user_id"])
        bucket = buckets.setdefault(
            actor_id,
            {
                "reviewer_user_id": None if actor_id == "None" else actor_id,
                "reviewer": people.get(actor_id, "Unassigned"),
                "assigned": 0,
                "opened": 0,
                "decisions_started": 0,
                "submitted": 0,
                "committed": 0,
                "followups_pending": 0,
                "waiting": 0,
                "first_seconds": [],
                "finish_seconds": [],
            },
        )
        metadata = row["review_metadata"] or {}
        if metadata.get("review_triage_carryover_from"):
            bucket["followups_pending"] += row["status"] in {
                "ready",
                "changes_requested",
                "submitted",
            }
            continue
        bucket["assigned"] += 1
        opened = views.get((str(row["change_set_id"]), actor_id))
        bucket["opened"] += opened is not None
        first = metadata.get("first_triage_decision_at")
        if isinstance(first, str):
            first_at = datetime.fromisoformat(first).astimezone(timezone.utc)
            bucket["decisions_started"] += 1
            if opened is not None and first_at >= opened:
                bucket["first_seconds"].append((first_at - opened).total_seconds())
        bucket["submitted"] += row["submitted_at"] is not None
        bucket["committed"] += row["committed_at"] is not None
        bucket["waiting"] += row["status"] in {"ready", "changes_requested"}
        finished = row["submitted_at"] or row["committed_at"] or row["reviewed_at"]
        if opened is not None and finished is not None and as_utc(finished) >= opened:
            bucket["finish_seconds"].append((as_utc(finished) - opened).total_seconds())
    people_rows = []
    for bucket in buckets.values():
        first = bucket.pop("first_seconds")
        finish = bucket.pop("finish_seconds")
        bucket["median_seconds_to_first_decision"] = median(first) if first else None
        bucket["median_seconds_to_finish"] = median(finish) if finish else None
        people_rows.append(bucket)
    return {
        "since": since.isoformat(),
        "reviewers": sorted(people_rows, key=lambda row: row["reviewer"]),
    }
