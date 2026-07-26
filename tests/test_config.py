from __future__ import annotations

import asyncio
import gc
import os
import stat
import traceback
import warnings
from pathlib import Path
from uuid import UUID

import pytest
from api_helpers import drain_test_resources, register_test_resources
from fastapi import FastAPI
from pydantic import ValidationError
from starlette.testclient import TestClient

from lab_tracker.app_parts import runtime as runtime_module
from lab_tracker.app_parts.runtime import (
    build_app_runtime,
    configure_app_state,
    make_lifespan,
)
from lab_tracker.artifact_resolution import (
    ResolverRegistry,
    outbound_http_policy_from_config,
)
from lab_tracker.artifact_resolution_admission import (
    DEFAULT_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT,
    DEFAULT_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT,
)
from lab_tracker.config import DEFAULT_AUTH_SECRET_KEY, Settings
from lab_tracker.local_resolution_budget import (
    DEFAULT_LOCAL_RECOVERY_MAX_DIRECTORIES,
    DEFAULT_LOCAL_RECOVERY_MAX_FILES,
    MAX_LOCAL_RECOVERY_MAX_DIRECTORIES,
    MAX_LOCAL_RECOVERY_MAX_FILES,
    MAX_LOCAL_RESOLUTION_MAX_READ_BYTES,
)
from lab_tracker.models import StoreKind
from lab_tracker.store_health import (
    DEFAULT_STORE_HEALTH_CACHE_MAX_ENTRIES,
    DEFAULT_STORE_HEALTH_CACHE_TTL_SECONDS,
    DEFAULT_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS,
    StoreHealth,
    StoreHealthStatus,
    StoreProbeTarget,
)
from lab_tracker.store_health_admission import (
    DEFAULT_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT,
    DEFAULT_STORE_HEALTH_PER_ACTOR_IN_FLIGHT_LIMIT,
)


def _settings_from_environment() -> Settings:
    return Settings(_env_file=None)


def _clear_auth_env(monkeypatch) -> None:
    monkeypatch.delenv("LAB_TRACKER_AUTH_SECRET_KEY", raising=False)
    monkeypatch.delenv("LAB_TRACKER_ENVIRONMENT", raising=False)
    monkeypatch.delenv("LAB_TRACKER_AUTH_ENABLED", raising=False)
    monkeypatch.delenv(
        "LAB_TRACKER_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT",
        raising=False,
    )
    monkeypatch.delenv(
        "LAB_TRACKER_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT",
        raising=False,
    )
    for variable in (
        "LAB_TRACKER_RESOLVER_ALLOWED_ROOTS",
        "LAB_TRACKER_RESOLVER_RECOVERY",
        "LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES",
        "LAB_TRACKER_RESOLVER_RECOVERY_MAX_DIRECTORIES",
        "LAB_TRACKER_RESOLVER_RECOVERY_MAX_BYTES",
        "LAB_TRACKER_STORE_AUTHORITY_GRANTS_JSON",
        "LAB_TRACKER_RCLONE_ALLOWED_REMOTES",
        "LAB_TRACKER_GIT_ALLOWED_REMOTES",
        "LAB_TRACKER_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT",
        "LAB_TRACKER_STORE_HEALTH_PER_ACTOR_IN_FLIGHT_LIMIT",
        "LAB_TRACKER_STORE_HEALTH_CACHE_MAX_ENTRIES",
        "LAB_TRACKER_STORE_HEALTH_CACHE_TTL_SECONDS",
        "LAB_TRACKER_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS",
    ):
        monkeypatch.delenv(variable, raising=False)


def test_local_environment_allows_default_auth_secret(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    settings = _settings_from_environment()
    assert settings.auth_secret_key == DEFAULT_AUTH_SECRET_KEY
    assert settings.is_auth_enabled() is False
    assert settings.is_usage_events_enabled() is False


def test_local_environment_rejects_default_auth_secret_when_auth_enabled(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "true")
    with pytest.raises(ValidationError, match="strong non-placeholder"):
        _settings_from_environment()


def test_local_environment_can_enable_auth_with_custom_secret(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "true")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "custom-local-secret")
    settings = _settings_from_environment()
    assert settings.is_auth_enabled() is True


def test_dotenv_ignores_non_lab_tracker_keys(tmp_path, monkeypatch):
    _clear_auth_env(monkeypatch)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "POSTGRES_DB=lab_tracker\n"
        "LAB_TRACKER_ENVIRONMENT=local\n"
        "LAB_TRACKER_OPENAI_MODEL=gpt-test\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=dotenv_path)

    assert settings.environment == "local"
    assert settings.openai_model == "gpt-test"


def test_store_authority_grants_default_to_deny_all_input(monkeypatch):
    _clear_auth_env(monkeypatch)

    assert _settings_from_environment().store_authority_grants_json == ""


def test_store_authority_grants_load_exact_environment_value(monkeypatch):
    _clear_auth_env(monkeypatch)
    configured = '{"schema":"lab-tracker/store-authority/v1","grants":[]}'
    monkeypatch.setenv("LAB_TRACKER_STORE_AUTHORITY_GRANTS_JSON", configured)

    assert _settings_from_environment().store_authority_grants_json == configured


def test_store_authority_grants_load_exact_dotenv_value(tmp_path, monkeypatch):
    _clear_auth_env(monkeypatch)
    configured = '{"schema":"lab-tracker/store-authority/v1","grants":[]}'
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        f"LAB_TRACKER_STORE_AUTHORITY_GRANTS_JSON='{configured}'\n",
        encoding="utf-8",
    )

    assert Settings(_env_file=dotenv_path).store_authority_grants_json == configured


