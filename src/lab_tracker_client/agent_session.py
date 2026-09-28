"""Agent-session retrospective capture: the trigger behind ``lt agent session-end``.

A coding agent's ``SessionEnd`` hook pipes its hook JSON (``session_id``,
``transcript_path``, ``cwd``, ``hook_event_name``, ``reason``) to
``lt agent session-end``. For a session in a checkout bound to a project,
this module reads the agent's JSONL transcript once, within fixed bounds, and
distills a retrospective packet: what the person asked, which files the agent
wrote or edited (repo-relative paths only), which commands it ran (the
agent's own ``description`` of each command in preference to the raw command
line), how tests and linters came out, and the agent's final message.
Secrets are redacted, every excerpt is capped, and the transcript itself
never leaves the machine: only its SHA-256 and a ``file://`` pointer are
recorded.

The packet lands as ONE staged-note event in the checkout's watch outbox with
``payload.request_draft`` set, so the sync that uploads it asks the server
for a draft whose decision, dead-end, and pivot proposals wait in the human
review queue. Nothing commits: AI can suggest; only a person commits.

Fail-soft by contract: an unbound checkout, a missing or malformed
transcript, or an unreachable server never raises into the agent's shell.
Unbound checkouts are skipped with one stderr notice and nothing is sent or
queued; trivially short sessions (no file edits and fewer than
:data:`MIN_PROMPTS_WITHOUT_EDITS` prompts) are skipped silently.
``LAB_TRACKER_AGENT_HOOKS=0`` turns every agent hook off.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from lab_tracker_client import gitinfo
from lab_tracker_client import watch as watch_capture
from lab_tracker_client.capture_project import resolve_capture_project
from lab_tracker_client.client import LabTracker
from lab_tracker_client.git_capture import resolve_watch_config
from lab_tracker_client.redaction import redact_capture_text

JsonObject = dict[str, Any]

AGENT_HOOKS_ENV = "LAB_TRACKER_AGENT_HOOKS"
DEFAULT_AGENT = "claude-code"
CAPTURE_KIND = "agent_session_retrospective"
ADAPTER = "lt-agent-session"
SOURCE_PROVIDER = "agent-session"
BODY_HEADING = "Agent session retrospective"
SESSION_END_EVENT = "SessionEnd"
PROJECT_DIR_ENV = "CLAUDE_PROJECT_DIR"

# Skip rule: a session with no file edits needs at least this many prompts.
MIN_PROMPTS_WITHOUT_EDITS = 3

# Bounds. Every excerpt stored is capped; docs/agent-session-capture.md lists them.
MAX_HOOK_INPUT_CHARS = 8 * 1024 * 1024
MAX_TRANSCRIPT_LINE_BYTES = 1024 * 1024
MAX_TRANSCRIPT_BYTES = 256 * 1024 * 1024
PARSE_DEADLINE_SECONDS = 10.0
MAX_PROMPT_CHARS = 800
MAX_PROMPTS_TOTAL_CHARS = 6000
MAX_PROMPTS = 30
MAX_FILES_LISTED = 50
MAX_COMMANDS_LISTED = 30
MAX_COMMAND_CHARS = 160
MAX_CHECKS_LISTED = 10
MAX_CHECK_SUMMARY_CHARS = 200
MAX_FINAL_MESSAGE_CHARS = 3000
MAX_BODY_CHARS = 20000
# Internal memory bounds while streaming a long transcript.
_MAX_TRACKED_PATHS = 500
_MAX_TRACKED_COMMANDS = 200
_MAX_TRACKED_CHECKS = 200
_RESULT_TAIL_CHARS = 4000
# Best-effort drain after queuing: one short reachability probe, then at most
# this many outbox events, each request bounded by the timeout.
SYNC_TIMEOUT_SECONDS = 5.0
SYNC_EVENT_LIMIT = 10

EDIT_TOOL_PATH_KEYS: Mapping[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
SHELL_TOOLS = frozenset({"Bash"})


def agent_hooks_enabled() -> bool:
    """False only when the kill switch is set (``0``, ``false``, ``no``, ``off``)."""

    return os.getenv(AGENT_HOOKS_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

def redact_secrets(text: str) -> str:
    """Remove credential-shaped material from free text before it is stored.

    A thin call into the client's one redactor,
    :func:`lab_tracker_client.redaction.redact_capture_text` (private keys,
    credentialed URLs and query secrets, credential headers and ``Bearer``
    values, secret flags and ``NAME=value`` / ``name: value`` assignments,
    ``-u user:password``, well-known token shapes, and the client's own
    credential variables). Over-redaction is the accepted failure mode.
    """

    return redact_capture_text(text)


def _clean(text: str, limit: int) -> str:
    """Redact, then cap: a secret is never left half-cut at the boundary."""

    # Pre-cut generously so redaction runs on bounded text; the final cap
    # discards the pre-cut tail, where a secret could have been split.
    return _bounded(redact_secrets(text[: limit * 4]), limit)


def _bounded(text: str, limit: int) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: max(0, limit - 1)].rstrip() + "…"


# --------------------------------------------------------------------------
# Hook input
# --------------------------------------------------------------------------

_SALVAGE_KEYS = (
    "session_id",
    "transcript_path",
    "cwd",
    "hook_event_name",
    "reason",
    "tool_name",
    "file_path",
    "notebook_path",
)


def read_hook_input(
    stream: IO[str] | None = None, *, max_chars: int = MAX_HOOK_INPUT_CHARS
) -> JsonObject:
    """Parse the hook JSON an agent pipes on stdin; ``{}`` when there is none.

    A terminal stdin is never read (a manual run must not hang). Input past
    ``max_chars`` (a ``Write`` of a large file carries its whole content) is
    drained unread and the scalar fields the hooks need are salvaged from
    the head, where agents put them. Never raises.
    """

    source = sys.stdin if stream is None else stream
    if source is None:
        return {}
    try:
        if source.isatty():
            return {}
        text = source.read(max_chars + 1)
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    if len(text) > max_chars:
        _drain(source)
        return _salvage_hook_fields(text[:max_chars])
    try:
        payload = json.loads(text) if text.strip() else {}
    except ValueError:
        return _salvage_hook_fields(text)
    return payload if isinstance(payload, dict) else {}


def _drain(source: IO[str]) -> None:
    with suppress(OSError, ValueError, UnicodeDecodeError):
        while source.read(1024 * 1024):
            pass


def _salvage_hook_fields(head: str) -> JsonObject:
    """Recover top-level-looking string fields from an unparseable hook payload."""

    salvaged: JsonObject = {}
    tool_input: JsonObject = {}
    for key in _SALVAGE_KEYS:
        match = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', head)
        if match is None:
            continue
        with suppress(ValueError):
            value = json.loads(f'"{match.group(1)}"')
            if key in {"file_path", "notebook_path"}:
                tool_input[key] = value
            else:
                salvaged[key] = value
    if tool_input:
        salvaged["tool_input"] = tool_input
    return salvaged


def hook_base_dir(hook: Mapping[str, Any], explicit: str | None = None) -> Path:
    """The checkout directory a hook acts for.

    ``--repo`` first, then Claude Code's ``CLAUDE_PROJECT_DIR`` (the project
    root whose settings installed the hook; it stays put when the agent
    enters a worktree, so captures land in the outbox that scheduled drains
    empty), then the hook payload's ``cwd``, then the process directory.
    """

    for candidate in (explicit, os.getenv(PROJECT_DIR_ENV), hook.get("cwd")):
        if isinstance(candidate, str) and candidate.strip():
            return Path(candidate.strip()).expanduser()
    return Path.cwd()


def hook_cwd(hook: Mapping[str, Any], fallback: Path) -> Path:
    """Where the agent was working, for resolving relative tool paths."""

    value = hook.get("cwd")
    if isinstance(value, str) and value.strip():
        return Path(value.strip()).expanduser()
    return fallback


# --------------------------------------------------------------------------
# Transcript digest
# --------------------------------------------------------------------------

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_LOCAL_OUTPUT_PREFIXES = (
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
)
_INTERRUPTED_MARKERS = frozenset(
    {"[Request interrupted by user]", "[Request interrupted by user for tool use]"}
)
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_TEST_COMMAND_RE = re.compile(
    r"(?:^|[\s;&|(])(?:pytest|py\.test|python[0-9.]*\s+-m\s+(?:pytest|unittest)|tox|nox"
    r"|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test[\w:.-]*|vitest|jest|playwright\s+test"
    r"|go\s+test|cargo\s+test|dotnet\s+test|ctest|make\s+(?:test|check)|R\s+CMD\s+check)"
    r"(?![\w-])",
    re.IGNORECASE,
)
_LINT_COMMAND_RE = re.compile(
    r"(?:^|[\s;&|(])(?:ruff|flake8|pylint|mypy|pyright|black\s+--check|isort\s+--check"
    r"|eslint|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:lint|typecheck)[\w:.-]*|tsc"
    r"|prettier\s+--check|shellcheck|golangci-lint|cargo\s+clippy)(?![\w-])",
    re.IGNORECASE,
)
_FAILED_COUNT_RE = re.compile(r"\b[1-9]\d* (?:failed|failing|errors?)\b", re.IGNORECASE)
_EXIT_CODE_RE = re.compile(r"\bexit (?:code|status):?\s*[1-9]\d*\b", re.IGNORECASE)
_SUMMARY_LINE_RE = re.compile(
    r"^(?:=+ .*\b(?:passed|failed|errors?|skipped|no tests ran|deselected)\b.* =+"
    r"|\d+ (?:passed|failed)\b.*"
    r"|(?:Tests?|Test Files|Test Suites):?\s+\d.*"
    r"|All checks passed!?"
    r"|Found \d+ errors?\b.*"
    r"|Success: no issues found.*"
    r"|\S*\s*\d+ problems? \(.*\)"
    r"|(?:ok|FAIL)\s+\S+.*"
    r"|test result: .*)$",
    re.IGNORECASE,
)


@dataclass
class CheckRun:
    """One test or lint command the agent ran, and how it came out."""

    kind: str
    label: str
    outcome: str = "unknown"
    summary: str = ""


@dataclass
class TranscriptDigest:
    """The bounded, redacted facts a retrospective packet is rendered from."""

    prompts: list[str] = field(default_factory=list)
    prompt_count: int = 0
    edited_files: dict[str, Counter[str]] = field(default_factory=dict)
    edited_files_overflow: int = 0
    outside_edit_count: int = 0
    commands: dict[str, int] = field(default_factory=dict)
    commands_overflow: int = 0
    command_count: int = 0
    checks: list[CheckRun] = field(default_factory=list)
    final_message: str = ""
    tool_call_count: int = 0
    model: str = ""
    client_version: str = ""
    sha256: str = ""
    bytes_read: int = 0
    lines: int = 0
    malformed_lines: int = 0
    oversized_lines: int = 0
    complete: bool = True

    @property
    def edit_count(self) -> int:
        """Distinct files edited, inside or outside the checkout."""

        return len(self.edited_files) + self.edited_files_overflow + self.outside_edit_count

    @property
    def failed_check_count(self) -> int:
        return sum(1 for check in self.checks if check.outcome == "failed")


class _DigestBuilder:
    """Streams transcript records into a :class:`TranscriptDigest`."""

    def __init__(self, *, checkout: Path | None, cwd: Path | None) -> None:
        self.digest = TranscriptDigest()
        self._checkout = checkout
        self._cwd = cwd or checkout
        self._prompt_chars = 0
        # tool_use id -> (repo-relative display path or None when outside, tool, raw path)
        self._edits: dict[str, tuple[str | None, str, str]] = {}
        self._failed_edit_ids: set[str] = set()
        self._checks: dict[str, CheckRun] = {}
        self._final_id: str | None = None
        self._final_parts: list[str] = []

    def feed(self, record: Mapping[str, Any]) -> None:
        kind = record.get("type")
        version = record.get("version")
        if isinstance(version, str) and version:
            self.digest.client_version = version
        if kind == "user":
            self._feed_user(record)
        elif kind == "assistant":
            self._feed_assistant(record)

    def _feed_user(self, record: Mapping[str, Any]) -> None:
        message = record.get("message")
        if not isinstance(message, Mapping):
            return
        content = message.get("content")
        blocks = (
            [block for block in content if isinstance(block, Mapping)]
            if isinstance(content, list)
            else []
        )
        results = [block for block in blocks if block.get("type") == "tool_result"]
        for block in results:
            self._feed_tool_result(block, record.get("toolUseResult"))
        if results or "toolUseResult" in record:
            return
        if record.get("isSidechain") or record.get("isMeta") or record.get("isCompactSummary"):
            return
        if isinstance(content, str):
            texts = [content]
            images = 0
        else:
            texts = [
                str(block.get("text"))
                for block in blocks
                if block.get("type") == "text" and isinstance(block.get("text"), str)
            ]
            images = sum(1 for block in blocks if block.get("type") == "image")
        prompt = _normalize_prompt("\n\n".join(texts))
        if prompt is None and not images:
            return
        if images:
            prompt = f"{prompt or ''} [+{images} image(s)]".strip()
        self._add_prompt(prompt or "")

    def _add_prompt(self, prompt: str) -> None:
        self.digest.prompt_count += 1
        if len(self.digest.prompts) >= MAX_PROMPTS:
            return
        remaining = MAX_PROMPTS_TOTAL_CHARS - self._prompt_chars
        if remaining < 40:
            return
        cleaned = _clean(prompt, min(MAX_PROMPT_CHARS, remaining))
        if not cleaned:
            return
        self.digest.prompts.append(cleaned)
        self._prompt_chars += len(cleaned)

    def _feed_assistant(self, record: Mapping[str, Any]) -> None:
        message = record.get("message")
        if not isinstance(message, Mapping):
            return
        model = message.get("model")
        if isinstance(model, str) and model and not model.startswith("<"):
            self.digest.model = model
        content = message.get("content")
        if not isinstance(content, list):
            return
        sidechain = bool(record.get("isSidechain"))
        message_id = str(message.get("id") or record.get("uuid") or "")
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "tool_use":
                self._feed_tool_use(block)
            elif block.get("type") == "text" and not sidechain:
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    self._add_final_text(message_id, text)

    def _add_final_text(self, message_id: str, text: str) -> None:
        if message_id != self._final_id:
            self._final_id = message_id
            self._final_parts = []
        if sum(len(part) for part in self._final_parts) < MAX_FINAL_MESSAGE_CHARS * 4:
            self._final_parts.append(text)

    def _feed_tool_use(self, block: Mapping[str, Any]) -> None:
        self.digest.tool_call_count += 1
        name = str(block.get("name") or "")
        tool_id = str(block.get("id") or "")
        tool_input = block.get("input")
        if not isinstance(tool_input, Mapping):
            return
        path_key = EDIT_TOOL_PATH_KEYS.get(name)
        if path_key is not None:
            raw_path = tool_input.get(path_key) or tool_input.get("file_path")
            if isinstance(raw_path, str) and raw_path.strip():
                self._add_edit(tool_id, raw_path.strip(), name)
            return
        if name in SHELL_TOOLS:
            command = tool_input.get("command")
            if isinstance(command, str) and command.strip():
                description = tool_input.get("description")
                self._add_command(
                    tool_id,
                    command,
                    description if isinstance(description, str) else "",
                )

    def _add_edit(self, tool_id: str, raw_path: str, tool: str) -> None:
        if len(self._edits) >= _MAX_TRACKED_PATHS * 8:
            return
        key = tool_id or f"edit-{len(self._edits)}"
        self._edits[key] = (self._display_path(raw_path), tool, raw_path)

    def _display_path(self, raw_path: str) -> str | None:
        if self._checkout is None:
            return None
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (self._cwd or self._checkout) / path
        for candidate in (Path(os.path.normpath(path)), _resolved(path)):
            if candidate is None:
                continue
            with suppress(ValueError):
                relative = candidate.relative_to(self._checkout).as_posix()
                return _clean(relative, 300) if relative != "." else None
        return None

    def _add_command(self, tool_id: str, command: str, description: str) -> None:
        self.digest.command_count += 1
        label = _command_label(command, description)
        if label in self.digest.commands:
            self.digest.commands[label] += 1
        elif len(self.digest.commands) < _MAX_TRACKED_COMMANDS:
            self.digest.commands[label] = 1
        else:
            self.digest.commands_overflow += 1
        kind = _check_kind(command)
        if kind is None:
            return
        if len(self._checks) >= _MAX_TRACKED_CHECKS:
            self._checks.pop(next(iter(self._checks)))
        self._checks[tool_id or f"check-{self.digest.command_count}"] = CheckRun(kind, label)

    def _feed_tool_result(self, block: Mapping[str, Any], tool_use_result: Any) -> None:
        tool_id = str(block.get("tool_use_id") or "")
        is_error = block.get("is_error") is True
        if tool_id in self._edits and is_error:
            self._failed_edit_ids.add(tool_id)
        check = self._checks.get(tool_id)
        if check is None:
            return
        text = _result_text(block.get("content"))
        interrupted = False
        if isinstance(tool_use_result, Mapping):
            interrupted = tool_use_result.get("interrupted") is True
            if not text:
                text = "\n".join(
                    str(tool_use_result.get(key) or "") for key in ("stdout", "stderr")
                )[-_RESULT_TAIL_CHARS:]
        check.outcome, check.summary = _check_outcome(
            is_error=is_error, text=text, interrupted=interrupted
        )

    def finish(self) -> TranscriptDigest:
        digest = self.digest
        outside: set[str] = set()
        overflow: set[str] = set()
        for tool_id, (display, tool, raw_path) in self._edits.items():
            # An edit the agent's tool rejected (is_error) changed nothing.
            if tool_id in self._failed_edit_ids:
                continue
            if display is None:
                outside.add(raw_path)
            elif display in digest.edited_files:
                digest.edited_files[display][tool] += 1
            elif len(digest.edited_files) < _MAX_TRACKED_PATHS:
                digest.edited_files[display] = Counter({tool: 1})
            else:
                overflow.add(display)
        digest.edited_files_overflow = len(overflow)
        digest.outside_edit_count = len(outside)
        digest.checks = list(self._checks.values())
        if self._final_parts:
            digest.final_message = _clean("\n\n".join(self._final_parts), MAX_FINAL_MESSAGE_CHARS)
        return digest


def _resolved(path: Path) -> Path | None:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _normalize_prompt(text: str) -> str | None:
    """A person's prompt text, or ``None`` for agent-internal user records."""

    cleaned = _REMINDER_RE.sub("", text).strip()
    if not cleaned or cleaned in _INTERRUPTED_MARKERS:
        return None
    if cleaned.startswith(_LOCAL_OUTPUT_PREFIXES):
        return None
    name = _COMMAND_NAME_RE.search(cleaned)
    if name is not None:
        args = _COMMAND_ARGS_RE.search(cleaned)
        arg_text = args.group(1).strip() if args else ""
        # A bare slash command (/clear, /compact) is not a request.
        return f"{name.group(1).strip()} {arg_text}" if arg_text else None
    return cleaned


