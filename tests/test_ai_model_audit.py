"""Currency is a reviewed policy; access checks never infer quality or mutate data."""

import asyncio
import json
from datetime import date

import httpx
import pytest

from lab_tracker.ai_model_audit import audit_ai_models
from lab_tracker.ai_model_catalog import MODEL_POLICIES
from lab_tracker.config import Settings
from lab_tracker.graph_drafting import OpenAIGraphDraftClient

REVIEW_DAY = date(2026, 10, 8)


def settings(**overrides):
    return Settings(_env_file=None, environment="local", **overrides)


def test_offline_audit_covers_all_retained_uses_without_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("An ordinary model audit must not make a network request")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    audit = audit_ai_models(settings(), today=REVIEW_DAY)
    assert len(audit.models) == 4
    assert {item.setting for item in audit.models} == {
        f"LAB_TRACKER_{policy.setting.upper()}" for policy in MODEL_POLICIES
    }
    assert {work for item in audit.models for work in item.workloads} == {
        "note_graph_draft",
        "daily_review",
        "analysis_graph_draft",
        "member_alignment",
        "voice_transcription",
    }
    assert all(item.currency == "recommended" for item in audit.models)
    assert all(item.availability.status == "not_checked" for item in audit.models)
    assert [item.configured_model for item in audit.models if item.active] == [
        "gpt-6.1-sol",
        "gpt-4o-mini-transcribe",
    ]
    assert not audit.attention_needed


@pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-4o-mini-2024-07-18", "gpt-5.6-sol"])
def test_explicit_older_model_is_preserved_and_flagged(model):
    audit = audit_ai_models(settings(openai_model=model), today=REVIEW_DAY)
    assert audit.models[0].configured_model == model
    assert audit.models[0].currency == "superseded"
    assert audit.models[0].recommended_model == "gpt-6.1-sol"
    assert audit.attention_needed


def test_unknown_model_is_unreviewed_instead_of_ranked_by_name():
    audit = audit_ai_models(settings(openai_model="gpt-99-new-model"), today=REVIEW_DAY)
    assert audit.models[0].currency == "unreviewed"
    assert audit.attention_needed


def test_custom_endpoint_does_not_inherit_official_model_currency():
    audit = audit_ai_models(
        settings(openai_base_url="http://localhost:1234/v1"),
        today=REVIEW_DAY,
    )
    assert all(item.currency == "custom_endpoint" for item in audit.models if item.active)
    assert audit.attention_needed


@pytest.mark.parametrize("day,overdue", [(date(2026, 11, 6), False), (date(2026, 11, 7), True)])
def test_recommendation_expires_without_changing_configured_model(day, overdue):
    audit = audit_ai_models(settings(), today=day)
    assert audit.models[0].configured_model == "gpt-6.1-sol"
    assert audit.models[0].review_overdue is overdue
    assert audit.attention_needed is overdue


def test_inactive_provider_drift_is_reported_but_does_not_fail_active_audit():
    audit = audit_ai_models(settings(anthropic_model="claude-3-5-sonnet-latest"), today=REVIEW_DAY)
    assert audit.models[2].currency == "superseded"
    assert not audit.models[2].active
    assert not audit.attention_needed


@pytest.mark.parametrize(
    "provider,active_setting",
    [
        ("claude", "LAB_TRACKER_ANTHROPIC_MODEL"),
        ("gemini", "LAB_TRACKER_GOOGLE_MODEL"),
    ],
)
def test_provider_aliases_and_google_model_prefix(provider, active_setting):
    audit = audit_ai_models(
        settings(graph_draft_provider=provider, google_model="models/gemini-3.8-flash"),
        today=REVIEW_DAY,
    )
    assert [item.setting for item in audit.models if item.active] == [active_setting]
    assert audit.models[3].currency == "recommended"


def test_known_unsupported_reasoning_setting_is_flagged():
    audit = audit_ai_models(settings(openai_reasoning_effort="none"), today=REVIEW_DAY)
    assert audit.models[0].warnings
    assert audit.attention_needed


def test_metadata_probes_use_correct_auth_and_check_both_configured_and_recommended():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert not request.content
        if request.url.host == "api.openai.com":
            assert request.headers["Authorization"] == "Bearer openai-test-secret"
            if request.url.path.endswith("gpt-4o-mini"):
                return httpx.Response(404, json={"error": "old model"})
        elif request.url.host == "api.anthropic.com":
            assert request.headers["x-api-key"] == "anthropic-test-secret"
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert "authorization" not in request.headers
        else:
            assert request.headers["x-goog-api-key"] == "google-test-secret"
            assert "key" not in request.url.params
            return httpx.Response(200, json={"name": "models/gemini-3.8-flash"})
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})

    audit = audit_ai_models(
        settings(
            openai_model="gpt-4o-mini",
            openai_api_key="openai-test-secret",
            anthropic_api_key="anthropic-test-secret",
            google_api_key="google-test-secret",
        ),
        today=REVIEW_DAY,
        check_availability=True,
        transport=httpx.MockTransport(handler),
    )
    assert len(requests) == 5
    assert audit.models[0].availability.status == "unavailable"
    assert audit.models[0].recommendation_availability.status == "available"
    assert audit.models[0].currency == "superseded"
    assert audit.models[3].availability.resolved_model == "gemini-3.8-flash"
    assert "test-secret" not in json.dumps(audit.as_dict())


