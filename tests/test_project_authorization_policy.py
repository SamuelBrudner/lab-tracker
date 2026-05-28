from __future__ import annotations

from uuid import uuid4

import pytest

from api_helpers import repository_backed_api
from lab_tracker.auth import AuthContext, Role
from lab_tracker.db_models import UserModel
from lab_tracker.errors import AuthError
from lab_tracker.models import ProjectMembershipRole


def _actor(role: Role = Role.VIEWER) -> AuthContext:
    return AuthContext(user_id=uuid4(), role=role)


def _registered_actor(api, role: Role = Role.VIEWER) -> AuthContext:
    actor = _actor(role)
    _, session = api._test_resources  # type: ignore[attr-defined]
    session.add(
        UserModel(
            user_id=str(actor.user_id),
            username=f"user-{actor.user_id.hex}",
            password_hash="unused",
            role=role.value,
        )
    )
    session.commit()
    return actor


def _project_with_admin():
    api = repository_backed_api()
    admin = _actor(Role.ADMIN)
    project = api.create_project("Policy project", actor=admin)
    return api, admin, project


def test_admin_has_global_project_access_without_membership() -> None:
    api, admin, project = _project_with_admin()
    policy = api.project_authorization

    assert policy.accessible_project_ids(admin) is None
    assert policy.membership_role(project.project_id, admin) == ProjectMembershipRole.OWNER
    policy.require_read(project.project_id, actor=admin)
    policy.require_contributor(project.project_id, actor=admin)
    policy.require_owner(project.project_id, actor=admin)


def test_viewer_membership_can_read_but_not_contribute_or_own() -> None:
    api, admin, project = _project_with_admin()
    viewer = _registered_actor(api, Role.VIEWER)
    api.upsert_project_membership(
        project.project_id,
        viewer.user_id,
        ProjectMembershipRole.VIEWER,
        actor=admin,
    )

    assert api.project_authorization.accessible_project_ids(viewer) == {project.project_id}
    assert (
        api.project_authorization.membership_role(project.project_id, viewer)
        == ProjectMembershipRole.VIEWER
    )
    api.project_authorization.require_read(project.project_id, actor=viewer)
    with pytest.raises(AuthError, match="Project contributor access required."):
        api.project_authorization.require_contributor(project.project_id, actor=viewer)
    with pytest.raises(AuthError, match="Project owner access required."):
        api.project_authorization.require_owner(project.project_id, actor=viewer)


def test_contributor_membership_can_read_and_contribute_but_not_own() -> None:
    api, admin, project = _project_with_admin()
    contributor = _registered_actor(api, Role.VIEWER)
    api.upsert_project_membership(
        project.project_id,
        contributor.user_id,
        ProjectMembershipRole.CONTRIBUTOR,
        actor=admin,
    )

    assert (
        api.project_authorization.membership_role(project.project_id, contributor)
        == ProjectMembershipRole.CONTRIBUTOR
    )
    api.project_authorization.require_read(project.project_id, actor=contributor)
    api.project_authorization.require_contributor(project.project_id, actor=contributor)
    with pytest.raises(AuthError, match="Project owner access required."):
        api.project_authorization.require_owner(project.project_id, actor=contributor)


def test_owner_membership_can_read_contribute_and_own() -> None:
    api, admin, project = _project_with_admin()
    owner = _registered_actor(api, Role.VIEWER)
    api.upsert_project_membership(
        project.project_id,
        owner.user_id,
        ProjectMembershipRole.OWNER,
        actor=admin,
    )

    assert (
        api.project_authorization.membership_role(project.project_id, owner)
        == ProjectMembershipRole.OWNER
    )
    api.project_authorization.require_read(project.project_id, actor=owner)
    api.project_authorization.require_contributor(project.project_id, actor=owner)
    api.project_authorization.require_owner(project.project_id, actor=owner)


def test_unauthenticated_and_unrelated_actors_are_denied() -> None:
    api, _, project = _project_with_admin()
    unrelated = _registered_actor(api, Role.VIEWER)

    with pytest.raises(AuthError, match="Authentication required."):
        api.project_authorization.accessible_project_ids(None)
    with pytest.raises(AuthError, match="Authentication required."):
        api.project_authorization.require_read(project.project_id, actor=None)

    assert api.project_authorization.accessible_project_ids(unrelated) == set()
    assert api.project_authorization.membership_role(project.project_id, unrelated) is None
    with pytest.raises(AuthError, match="Project access required."):
        api.project_authorization.require_read(project.project_id, actor=unrelated)
    with pytest.raises(AuthError, match="Project contributor access required."):
        api.project_authorization.require_contributor(project.project_id, actor=unrelated)
    with pytest.raises(AuthError, match="Project owner access required."):
        api.project_authorization.require_owner(project.project_id, actor=unrelated)