def test_store_authority_grants_are_hidden_from_settings_rendering():
    secret = "sag-sensitive-sentinel-7f94d128"
    settings = Settings(
        _env_file=None,
        store_authority_grants_json=secret,
    )

    assert secret not in repr(settings)
    assert secret not in str(settings)
    assert "store_authority_grants_json" not in settings.model_dump()
    assert "store_authority_grants_json" not in settings.model_dump_json()


def test_store_authority_grants_are_hidden_from_other_settings_validation_errors(
    caplog,
):
    secret = "sag-validation-secret-7f94d128"

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            environment="production",
            auth_enabled=False,
            store_authority_grants_json=secret,
        )

    error = exc_info.value
    rendered = "".join(
        traceback.format_exception(
            type(error),
            error,
            error.__traceback__,
        )
    )
    assert secret not in str(error)
    assert secret not in repr(error)
    assert secret not in rendered
    assert secret not in caplog.text


def test_dotenv_loads_resolver_settings(tmp_path, monkeypatch):
    _clear_auth_env(monkeypatch)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "LAB_TRACKER_ENVIRONMENT=local\n"
        f"LAB_TRACKER_RESOLVER_ALLOWED_ROOTS={tmp_path}\n"
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES=http://10.20.1.7\n"
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS=10.20.0.0/16\n"
        "LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS=12.5\n"
        "LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS=7.25\n"
        "LAB_TRACKER_RESOLVER_RECOVERY=true\n"
        "LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES=23\n"
        "LAB_TRACKER_RESOLVER_RECOVERY_MAX_DIRECTORIES=29\n"
        "LAB_TRACKER_RESOLVER_RECOVERY_MAX_BYTES=1048576\n"
        "LAB_TRACKER_RCLONE_ALLOWED_REMOTES=lab-onedrive,archive-s3\n"
        "LAB_TRACKER_GIT_ALLOWED_REMOTES=https://git.example/lab\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=dotenv_path)
    policy = outbound_http_policy_from_config(
        allowed_authorities=settings.resolver_http_allowed_authorities,
        allowed_networks=settings.resolver_http_allowed_networks,
    )

    assert policy.authorize("http://10.20.1.7/artifact.bin").hostname == "10.20.1.7"
    assert settings.resolver_allowed_roots == str(tmp_path)
    assert settings.resolver_http_deadline_seconds == 12.5
    assert settings.resolver_subprocess_deadline_seconds == 7.25
    assert settings.resolver_recovery is True
    assert settings.resolver_recovery_max_files == 23
    assert settings.resolver_recovery_max_directories == 29
    assert settings.resolver_recovery_max_bytes == 1_048_576
    assert settings.rclone_allowed_remotes == "lab-onedrive,archive-s3"
    assert settings.git_allowed_remotes == "https://git.example/lab"


def test_rclone_remote_policy_defaults_to_deny_all(monkeypatch):
    _clear_auth_env(monkeypatch)

    assert _settings_from_environment().rclone_allowed_remotes == ""


def test_local_filesystem_authority_defaults_to_deny_all(monkeypatch):
    _clear_auth_env(monkeypatch)

    assert _settings_from_environment().resolver_allowed_roots == ""


def test_git_remote_policy_defaults_to_deny_all(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.delenv("LAB_TRACKER_GIT_ALLOWED_REMOTES", raising=False)

    assert _settings_from_environment().git_allowed_remotes == ""


def test_resolver_http_deadline_defaults_to_thirty_seconds(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.delenv("LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS", raising=False)

    assert _settings_from_environment().resolver_http_deadline_seconds == 30.0


def test_local_resolution_controls_have_fail_closed_defaults(monkeypatch):
    _clear_auth_env(monkeypatch)

    settings = _settings_from_environment()

    assert settings.resolver_recovery is False
    assert settings.resolver_recovery_max_files == DEFAULT_LOCAL_RECOVERY_MAX_FILES
    assert (
        settings.resolver_recovery_max_directories
        == DEFAULT_LOCAL_RECOVERY_MAX_DIRECTORIES
    )
    assert settings.resolver_recovery_max_bytes == MAX_LOCAL_RESOLUTION_MAX_READ_BYTES


def test_runtime_rejects_enabled_recovery_roots_that_exceed_one_helper_request():
    if os.name == "nt":
        roots = [
            rf"C:\{'a' * 200}\{index:02d}{'b' * 190}"
            for index in range(64)
        ]
    else:
        roots = [
            f"/{'a' * 200}/{index:02d}{'b' * 190}"
            for index in range(64)
        ]
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        resolver_allowed_roots=os.pathsep.join(roots),
        resolver_recovery=True,
    )

    with pytest.raises(
        ValueError,
        match="roots exceed the bounded helper request limit",
    ):
        build_app_runtime(settings)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("LAB_TRACKER_RESOLVER_RECOVERY", "sometimes"),
        ("LAB_TRACKER_RESOLVER_RECOVERY", "2"),
        ("LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES", "0"),
        ("LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES", "-1"),
        ("LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES", "1.5"),
        (
            "LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES",
            str(MAX_LOCAL_RECOVERY_MAX_FILES + 1),
        ),
        ("LAB_TRACKER_RESOLVER_RECOVERY_MAX_DIRECTORIES", "0"),
        ("LAB_TRACKER_RESOLVER_RECOVERY_MAX_DIRECTORIES", "-1"),
        ("LAB_TRACKER_RESOLVER_RECOVERY_MAX_DIRECTORIES", "1.5"),
        (
            "LAB_TRACKER_RESOLVER_RECOVERY_MAX_DIRECTORIES",
            str(MAX_LOCAL_RECOVERY_MAX_DIRECTORIES + 1),
        ),
        ("LAB_TRACKER_RESOLVER_RECOVERY_MAX_BYTES", "0"),
        ("LAB_TRACKER_RESOLVER_RECOVERY_MAX_BYTES", "true"),
        (
            "LAB_TRACKER_RESOLVER_RECOVERY_MAX_BYTES",
            str(MAX_LOCAL_RESOLUTION_MAX_READ_BYTES + 1),
        ),
    ],
)
def test_invalid_local_resolution_controls_fail_settings(
    monkeypatch,
    variable,
    value,
):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError, match="RESOLVER_RECOVERY|recovery limits"):
        _settings_from_environment()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resolver_recovery", 1),
        ("resolver_recovery_max_files", True),
        ("resolver_recovery_max_files", 1.5),
        ("resolver_recovery_max_directories", True),
        ("resolver_recovery_max_directories", 1.5),
        ("resolver_recovery_max_bytes", False),
        ("resolver_recovery_max_bytes", 1.5),
    ],
)
def test_local_resolution_controls_reject_coercible_python_values(field, value):
    with pytest.raises(ValidationError, match="RESOLVER_RECOVERY|recovery limits"):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_resolver_http_deadline_must_be_finite_and_positive(monkeypatch, value):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS", value)

    with pytest.raises(ValidationError, match="must be finite and greater than 0"):
        _settings_from_environment()


