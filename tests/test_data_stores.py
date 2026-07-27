from __future__ import annotations

from unittest.mock import Mock
from uuid import uuid4

import pytest
from api_helpers import (
    TEST_STORE_AUTHORITY_GRANT_ID,
    ExactCandidateTestStoreAuthority,
    repository_backed_api,
)
from sqlalchemy.exc import IntegrityError

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.config import Settings
from lab_tracker.data_store_definition import ValidatedDataStoreDefinition
from lab_tracker.db import Base, get_engine, get_session_factory
from lab_tracker.errors import (
    AuthError,
    ConflictError,
    DataStorePersistenceError,
    StoreAuthorityDeniedError,
    ValidationError,
)
from lab_tracker.models import DataStore, StoreCapability, StoreKind
from lab_tracker.repository import DataStoreInsertError, DataStoreNameRaceError
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


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
    repo.data_stores.insert(store)

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

    repo.data_stores.insert(store)

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
    unit_of_work = Mock(side_effect=AssertionError("unit of work must follow authorization"))
    reserve_write = Mock(side_effect=AssertionError("writer reservation must follow authorization"))
    monkeypatch.setattr(
        ValidatedDataStoreDefinition,
        "create",
        semantic_validation,
    )
    monkeypatch.setattr(api.data_stores, "unit_of_work", unit_of_work)
    monkeypatch.setattr(
        api._repository.data_stores,
        "reserve_registration_write",
        reserve_write,
    )

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
    reserve_write.assert_not_called()
    stores, total = api._repository.data_stores.query(project_id=project.project_id)
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

    authorization = Mock(wraps=getattr(api.project_authorization, authorization_method_name))
    unit_of_work = Mock(
        side_effect=AssertionError("invalid definitions must not enter a unit of work")
    )
    reserve_write = Mock(
        side_effect=AssertionError("invalid definitions must not reserve SQLite's writer")
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
        "reserve_registration_write",
        reserve_write,
    )
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
    reserve_write.assert_not_called()
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

    stores, total = api._repository.data_stores.query(project_id=project.project_id)
    assert stores == []
    assert total == 0


def test_authority_denial_precedes_duplicate_lookup_and_persistence(
    monkeypatch,
) -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Denied authority project", actor=actor)
    authority = ExactCandidateTestStoreAuthority()
    api._store_authority_registry = authority
    api.data_stores.store_authority_registry = authority
    duplicate_lookup = Mock(
        side_effect=AssertionError("authority denial must precede duplicate lookup")
    )
    insert = Mock(side_effect=AssertionError("authority denial must precede insert"))
    reserve_write = Mock(
        side_effect=AssertionError("authority denial must precede writer reservation")
    )
    monkeypatch.setattr(
        api._repository.data_stores,
        "scoped_store_by_name",
        duplicate_lookup,
    )
    monkeypatch.setattr(api._repository.data_stores, "insert", insert)
    monkeypatch.setattr(
        api._repository.data_stores,
        "reserve_registration_write",
        reserve_write,
    )

    with pytest.raises(
        StoreAuthorityDeniedError,
        match=r"^Data store authority is unavailable\.$",
    ):
        api.create_data_store(
            project_id=project.project_id,
            name="denied-http",
            kind=StoreKind.HTTP,
            root="https://files.example.test/approved",
            authority_grant_id="unknown-authority",
            actor=actor,
        )

    duplicate_lookup.assert_not_called()
    insert.assert_not_called()
    reserve_write.assert_not_called()


def test_registration_reserves_writer_after_authority_and_before_insert(
    monkeypatch,
) -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Ordered writer reservation", actor=actor)
    authority = ExactCandidateTestStoreAuthority()
    api._store_authority_registry = authority
    api.data_stores.store_authority_registry = authority
    events: list[str] = []
    original_authorize = authority.authorize
    original_reserve = api._repository.data_stores.reserve_registration_write
    original_insert = api._repository.data_stores.insert

    def authorize(**kwargs):
        events.append("authority")
        return original_authorize(**kwargs)

    def reserve_registration_write() -> None:
        events.append("reserve")
        original_reserve()

    def insert(store: DataStore) -> None:
        events.append("insert")
        original_insert(store)

    monkeypatch.setattr(authority, "authorize", authorize)
    monkeypatch.setattr(
        api._repository.data_stores,
        "reserve_registration_write",
        reserve_registration_write,
    )
    monkeypatch.setattr(api._repository.data_stores, "insert", insert)

    created = api.create_data_store(
        project_id=project.project_id,
        name="ordered-http",
        kind=StoreKind.HTTP,
        root="https://files.example.test/approved",
        authority_grant_id=TEST_STORE_AUTHORITY_GRANT_ID,
        actor=actor,
    )

    assert created.authority_grant_id == TEST_STORE_AUTHORITY_GRANT_ID
    assert events == ["authority", "reserve", "insert"]


