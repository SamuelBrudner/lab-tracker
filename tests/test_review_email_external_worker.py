from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import lab_tracker.review_email_external_worker as worker
from lab_tracker.db_models import ReviewEmailOutboxModel
from lab_tracker.models import ReviewEmailDeliveryStatus
from lab_tracker.review_email_transport import (
    ReviewReadyEmail,
    render_review_email_content,
)


def test_provider_message_id_limit_matches_persistence_schema() -> None:
    assert (
        ReviewEmailOutboxModel.__table__.c.provider_message_id.type.length
        == worker.MAX_PROVIDER_MESSAGE_ID_LENGTH
    )


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "auth_secret_key": "strong-test-secret",
        "public_base_url": "https://lab-tracker.example.test",
        "review_email_claim_lease_seconds": 300,
        "review_email_enabled": True,
        "review_email_transport": "external",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_api(
    monkeypatch: pytest.MonkeyPatch,
    review_emails: object,
) -> None:
    @contextmanager
    def fake_api(_settings: object):
        yield SimpleNamespace(review_emails=review_emails)

    monkeypatch.setattr(worker, "_api", fake_api)


def test_test_subcommand_requires_destination_email() -> None:
    args = worker._parser().parse_args(["test", "--to", "Test.User@Example.ORG"])

    assert args.command == "test"
    assert args.destination_email == "Test.User@Example.ORG"

    with pytest.raises(SystemExit):
        worker._parser().parse_args(["test"])


def test_enqueue_test_uses_durable_no_graph_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery_id = uuid4()
    review_emails = SimpleNamespace()

    def enqueue_test(destination_email: str) -> SimpleNamespace:
        assert destination_email == "Test.User@Example.ORG"
        return SimpleNamespace(
            delivery_id=delivery_id,
            event_type="test",
            change_set_id=None,
            status=ReviewEmailDeliveryStatus.PENDING,
        )

    review_emails.enqueue_test = enqueue_test
    _patch_api(monkeypatch, review_emails)

    result = worker.enqueue_test(
        _settings(),
        destination_email="Test.User@Example.ORG",
    )

    assert result == {
        "delivery_id": str(delivery_id),
        "event_type": "test",
        "status": "pending",
    }
    assert "to" not in result


@pytest.mark.parametrize(
    ("event_type", "change_set_id"),
    [
        ("review_ready", None),
        ("test", uuid4()),
    ],
)
def test_enqueue_test_rejects_a_broken_no_graph_invariant(
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    change_set_id: object,
) -> None:
    review_emails = SimpleNamespace(
        enqueue_test=lambda _destination: SimpleNamespace(
            delivery_id=uuid4(),
            event_type=event_type,
            change_set_id=change_set_id,
            status=ReviewEmailDeliveryStatus.PENDING,
        )
    )
    _patch_api(monkeypatch, review_emails)

    with pytest.raises(RuntimeError, match="no-graph invariant"):
        worker.enqueue_test(
            _settings(),
            destination_email="reviewer@example.test",
        )


def test_claim_returns_only_fixed_test_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery_id = uuid4()
    claim_token = uuid4()
    idempotency_key = "review-email-test:v1:opaque-delivery-id"
    delivery = SimpleNamespace(
        delivery_id=delivery_id,
        claim_token=claim_token,
        event_type="test",
        change_set_id=None,
        recipient_user_id=None,
        destination_email="Test.User@Example.ORG",
        idempotency_key=idempotency_key,
    )
    review_emails = SimpleNamespace(
        claim_next=lambda *, lease_seconds: delivery,
    )
    _patch_api(monkeypatch, review_emails)

    result = worker.claim(_settings())
    expected = render_review_email_content(
        ReviewReadyEmail(
            recipient_email="Test.User@example.org",
            review_url="https://lab-tracker.example.test/app/",
            idempotency_key=idempotency_key,
            event_type="test",
        )
    )

    assert result == {
        "delivery": {
            "delivery_id": str(delivery_id),
            "claim_token": str(claim_token),
            "to": "Test.User@example.org",
            "subject": expected.subject,
            "text_content": expected.text_content,
            "dedupe_marker": expected.dedupe_marker,
        }
    }
    rendered = json.dumps(result)
    assert "project" not in rendered.lower()
    assert "proposal" not in rendered.lower()
    assert "science" not in rendered.lower()
    assert "count" not in rendered.lower()
    assert idempotency_key not in rendered
    assert expected.dedupe_marker in result["delivery"]["text_content"]

    assert worker.claim(_settings())["delivery"]["dedupe_marker"] == expected.dedupe_marker


