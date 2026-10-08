"""Reviewed, task-specific model choices; model IDs alone cannot rank quality.

When updating a choice, verify its source, endpoint and input/output contract,
then update reviewed_on. Expiring the review prevents indefinite claims of
currency. Do not pick the lexicographically newest ID returned by /models.
"""

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class ModelPolicy:
    provider: str
    setting: str
    workloads: tuple[str, ...]
    recommended_model: str
    source_url: str
    rationale: str
    reviewed_on: date
    superseded_models: tuple[str, ...] = ()
    accepted_snapshots: tuple[str, ...] = ()
    review_interval_days: int = 30

    @property
    def review_due_on(self) -> date:
        return self.reviewed_on + timedelta(days=self.review_interval_days)


OPENAI_GRAPH = ModelPolicy(
    provider="openai",
    setting="openai_model",
    workloads=("note_graph_draft", "daily_review", "analysis_graph_draft", "member_alignment"),
    recommended_model="gpt-6.1-sol",
    source_url="https://developers.openai.com/api/docs/models/gpt-6.1-sol",
    rationale="Quality-oriented graph reasoning with image input and Responses structured outputs.",
    reviewed_on=date(2026, 10, 8),
    superseded_models=(
        "gpt-4o-mini",
        "gpt-4o",
        "gpt-4.1-mini",
        "gpt-4.1",
        "gpt-5-mini",
        "gpt-5",
        "gpt-5.6-sol",
        "gpt-6-sol",
    ),
)
OPENAI_TRANSCRIPTION = ModelPolicy(
    provider="openai",
    setting="openai_transcription_model",
    workloads=("voice_transcription",),
    recommended_model="gpt-4o-mini-transcribe",
    source_url="https://developers.openai.com/api/docs/models/gpt-4o-mini-transcribe",
    rationale="Dedicated low-cost audio transcription; graph models do not accept this endpoint.",
    reviewed_on=date(2026, 10, 8),
    superseded_models=("whisper-1", "gpt-4o-mini-transcribe-2025-03-20"),
    accepted_snapshots=("gpt-4o-mini-transcribe-2025-12-15",),
)
ANTHROPIC_GRAPH = ModelPolicy(
    provider="anthropic",
    setting="anthropic_model",
    workloads=OPENAI_GRAPH.workloads,
    recommended_model="claude-sonnet-5-5",
    source_url="https://platform.claude.com/docs/en/models/sonnet-5-5/overview",
    rationale="Current Sonnet generation, with text/image input and the Messages endpoint.",
    reviewed_on=date(2026, 10, 8),
    superseded_models=(
        "claude-3-5-sonnet-latest",
        "claude-3-5-sonnet-20241022",
        "claude-sonnet-4-20250514",
        "claude-sonnet-4-5",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
    ),
)
GOOGLE_MULTIMODAL = ModelPolicy(
    provider="google",
    setting="google_model",
    workloads=(*OPENAI_GRAPH.workloads, "voice_transcription"),
    recommended_model="gemini-3.8-flash",
    source_url="https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash",
    rationale="Current stable Flash generation, with text/image/audio input and JSON output.",
    reviewed_on=date(2026, 10, 8),
    superseded_models=(
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "gemini-3-flash-preview",
        "gemini-3.5-flash",
        "gemini-3.6-flash",
        "gemini-3.7-flash",
    ),
)

MODEL_POLICIES = (OPENAI_GRAPH, OPENAI_TRANSCRIPTION, ANTHROPIC_GRAPH, GOOGLE_MULTIMODAL)
PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
}
