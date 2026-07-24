from __future__ import annotations

from uuid import uuid4

import pytest
from api_helpers import repository_backed_api

from lab_tracker.auth import AuthContext, Role
from lab_tracker.db_models import UserModel
from lab_tracker.errors import AuthError, ValidationError
from lab_tracker.models import GroupMembership, ProjectGroup, ProjectMembershipRole


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


def _create_group(api, *, name: str = "Policy group", group_read_all: bool = False):
    group = ProjectGroup(group_id=uuid4(), name=name, group_read_all=group_read_all)
    api._repository.project_groups.save(group)  # type: ignore[attr-defined]
    api._repository.commit()  # type: ignore[attr-defined]
    return group


def _assign_project_group(api, project, group):
    grouped_project = project.model_copy(update={"group_id": group.group_id})
    api._repository.projects.save(grouped_project)  # type: ignore[attr-defined]
    api._repository.commit()  # type: ignore[attr-defined]
    return grouped_project


def _add_group_membership(api, group, actor: AuthContext, role: ProjectMembershipRole):
    membership = GroupMembership(
        membership_id=uuid4(),
        group_id=group.group_id,
        user_id=actor.user_id,
        role=role,
    )
    api._repository.group_memberships.save(membership)  # type: ignore[attr-defined]
    api._repository.commit()  # type: ignore[attr-defined]
    return membership


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


def test_project_owner_membership_can_update_and_delete_project() -> None:
    api, admin, project = _project_with_admin()
    owner = _registered_actor(api, Role.VIEWER)
    api.upsert_project_membership(
        project.project_id,
        owner.user_id,
        ProjectMembershipRole.OWNER,
        actor=admin,
    )

    updated = api.update_project(project.project_id, name="Owned project", actor=owner)

    assert updated.name == "Owned project"

    deletable = api.create_project("Owned deletion project", actor=admin)
    api.upsert_project_membership(
        deletable.project_id,
        owner.user_id,
        ProjectMembershipRole.OWNER,
        actor=admin,
    )

    deleted = api.delete_project(deletable.project_id, actor=owner)

    assert deleted.project_id == deletable.project_id


def test_global_editor_without_project_membership_cannot_update_project() -> None:
    api, _, project = _project_with_admin()
    editor = _registered_actor(api, Role.EDITOR)

    with pytest.raises(AuthError, match="Project owner access required."):
        api.update_project(project.project_id, name="Forbidden", actor=editor)


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


def test_read_predicates_are_non_throwing_for_absent_and_unrelated_actors() -> None:
    api, admin, project = _project_with_admin()
    project_member = _registered_actor(api, Role.VIEWER)
    unrelated = _registered_actor(api, Role.VIEWER)
    api.upsert_project_membership(
        project.project_id,
        project_member.user_id,
        ProjectMembershipRole.VIEWER,
        actor=admin,
    )
    group = _create_group(api)
    group_member = _registered_actor(api, Role.VIEWER)
    _add_group_membership(
        api,
        group,
        group_member,
        ProjectMembershipRole.VIEWER,
    )

    assert api.project_authorization.can_read(project.project_id, actor=admin) is True
    assert (
        api.project_authorization.can_read(project.project_id, actor=project_member)
        is True
    )
    assert api.project_authorization.can_read(project.project_id, actor=unrelated) is False
    assert api.project_authorization.can_read(project.project_id, actor=None) is False

    assert api.project_authorization.can_group_read(group.group_id, actor=admin) is True
    assert (
        api.project_authorization.can_group_read(group.group_id, actor=group_member)
        is True
    )
    assert (
        api.project_authorization.can_group_read(group.group_id, actor=unrelated)
        is False
    )
    assert api.project_authorization.can_group_read(group.group_id, actor=None) is False


def test_group_owner_inherits_owner_access_to_child_projects() -> None:
    api, admin, first_project = _project_with_admin()
    second_project = api.create_project("Second grouped project", actor=admin)
    group = _create_group(api)
    first_project = _assign_project_group(api, first_project, group)
    second_project = _assign_project_group(api, second_project, group)
    group_owner = _registered_actor(api, Role.VIEWER)
    _add_group_membership(api, group, group_owner, ProjectMembershipRole.OWNER)

    assert api.project_authorization.accessible_project_ids(group_owner) == {
        first_project.project_id,
        second_project.project_id,
    }
    assert (
        api.project_authorization.membership_role(first_project.project_id, group_owner)
        == ProjectMembershipRole.OWNER
    )
    assert (
        api.project_authorization.can_read(first_project.project_id, actor=group_owner)
        is True
    )
    api.project_authorization.require_read(first_project.project_id, actor=group_owner)
    api.project_authorization.require_contributor(first_project.project_id, actor=group_owner)
    api.project_authorization.require_owner(first_project.project_id, actor=group_owner)
    updated = api.update_project(
        second_project.project_id,
        name="Updated through group ownership",
        actor=group_owner,
    )
    assert updated.name == "Updated through group ownership"