@pytest.mark.parametrize(
    ("claim_token", "event_type", "message"),
    [
        (None, "test", "lease token"),
        (uuid4(), "unknown", "unsupported event type"),
    ],
)
def test_claim_rejects_corrupt_delivery_state(
    monkeypatch: pytest.MonkeyPatch,
    claim_token: object,
    event_type: str,
    message: str,
) -> None:
    review_emails = SimpleNamespace(
        claim_next=lambda *, lease_seconds: SimpleNamespace(
            delivery_id=uuid4(),
            claim_token=claim_token,
            event_type=event_type,
            change_set_id=None,
            recipient_user_id=None,
            destination_email="reviewer@example.test",
            idempotency_key="test-idempotency-key",
        )
    )
    _patch_api(monkeypatch, review_emails)

    with pytest.raises(RuntimeError, match=message):
        worker.claim(_settings())


def test_claim_revalidates_destination_before_releasing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_emails = SimpleNamespace(
        claim_next=lambda *, lease_seconds: SimpleNamespace(
            delivery_id=uuid4(),
            claim_token=uuid4(),
            event_type="test",
            change_set_id=None,
            recipient_user_id=None,
            destination_email="reviewer@example.test\nBcc: observer@example.test",
            idempotency_key="test-idempotency-key",
        )
    )
    _patch_api(monkeypatch, review_emails)

    with pytest.raises(Exception, match="one valid email address"):
        worker.claim(_settings())


@pytest.mark.parametrize(
    "provider_message_id",
    [
        "valid\nInjected: value",
        "x" * (worker.MAX_PROVIDER_MESSAGE_ID_LENGTH + 1),
        "invalid\x00value",
    ],
)
def test_mark_accepted_rejects_unsafe_provider_message_id(
    monkeypatch: pytest.MonkeyPatch,
    provider_message_id: str,
) -> None:
    review_emails = SimpleNamespace(
        mark_accepted=lambda *_args, **_kwargs: pytest.fail(
            "unsafe provider message id reached the service"
        )
    )
    _patch_api(monkeypatch, review_emails)

    with pytest.raises(ValueError, match="provider_message_id"):
        worker.mark_accepted(
            _settings(),
            delivery_id=uuid4(),
            claim_token=uuid4(),
            provider_message_id=provider_message_id,
        )


def test_mark_accepted_trims_safe_provider_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery_id = uuid4()
    claim_token = uuid4()
    captured: dict[str, object] = {}

    def mark_accepted(
        actual_delivery_id: object,
        *,
        claim_token: object,
        provider_message_id: object,
    ) -> SimpleNamespace:
        captured.update(
            delivery_id=actual_delivery_id,
            claim_token=claim_token,
            provider_message_id=provider_message_id,
        )
        return SimpleNamespace(
            delivery_id=actual_delivery_id,
            status=ReviewEmailDeliveryStatus.ACCEPTED,
        )

    _patch_api(
        monkeypatch,
        SimpleNamespace(mark_accepted=mark_accepted),
    )

    result = worker.mark_accepted(
        _settings(),
        delivery_id=delivery_id,
        claim_token=claim_token,
        provider_message_id="  <outlook-message-id@example.test>  ",
    )

    assert captured == {
        "delivery_id": delivery_id,
        "claim_token": claim_token,
        "provider_message_id": "<outlook-message-id@example.test>",
    }
    assert result == {"delivery_id": str(delivery_id), "status": "accepted"}