def test_resolver_http_deadline_cannot_exceed_one_day(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS", "86400.0001")

    with pytest.raises(
        ValidationError,
        match="LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS",
    ):
        _settings_from_environment()


def test_resolver_subprocess_deadline_defaults_to_thirty_seconds(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.delenv(
        "LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS",
        raising=False,
    )

    assert _settings_from_environment().resolver_subprocess_deadline_seconds == 30.0


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_resolver_subprocess_deadline_must_be_finite_and_positive(
    monkeypatch,
    value,
):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS", value)

    with pytest.raises(ValidationError, match="must be finite and greater than 0"):
        _settings_from_environment()


def test_resolver_subprocess_deadline_cannot_exceed_one_day(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv(
        "LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS",
        "86400.0001",
    )

    with pytest.raises(
        ValidationError,
        match="LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS",
    ):
        _settings_from_environment()


def test_artifact_resolution_admission_limits_default_to_bounded_capacity(monkeypatch):
    _clear_auth_env(monkeypatch)

    settings = _settings_from_environment()

    assert (
        settings.artifact_resolution_global_in_flight_limit
        == DEFAULT_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT
    )
    assert (
        settings.artifact_resolution_per_actor_in_flight_limit
        == DEFAULT_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT
    )


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("LAB_TRACKER_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT", "0"),
        ("LAB_TRACKER_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT", "33"),
        ("LAB_TRACKER_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT", "0"),
    ],
)
def test_artifact_resolution_admission_limits_are_validated(monkeypatch, variable, value):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError, match="ARTIFACT_RESOLUTION"):
        _settings_from_environment()


def test_artifact_resolution_actor_limit_cannot_exceed_global_limit(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT", "2")
    monkeypatch.setenv("LAB_TRACKER_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT", "3")

    with pytest.raises(ValidationError, match="PER_ACTOR_IN_FLIGHT_LIMIT"):
        _settings_from_environment()


def test_store_health_control_plane_defaults_are_bounded(monkeypatch):
    _clear_auth_env(monkeypatch)

    settings = _settings_from_environment()

    assert (
        settings.store_health_global_in_flight_limit == DEFAULT_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT
    )
    assert (
        settings.store_health_per_actor_in_flight_limit
        == DEFAULT_STORE_HEALTH_PER_ACTOR_IN_FLIGHT_LIMIT
    )
    assert settings.store_health_cache_max_entries == DEFAULT_STORE_HEALTH_CACHE_MAX_ENTRIES
    assert settings.store_health_cache_ttl_seconds == DEFAULT_STORE_HEALTH_CACHE_TTL_SECONDS
    assert (
        settings.store_health_singleflight_wait_seconds
        == DEFAULT_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS
    )


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("LAB_TRACKER_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT", "0"),
        ("LAB_TRACKER_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT", "17"),
        ("LAB_TRACKER_STORE_HEALTH_PER_ACTOR_IN_FLIGHT_LIMIT", "0"),
        ("LAB_TRACKER_STORE_HEALTH_CACHE_MAX_ENTRIES", "0"),
        ("LAB_TRACKER_STORE_HEALTH_CACHE_MAX_ENTRIES", "4097"),
        ("LAB_TRACKER_STORE_HEALTH_CACHE_TTL_SECONDS", "0"),
        ("LAB_TRACKER_STORE_HEALTH_CACHE_TTL_SECONDS", "301"),
        ("LAB_TRACKER_STORE_HEALTH_CACHE_TTL_SECONDS", "nan"),
        ("LAB_TRACKER_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS", "0"),
        ("LAB_TRACKER_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS", "61"),
        ("LAB_TRACKER_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS", "inf"),
    ],
)
def test_store_health_control_plane_settings_are_validated(
    monkeypatch,
    variable,
    value,
):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError, match="STORE_HEALTH"):
        _settings_from_environment()


@pytest.mark.parametrize(
    "field",
    [
        "store_health_global_in_flight_limit",
        "store_health_per_actor_in_flight_limit",
        "store_health_cache_max_entries",
        "store_health_cache_ttl_seconds",
        "store_health_singleflight_wait_seconds",
    ],
)
def test_store_health_control_plane_settings_reject_booleans(field):
    with pytest.raises(ValidationError, match="do not accept booleans"):
        Settings(_env_file=None, **{field: True})


