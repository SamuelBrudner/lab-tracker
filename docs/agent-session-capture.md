# Agent session capture

Coding agents do a good share of the analysis work in a Lab Tracker repo:
they fit the model, rerun the tests, rewrite the figure script. Two optional
Claude Code hooks let that work reach the review queue without anyone
copying notes out of the chat:

| Hook | Runs | What it does |
| --- | --- | --- |
| `SessionEnd` | `lt agent session-end --fail-silent` | Stages one bounded, redacted **retrospective** of the finished session and asks for AI-drafted proposals (decisions, dead ends, pivots) that a person reviews. |
| `PostToolUse` on `Write\|Edit\|MultiEdit\|NotebookEdit` | `lt watch touch --fail-silent` | Queues a file the agent just wrote **when it falls under a configured watch folder**, so the watch becomes event-driven inside agent sessions instead of waiting for the next scheduled `lt watch run`. |

Nothing here commits. The retrospective is a staged note; the proposals
drafted from it wait in `/app/batches` for a person to accept, edit, or
reject ([review-and-commit model](review-and-commit-model.md)). This is the
implemented slice of the
[session retrospective design](ara-session-retrospective-design.md).

## Set it up (one consented command)

Capturing agent conversations is its own opt-in. `lt setup init` never adds
these hooks, and `lt setup status` only reports whether they are installed
(`agent_hooks`); it never suggests them.

```bash
lt setup agent-hooks --dry-run     # show the .claude/settings.local.json diff
lt setup agent-hooks --yes         # apply it, for you alone
lt setup agent-hooks --uninstall --yes
```

- A non-interactive run without `--yes` or `--dry-run` fails without writing.
- **The personal `.claude/settings.local.json` is the default.** Claude Code
  merges it with the shared `.claude/settings.json`, so the scaffolded
  `SessionStart`/`UserPromptSubmit` hooks keep working and the capture hooks
  apply only to the person who opted in. The file must stay out of version
  control: Lab Tracker's scaffold does not manage `.gitignore`, so add
  `.claude/settings.local.json` to it if it is not ignored already.
  `lt setup agent-hooks` checks with `git check-ignore` and warns when it is not.
- **`--shared` writes the usually committed `.claude/settings.json` instead**
  and prints a warning, because once that file is committed **everyone who
  clones the repository and has `lt` configured has their coding-agent sessions
  captured** into the bound project without opting in themselves. That is a
  team decision, never a setup default. (Earlier drafts of this command had a
  `--local` flag; the personal file is now the default and `--local` is not
  accepted.)
- `--uninstall` removes the entries from the file its scope names (the personal
  file, or the shared file with `--shared`) and reports when the other file
  still has them.
- The managed entries are recognised by their command (`lt agent session-end
  ...`, `lt watch touch ...`). Everything else in the file, including the
  scaffolded hooks and any hook you added, is preserved; installing twice
  changes nothing, and `--uninstall` removes only the managed entries.
- `lt update` (and `lt setup init --force`) never touch the personal file. They
  rewrite `.claude/settings.json` to the current scaffold but carry agent-hooks
  entries installed there with `--shared` forward, so a refresh never silently
  drops that opt-in either.
- `lt setup status` reports the hooks from either file (`agent_hooks.scopes` is
  `["local"]`, `["shared"]`, or both).
- The `SessionEnd` entry sets `"timeout": 60`. Claude Code gives all
  `SessionEnd` hooks a shared 1.5-second budget unless a hook's own timeout
  raises it (up to 60 seconds); starting `lt` alone takes about a second. The
  `PostToolUse` entry is `"async": true`, so an edit never waits on it.

The hooks call bare `lt` (like the scaffolded ones); the command warns when no
`lt` is on the current `PATH`.

## When anything is captured

Both hooks capture only for a checkout **bound to a project**: an explicit
`--project`, `LAB_TRACKER_PROJECT_ID`, or the checkout's `lt_ids.json`
(`lt project bind`). A watch config's project counts for `lt watch touch`,
because a person declared that folder with `lt watch add`; a connection
profile's default project never counts. An unbound checkout is skipped with
one stderr line naming `lt project bind`, and nothing is sent or queued.

The checkout is `--repo`, else `CLAUDE_PROJECT_DIR` (the project whose settings
installed the hook; it stays put when the agent enters a worktree), else the
hook's `cwd`.

### `lt agent session-end`

Reads the `SessionEnd` hook JSON on stdin (`session_id`, `transcript_path`,
`cwd`, `reason`, `last_assistant_message`) and streams the session's JSONL
transcript once. It skips, silently and without queuing:

- **trivially short sessions**: no file edits and fewer than **3** prompts
  (`--min-prompts N` changes the threshold);
- hook events other than `SessionEnd`, a missing transcript, and directories
  outside a git checkout.

Otherwise it writes **one** staged-note event to the checkout's watch outbox
(`.lab-tracker/outbox/watch`) with `payload.request_draft` set, then drains
that outbox best-effort. The note body is markdown under the heading
**Agent session retrospective**:

- **What the person asked** — the person's prompts (not slash commands without
  arguments, local command output, subagent prompts, or interrupt markers);
- **Files the agent wrote or edited** — repo-relative paths from `Write`,
  `Edit`, `MultiEdit`, and `NotebookEdit`, with per-tool counts; edits the
  tool rejected are left out, and files outside the checkout are counted but
  never named;
- **Commands the agent ran** — the agent's own `description` of each `Bash`
  command, or the redacted first line of the command when it gave none;
- **Tests and linters** — recognised test and lint runs (pytest, npm/vitest/
  jest, go/cargo test, ruff, mypy, eslint, tsc, …) with passed/failed/
  interrupted and the tool's own summary line;
