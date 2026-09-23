"""Username attachment on graph drafts must not hide backend failures (L91)."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import OperationalError

from lab_tracker.routes.graph_drafts import _attach_graph_usernames


def _request(get_user_by_id) -> SimpleNamespace:  # noqa: ANN001
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_service=SimpleNamespace(
                    get_user_by_id=get_user_by_id,
                )
            )
        )
    )


def _change_set(created_by: str) -> SimpleNamespace:
    return SimpleNamespace(
        created_by=created_by,
        created_by_username=None,
        review_assignee=None,
        review_assignee_username=None,
        submitted_by=None,
        submitted_by_username=None,
        reviewed_by=None,
        reviewed_by_username=None,
        committed_by=None,
        committed_by_username=None,
    )


def test_legacy_non_uuid_attribution_is_left_without_a_username() -> None:
    def never_called(_user_id: UUID) -> None:
        raise AssertionError("a legacy attribution string is not a user id")

    change_set = _attach_graph_usernames(_request(never_called), _change_set("alice"))

    assert change_set.created_by_username is None


def test_user_lookup_failures_propagate_instead_of_dropping_the_username() -> None:
    def broken_lookup(_user_id: UUID) -> None:
        raise OperationalError("SELECT", {}, Exception("database is locked"))

    with pytest.raises(OperationalError):
        _attach_graph_usernames(_request(broken_lookup), _change_set(str(uuid4())))


def test_known_user_id_gets_its_username() -> None:
    user_id = uuid4()

    def lookup(requested: UUID) -> SimpleNamespace:
        assert requested == user_id
        return SimpleNamespace(username="graph-creator")

    change_set = _attach_graph_usernames(_request(lookup), _change_set(str(user_id)))

    assert change_set.created_by_username == "graph-creator"
