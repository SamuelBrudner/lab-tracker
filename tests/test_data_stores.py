from __future__ import annotations

from unittest.mock import Mock
from uuid import uuid4

import pytest
from api_helpers import repository_backed_api
from sqlalchemy.exc import IntegrityError

from lab_tracker.auth import AuthContext, Role
from lab_tracker.data_store_definition import ValidatedDataStoreDefinition
from lab_tracker.errors import AuthError, ValidationError
from lab_tracker.models import DataStore, StoreCapability, StoreKind


def _actor() -> AuthContext:
    return AuthContext(user_id=uuid4(), role=Role.ADMIN)


def _store(project_id, **overrides) -> DataStore:
    fields = {
        "store_id": uuid4(),
        "project_id": project_id,
        "name": "lab-onedrive",
        "kind": StoreKind.ONEDRIVE,
        "capabilities": [StoreCapability.BYTES_BY_PATH, StoreCapability.BYTE_RANGE],
        "root": "/OneDrive/experiments",
        "credential_ref": "onedrive-remote",
        "is_default": False,
    }
    fields.update(overrides)
    return DataStore(**fields)


def test_data_store_round_trips_through_repository():
    api = repository_backed_api()
    project = api.create_project("Store project", actor=_actor())
    repo = api._repository

    store = _store(project.project_id, is_default=True)
    repo.data_stores.save(store)

    reloaded = repo.data_stores.get(store.store_id)
    assert reloaded is not None
    assert reloaded.name == "lab-onedrive"
    assert reloaded.kind is StoreKind.ONEDRIVE
    assert reloaded.capabilities == [
        StoreCapability.BYTES_BY_PATH,
        StoreCapability.BYTE_RANGE,
    ]
    assert reloaded.root == "/OneDrive/experiments"
    assert reloaded.credential_ref == "onedrive-remote"
    assert reloaded.is_default is True


@pytest.mark.parametrize(
    "overrides",
    (
        {
            "name": " padded legacy name ",
            "kind": StoreKind.HTTP,
            "root": "https://operator:legacy-secret@files.example/private",
        },
        {
            "name": "legacy-endpoint",
            "kind": StoreKind.LOCAL_FS,
            "root": "/legacy/root",
            "endpoint": "https://dead.example/",
        },
        {
            "name": "legacy-object-table",
            "kind": StoreKind.OBJECT_TABLE,
            "root": "legacy_table",
        },
    ),
)
def test_invalid_legacy_data_store_rows_remain_hydratable(overrides):
    api = repository_backed_api()
    project = api.create_project("Legacy store project", actor=_actor())
    repo = api._repository
    store = _store(project.project_id, **overrides)

    repo.data_stores.save(store)

    reloaded = repo.data_stores.get(store.store_id)
    assert reloaded is not None
    assert reloaded.name == store.name
    assert reloaded.kind is store.kind
    assert reloaded.root == store.root
    assert reloaded.endpoint == store.endpoint
    assert reloaded.credential_ref == store.credential_ref


def test_direct_create_authorizes_before_definition_validation(monkeypatch):
    api = repository_backed_api()
    project = api.create_project("Protected store project", actor=_actor())
    viewer = AuthContext(user_id=uuid4(), role=Role.VIEWER)
    semantic_validation = Mock(
        side_effect=AssertionError("semantic validation must follow authorization")
    )
    unit_of_work = Mock(
        side_effect=AssertionError("unit of work must follow authorization")
    )
    monkeypatch.setattr(
        ValidatedDataStoreDefinition,
        "create",
        semantic_validation,
    )
    monkeypatch.setattr(api.data_stores, "unit_of_work", unit_of_work)

    with pytest.raises(AuthError, match="Project contributor access required"):
        api.create_data_store(
            project_id=project.project_id,
            name=" padded ",
            kind=StoreKind.HTTP,
            root="https://operator:secret@files.example/private",
            actor=viewer,
        )

    semantic_validation.assert_not_called()
    unit_of_work.assert_not_called()
    stores, total = api._repository.data_stores.query(
        project_id=project.project_id
    )
    assert stores == []
    assert total == 0