def test_mark_failed_releases_only_allowlisted_error_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery_id = uuid4()
    captured: dict[str, object] = {}

    def mark_failed(
        actual_delivery_id: object,
        **kwargs: object,
    ) -> SimpleNamespace:
        captured.update(delivery_id=actual_delivery_id, **kwargs)
        return SimpleNamespace(
            delivery_id=actual_delivery_id,
            status=ReviewEmailDeliveryStatus.FAILED,
        )

    _patch_api(monkeypatch, SimpleNamespace(mark_failed=mark_failed))

    worker.mark_failed(
        _settings(),
        delivery_id=delivery_id,
        claim_token=uuid4(),
        retryable=False,
        error_code="provider said: private-project-name",
    )

    assert captured["error_message"] == "External email provider error."
    assert "private-project-name" not in json.dumps(captured, default=str)


def test_main_test_command_prints_machine_readable_receipt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    delivery_id = uuid4()
    monkeypatch.setattr(worker, "get_settings", _settings)
    monkeypatch.setattr(
        worker,
        "enqueue_test",
        lambda settings, *, destination_email: {
            "delivery_id": str(delivery_id),
            "event_type": "test",
            "status": "pending",
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "python -m lab_tracker.review_email_external_worker",
            "test",
            "--to",
            "reviewer@example.test",
        ],
    )

    worker.main()

    assert json.loads(capsys.readouterr().out) == {
        "delivery_id": str(delivery_id),
        "event_type": "test",
        "status": "pending",
    }


def test_bridge_rejects_non_external_or_disabled_transport() -> None:
    with pytest.raises(RuntimeError, match="disabled"):
        worker.enqueue_test(
            _settings(review_email_enabled=False),
            destination_email="reviewer@example.test",
        )
    with pytest.raises(RuntimeError, match="not external"):
        worker.enqueue_test(
            _settings(review_email_transport="smtp"),
            destination_email="reviewer@example.test",
        )


def test_worker_settings_reads_explicit_runtime_secret_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "auth-secret-key"
    secret_file.write_text(
        "runtime-secret-value-long-enough-for-production\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("LAB_TRACKER_AUTH_SECRET_KEY", raising=False)
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY_FILE", str(secret_file))
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv(
        "LAB_TRACKER_PUBLIC_BASE_URL",
        "https://lab-tracker.example.test",
    )
    monkeypatch.setenv("LAB_TRACKER_REVIEW_EMAIL_ENABLED", "true")
    monkeypatch.setenv("LAB_TRACKER_REVIEW_EMAIL_TRANSPORT", "external")

    settings = worker._worker_settings()

    assert settings.auth_secret_key == "runtime-secret-value-long-enough-for-production"
    assert settings.database_url == "sqlite+pysqlite:///:memory:"


@pytest.mark.parametrize("contents", ["", "x" * (worker.MAX_AUTH_SECRET_FILE_BYTES + 1)])
def test_worker_settings_rejects_invalid_runtime_secret_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contents: str,
) -> None:
    secret_file = tmp_path / "auth-secret-key"
    secret_file.write_text(contents, encoding="utf-8")
    monkeypatch.delenv("LAB_TRACKER_AUTH_SECRET_KEY", raising=False)
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY_FILE", str(secret_file))

    with pytest.raises(RuntimeError, match="empty or too large"):
        worker._worker_settings()


def test_worker_settings_reports_unreadable_runtime_secret_without_its_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing_file = tmp_path / "sensitive-runtime-location" / "auth-secret-key"
    monkeypatch.delenv("LAB_TRACKER_AUTH_SECRET_KEY", raising=False)
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY_FILE", str(missing_file))

    with pytest.raises(RuntimeError) as exc_info:
        worker._worker_settings()

    assert "could not read" in str(exc_info.value)
    assert str(missing_file) not in str(exc_info.value)


def test_compose_external_control_profile_is_primary_only_and_least_privilege() -> None:
    compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    service = compose.split("  review-email-control:\n", maxsplit=1)[1].split(
        "\n  postgres:",
        maxsplit=1,
    )[0]

    assert "review-email-external" in service
    assert "LAB_TRACKER_AUTH_SECRET_KEY_FILE" in service
    assert "app_data:/app/data:ro" in service
    assert "LAB_TRACKER_REVIEW_EMAIL_TRANSPORT: external" in service
    assert "shared-provider" not in service
    assert "OPENAI" not in service
    assert "MARION" not in service.upper()
    assert "ports:" not in service