def _command_label(command: str, description: str) -> str:
    if description.strip():
        return _clean(" ".join(description.split()), MAX_COMMAND_CHARS)
    first_line = command.strip().splitlines()[0] if command.strip() else ""
    extra = "" if len(command.strip().splitlines()) <= 1 else " (multi-line)"
    return "$ " + _clean(" ".join(first_line.split()), MAX_COMMAND_CHARS) + extra


def _check_kind(command: str) -> str | None:
    lines = command.strip().splitlines()
    first_line = _QUOTED_RE.sub("''", lines[0]) if lines else ""
    if _TEST_COMMAND_RE.search(first_line):
        return "test"
    if _LINT_COMMAND_RE.search(first_line):
        return "lint"
    return None


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content[-_RESULT_TAIL_CHARS:]
    if isinstance(content, list):
        parts = [
            str(block.get("text"))
            for block in content
            if isinstance(block, Mapping) and isinstance(block.get("text"), str)
        ]
        return "\n".join(parts)[-_RESULT_TAIL_CHARS:]
    return ""


def _check_outcome(*, is_error: bool, text: str, interrupted: bool) -> tuple[str, str]:
    summary = ""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped and _SUMMARY_LINE_RE.match(stripped):
            summary = _clean(stripped, MAX_CHECK_SUMMARY_CHARS)
            break
    if interrupted:
        return "interrupted", summary
    if is_error or _FAILED_COUNT_RE.search(text) or _EXIT_CODE_RE.search(text):
        return "failed", summary
    return "passed", summary