@pytest.mark.parametrize(
    "field",
    [
        "store_health_global_in_flight_limit",
        "store_health_per_actor_in_flight_limit",
        "store_health_cache_max_entries",
    ],
)
def test_store_health_integer_settings_reject_non_integral_numbers(field):
    with pytest.raises(ValidationError, match="require integers"):
        Settings(_env_file=None, **{field: 1.5})


def test_store_health_actor_limit_cannot_exceed_global_limit(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT", "2")
    monkeypatch.setenv("LAB_TRACKER_STORE_HEALTH_PER_ACTOR_IN_FLIGHT_LIMIT", "3")

    with pytest.raises(ValidationError, match="STORE_HEALTH_PER_ACTOR"):
        _settings_from_environment()


def test_host_io_admission_limits_share_one_worker_capacity_budget(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv(
        "LAB_TRACKER_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT",
        "20",
    )
    monkeypatch.setenv("LAB_TRACKER_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT", "13")

    with pytest.raises(ValidationError, match="plus"):
        _settings_from_environment()


@pytest.mark.parametrize(
    ("variable", "field"),
    [
        (
            "LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS",
            "resolver_http_deadline_seconds",
        ),
        (
            "LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS",
            "resolver_subprocess_deadline_seconds",
        ),
    ],
)
def test_resolver_deadlines_allow_exactly_one_day(monkeypatch, variable, field):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv(variable, "86400")

    assert getattr(_settings_from_environment(), field) == 86_400.0


@pytest.mark.parametrize(
    "field",
    [
        "resolver_http_deadline_seconds",
        "resolver_subprocess_deadline_seconds",
    ],
)
def test_resolver_deadlines_reject_booleans(field):
    with pytest.raises(ValidationError, match="do not accept booleans"):
        Settings(_env_file=None, **{field: True})


def test_invalid_dotenv_http_policy_fails_runtime_composition(tmp_path, monkeypatch):
    _clear_auth_env(monkeypatch)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "LAB_TRACKER_ENVIRONMENT=local\n"
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES=https://files.lab/path\n"
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS=10.20.0.0/16\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=dotenv_path)

    with pytest.raises(ValueError, match="exact origins"):
        build_app_runtime(settings)


def test_invalid_store_authority_config_fails_before_logging_or_runtime_side_effects(
    monkeypatch,
    caplog,
):
    _clear_auth_env(monkeypatch)
    secret = "operator-secret-must-not-leak"
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        store_authority_grants_json=(
            '{"schema":"lab-tracker/store-authority/v1","grants":[],'
            f'"password":"{secret}"}}'
        ),
    )

    def unexpected_side_effect(*_args, **_kwargs):
        pytest.fail("invalid store authority configuration reached a runtime side effect")

    for name in (
        "configure_logging",
        "outbound_http_policy_from_config",
        "BoundedSubprocessExecutor",
        "BoundedLocalFilesystemOperations",
        "SafeHttpClient",
        "_OwnedGitHealthWorkdir",
        "get_engine",
        "LocalFileStorageBackend",
        "LocalNoteStorage",
        "CachedStoreHealthProbe",
    ):
        monkeypatch.setattr(runtime_module, name, unexpected_side_effect)
    caplog.clear()

    with pytest.raises(ValueError) as exc_info:
        build_app_runtime(settings)

    rendered = "".join(
        traceback.format_exception(
            type(exc_info.value),
            exc_info.value,
            exc_info.value.__traceback__,
        )
    )
    assert secret not in rendered
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert not caplog.records


def test_invalid_local_policy_fails_before_workdir_or_database_without_path_leak(
    monkeypatch,
):
    _clear_auth_env(monkeypatch)
    secret = "startup-secret-must-not-leak"
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        resolver_allowed_roots=f"\0{secret}",
    )

    def unexpected_side_effect(*_args, **_kwargs):
        pytest.fail("invalid local policy reached a runtime side effect")

    monkeypatch.setattr(runtime_module, "mkdtemp", unexpected_side_effect)
    monkeypatch.setattr(runtime_module, "get_engine", unexpected_side_effect)
    monkeypatch.setattr(runtime_module, "SafeHttpClient", unexpected_side_effect)

    with pytest.raises(ValueError) as exc_info:
        build_app_runtime(settings)

    rendered = f"{exc_info.value!s}\n{exc_info.value!r}"
    assert "local" in rendered.lower()
    assert secret not in rendered


def test_invalid_git_policy_fails_before_workdir_or_database_without_secret_leak(
    monkeypatch,
):
    _clear_auth_env(monkeypatch)
    secret = "startup-secret-must-not-leak"
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        git_allowed_remotes=f"https://operator:{secret}@git.example/lab/repo.git",
    )

    def unexpected_side_effect(*_args, **_kwargs):
        pytest.fail("invalid Git policy reached a runtime side effect")

    monkeypatch.setattr(runtime_module, "mkdtemp", unexpected_side_effect)
    monkeypatch.setattr(runtime_module, "get_engine", unexpected_side_effect)
    monkeypatch.setattr(runtime_module, "SafeHttpClient", unexpected_side_effect)

    with pytest.raises(ValueError) as exc_info:
        build_app_runtime(settings)

    rendered = f"{exc_info.value!s}\n{exc_info.value!r}"
    assert "LAB_TRACKER_GIT_ALLOWED_REMOTES" in rendered
    assert secret not in rendered