@pytest.mark.parametrize("failure", ["http", "transport", "json", "oversized", "redirect", "echo"])
def test_probe_failures_are_advisory_and_never_expose_secrets(failure):
    secret = "provider-super-secret"

    def handler(request):
        if failure == "transport":
            raise httpx.ConnectError(secret, request=request)
        if failure == "http":
            return httpx.Response(401, json={"error": secret})
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://other.example/models"})
        if failure == "json":
            return httpx.Response(200, text=secret)
        if failure == "oversized":
            return httpx.Response(200, json={"id": "m", "extra": "x" * 65536})
        return httpx.Response(200, json={"id": secret})

    audit = audit_ai_models(
        settings(openai_api_key=secret),
        today=REVIEW_DAY,
        check_availability=True,
        transport=httpx.MockTransport(handler),
    )
    assert audit.models[0].availability.status == "error"
    assert secret not in json.dumps(audit.as_dict())
    assert audit.attention_needed


def test_missing_credentials_skip_metadata_requests():
    def forbidden(request):
        raise AssertionError("No request without a credential")

    audit = audit_ai_models(
        settings(openai_api_key="", anthropic_api_key="", google_api_key=""),
        today=REVIEW_DAY,
        check_availability=True,
        transport=httpx.MockTransport(forbidden),
    )
    assert all(item.availability.status == "credential_missing" for item in audit.models)
    assert audit.attention_needed


def test_invalid_custom_endpoint_is_an_advisory_probe_error():
    audit = audit_ai_models(
        settings(openai_base_url="http://[invalid-host]/v1", openai_api_key="test-key"),
        today=REVIEW_DAY,
        check_availability=True,
    )
    assert audit.models[0].currency == "custom_endpoint"
    assert audit.models[0].availability.status == "error"
    assert audit.models[0].recommendation_availability.status == "not_checked"


def test_metadata_probe_has_a_whole_request_deadline(monkeypatch):
    from lab_tracker import ai_model_audit

    async def stalled_metadata(*args):
        await asyncio.sleep(10)
        raise AssertionError("Deadline should cancel the request")

    wait_for = asyncio.wait_for
    monkeypatch.setattr(ai_model_audit, "_model_metadata", stalled_metadata)
    monkeypatch.setattr(asyncio, "wait_for", lambda request, timeout: wait_for(request, 0.01))
    audit = audit_ai_models(
        settings(openai_api_key="test-key"),
        today=REVIEW_DAY,
        check_availability=True,
    )
    assert audit.models[0].availability.status == "error"
    assert audit.attention_needed


@pytest.mark.parametrize("mode", ["note", "batch", "analysis"])
def test_sol_default_uses_responses_structured_output_for_every_graph_workload(mode):
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert body["model"] == "gpt-6.1-sol"
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True
        assert "temperature" not in body
        return httpx.Response(200, json={"output_text": '{"operations": []}'})

    client = OpenAIGraphDraftClient(
        api_key="test-key",
        model=settings().openai_model,
        transport=httpx.MockTransport(handler),
    )
    try:
        if mode == "note":
            result = client.draft_from_note(
                source_artifacts=[{"transcript_text": "Synthetic note"}]
            )
        elif mode == "batch":
            result = client.draft_from_batch(batch_context={"batch_notes": [{"id": "synthetic"}]})
        else:
            result = client.draft_from_analysis_evidence(
                evidence_text="Synthetic evidence", project_context={}
            )
        assert result["operations"] == []
        assert len(requests) == 1
    finally:
        client.close()


@pytest.mark.parametrize("model,exit_code", [("gpt-6.1-sol", 0), ("gpt-4o-mini", 1)])
def test_models_cli_strict_reports_existing_config_without_mutation(
    monkeypatch, capsys, model, exit_code
):
    from lab_tracker import ai_model_audit, cli

    configured = settings(openai_model=model)
    monkeypatch.setattr(cli, "get_settings", lambda: configured)
    monkeypatch.setattr(
        ai_model_audit,
        "audit_ai_models",
        lambda settings, check_availability: audit_ai_models(settings, today=REVIEW_DAY),
    )
    if exit_code:
        with pytest.raises(SystemExit) as error:
            cli.main(["models", "--strict", "--json"])
        assert error.value.code == exit_code
    else:
        cli.main(["models", "--strict", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["models"][0]["configured_model"] == model
    assert payload["attention_needed"] is bool(exit_code)
    assert configured.openai_model == model