def digest_transcript(
    path: Path,
    *,
    checkout: Path | None,
    cwd: Path | None = None,
    deadline_seconds: float = PARSE_DEADLINE_SECONDS,
    max_bytes: int = MAX_TRANSCRIPT_BYTES,
    max_line_bytes: int = MAX_TRANSCRIPT_LINE_BYTES,
    clock: Callable[[], float] = time.monotonic,
) -> TranscriptDigest:
    """Stream one JSONL transcript into a bounded digest; never loads it whole.

    Malformed lines are counted and skipped; a line longer than
    ``max_line_bytes`` (an image or a huge tool result) is hashed but never
    parsed; reading stops at ``max_bytes`` or ``deadline_seconds`` and the
    digest then says it is incomplete. Raises ``OSError`` only when the file
    cannot be opened.
    """

    builder = _DigestBuilder(checkout=_resolved(checkout) if checkout else None, cwd=cwd)
    digest = builder.digest
    hasher = hashlib.sha256()
    started = clock()
    with path.open("rb") as handle:
        while True:
            if digest.bytes_read >= max_bytes or clock() - started > deadline_seconds:
                digest.complete = not handle.read(1)
                break
            chunk = handle.readline(max_line_bytes + 1)
            if not chunk:
                break
            hasher.update(chunk)
            digest.bytes_read += len(chunk)
            digest.lines += 1
            if len(chunk) > max_line_bytes:
                digest.oversized_lines += 1
                while not chunk.endswith(b"\n"):
                    chunk = handle.readline(max_line_bytes)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    digest.bytes_read += len(chunk)
                continue
            _feed_line(builder, chunk)
    digest.sha256 = hasher.hexdigest()
    return builder.finish()


