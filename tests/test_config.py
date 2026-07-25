from __future__ import annotations

import asyncio
import gc
import os
import stat
import warnings
from pathlib import Path

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


def _settings_from_environment() -> Settings:
    return Settings(_env_file=None)


def _clear_auth_env(monkeypatch) -> None:
    monkeypatch.delenv("LAB_TRACKER_AUTH_SECRET_KEY", raising=False)
    monkeypatch.delenv("LAB_TRACKER_ENVIRONMENT", raising=False)
    monkeypatch.delenv("LAB_TRACKER_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("LAB_TRACKER_SOURCE_REVISION", raising=False)
    monkeypatch.delenv(
        "LAB_TRACKER_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT",
        raising=False,
    )
    monkeypatch.delenv(
        "LAB_TRACKER_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT",
        raising=False,
    )


def test_local_environment_allows_default_auth_secret(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    settings = _settings_from_environment()
    assert settings.auth_secret_key == DEFAULT_AUTH_SECRET_KEY
    assert settings.is_auth_enabled() is False
    assert settings.is_usage_events_enabled() is False


def test_source_revision_is_normalized_for_runtime_readiness(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv(
        "LAB_TRACKER_SOURCE_REVISION",
        " AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA ",
    )

    assert _settings_from_environment().source_revision == "a" * 40


def test_source_revision_defaults_to_unknown(monkeypatch):
    _clear_auth_env(monkeypatch)

    assert _settings_from_environment().source_revision == "unknown"


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


def test_dotenv_loads_resolver_settings(tmp_path, monkeypatch):
    _clear_auth_env(monkeypatch)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "LAB_TRACKER_ENVIRONMENT=local\n"
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES=http://10.20.1.7\n"
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS=10.20.0.0/16\n"
        "LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS=12.5\n"
        "LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS=7.25\n"
        "LAB_TRACKER_GIT_ALLOWED_REMOTES=https://git.example/lab\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=dotenv_path)
    policy = outbound_http_policy_from_config(
        allowed_authorities=settings.resolver_http_allowed_authorities,
        allowed_networks=settings.resolver_http_allowed_networks,
    )

    assert policy.authorize("http://10.20.1.7/artifact.bin").hostname == "10.20.1.7"
    assert settings.resolver_http_deadline_seconds == 12.5
    assert settings.resolver_subprocess_deadline_seconds == 7.25
    assert settings.git_allowed_remotes == "https://git.example/lab"


def test_git_remote_policy_defaults_to_deny_all(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.delenv("LAB_TRACKER_GIT_ALLOWED_REMOTES", raising=False)

    assert _settings_from_environment().git_allowed_remotes == ""


def test_resolver_http_deadline_defaults_to_thirty_seconds(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.delenv("LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS", raising=False)

    assert _settings_from_environment().resolver_http_deadline_seconds == 30.0


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

    with pytest.raises(ValueError) as exc_info:
        build_app_runtime(settings)

    rendered = f"{exc_info.value!s}\n{exc_info.value!r}"
    assert "LAB_TRACKER_GIT_ALLOWED_REMOTES" in rendered
    assert secret not in rendered


def test_runtime_installs_one_validated_policy_graph_and_registry(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv(
        "LAB_TRACKER_GIT_ALLOWED_REMOTES",
        "https://environment.example/ignored",
    )
    resolver_registry = ResolverRegistry()
    captured: dict[str, object] = {}

    def recording_registry_from_env(
        *,
        http_policy,
        git_remote_policy,
        http_deadline_seconds,
        subprocess_deadline_seconds,
    ):
        captured["http_policy"] = http_policy
        captured["git_remote_policy"] = git_remote_policy
        captured["http_deadline_seconds"] = http_deadline_seconds
        captured["subprocess_deadline_seconds"] = subprocess_deadline_seconds
        return resolver_registry

    monkeypatch.setattr(
        runtime_module,
        "registry_from_env",
        recording_registry_from_env,
    )
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        resolver_http_allowed_authorities="http://10.20.1.7",
        resolver_http_allowed_networks="10.20.0.0/16",
        resolver_http_deadline_seconds=12.5,
        resolver_subprocess_deadline_seconds=7.25,
        artifact_resolution_global_in_flight_limit=5,
        artifact_resolution_per_actor_in_flight_limit=3,
        git_allowed_remotes="https://settings.example/lab",
    )
    runtime = build_app_runtime(settings)
    app = FastAPI()
    git_health_workdir = runtime.git_health_workdir
    try:
        configure_app_state(app, runtime)

        assert app.state.outbound_http_policy is runtime.outbound_http_policy
        assert app.state.git_remote_policy is runtime.git_remote_policy
        assert app.state.resolver_registry is resolver_registry
        assert (
            app.state.artifact_resolution_admission
            is runtime.artifact_resolution_admission
        )
        assert runtime.artifact_resolution_admission.global_in_flight_limit == 5
        assert runtime.artifact_resolution_admission.per_actor_in_flight_limit == 3
        assert app.state.git_health_workdir is runtime.git_health_workdir
        assert app.state.store_health_checker is runtime.store_health_checker
        assert app.state.cleanup_git_health_workdir.__self__ is runtime
        assert (
            runtime.store_health_checker.git_remote_policy
            is runtime.git_remote_policy
        )
        assert (
            runtime.store_health_checker.git_health_workdir
            is runtime.git_health_workdir
        )
        assert captured == {
            "http_policy": runtime.outbound_http_policy,
            "git_remote_policy": runtime.git_remote_policy,
            "http_deadline_seconds": 12.5,
            "subprocess_deadline_seconds": 7.25,
        }
        assert git_health_workdir.is_dir()
        assert list(git_health_workdir.iterdir()) == []
        assert not (git_health_workdir / ".git").exists()
        if os.name != "nt":
            assert stat.S_IMODE(git_health_workdir.stat().st_mode) == 0o700
        assert (
            runtime.git_remote_policy.authorize(
                "https://settings.example/lab/repo.git"
            )
            is not None
        )
        assert (
            runtime.git_remote_policy.authorize(
                "https://environment.example/ignored/repo.git"
            )
            is None
        )
        assert (
            runtime.outbound_http_policy.authorize(
                "http://10.20.1.7/artifact.bin"
            ).hostname
            == "10.20.1.7"
        )
    finally:
        runtime.engine.dispose()
        app.state.cleanup_git_health_workdir()
        app.state.cleanup_git_health_workdir()

    assert not git_health_workdir.exists()


def test_lifespan_removes_app_owned_git_health_workdir(monkeypatch):
    _clear_auth_env(monkeypatch)
    resolver_registry = ResolverRegistry()

    def recording_registry_from_env(
        *,
        http_policy,
        git_remote_policy,
        http_deadline_seconds,
        subprocess_deadline_seconds,
    ):
        del (
            http_policy,
            git_remote_policy,
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
    assert not [
        warning for warning in captured if issubclass(warning.category, ResourceWarning)
    ]


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
    assert (
        "LAB_TRACKER_GIT_ALLOWED_REMOTES: "
        "${LAB_TRACKER_GIT_ALLOWED_REMOTES:-}"
    ) in compose


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