def test_invalid_rclone_policy_fails_before_workdir_or_database_without_secret_leak(
    monkeypatch,
):
    _clear_auth_env(monkeypatch)
    secret = "startup-secret-must-not-leak"
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        rclone_allowed_remotes=f"valid,{secret}:invalid",
    )

    def unexpected_side_effect(*_args, **_kwargs):
        pytest.fail("invalid rclone policy reached a runtime side effect")

    monkeypatch.setattr(runtime_module, "mkdtemp", unexpected_side_effect)
    monkeypatch.setattr(runtime_module, "get_engine", unexpected_side_effect)
    monkeypatch.setattr(runtime_module, "SafeHttpClient", unexpected_side_effect)

    with pytest.raises(ValueError) as exc_info:
        build_app_runtime(settings)

    rendered = f"{exc_info.value!s}\n{exc_info.value!r}"
    assert "LAB_TRACKER_RCLONE_ALLOWED_REMOTES" in rendered
    assert secret not in rendered


def test_runtime_installs_one_validated_policy_graph_and_registry(
    monkeypatch,
    tmp_path,
):
    _clear_auth_env(monkeypatch)
    settings_local_root = tmp_path / "settings-local"
    environment_local_root = tmp_path / "environment-local"
    settings_local_root.mkdir()
    environment_local_root.mkdir()
    monkeypatch.setenv(
        "LAB_TRACKER_RESOLVER_ALLOWED_ROOTS",
        str(environment_local_root),
    )
    monkeypatch.setenv(
        "LAB_TRACKER_RCLONE_ALLOWED_REMOTES",
        "environment-remote",
    )
    monkeypatch.setenv(
        "LAB_TRACKER_GIT_ALLOWED_REMOTES",
        "https://environment.example/ignored",
    )
    resolver_registry = ResolverRegistry()
    captured: dict[str, object] = {}
    outbound_http_client = object()
    safe_http_client_timeouts: list[float] = []

    class FalseyProcessExecutor:
        def __bool__(self) -> bool:
            return False

    process_executor = FalseyProcessExecutor()
    monkeypatch.setattr(
        runtime_module,
        "BoundedSubprocessExecutor",
        lambda: process_executor,
    )

    def recording_safe_http_client(*, timeout):
        safe_http_client_timeouts.append(timeout)
        return outbound_http_client

    monkeypatch.setattr(
        runtime_module,
        "SafeHttpClient",
        recording_safe_http_client,
    )

    def recording_registry_from_env(
        *,
        local_file_reader,
        local_recovery_enumerator,
        local_resolution_limits,
        recovery,
        http_policy,
        http_client,
        rclone_remote_policy,
        git_remote_policy,
        process_executor,
        http_deadline_seconds,
        subprocess_deadline_seconds,
    ):
        assert safe_http_client_timeouts == [12.5]
        captured["registry_local_file_reader"] = local_file_reader
        captured["registry_local_recovery_enumerator"] = local_recovery_enumerator
        captured["registry_local_resolution_limits"] = local_resolution_limits
        captured["registry_recovery"] = recovery
        captured["registry_http_policy"] = http_policy
        captured["registry_http_client"] = http_client
        captured["registry_rclone_remote_policy"] = rclone_remote_policy
        captured["registry_git_remote_policy"] = git_remote_policy
        captured["registry_process_executor"] = process_executor
        captured["registry_http_deadline_seconds"] = http_deadline_seconds
        captured["registry_subprocess_deadline_seconds"] = subprocess_deadline_seconds
        return resolver_registry

    monkeypatch.setattr(
        runtime_module,
        "registry_from_env",
        recording_registry_from_env,
    )

    def recording_local_store_health_probe(
        *,
        inspector,
        deadline_seconds,
    ):
        captured["health_local_inspector"] = inspector
        captured["health_local_deadline_seconds"] = deadline_seconds

        def probe(target):
            captured["local_health_target"] = target
            return StoreHealth(StoreHealthStatus.HEALTHY)

        return probe

    monkeypatch.setattr(
        runtime_module,
        "LocalStoreHealthProbe",
        recording_local_store_health_probe,
    )

    def recording_http_store_health_probe(
        *,
        policy,
        client,
        deadline_seconds,
    ):
        captured["health_http_policy"] = policy
        captured["health_http_client"] = client
        captured["health_http_deadline_seconds"] = deadline_seconds

        def probe(target):
            captured["http_health_target"] = target
            return StoreHealth(StoreHealthStatus.HEALTHY)

        return probe

    monkeypatch.setattr(
        runtime_module,
        "HttpStoreHealthProbe",
        recording_http_store_health_probe,
    )

    def recording_rclone_store_health_probe(
        *,
        policy,
        executor,
        deadline_seconds,
    ):
        captured["health_rclone_policy"] = policy
        captured["health_rclone_executor"] = executor
        captured["health_rclone_deadline_seconds"] = deadline_seconds

        def probe(target):
            captured["rclone_health_target"] = target
            return StoreHealth(StoreHealthStatus.HEALTHY)

        return probe

    monkeypatch.setattr(
        runtime_module,
        "RcloneStoreHealthProbe",
        recording_rclone_store_health_probe,
    )

    def recording_git_store_health_probe(
        *,
        policy,
        executor,
        workdir,
        deadline_seconds,
    ):
        captured["health_git_policy"] = policy
        captured["health_git_executor"] = executor
        captured["health_git_workdir"] = workdir
        captured["health_git_deadline_seconds"] = deadline_seconds

        def probe(target):
            captured["git_health_target"] = target
            return StoreHealth(StoreHealthStatus.HEALTHY)

        return probe

    monkeypatch.setattr(
        runtime_module,
        "GitStoreHealthProbe",
        recording_git_store_health_probe,
    )

    def recording_check_store_health(target):
        captured["legacy_health_target"] = target
        return StoreHealth(StoreHealthStatus.HEALTHY)

    monkeypatch.setattr(
        runtime_module,
        "check_store_health",
        recording_check_store_health,
    )
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        resolver_allowed_roots=str(settings_local_root),
        resolver_http_allowed_authorities="http://10.20.1.7",
        resolver_http_allowed_networks="10.20.0.0/16",
        resolver_http_deadline_seconds=12.5,
        resolver_subprocess_deadline_seconds=7.25,
        resolver_recovery=True,
        resolver_recovery_max_files=23,
        resolver_recovery_max_directories=29,
        resolver_recovery_max_bytes=1_048_576,
        artifact_resolution_global_in_flight_limit=5,
        artifact_resolution_per_actor_in_flight_limit=3,
        store_health_global_in_flight_limit=3,
        store_health_per_actor_in_flight_limit=2,
        store_health_cache_max_entries=17,
        store_health_cache_ttl_seconds=4.5,
        store_health_singleflight_wait_seconds=2.25,
        rclone_allowed_remotes="settings-remote",
        git_allowed_remotes="https://settings.example/lab",
    )
    runtime = build_app_runtime(settings)
    app = FastAPI()
    git_health_workdir = runtime.git_health_workdir
    try:
        configure_app_state(app, runtime)

        assert not hasattr(app.state, "local_filesystem_authority")
        assert not hasattr(app.state, "local_filesystem_operations")
        assert not hasattr(app.state, "local_path_policy")
        assert not hasattr(runtime, "local_filesystem_authority")
        assert not hasattr(runtime, "local_path_policy")
        assert app.state.outbound_http_policy is runtime.outbound_http_policy
        assert runtime.outbound_http_client is outbound_http_client
        assert app.state.rclone_remote_policy is runtime.rclone_remote_policy
        assert app.state.git_remote_policy is runtime.git_remote_policy
        assert app.state.process_executor is runtime.process_executor
        assert runtime.process_executor is process_executor
        assert app.state.resolver_registry is resolver_registry
        assert app.state.artifact_resolution_admission is runtime.artifact_resolution_admission
        assert runtime.artifact_resolution_admission.global_in_flight_limit == 5
        assert runtime.artifact_resolution_admission.per_actor_in_flight_limit == 3
        assert app.state.store_health_admission is runtime.store_health_admission
        assert runtime.store_health_admission.global_in_flight_limit == 3
        assert runtime.store_health_admission.per_actor_in_flight_limit == 2
        assert app.state.git_health_workdir is runtime.git_health_workdir
        assert app.state.store_health_checker is runtime.store_health_checker
        assert runtime.store_health_checker.max_entries == 17
        assert runtime.store_health_checker.ttl_seconds == 4.5
        assert runtime.store_health_checker.waiter_timeout_seconds == 2.25
        assert app.state.cleanup_git_health_workdir.__self__ is runtime
        assert safe_http_client_timeouts == [12.5]
        assert captured["registry_local_file_reader"] is runtime.local_filesystem_operations
        assert (
            captured["registry_local_recovery_enumerator"]
            is runtime.local_filesystem_operations
        )
        local_resolution_limits = captured["registry_local_resolution_limits"]
        assert local_resolution_limits.max_read_bytes == 1_048_576
        assert local_resolution_limits.deadline_seconds == 7.25
        recovery = captured["registry_recovery"]
        assert recovery.enabled is True
        assert recovery.max_files == 23
        assert recovery.max_directories == 29
        assert recovery.max_bytes == 1_048_576
        assert captured["health_local_inspector"] is runtime.local_filesystem_operations
        assert runtime.local_filesystem_operations.executor is runtime.process_executor
        assert captured["registry_http_policy"] is runtime.outbound_http_policy
        assert captured["health_http_policy"] is runtime.outbound_http_policy
        assert captured["registry_http_client"] is runtime.outbound_http_client
        assert captured["health_http_client"] is runtime.outbound_http_client
        assert captured["registry_rclone_remote_policy"] is runtime.rclone_remote_policy
        assert captured["health_rclone_policy"] is runtime.rclone_remote_policy
        assert captured["registry_git_remote_policy"] is runtime.git_remote_policy
        assert captured["health_git_policy"] is runtime.git_remote_policy
        assert captured["registry_process_executor"] is runtime.process_executor
        assert captured["health_rclone_executor"] is runtime.process_executor
        assert captured["health_git_executor"] is runtime.process_executor
        assert captured["health_git_workdir"] is runtime.git_health_workdir
        assert captured["registry_http_deadline_seconds"] == 12.5
        assert captured["health_http_deadline_seconds"] == 12.5
        assert captured["registry_subprocess_deadline_seconds"] == 7.25
        assert captured["health_local_deadline_seconds"] == 7.25
        assert captured["health_rclone_deadline_seconds"] == 7.25
        assert captured["health_git_deadline_seconds"] == 7.25

        local_health_target = StoreProbeTarget(
            store_id=UUID(int=4),
            name="runtime-local-wiring",
            kind=StoreKind.LOCAL_FS,
            root=str(settings_local_root),
            endpoint=None,
            credential_ref=None,
        )
        assert runtime.store_health_checker(local_health_target).is_healthy
        assert captured["local_health_target"] is local_health_target
        assert "legacy_health_target" not in captured

        http_health_target = StoreProbeTarget(
            store_id=UUID(int=1),
            name="runtime-http-wiring",
            kind=StoreKind.HTTP,
            root="http://10.20.1.7/artifact.bin",
            endpoint=None,
            credential_ref=None,
        )
        assert runtime.store_health_checker(http_health_target).is_healthy
        assert captured["http_health_target"] is http_health_target
        assert "legacy_health_target" not in captured

        rclone_health_target = StoreProbeTarget(
            store_id=UUID(int=2),
            name="runtime-rclone-wiring",
            kind=StoreKind.RCLONE,
            root="/",
            endpoint=None,
            credential_ref="settings-remote",
        )
        assert runtime.store_health_checker(rclone_health_target).is_healthy
        assert captured["rclone_health_target"] is rclone_health_target
        assert "legacy_health_target" not in captured

        git_health_target = StoreProbeTarget(
            store_id=UUID(int=3),
            name="runtime-wiring",
            kind=StoreKind.GIT,
            root="https://settings.example/lab/repo.git",
            endpoint=None,
            credential_ref=None,
        )
        assert runtime.store_health_checker(git_health_target).is_healthy
        assert captured["git_health_target"] is git_health_target
        assert "legacy_health_target" not in captured
        assert git_health_workdir.is_dir()
        assert list(git_health_workdir.iterdir()) == []
        assert not (git_health_workdir / ".git").exists()
        if os.name != "nt":
            assert stat.S_IMODE(git_health_workdir.stat().st_mode) == 0o700
        assert (
            runtime.local_filesystem_operations.authority.select_directory(
                str(settings_local_root)
            )
            is not None
        )
        assert (
            runtime.local_filesystem_operations.authority.select_directory(
                str(environment_local_root)
            )
            is None
        )
        assert runtime.rclone_remote_policy.authorize("settings-remote") is not None
        assert runtime.rclone_remote_policy.authorize("environment-remote") is None
        assert (
            runtime.git_remote_policy.authorize("https://settings.example/lab/repo.git") is not None
        )
        assert (
            runtime.git_remote_policy.authorize("https://environment.example/ignored/repo.git")
            is None
        )
        assert (
            runtime.outbound_http_policy.authorize("http://10.20.1.7/artifact.bin").hostname
            == "10.20.1.7"
        )
    finally:
        runtime.engine.dispose()
        app.state.cleanup_git_health_workdir()
        app.state.cleanup_git_health_workdir()

    assert not git_health_workdir.exists()