def _feed_line(builder: _DigestBuilder, chunk: bytes) -> None:
    text = chunk.decode("utf-8", errors="replace").strip()
    if not text:
        return
    try:
        record = json.loads(text)
    except ValueError:
        builder.digest.malformed_lines += 1
        return
    if not isinstance(record, Mapping):
        builder.digest.malformed_lines += 1
        return
    try:
        builder.feed(record)
    except Exception:  # noqa: BLE001 - one odd record must never lose the session.
        builder.digest.malformed_lines += 1


# --------------------------------------------------------------------------
# Packet rendering
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionFacts:
    """Where and how the session ran; everything the body states besides the digest."""

    agent: str
    session_id: str
    reason: str
    repo_name: str
    branch: str
    head: str
    transcript_uri: str


def render_retrospective(facts: SessionFacts, digest: TranscriptDigest) -> tuple[str, bool]:
    """Markdown body for the staged note, and whether it hit :data:`MAX_BODY_CHARS`.

    Deterministic for a given transcript and checkout state, so re-running
    the hook for the same session queues nothing new.
    """

    lines = [
        f"# {BODY_HEADING}",
        "",
        f"A bounded, redacted summary of a `{facts.agent}` coding-agent session in "
        f"`{facts.repo_name}`, captured by `lt agent session-end` when the session ended. "
        "It is evidence of what a person asked and what the agent did, not the transcript "
        "(which stays on the machine that ran the session).",
        "",
        "Treat it as agent-session evidence for human-reviewed proposals: where it states "
        "a decision (the choice, alternatives, rationale), a dead end (the hypothesis, how "
        "it failed, the lesson), or a pivot (the trigger and rationale), propose it as "
        "such; otherwise stay conservative. The agent's own claims in its final message "
        "are not verified results.",
        "",
        "## Session",
        f"- Agent: `{facts.agent}`" + (f" ({digest.model})" if digest.model else ""),
        f"- Session: `{facts.session_id}`",
    ]
    if facts.reason:
        lines.append(f"- Ended: {facts.reason}")
    repo_line = f"- Repository: `{facts.repo_name}`"
    if facts.branch:
        repo_line += f" on `{facts.branch}`"
    if facts.head:
        repo_line += f" at `{facts.head[:12]}`"
    lines.append(repo_line)
    transcript_line = f"- Transcript: sha256 `{digest.sha256}`, {digest.lines} line(s)"
    if not digest.complete:
        transcript_line += " (read partially: size or time bound reached)"
    lines.append(transcript_line + "; not uploaded")
    lines.append(
        f"- Counts: {digest.prompt_count} prompt(s), {digest.edit_count} file(s) edited, "
        f"{digest.command_count} command(s), {len(digest.checks)} test/lint run(s)"
    )
    lines.extend(_prompt_section(digest))
    lines.extend(_files_section(digest))
    lines.extend(_commands_section(digest))
    lines.extend(_checks_section(digest))
    if digest.final_message:
        lines.extend(["", "## Final agent message", "", *_blockquote(digest.final_message)])
    lines.extend(
        [
            "",
            "## Capture limits",
            f"Prompts are capped at {MAX_PROMPT_CHARS} characters each and "
            f"{MAX_PROMPTS_TOTAL_CHARS} in total ({MAX_PROMPTS} at most); at most "
            f"{MAX_FILES_LISTED} files, {MAX_COMMANDS_LISTED} commands, and "
            f"{MAX_CHECKS_LISTED} test/lint runs are listed; the final message is capped at "
            f"{MAX_FINAL_MESSAGE_CHARS} characters. Credential-shaped text is redacted.",
        ]
    )
    body = "\n".join(lines).strip() + "\n"
    if len(body) <= MAX_BODY_CHARS:
        return body, False
    marker = f"\n\n_(retrospective truncated at {MAX_BODY_CHARS} characters)_\n"
    return body[: MAX_BODY_CHARS - len(marker)].rstrip() + marker, True


