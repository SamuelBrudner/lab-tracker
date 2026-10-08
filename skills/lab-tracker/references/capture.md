# Consumer integration and capture

Use `lab_tracker init --target <repo>` to scaffold portable consumer integration
files. The generated `scripts/lt.py` is a thin shim over `lab_tracker_client`,
and the generated `.mcp.json` uses the portable `lt-mcp` command instead of a
workstation-specific Python path.

After upgrading the installed package, run `lt update` inside a consumer repo to
refresh everything to the new version in one step: managed prompt blocks are
re-rendered in place (consent decisions preserved; add missing conventions
blocks with `--yes`), and scaffolded files (`.claude/settings.json` hooks,
`.mcp.json`, `.cursor/mcp.json`, `.gemini/settings.json`, `scripts/lt.py`,
`AGENTS.lt.md`) are rewritten to the current canonical text with any customised
previous file kept next to it as `*.bak-lt-update`. `lt_ids.json` is never
touched. Use `--dry-run` to preview and `lt doctor` to confirm the repo is in
sync afterwards.

To refresh both skills and their supporting references, machine-wide, run
`lt update --skills-only`. It installs or refreshes the complete skill trees in the Claude and
Codex skill homes and touches no repository or file in the current directory
(`--dry-run` previews; `--yes` and `--target` are refused; a relative
`LAB_TRACKER_SKILLS_HOME` resolves against the current directory, so keep that
override absolute). `lt setup status` suggests it when a skill is missing or
stale. Bare `lt update` refreshes the repo's files only;
`lt update --install-skills` refreshes both skills in addition to the repo.

For substantive, rerunnable notes, prefer `lab_tracker_client.LabTracker` or
the generated `scripts.lt.upsert_note(...)`. Notes are idempotent by the first
non-blank line of `content`; treat that first line as a stable marker.

## Capture Surfaces

Every capture path lands staged notes or proposed links for human review, and
the ones that fire everywhere capture only in a checkout bound to a project
(`lt project bind`, `LAB_TRACKER_PROJECT_ID`). Offer the paths that match how
the person works; setup verbs need their consent (`--dry-run`, then `--yes`).
`docs/capture-guide.md` is the map for people; this is the map for agents.

- **Figures without code changes:** `lt setup autotrack` (IPython saves and
  inline displays), `--jupyter` (one staged page per notebook per day),
  `--scripts` (plain `python` saves and `plt.show()` through a `.pth` file),
  `--r` (`ggsave()` and file devices via `~/.Rprofile`). MATLAB uses
  `labtracker.savefig`. Kill switch: `LAB_TRACKER_AUTOTRACK=0`. See
  `docs/notebook-and-script-capture.md`, `docs/lab-tracker-r.md`,
  `docs/lab-tracker-matlab.md`.
- **One file from anything:** `lt capture file PATH [--kind] [--metadata k=v]
  [--require-bound]`; prints the result JSON and exits 0 for every capture
  outcome.
- **Runs:** `lt run [--output DIR] -- <command>` records the redacted command
  line, git and working-copy state, a lockfile fingerprint, and hashed
  pointers to files written under `--output`, and exits with the command's
  own status. See `docs/run-capture.md`.
- **Pipelines, CI, clusters:** `lt pipeline report|nextflow|dvc`, the
  Snakemake and Kedro adapters in `lab_tracker_client.integrations`, the
  `.github/actions/lab-tracker-repo-report` action, `lt hpc submit`, and the
  Slurm TaskEpilog (`lt hpc epilog`, `scripts/slurm-task-epilog.sh`). See
  `docs/pipeline-capture.md` and `docs/hpc-analysis-capture.md`.
- **Folders, commits, instruments:** `lt watch add` plus `lt setup schedule`,
  `lt hooks install`, and `lt session use <code>`. Watched FCS, OME-TIFF, and
  NWB files carry `format_*` header metadata. See `docs/watch-folder-capture.md`
  and `docs/decoded-labels-and-file-headers.md`.
- **Coding-agent sessions:** `lt setup agent-hooks` (personal
  `.claude/settings.local.json` by default; `--shared` is a team decision)
  installs `lt agent session-end` (one redacted retrospective per session,
  drafted into review) and `lt watch touch` (queues a watched file an agent
  writes). Kill switch: `LAB_TRACKER_AGENT_HOOKS=0`. See
  `docs/agent-session-capture.md`.
  `lt doctor`, `lt doctor --all`, and `lt setup status` explain missing or
  partial hooks and offer preview and personal installation commands. The
  notice is optional guidance; diagnostics never enable capture themselves.
- **Bench, in the web app:** a session's page has its capture QR, an NFC tag
  writer, photo import, a voice debrief, and **Open bench kiosk**; the Devices
  page has the bench kiosk, a hands-free shortcut (`POST
  /notes/voice-capture`), a desktop bookmarklet, and the person's email
  capture address. See `docs/bench-capture.md`.
- **Server channels (operator settings):** Slack, email-to-capture,
  instrument calendars (ICS), registered-store scans, and photo barcode
  decoding (the `decode` extra). See `docs/server-capture-channels.md`.
- **Linking during review:** detectors propose `was_derived_from` links for
  shared bytes, named sessions or commits, the same code tree as a commit
  (`worktree_tree_match`), and captures made during one of the author's own
  sessions (`time_window_match`); `GET /projects/{id}/session-suggestions`
  suggests session bookkeeping; batch drafts add one deterministic day log per
  busy session and may carry read-only capture-setup tips
  (`context_packet.capture_setup`) to report, never act on. None of these commit
  anything. See `docs/session-suggestions.md`.