def test_runtime_retains_one_store_authority_snapshot_without_environment_reread(
    monkeypatch,
):
    _clear_auth_env(monkeypatch)
    configured = '{"schema":"lab-tracker/store-authority/v1","grants":[]}'
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        store_authority_grants_json=configured,
    )
    monkeypatch.setenv(
        "LAB_TRACKER_STORE_AUTHORITY_GRANTS_JSON",
        "environment-must-not-be-reread",
    )

    runtime = build_app_runtime(settings)
    app = FastAPI()
    try:
        configure_app_state(app, runtime)

        assert app.state.store_authority_registry is runtime.store_authority_registry
        assert "store_authority_registry=" not in repr(runtime)
    finally:
        runtime.engine.dispose()
        runtime.cleanup_git_health_workdir()


def test_lifespan_removes_app_owned_git_health_workdir(monkeypatch):
    _clear_auth_env(monkeypatch)
    resolver_registry = ResolverRegistry()

    def recording_registry_from_env(
        *,
        local_file_reader,
        local_recovery_enumerator,
        local_resolution_limits,
        recovery,
        http_policy,
        http_client,
        rclone_remote_policy,
        git_remote_policy,
        process_executor,
        http_deadline_seconds,
        subprocess_deadline_seconds,
    ):
        del (
            local_file_reader,
            local_recovery_enumerator,
            local_resolution_limits,
            recovery,
            http_policy,
            http_client,
            rclone_remote_policy,
            git_remote_policy,
            process_executor,
            http_deadline_seconds,
            subprocess_deadline_seconds,
        )
        return resolver_registry

    monkeypatch.setattr(
        runtime_module,
        "registry_from_env",
        recording_registry_from_env,
    )
    runtime = build_app_runtime(
        Settings(
            _env_file=None,
            database_url="sqlite+pysqlite:///:memory:",
        )
    )
    git_health_workdir = runtime.git_health_workdir
    app = FastAPI(lifespan=make_lifespan(runtime))
    configure_app_state(app, runtime)

    with TestClient(app):
        assert git_health_workdir.is_dir()
        assert list(git_health_workdir.iterdir()) == []
        assert not (git_health_workdir / ".git").exists()

    assert not git_health_workdir.exists()