def _prompt_section(digest: TranscriptDigest) -> list[str]:
    lines = ["", "## What the person asked"]
    if not digest.prompts:
        lines.extend(["", "_No prompts were recorded._"])
        return lines
    for index, prompt in enumerate(digest.prompts, start=1):
        quoted = _blockquote(prompt)
        lines.extend(["", f"**{index}.**", *quoted])
    omitted = digest.prompt_count - len(digest.prompts)
    if omitted > 0:
        lines.extend(["", f"_{omitted} later prompt(s) not listed (size bound)._"])
    return lines


def _files_section(digest: TranscriptDigest) -> list[str]:
    lines = ["", "## Files the agent wrote or edited", ""]
    if not digest.edit_count:
        lines.append("_None._")
        return lines
    listed = list(digest.edited_files.items())[:MAX_FILES_LISTED]
    for path, tools in listed:
        detail = ", ".join(
            f"{tool} ×{count}" if count > 1 else tool for tool, count in tools.items()
        )
        lines.append(f"- {_code_span(path)} ({detail})")
    unlisted = len(digest.edited_files) - len(listed) + digest.edited_files_overflow
    if unlisted > 0:
        lines.append(f"- _{unlisted} more file(s) in the checkout not listed_")
    if digest.outside_edit_count:
        lines.append(
            f"- _{digest.outside_edit_count} file(s) outside this checkout (paths not recorded)_"
        )
    return lines


