"""Advisory model currency and explicit, non-inference availability checks."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

import httpx

from lab_tracker.ai_model_catalog import MODEL_POLICIES, PROVIDER_BASE_URLS, ModelPolicy

if TYPE_CHECKING:
    from lab_tracker.config import Settings

Currency = Literal["recommended", "superseded", "unreviewed", "custom_endpoint"]
Availability = Literal["not_checked", "credential_missing", "available", "unavailable", "error"]


@dataclass
class ModelAvailability:
    status: Availability = "not_checked"
    resolved_model: str | None = None
    checked_at: str | None = None
    message: str | None = None


@dataclass
class AIModelStatus:
    provider: str
    setting: str
    workloads: list[str]
    active: bool
    configured_model: str
    recommended_model: str
    currency: Currency
    reviewed_on: str
    review_due_on: str
    review_overdue: bool
    source_url: str
    rationale: str
    warnings: list[str] = field(default_factory=list)
    availability: ModelAvailability = field(default_factory=ModelAvailability)
    recommendation_availability: ModelAvailability = field(default_factory=ModelAvailability)

    @property
    def needs_attention(self) -> bool:
        return (
            self.currency != "recommended"
            or self.review_overdue
            or bool(self.warnings)
            or self.availability.status in {"credential_missing", "unavailable", "error"}
            or self.recommendation_availability.status in {"unavailable", "error"}
        )


@dataclass
class AIModelAudit:
    checked_at: str
    models: list[AIModelStatus]
    attention_needed: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalized_model(provider: str, model: str) -> str:
    return model.removeprefix("models/") if provider == "google" else model


def _currency(policy: ModelPolicy, model: str, *, official_endpoint: bool) -> Currency:
    if not official_endpoint:
        return "custom_endpoint"
    if model in (policy.recommended_model, *policy.accepted_snapshots):
        return "recommended"
    # Dated OpenAI snapshots of a known older graph family are also older.
    base = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model) if policy.setting == "openai_model" else model
    if model in policy.superseded_models or base in policy.superseded_models:
        return "superseded"
    return "unreviewed"


def audit_ai_models(
    settings: Settings,
    *,
    today: date | None = None,
    check_availability: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AIModelAudit:
    """Inventory every retained AI setting; ordinary reads never contact a provider.

    Metadata probes check the configured model and the reviewed recommendation,
    once per distinct model. They send no research data or inference requests.
    Availability does not verify quota, request compatibility or graph quality.
    """
    from lab_tracker.config import resolve_graph_draft_provider

    now = datetime.now(timezone.utc)
    today = today or now.date()
    active_provider = resolve_graph_draft_provider(settings.graph_draft_provider)
    models = []
    probes: dict[tuple[str, str], ModelAvailability] = {}
    for policy in MODEL_POLICIES:
        configured = _normalized_model(policy.provider, getattr(settings, policy.setting).strip())
        base_url = getattr(settings, f"{policy.provider}_base_url").rstrip("/")
        official_endpoint = base_url == PROVIDER_BASE_URLS[policy.provider]
        warnings = []
        if (
            policy.setting == "openai_model"
            and configured == "gpt-6.1-sol"
            and settings.openai_reasoning_effort == "none"
        ):
            warnings.append(
                "GPT-6.1 Sol does not support reasoning effort 'none'; use low or higher."
            )
        status = AIModelStatus(
            provider=policy.provider,
            setting=f"LAB_TRACKER_{policy.setting.upper()}",
            workloads=list(policy.workloads),
            active=policy.provider == active_provider,
            configured_model=configured,
            recommended_model=policy.recommended_model,
            currency=_currency(policy, configured, official_endpoint=official_endpoint),
            reviewed_on=policy.reviewed_on.isoformat(),
            review_due_on=policy.review_due_on.isoformat(),
            review_overdue=today >= policy.review_due_on or today < policy.reviewed_on,
            source_url=policy.source_url,
            rationale=policy.rationale,
            warnings=warnings,
        )
        if check_availability:
            key = getattr(settings, f"{policy.provider}_api_key").strip()
            for model in (
                {configured, policy.recommended_model} if official_endpoint else {configured}
            ):
                probe_key = (policy.provider, model)
                if probe_key not in probes:
                    probes[probe_key] = _probe_model(
                        policy.provider,
                        model,
                        base_url,
                        key,
                        transport=transport,
                    )
            status.availability = probes[(policy.provider, configured)]
            if official_endpoint:
                status.recommendation_availability = probes[
                    (policy.provider, policy.recommended_model)
                ]
        models.append(status)
    return AIModelAudit(
        checked_at=now.isoformat(),
        models=models,
        attention_needed=(
            active_provider not in PROVIDER_BASE_URLS
            or any(item.active and item.needs_attention for item in models)
        ),
    )


def _probe_model(
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    *,
    transport: httpx.AsyncBaseTransport | None,
) -> ModelAvailability:
    checked_at = datetime.now(timezone.utc).isoformat()
    if not api_key:
        return ModelAvailability(status="credential_missing", checked_at=checked_at)
    headers = {"Authorization": f"Bearer {api_key}"}
    if provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    elif provider == "google":
        headers = {"x-goog-api-key": api_key}
    # A model name is a path segment, never an arbitrary URL or query string.
    url = f"{base_url}/models/{quote(model, safe='')}"
    try:
        status_code, payload = asyncio.run(
            asyncio.wait_for(_model_metadata(url, headers, transport), timeout=5.0)
        )
        if status_code == 404:
            return ModelAvailability(
                status="unavailable",
                checked_at=checked_at,
                message="Provider did not expose this model to this credential (HTTP 404).",
            )
        if status_code != 200:
            return ModelAvailability(
                status="error",
                checked_at=checked_at,
                message=f"Model metadata check failed (HTTP {status_code}).",
            )
        resolved = payload.get("name" if provider == "google" else "id")
        if not isinstance(resolved, str) or not resolved.strip() or api_key in resolved:
            raise ValueError("Missing model identifier")
        return ModelAvailability(
            status="available",
            checked_at=checked_at,
            resolved_model=_normalized_model(provider, resolved),
        )
    except (
        httpx.HTTPError,
        httpx.InvalidURL,
        ValueError,
        TypeError,
        AttributeError,
        asyncio.TimeoutError,
    ):
        # Do not echo exception messages, response bodies, keys or base URLs.
        return ModelAvailability(
            status="error",
            checked_at=checked_at,
            message="Model metadata check failed; verify the provider connection and credentials.",
        )


async def _model_metadata(
    url: str,
    headers: dict[str, str],
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[int, dict[str, Any]]:
    async with (
        httpx.AsyncClient(
            timeout=5.0,
            follow_redirects=False,
            transport=transport,
        ) as client,
        client.stream("GET", url, headers=headers) as response,
    ):
        if response.status_code != 200:
            return response.status_code, {}
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > 64 * 1024:
                raise ValueError("Model metadata exceeds the response limit")
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("Model metadata must be an object")
        return response.status_code, payload