def test_lifespan_removes_git_health_workdir_when_engine_disposal_fails(
    monkeypatch,
):
    _clear_auth_env(monkeypatch)
    monkeypatch.setattr(
        runtime_module,
        "registry_from_env",
        lambda **_kwargs: ResolverRegistry(),
    )
    runtime = build_app_runtime(
        Settings(
            _env_file=None,
            database_url="sqlite+pysqlite:///:memory:",
        )
    )
    git_health_workdir = runtime.git_health_workdir
    original_dispose = runtime.engine.dispose

    def raising_dispose():
        original_dispose()
        raise RuntimeError("engine disposal failed")

    monkeypatch.setattr(runtime.engine, "dispose", raising_dispose)

    async def run_lifespan() -> None:
        app = FastAPI()
        configure_app_state(app, runtime)
        async with make_lifespan(runtime)(app):
            assert git_health_workdir.is_dir()

    with pytest.raises(RuntimeError, match="engine disposal failed"):
        asyncio.run(run_lifespan())

    assert not git_health_workdir.exists()


def test_git_health_workdir_gc_fallback_is_silent(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setattr(
        runtime_module,
        "registry_from_env",
        lambda **_kwargs: ResolverRegistry(),
    )
    runtime = build_app_runtime(
        Settings(
            _env_file=None,
            database_url="sqlite+pysqlite:///:memory:",
        )
    )
    git_health_workdir = runtime.git_health_workdir
    runtime.engine.dispose()

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", ResourceWarning)
        del runtime
        gc.collect()

    assert not git_health_workdir.exists()
    assert not [warning for warning in captured if issubclass(warning.category, ResourceWarning)]


def test_test_resource_drain_runs_cleanup_after_engine_disposal_error():
    events: list[str] = []

    class RaisingEngine:
        def dispose(self) -> None:
            events.append("engine")
            raise RuntimeError("engine disposal failed")

    register_test_resources(
        RaisingEngine(),  # type: ignore[arg-type]
        None,
        lambda: events.append("cleanup"),
    )

    with pytest.raises(RuntimeError, match="engine disposal failed"):
        drain_test_resources()

    assert events == ["engine", "cleanup"]


def test_compose_forwards_outbound_artifact_policy_settings():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert (
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES: "
        "${LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES:-}"
    ) in compose
    assert (
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS: "
        "${LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS:-}"
    ) in compose
    assert (
        "LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS: "
        "${LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS:-30}"
    ) in compose
    assert (
        "LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS: "
        "${LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS:-30}"
    ) in compose
    assert ("LAB_TRACKER_GIT_ALLOWED_REMOTES: ${LAB_TRACKER_GIT_ALLOWED_REMOTES:-}") in compose
    assert (
        "LAB_TRACKER_RCLONE_ALLOWED_REMOTES: ${LAB_TRACKER_RCLONE_ALLOWED_REMOTES:-}"
    ) in compose
    assert (
        "LAB_TRACKER_STORE_AUTHORITY_GRANTS_JSON: "
        "${LAB_TRACKER_STORE_AUTHORITY_GRANTS_JSON:-}"
    ) in compose


def test_compose_forwards_store_health_control_plane_settings():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    expected = {
        "LAB_TRACKER_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT": "4",
        "LAB_TRACKER_STORE_HEALTH_PER_ACTOR_IN_FLIGHT_LIMIT": "1",
        "LAB_TRACKER_STORE_HEALTH_CACHE_MAX_ENTRIES": "256",
        "LAB_TRACKER_STORE_HEALTH_CACHE_TTL_SECONDS": "10",
        "LAB_TRACKER_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS": "10",
    }
    for variable, default in expected.items():
        assert f"{variable}: ${{{variable}:-{default}}}" in compose


def test_default_openai_model_is_standard_account_model(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    settings = _settings_from_environment()
    assert settings.openai_model == "gpt-4o-mini"
    assert settings.openai_reasoning_effort is None
    assert settings.openai_reasoning_mode is None


def test_openai_reasoning_settings_load_from_environment(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_OPENAI_REASONING_EFFORT", "max")
    monkeypatch.setenv("LAB_TRACKER_OPENAI_REASONING_MODE", "pro")

    settings = _settings_from_environment()

    assert settings.openai_reasoning_effort == "max"
    assert settings.openai_reasoning_mode == "pro"


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("LAB_TRACKER_OPENAI_REASONING_EFFORT", "ultra"),
        ("LAB_TRACKER_OPENAI_REASONING_MODE", "turbo"),
    ],
)
def test_openai_reasoning_settings_reject_unknown_values(monkeypatch, variable, value):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError):
        _settings_from_environment()