def _commands_section(digest: TranscriptDigest) -> list[str]:
    lines = ["", "## Commands the agent ran", ""]
    if not digest.commands:
        lines.append("_None._")
        return lines
    listed = list(digest.commands.items())[:MAX_COMMANDS_LISTED]
    for label, count in listed:
        rendered = _code_span(label[2:]) if label.startswith("$ ") else label
        lines.append(f"- {rendered}" + (f" (×{count})" if count > 1 else ""))
    unlisted = len(digest.commands) - len(listed) + digest.commands_overflow
    if unlisted > 0:
        lines.append(f"- _{unlisted} more distinct command(s) not listed_")
    return lines


def _checks_section(digest: TranscriptDigest) -> list[str]:
    if not digest.checks:
        return []
    lines = ["", "## Tests and linters", ""]
    shown = digest.checks[-MAX_CHECKS_LISTED:]
    earlier = len(digest.checks) - len(shown)
    if earlier:
        lines.append(f"- _{earlier} earlier run(s) not listed_")
    for check in shown:
        label = _code_span(check.label[2:]) if check.label.startswith("$ ") else check.label
        line = f"- {check.kind}: {label} — **{check.outcome}**"
        if check.summary:
            line += f" ({_code_span(check.summary)})"
        lines.append(line)
    return lines