def test_registration_fence_preserves_outer_transaction_rollback() -> None:
    api = repository_backed_api()
    actor = _actor()
    authority = ExactCandidateTestStoreAuthority()
    api._store_authority_registry = authority
    api.data_stores.store_authority_registry = authority
    project_id = None
    store_id = None

    with (
        pytest.raises(RuntimeError, match="rollback complete registration"),
        api.data_stores.application_transaction(),
    ):
        project = api.create_project(
            "Rolled-back registration project",
            actor=actor,
        )
        project_id = project.project_id
        store = api.create_data_store(
            project_id=project.project_id,
            name="rolled-back-http",
            kind=StoreKind.HTTP,
            root="https://files.example.test/approved",
            authority_grant_id=TEST_STORE_AUTHORITY_GRANT_ID,
            actor=actor,
        )
        store_id = store.store_id
        raise RuntimeError("rollback complete registration")

    assert project_id is not None
    assert store_id is not None
    assert api._repository.projects.get(project_id) is None
    assert api._repository.data_stores.get(store_id) is None


def test_direct_service_failure_releases_sqlite_registration_fence(
    tmp_path,
    monkeypatch,
) -> None:
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'direct-fence.db'}",
        environment="local",
        auth_enabled=False,
        _env_file=None,
    )
    engine = get_engine(settings)
    Base.metadata.create_all(bind=engine)
    session = get_session_factory(settings, engine=engine)()
    repository = SQLAlchemyLabTrackerRepository(session)
    authority = ExactCandidateTestStoreAuthority()
    api = LabTrackerAPI(
        repository=repository,
        settings=settings,
        store_authority_registry=authority,
    )
    actor = _actor()
    try:
        project = api.create_project("Direct fence release", actor=actor)

        def fail_after_fence(_store: DataStore) -> None:
            raise DataStoreInsertError

        monkeypatch.setattr(repository.data_stores, "insert", fail_after_fence)

        with pytest.raises(DataStorePersistenceError):
            api.data_stores.create_data_store(
                project_id=project.project_id,
                name="failed-http",
                kind=StoreKind.HTTP,
                root="https://files.example.test/approved",
                authority_grant_id=TEST_STORE_AUTHORITY_GRANT_ID,
                actor=actor,
            )

        driver_connection = session.connection().connection.driver_connection
        assert driver_connection.in_transaction is False
        with engine.connect() as competitor:
            competitor.exec_driver_sql("BEGIN IMMEDIATE")
            competitor.rollback()
    finally:
        session.close()
        engine.dispose()


def test_name_race_translation_discards_the_database_exception_chain(
    monkeypatch,
) -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Safe conflict project", actor=actor)
    authority = ExactCandidateTestStoreAuthority()
    api._store_authority_registry = authority
    api.data_stores.store_authority_registry = authority
    secret = "raw-insert-parameter-must-not-survive"

    def unsafe_repository_error(_entity: DataStore) -> None:
        raw_error = IntegrityError(
            "INSERT INTO data_stores (...) VALUES (...)",
            {"root": secret, "authority_grant_id": secret},
            RuntimeError("unique constraint failed"),
        )
        try:
            raise raw_error
        except IntegrityError as exc:
            raise DataStoreNameRaceError("safe repository conflict") from exc

    monkeypatch.setattr(
        api._repository.data_stores,
        "insert",
        unsafe_repository_error,
    )

    with pytest.raises(
        ConflictError,
        match=r"^A data store with this name already exists in the selected scope\.$",
    ) as captured:
        api.create_data_store(
            project_id=project.project_id,
            name="conflicting-http",
            kind=StoreKind.HTTP,
            root="https://files.example.test/approved",
            authority_grant_id=TEST_STORE_AUTHORITY_GRANT_ID,
            actor=actor,
        )

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert secret not in str(captured.value)


