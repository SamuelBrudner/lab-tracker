"""Stdin/stdout contract for ``lt`` commands run as Claude Code hooks.

Claude Code runs a hook command with a JSON payload on stdin: the common
fields (``session_id``, ``transcript_path``, ``cwd``, ``hook_event_name``)
plus the event's own, such as ``prompt`` for ``UserPromptSubmit``. It reads
stdout that starts with ``{`` and ends with ``}`` as structured hook output
and drops unknown top-level keys without an error, so a command's ordinary
JSON result never reaches the agent. On ``SessionStart`` and
``UserPromptSubmit`` a hook adds context through
``hookSpecificOutput.additionalContext``, the shape :func:`context_output`
builds. See https://code.claude.com/docs/en/hooks.

A command recognises a hook by its stdin, not by a flag, so the scaffolded
``.claude/settings.json`` command lines stay unchanged and repos scaffolded
before this contract existed pick it up with a client upgrade alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, TextIO

from lab_tracker_client.client import LTValidationError

JsonObject = dict[str, Any]

# Events whose hookSpecificOutput takes additionalContext and that the
# scaffold wires: SessionStart runs `lt setup status --brief`, and
# UserPromptSubmit runs `lt prime --if-research-facing`.
CONTEXT_EVENTS = frozenset({"SessionStart", "UserPromptSubmit"})
PRIME_CONTEXT_HEADER = (
    "lab-tracker: open questions that advance active goals, ranked for this "
    "research-facing prompt (`lt prime` JSON):"
)


@dataclass(frozen=True)
class HookPayload:
    """The JSON object a hook runner sent on stdin."""

    event: str
    fields: JsonObject


def read_stdin(stream: TextIO) -> HookPayload | str:
    """Read all of ``stream``: a hook payload when it carries one, else its text.

    A payload is decoded from the raw bytes as JSON (UTF-8, as Claude Code
    writes it), so a prompt survives a console code page that could not
    decode it. Any other input is decoded with the stream's own encoding, as
    reading the text stream directly would.
    """

    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        text = stream.read()
        return _hook_payload(text) or text
    data: bytes = buffer.read()
    return _hook_payload(data) or data.decode(stream.encoding, stream.errors or "strict")


def read_piped_hook_payload(stream: TextIO | None) -> HookPayload | None:
    """The hook payload piped to ``stream``, or ``None`` when it carries none.

    A terminal, a missing or unreadable stream, and piped input that is not
    a hook payload all carry none. Only a pipe is read, so a person running
    the command at a terminal is never left waiting on stdin.
    """

    if stream is None or stream.isatty() or not stream.readable():
        return None
    value = read_stdin(stream)
    return value if isinstance(value, HookPayload) else None


def hook_prompt(payload: HookPayload) -> str:
    """The submitted prompt text from a ``UserPromptSubmit`` payload."""

    prompt = payload.fields.get("prompt")
    if payload.event != "UserPromptSubmit" or not isinstance(prompt, str):
        raise LTValidationError(
            "lt prime --if-research-facing classifies the `prompt` of a "
            f"UserPromptSubmit hook payload; stdin carried a {payload.event} "
            "payload without one."
        )
    return prompt


def context_output(payload: HookPayload, context: str) -> JsonObject:
    """Hook output that adds ``context`` to the agent's context window."""

    if payload.event not in CONTEXT_EVENTS:
        raise LTValidationError(
            f"lt adds hook context only on {', '.join(sorted(CONTEXT_EVENTS))} "
            f"hooks; stdin carried a {payload.event} payload."
        )
    return {
        "hookSpecificOutput": {
            "hookEventName": payload.event,
            "additionalContext": context,
        }
    }


def status_context(brief: JsonObject) -> str:
    """The ``lt setup status --brief`` result as context lines.

    The brief line already names the first suggestion; the rest follow as
    bullets so the agent sees every one.
    """

    lines = [str(brief["brief"])]
    lines.extend(f"- {suggestion}" for suggestion in brief["suggestions"][1:])
    return "\n".join(lines)


def prime_context(next_questions: JsonObject) -> str:
    """The ``lt prime`` result as context: a header line and compact JSON."""

    body = json.dumps(next_questions, ensure_ascii=False, separators=(",", ":"))
    return f"{PRIME_CONTEXT_HEADER}\n{body}"


def _hook_payload(data: str | bytes) -> HookPayload | None:
    try:
        value = json.loads(data)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    event = value.get("hook_event_name")
    if not isinstance(event, str) or not event:
        return None
    return HookPayload(event=event, fields=value)