def _blockquote(text: str) -> list[str]:
    return [f"> {line}".rstrip() for line in text.splitlines()] or [">"]


def _code_span(text: str) -> str:
    if "`" not in text:
        return f"`{text}`"
    single_ticks = text.replace("``", "'")
    return f"`` {single_ticks} ``"


# --------------------------------------------------------------------------
# Session-end capture
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionEndOptions:
    """Inputs for :func:`capture_session_end` besides the hook JSON."""

    agent: str = DEFAULT_AGENT
    project_id: str | None = None
    repo: str | None = None
    transcript: str | None = None
    session_id: str | None = None
    min_prompts: int = MIN_PROMPTS_WITHOUT_EDITS
    sync: bool = True
    dry_run: bool = False


def capture_session_end(
    hook: Mapping[str, Any],
    options: SessionEndOptions,
    *,
    client_factory: Callable[[], LabTracker] | None = None,
) -> JsonObject:
    """Queue one retrospective staged-note event for a finished agent session.

    Returns the command payload; every skip is a normal result (``action``
    ``skipped`` with a ``reason``), never an exception.
    """

    payload: JsonObject = {"command": "agent-session-end", "agent": options.agent}
    if not agent_hooks_enabled():
        return _skipped(payload, "disabled")
    event_name = str(hook.get("hook_event_name") or "")
    if event_name and event_name != SESSION_END_EVENT:
        return _skipped(payload, "not_session_end", hook_event_name=event_name)
    session_id = str(options.session_id or hook.get("session_id") or "").strip()
    transcript_value = str(options.transcript or hook.get("transcript_path") or "").strip()
    if not session_id or not transcript_value:
        return _skipped(payload, "no_session")
    payload["session_id"] = session_id
    base = hook_base_dir(hook, options.repo)
    checkout = _checkout_root(base)
    if checkout is None:
        return _skipped(payload, "no_checkout", cwd=str(base))
    payload["repo"] = str(checkout)
    bound = resolve_capture_project(checkout / "lt_ids.json", project_id=options.project_id)
    if bound is None or not bound.bound:
        _notice_unbound(checkout)
        return _skipped(payload, "unbound")
    payload["project_id"] = bound.project_id
    transcript = Path(transcript_value).expanduser()
    try:
        digest = digest_transcript(transcript, checkout=checkout, cwd=hook_cwd(hook, base))
    except OSError as exc:
        return _skipped(payload, "no_transcript", detail=str(exc))
    last_message = hook.get("last_assistant_message")
    if isinstance(last_message, str) and last_message.strip():
        # The agent's own report of its final message outranks the transcript scan.
        digest.final_message = _clean(last_message, MAX_FINAL_MESSAGE_CHARS)
    payload["counts"] = _counts(digest)
    if not digest.edit_count and digest.prompt_count < options.min_prompts:
        return _skipped(payload, "trivial_session", min_prompts=options.min_prompts)
    facts = SessionFacts(
        agent=options.agent,
        session_id=session_id,
        reason=str(hook.get("reason") or ""),
        repo_name=checkout.name,
        branch=gitinfo.git_output(checkout, "branch", "--show-current"),
        head=gitinfo.git_output(checkout, "rev-parse", "HEAD"),
        transcript_uri=(_resolved(transcript) or transcript.absolute()).as_uri(),
    )
    body, truncated = render_retrospective(facts, digest)
    config, config_error = resolve_watch_config(checkout)
    event = build_retrospective_event(
        facts, digest, body=body, body_truncated=truncated, project_id=bound.project_id
    )
    outbox = config.outbox_path()
    event_path = watch_capture.event_path(event, outbox)
    payload.update(
        {
            "outbox": str(outbox),
            "event_path": str(event_path),
            "event_id": event["event_id"],
            "project_source": bound.source.value,
        }
    )
    if config_error:
        payload["config_error"] = config_error
    if options.dry_run:
        payload["action"] = "would-queue"
        payload["body"] = body
        return payload
    already_queued = event_path.exists()
    watch_capture.write_event(event, outbox)
    payload["action"] = "already-queued" if already_queued else "queued"
    if options.sync:
        _drain_after_capture(payload, config, client_factory=client_factory)
    return payload