@pytest.mark.parametrize("scope", ("project", "group"))
def test_direct_create_validates_after_authorization_but_before_repository_writes(
    monkeypatch,
    scope,
):
    api = repository_backed_api()
    actor = _actor()
    if scope == "project":
        scope_id = api.create_project(
            "Invalid store project",
            actor=actor,
        ).project_id
        scope_arguments = {"project_id": scope_id}
        authorization_method_name = "require_contributor"
    else:
        scope_id = api.create_project_group(
            "Invalid store group",
            actor=actor,
        ).group_id
        scope_arguments = {"group_id": scope_id}
        authorization_method_name = "require_group_owner"

    authorization = Mock(
        wraps=getattr(api.project_authorization, authorization_method_name)
    )
    unit_of_work = Mock(
        side_effect=AssertionError("invalid definitions must not enter a unit of work")
    )
    scoped_store_by_name = Mock(
        side_effect=AssertionError("invalid definitions must not check duplicates")
    )
    save = Mock(side_effect=AssertionError("invalid definitions must not be saved"))
    monkeypatch.setattr(
        api.project_authorization,
        authorization_method_name,
        authorization,
    )
    monkeypatch.setattr(api.data_stores, "unit_of_work", unit_of_work)
    monkeypatch.setattr(
        api._repository.data_stores,
        "scoped_store_by_name",
        scoped_store_by_name,
    )
    monkeypatch.setattr(api._repository.data_stores, "save", save)
    secret = "direct-service-secret-must-not-leak"

    with pytest.raises(ValidationError) as exc_info:
        api.create_data_store(
            **scope_arguments,
            name="remote-http",
            kind=StoreKind.HTTP,
            root=f"https://operator:{secret}@files.example/private",
            actor=actor,
        )

    assert secret not in str(exc_info.value)
    authorization.assert_called_once_with(scope_id, actor=actor)
    unit_of_work.assert_not_called()
    scoped_store_by_name.assert_not_called()
    save.assert_not_called()


def test_direct_create_rejects_non_utf8_local_root_before_persistence():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Malformed local root project", actor=actor)

    with pytest.raises(ValidationError, match="Data store root is invalid"):
        api.create_data_store(
            project_id=project.project_id,
            name="local",
            kind=StoreKind.LOCAL_FS,
            root="/tmp/malformed-\udcff-root",
            actor=actor,
        )

    stores, total = api._repository.data_stores.query(
        project_id=project.project_id
    )
    assert stores == []
    assert total == 0


def test_data_store_query_default_and_name_lookup():
    api = repository_backed_api()
    project = api.create_project("Store project", actor=_actor())
    repo = api._repository

    s3 = _store(
        project.project_id,
        name="s3-archive",
        kind=StoreKind.S3,
        capabilities=[StoreCapability.VERSIONED_SNAPSHOT],
        root="s3://lab-archive",
        is_default=False,
    )
    onedrive = _store(project.project_id, name="lab-onedrive", is_default=True)
    repo.data_stores.save(s3)
    repo.data_stores.save(onedrive)

    stores, total = repo.data_stores.query(project_id=project.project_id)
    assert total == 2
    assert {store.name for store in stores} == {"s3-archive", "lab-onedrive"}

    assert repo.data_stores.get_by_name(project.project_id, "s3-archive").store_id == (
        s3.store_id
    )
    assert repo.data_stores.get_default(project.project_id).store_id == onedrive.store_id


def test_data_store_name_is_unique_per_project():
    api = repository_backed_api()
    project = api.create_project("Store project", actor=_actor())
    repo = api._repository

    repo.data_stores.save(_store(project.project_id, name="dup", root="s3://a"))
    with pytest.raises(IntegrityError):
        repo.data_stores.save(_store(project.project_id, name="dup", root="s3://b"))


def test_data_store_clear_default_keeps_one():
    api = repository_backed_api()
    project = api.create_project("Store project", actor=_actor())
    repo = api._repository

    first = _store(project.project_id, name="first", root="s3://a", is_default=True)
    second = _store(project.project_id, name="second", root="s3://b", is_default=True)
    repo.data_stores.save(first)
    repo.data_stores.save(second)

    repo.data_stores.clear_default(project.project_id, except_store_id=second.store_id)

    default = repo.data_stores.get_default(project.project_id)
    assert default is not None
    assert default.store_id == second.store_id