def test_group_viewer_and_contributor_do_not_inherit_project_read_by_default() -> None:
    api, admin, project = _project_with_admin()
    group = _create_group(api, group_read_all=False)
    project = _assign_project_group(api, project, group)
    viewer = _registered_actor(api, Role.VIEWER)
    contributor = _registered_actor(api, Role.VIEWER)
    _add_group_membership(api, group, viewer, ProjectMembershipRole.VIEWER)
    _add_group_membership(api, group, contributor, ProjectMembershipRole.CONTRIBUTOR)

    for actor in (viewer, contributor):
        assert api.project_authorization.accessible_project_ids(actor) == set()
        assert api.project_authorization.membership_role(project.project_id, actor) is None
        with pytest.raises(AuthError, match="Project access required."):
            api.project_authorization.require_read(project.project_id, actor=actor)


def test_group_read_all_grants_read_only_access_to_non_owner_group_members() -> None:
    api, admin, project = _project_with_admin()
    group = _create_group(api, group_read_all=True)
    project = _assign_project_group(api, project, group)
    viewer = _registered_actor(api, Role.VIEWER)
    contributor = _registered_actor(api, Role.VIEWER)
    _add_group_membership(api, group, viewer, ProjectMembershipRole.VIEWER)
    _add_group_membership(api, group, contributor, ProjectMembershipRole.CONTRIBUTOR)

    for actor in (viewer, contributor):
        assert api.project_authorization.accessible_project_ids(actor) == {project.project_id}
        assert (
            api.project_authorization.membership_role(project.project_id, actor)
            == ProjectMembershipRole.VIEWER
        )
        assert api.project_authorization.can_read(project.project_id, actor=actor) is True
        api.project_authorization.require_read(project.project_id, actor=actor)
        with pytest.raises(AuthError, match="Project contributor access required."):
            api.project_authorization.require_contributor(project.project_id, actor=actor)
        with pytest.raises(AuthError, match="Project owner access required."):
            api.project_authorization.require_owner(project.project_id, actor=actor)


def test_direct_project_membership_takes_precedence_over_group_inheritance() -> None:
    api, admin, project = _project_with_admin()
    group = _create_group(api)
    project = _assign_project_group(api, project, group)
    group_owner = _registered_actor(api, Role.VIEWER)
    _add_group_membership(api, group, group_owner, ProjectMembershipRole.OWNER)
    api.upsert_project_membership(
        project.project_id,
        group_owner.user_id,
        ProjectMembershipRole.VIEWER,
        actor=admin,
    )

    assert (
        api.project_authorization.membership_role(project.project_id, group_owner)
        == ProjectMembershipRole.VIEWER
    )
    api.project_authorization.require_read(project.project_id, actor=group_owner)
    with pytest.raises(AuthError, match="Project owner access required."):
        api.project_authorization.require_owner(project.project_id, actor=group_owner)


def test_group_owner_cannot_remove_the_last_direct_project_owner() -> None:
    api, admin, project = _project_with_admin()
    group = _create_group(api)
    project = _assign_project_group(api, project, group)
    group_owner = _registered_actor(api, Role.VIEWER)
    direct_owner = _registered_actor(api, Role.VIEWER)
    _add_group_membership(api, group, group_owner, ProjectMembershipRole.OWNER)
    api.upsert_project_membership(
        project.project_id,
        direct_owner.user_id,
        ProjectMembershipRole.OWNER,
        actor=admin,
    )

    with pytest.raises(ValidationError, match="Projects must keep at least one owner."):
        api.delete_project_membership(
            project.project_id,
            direct_owner.user_id,
            actor=group_owner,
        )


def test_cannot_demote_the_last_direct_project_owner() -> None:
    api, admin, project = _project_with_admin()
    owner = _registered_actor(api, Role.VIEWER)
    api.upsert_project_membership(
        project.project_id,
        owner.user_id,
        ProjectMembershipRole.OWNER,
        actor=admin,
    )

    with pytest.raises(ValidationError, match="Projects must keep at least one owner."):
        api.upsert_project_membership(
            project.project_id,
            owner.user_id,
            ProjectMembershipRole.VIEWER,
            actor=owner,
        )

    membership = api.get_project_membership_for_user(project.project_id, owner.user_id)
    assert membership is not None
    assert membership.role == ProjectMembershipRole.OWNER


def test_can_demote_project_owner_when_another_direct_owner_remains() -> None:
    api, admin, project = _project_with_admin()
    demoted_owner = _registered_actor(api, Role.VIEWER)
    retained_owner = _registered_actor(api, Role.VIEWER)
    api.upsert_project_membership(
        project.project_id,
        demoted_owner.user_id,
        ProjectMembershipRole.OWNER,
        actor=admin,
    )
    api.upsert_project_membership(
        project.project_id,
        retained_owner.user_id,
        ProjectMembershipRole.OWNER,
        actor=admin,
    )

    membership = api.upsert_project_membership(
        project.project_id,
        demoted_owner.user_id,
        ProjectMembershipRole.CONTRIBUTOR,
        actor=demoted_owner,
    )

    retained_membership = api.get_project_membership_for_user(
        project.project_id,
        retained_owner.user_id,
    )
    assert membership.role == ProjectMembershipRole.CONTRIBUTOR
    assert retained_membership is not None
    assert retained_membership.role == ProjectMembershipRole.OWNER