def build_retrospective_event(
    facts: SessionFacts,
    digest: TranscriptDigest,
    *,
    body: str,
    body_truncated: bool,
    project_id: str,
) -> JsonObject:
    """The one staged-note outbox event carrying the retrospective body."""

    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    metadata: dict[str, Any] = {
        "agent_name": facts.agent,
        "agent_session_id": facts.session_id,
        "agent_transcript_sha256": digest.sha256,
        "agent_transcript_bytes": digest.bytes_read,
        "agent_transcript_complete": digest.complete,
        "agent_prompt_count": digest.prompt_count,
        "agent_files_edited_count": digest.edit_count,
        "agent_command_count": digest.command_count,
        "agent_check_count": len(digest.checks),
        "agent_checks_failed_count": digest.failed_check_count,
        "agent_tool_call_count": digest.tool_call_count,
        "agent_body_truncated": body_truncated,
    }
    optional = {
        "agent_session_end_reason": facts.reason,
        "agent_model": digest.model,
        "agent_client_version": digest.client_version,
        "agent_git_branch": facts.branch,
        "agent_git_commit": facts.head,
        "agent_transcript_uri": facts.transcript_uri,
    }
    metadata.update({key: value for key, value in optional.items() if value})
    session_label = facts.session_id[:12]
    return watch_capture.make_event(
        capture_id=f"agent-session-{facts.agent}-{facts.session_id}",
        event_id=f"retro-{body_hash[:16]}",
        capture_kind=CAPTURE_KIND,
        adapter=ADAPTER,
        sink=watch_capture.SINK_STAGED_NOTE,
        source={
            "provider": SOURCE_PROVIDER,
            "uri": f"agent-session://{facts.agent}/{facts.session_id}",
            "external_id": f"agent-session:{facts.agent}:{facts.session_id}",
            "content_hash": body_hash,
        },
        context={"project_id": project_id},
        payload={
            "title": f"{BODY_HEADING}: {facts.repo_name} ({facts.agent} {session_label})",
            "summary": (
                f"{digest.prompt_count} prompt(s), {digest.edit_count} file(s) edited, "
                f"{digest.command_count} command(s), {len(digest.checks)} test/lint run(s)."
            ),
            "status": "staged",
            "body": body,
            "request_draft": True,
            "metadata": metadata,
        },
    )


def _counts(digest: TranscriptDigest) -> JsonObject:
    return {
        "prompts": digest.prompt_count,
        "files_edited": digest.edit_count,
        "commands": digest.command_count,
        "checks": len(digest.checks),
        "checks_failed": digest.failed_check_count,
        "tool_calls": digest.tool_call_count,
        "transcript_lines": digest.lines,
        "malformed_lines": digest.malformed_lines,
        "oversized_lines": digest.oversized_lines,
        "transcript_complete": digest.complete,
    }


def _skipped(payload: JsonObject, reason: str, **extra: Any) -> JsonObject:
    payload.update({"action": "skipped", "reason": reason, **extra})
    return payload


def _checkout_root(base: Path) -> Path | None:
    """The git checkout ``base`` sits in, via one bounded ``git`` probe."""

    probe = gitinfo.run_git(base, "rev-parse", "--show-toplevel")
    if not probe.ok or not probe.stdout:
        return None
    return _resolved(Path(probe.stdout)) or Path(probe.stdout)


def _notice_unbound(checkout: Path) -> None:
    print(
        f"lab-tracker: agent session retrospective skipped: {checkout} is not bound to a "
        "project (no lt_ids.json project_id, no LAB_TRACKER_PROJECT_ID). Nothing was sent "
        "or queued; `lt project bind` binds this checkout.",
        file=sys.stderr,
    )


def drain_watch_outbox(
    config: watch_capture.WatchConfig,
    *,
    client_factory: Callable[[], LabTracker] | None = None,
    limit: int = SYNC_EVENT_LIMIT,
) -> JsonObject:
    """Best-effort bounded drain of the checkout's watch outbox after a hook queued work.

    One unauthenticated ``/health`` probe first, so an unreachable server
    costs a single short timeout instead of one per queued event; then at
    most ``limit`` events. Draft requests ride on each event's own
    ``payload.request_draft``. Raises on any failure; callers record it.
    """

    factory = client_factory or (lambda: LabTracker.from_env(timeout_seconds=SYNC_TIMEOUT_SECONDS))
    client = factory()
    try:
        client.health()
        return watch_capture.sync_outbox(client, config, request_draft=False, limit=limit)
    finally:
        client.close()


def _drain_after_capture(
    payload: JsonObject,
    config: watch_capture.WatchConfig,
    *,
    client_factory: Callable[[], LabTracker] | None,
) -> None:
    try:
        payload["sync"] = drain_watch_outbox(config, client_factory=client_factory)
    except Exception as exc:  # noqa: BLE001 - the event is durable; a later sync retries.
        payload["sync_error"] = redact_secrets(str(exc))[:500]
        print(
            "lab-tracker: agent session retrospective queued at "
            f"{payload.get('outbox')}; sync did not complete ({payload['sync_error']}). "
            "`lt outbox sync` retries.",
            file=sys.stderr,
        )


__all__ = [
    "AGENT_HOOKS_ENV",
    "CAPTURE_KIND",
    "MIN_PROMPTS_WITHOUT_EDITS",
    "SessionEndOptions",
    "TranscriptDigest",
    "agent_hooks_enabled",
    "build_retrospective_event",
    "capture_session_end",
    "digest_transcript",
    "drain_watch_outbox",
    "read_hook_input",
    "redact_secrets",
    "render_retrospective",
]