def test_managed_postgres_urls_use_installed_psycopg_driver(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv(
        "LAB_TRACKER_DATABASE_URL",
        "postgres://lab_tracker:secret@db.example.org/lab_tracker",
    )
    settings = _settings_from_environment()
    assert settings.database_url == (
        "postgresql+psycopg://lab_tracker:secret@db.example.org/lab_tracker"
    )

    monkeypatch.setenv(
        "LAB_TRACKER_DATABASE_URL",
        "postgresql://lab_tracker:secret@db.example.org/lab_tracker",
    )
    settings = _settings_from_environment()
    assert settings.database_url == (
        "postgresql+psycopg://lab_tracker:secret@db.example.org/lab_tracker"
    )


def test_non_local_environment_rejects_default_auth_secret(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    with pytest.raises(ValidationError, match="strong non-placeholder"):
        _settings_from_environment()


def test_non_local_environment_rejects_placeholder_auth_secret(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "replace-with-a-strong-secret")
    with pytest.raises(ValidationError, match="strong non-placeholder"):
        _settings_from_environment()


def test_non_local_environment_rejects_example_random_secret_placeholder(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "LAB_TRACKER_AUTH_SECRET_KEY",
        "replace-with-a-strong-random-secret",
    )
    with pytest.raises(ValidationError, match="strong non-placeholder"):
        _settings_from_environment()


def test_non_local_environment_accepts_custom_auth_secret(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "custom-secret")
    settings = _settings_from_environment()
    assert settings.auth_secret_key == "custom-secret"
    assert settings.is_auth_enabled() is True
    assert settings.is_usage_events_enabled() is True


def test_usage_events_flag_overrides_environment(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_USAGE_EVENTS", "true")
    settings = _settings_from_environment()
    assert settings.is_usage_events_enabled() is True

    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "custom-secret")
    monkeypatch.setenv("LAB_TRACKER_USAGE_EVENTS", "false")
    settings = _settings_from_environment()
    assert settings.is_usage_events_enabled() is False


def test_non_local_environment_rejects_disabled_auth(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "false")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "custom-secret")
    with pytest.raises(ValidationError, match="LAB_TRACKER_AUTH_ENABLED=false"):
        _settings_from_environment()
