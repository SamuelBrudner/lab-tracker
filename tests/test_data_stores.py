from __future__ import annotations

from uuid import uuid4

import pytest
from api_helpers import repository_backed_api
from sqlalchemy.exc import IntegrityError

from lab_tracker.auth import AuthContext, Role
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