- **Final agent message** — `last_assistant_message` from the hook, else the
  last assistant text in the transcript.

Note metadata (all scalars): `agent_name`, `agent_session_id`,
`agent_session_end_reason`, `agent_transcript_sha256`, `agent_transcript_bytes`,
`agent_transcript_complete`, `agent_transcript_uri` (a `file://` pointer),
`agent_prompt_count`, `agent_files_edited_count`, `agent_command_count`,
`agent_check_count`, `agent_checks_failed_count`, `agent_tool_call_count`,
`agent_body_truncated`, and when known `agent_model`, `agent_client_version`,
`agent_git_branch`, `agent_git_commit`. The evidence capture kind is
`agent_session_retrospective` and the adapter `lt-agent-session`.

**The transcript itself is never uploaded** — only its SHA-256 and a local
`file://` pointer. The body is deterministic for a given transcript, so
running the hook twice for the same session queues nothing new.

`lt agent session-end --dry-run` prints the packet it would queue, body
included, and writes nothing — the way to see exactly what a session would
send. Without hook input on stdin, `--transcript`, `--session-id`, and `--repo`
capture a transcript by hand.

### `lt watch touch`

Reads the `PostToolUse` hook JSON (`tool_input.file_path`, or
`tool_input.notebook_path`) or takes paths as arguments. When the checkout's
own `.lab-tracker/watch.json` (or `LAB_TRACKER_WATCH_CONFIG`; a config in a
parent directory above the checkout is never used) has a watch whose scan
would capture that file — same root, include/exclude globs, hidden-file and
manifest rules; relative roots are anchored at the checkout — it queues
exactly the event `lt watch scan` would queue for it (same identity, so the
scheduled scan dedupes against it) and drains the outbox best-effort.

Anything else returns at once: no network call, no folder scan, no hashing.
Only write tools trigger it; a `Read` is never captured. Each agent write of a
watched file is captured as its own version, just as a scan run after each
write would; `--no-sync` queues without draining, and then versions a later
edit supersedes go `stale` before the next drain and are not uploaded.

## Per-event effort

None after the one `--yes`. Each session end costs the time to start `lt`
and read the transcript (bounded below) before Claude Code exits; each watched
write costs a background process. The person's effort is the review they
would do anyway: accept, edit, or reject the drafted proposals.

## Limits

| Bound | Value |
| --- | --- |
| Hook JSON read from stdin | 8 Mi characters (larger payloads are drained; the needed fields are salvaged from the head) |
| One transcript line | 1 MiB (longer lines, such as images, are hashed but never parsed) |
| Transcript read | 256 MiB or 10 seconds, whichever comes first (the note then says it read partially) |
| Each prompt / all prompts | 800 / 6,000 characters, at most 30 prompts |
| Files / commands / test-lint runs listed | 50 / 30 / 10 (the most recent runs) |
| Command label | 160 characters |
| Test/lint summary line | 200 characters |
| Final agent message | 3,000 characters |
| Whole note body | 20,000 characters |
| Drain after queuing | one `/health` probe, then at most 10 outbox events, each request timing out after 5 seconds |

Every excerpt is redacted before it is capped, so a secret cut at the boundary
never survives. Redaction reuses the server's provider-error redaction
(authorization headers, `?key=`/`token=` query values, Lab Tracker, OpenAI,
Anthropic, and Google keys) and adds private-key blocks, credentialed URLs
(`https://user:pass@…`), GitHub/GitLab/Slack/AWS/Hugging Face token shapes,
JWTs, `Bearer` values, `--password`/`--token`/`--api-key`-style flags,
`NAME_TOKEN=…`/`password: …` assignments, and `-u user:password`.
Over-redaction is the accepted failure mode.

## Failure modes and kill switches

- **Server unreachable:** the event stays queued in the outbox; `lt outbox
  sync`, the scheduled `lt watch run`, or the next hook drains it. One failed
  `/health` probe costs a single short timeout, not one per queued event.
- **Drafting slower than the hook's budget:** the note is kept; the draft
  request is retried by the next drain, and the server's generation claim
  makes the retry idempotent.
- **Anything unexpected:** `--fail-silent` swallows it; the hooks always exit
  0 and never block a session or an edit.
- **Stdout:** when stdin carries a hook payload the commands print nothing on
  stdout, because Claude Code parses a hook's JSON stdout as hook control
  output. Run them by hand, or with `--dry-run`, to see the JSON.
- **Kill switch:** `LAB_TRACKER_AGENT_HOOKS=0` (or `false`, `no`, `off`) turns
  both hooks into no-ops everywhere; `lt setup agent-hooks --uninstall --yes`
  removes them from the personal settings file (add `--shared` for the
  committed one).

## Other agents

Only Claude Code is installed and tested. What follows is from each vendor's
hooks documentation as read in September 2026 and has **not** been run
against the agents themselves.

- **Cursor** documents project hooks in `.cursor/hooks.json`
  (`{"version": 1, "hooks": {"afterFileEdit": [{"command": "..."}]}}`);
  `afterFileEdit` receives the edited file's absolute `file_path` at the top
  level of its JSON input, which `lt watch touch` accepts, and project hooks
  run from the project root. Its `sessionEnd` event documents no transcript
  format, so `lt agent session-end` does not support it (a non-`SessionEnd`
  event name is skipped).
- **Codex CLI** documents hooks in `.codex/hooks.json` or `config.toml`
  (project hooks only in trusted projects), but caps `SessionEnd` hooks at
  3 seconds, says its transcript format "isn't a stable interface for hooks",
  and edits files through `apply_patch`, whose paths are inside a patch in
  `tool_input.command`. Neither command supports Codex; `lt setup schedule`
  (a scheduled `lt watch run`) remains the recurrence there.