def test_insert_failure_translation_discards_the_database_exception_chain(
    monkeypatch,
) -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Safe internal error project", actor=actor)
    authority = ExactCandidateTestStoreAuthority()
    api._store_authority_registry = authority
    api.data_stores.store_authority_registry = authority
    secret = "raw-internal-parameter-must-not-survive"

    def unsafe_repository_error(_entity: DataStore) -> None:
        raw_error = IntegrityError(
            "INSERT INTO data_stores (...) VALUES (...)",
            {"root": secret, "authority_grant_id": secret},
            RuntimeError("unknown insert failure"),
        )
        try:
            raise raw_error
        except IntegrityError as exc:
            raise DataStoreInsertError from exc

    monkeypatch.setattr(
        api._repository.data_stores,
        "insert",
        unsafe_repository_error,
    )

    with pytest.raises(
        DataStorePersistenceError,
        match=r"^Data store registration could not be completed\.$",
    ) as captured:
        api.create_data_store(
            project_id=project.project_id,
            name="failing-http",
            kind=StoreKind.HTTP,
            root="https://files.example.test/approved",
            authority_grant_id=TEST_STORE_AUTHORITY_GRANT_ID,
            actor=actor,
        )

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert secret not in str(captured.value)


def test_direct_capability_iterable_is_bounded_before_authorization() -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Bounded capabilities project", actor=actor)
    authority = ExactCandidateTestStoreAuthority()
    api._store_authority_registry = authority
    api.data_stores.store_authority_registry = authority

    class HostileCapabilities:
        def __iter__(self):
            raise RuntimeError("must not escape")

    with pytest.raises(
        StoreAuthorityDeniedError,
        match=r"^Data store authority is unavailable\.$",
    ) as captured:
        api.create_data_store(
            project_id=project.project_id,
            name="hostile-capabilities",
            kind=StoreKind.HTTP,
            root="https://files.example.test/approved",
            capabilities=HostileCapabilities(),
            authority_grant_id=TEST_STORE_AUTHORITY_GRANT_ID,
            actor=actor,
        )

    assert captured.value.__cause__ is None


def test_oversized_direct_capability_iterable_denies_before_authorization() -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Oversized capabilities project", actor=actor)
    authority = Mock(
        authorize=Mock(
            side_effect=AssertionError("oversized capabilities must be denied before authorization")
        )
    )
    api._store_authority_registry = authority
    api.data_stores.store_authority_registry = authority

    with pytest.raises(
        StoreAuthorityDeniedError,
        match=r"^Data store authority is unavailable\.$",
    ):
        api.create_data_store(
            project_id=project.project_id,
            name="oversized-capabilities",
            kind=StoreKind.HTTP,
            root="https://files.example.test/approved",
            capabilities=[StoreCapability.BYTES_BY_PATH] * (len(StoreCapability) + 1),
            authority_grant_id=TEST_STORE_AUTHORITY_GRANT_ID,
            actor=actor,
        )

    authority.authorize.assert_not_called()
    stores, total = api._repository.data_stores.query(project_id=project.project_id)
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
    repo.data_stores.insert(s3)
    repo.data_stores.insert(onedrive)

    stores, total = repo.data_stores.query(project_id=project.project_id)
    assert total == 2
    assert {store.name for store in stores} == {"s3-archive", "lab-onedrive"}

    assert repo.data_stores.get_by_name(project.project_id, "s3-archive").store_id == (s3.store_id)
    assert repo.data_stores.get_default(project.project_id).store_id == onedrive.store_id


def test_data_store_name_is_unique_per_project():
    api = repository_backed_api()
    project = api.create_project("Store project", actor=_actor())
    repo = api._repository

    repo.data_stores.insert(_store(project.project_id, name="dup", root="s3://a"))
    with pytest.raises(DataStoreNameRaceError):
        repo.data_stores.insert(_store(project.project_id, name="dup", root="s3://b"))


def test_data_store_clear_default_keeps_one():
    api = repository_backed_api()
    project = api.create_project("Store project", actor=_actor())
    repo = api._repository

    first = _store(project.project_id, name="first", root="s3://a", is_default=True)
    second = _store(project.project_id, name="second", root="s3://b", is_default=True)
    repo.data_stores.insert(first)
    repo.data_stores.insert(second)

    repo.data_stores.clear_default(project.project_id, except_store_id=second.store_id)

    default = repo.data_stores.get_default(project.project_id)
    assert default is not None
    assert default.store_id == second.store_id
