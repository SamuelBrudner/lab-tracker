"""Reviewer revision inputs: typed feedback, dictated audio, and attached images."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lab_tracker.errors import ValidationError
from lab_tracker.graph_drafting import GraphDraftClient, GraphDraftingError
from lab_tracker.provider_error_redaction import provider_error_message


@dataclass(frozen=True)
class RevisionUpload:
    """A reviewer-supplied audio or image upload."""

    content: bytes
    filename: str
    content_type: str

    @property
    def is_audio(self) -> bool:
        return self.content_type.lower().startswith("audio/")

    @property
    def is_image(self) -> bool:
        return self.content_type.lower().startswith("image/")


@dataclass
class RevisionInputs:
    """Optional rich inputs accompanying reviewer revision feedback."""

    audio: RevisionUpload | None = None
    attachments: list[RevisionUpload] = field(default_factory=list)


def resolve_revision_feedback(
    feedback: str | None,
    audio: RevisionUpload | None,
    draft_client: GraphDraftClient,
) -> tuple[str, str]:
    typed = (feedback or "").strip()
    transcript = ""
    if audio is not None:
        if not audio.is_audio:
            raise ValidationError("Dictated feedback must be an audio upload.")
        transcribe_audio = getattr(draft_client, "transcribe_audio", None)
        if not callable(transcribe_audio):
            raise ValidationError(
                "Configured draft client does not support audio transcription."
            )
        try:
            response = transcribe_audio(
                audio_bytes=audio.content,
                filename=audio.filename,
                content_type=audio.content_type,
                prompt=typed or None,
            )
        except GraphDraftingError as exc:
            raise ValidationError(
                f"Could not transcribe dictated feedback: {provider_error_message(exc)}"
            ) from exc
        transcript = _revision_transcript_text(response)
        if not transcript:
            raise ValidationError("Dictated feedback transcription returned no text.")
    combined = "\n\n".join(part for part in (typed, transcript) if part).strip()
    return combined, transcript

def prepare_revision_attachments(
    attachments: list[RevisionUpload],
) -> tuple[list[dict[str, Any]], list[str]]:
    extra_images: list[dict[str, Any]] = []
    labels: list[str] = []
    for attachment in attachments:
        if not attachment.is_image:
            raise ValidationError(
                f"Attached file {attachment.content_type!r} is not a supported image type."
            )
        if not attachment.content:
            raise ValidationError(f"Attached image {attachment.filename!r} is empty.")
        extra_images.append(
            {
                "image_bytes": attachment.content,
                "content_type": attachment.content_type,
            }
        )
        labels.append(attachment.filename or "image")
    return extra_images, labels


def _revision_transcript_text(transcript: Any) -> str:
    if isinstance(transcript, str):
        return transcript.strip()
    if isinstance(transcript, dict):
        text = transcript.get("text")
        if isinstance(text, str):
            return text.strip()
    return ""
