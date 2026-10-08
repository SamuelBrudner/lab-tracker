# Capture Guide

Lab Tracker is most useful when capture costs almost nothing. This page is the
map: find the way you already work, set up the matching path once, and from
then on the research record fills in while you work. Each row links to the
guide with the full setup, limits, and metadata.

Every path follows the same rules:

- **It stages, a person decides.** Captures land as staged notes or proposed
  links. The daily review turns them into graph changes only when a person
  accepts them (or a project owner has granted delegated curation).
- **It only captures what you bound.** Hooks that fire everywhere (IPython,
  Jupyter, plain Python scripts, R, coding agents) capture only inside a
  checkout bound to a project with `lt project bind`, or with
  `LAB_TRACKER_PROJECT_ID` set. Anything else is skipped with a notice.
- **It never gets in your way.** Client capture is fail-soft: a failure never
  breaks your notebook, script, command, or save. Work captured while the
  server is unreachable waits in the checkout's outbox and syncs later.
- **It points, it does not copy.** Large files stay where they are; Lab
  Tracker keeps a pointer, a content hash, and bounded text.
- **Setup asks first.** Every setup command previews with `--dry-run` and
  applies only with `--yes`, and every hook has a kill switch.

New to the command line side? Start with `lt setup status`, which is
read-only and lists what is and is not set up on this machine and repository.
A coding agent with the `lab-tracker-setup` skill can walk you through the
rest one approved command at a time.

`lt doctor` also explains optional Claude Code session capture when it is missing
or only partly installed, with preview and personal installation commands.
The same guidance appears in `lt setup status` and in each repository checked by
`lt doctor --all`. These checks never enable capture themselves.

## At the bench

| You want to... | Use | Guide |
| --- | --- | --- |
| Jot a note, photo, or voice memo from your phone | The paired phone capture page, offline-first | [Phone capture quickstart](phone-capture-quickstart.md) |
| Capture straight into the session you are running | Scan the session's **Capture into this session** QR, or tap an NFC tag written from that session's page | [Bench capture](bench-capture.md#nfc-station-tags) |
| Log barcode scans at a shared bench computer | **Devices → Bench kiosk**, or **Open bench kiosk** on a session | [Bench capture](bench-capture.md#kiosk-scan-station) |
| Send a batch of shares without confirming each one | **Trust shares into this session** for 1, 2, or 4 hours | [Bench capture](bench-capture.md#trusted-share-window) |
| Add the day's notebook and plate photos at the end | **Import photos** on the session page | [Bench capture](bench-capture.md#end-of-session-photo-import) |
| Say what happened when you close a session | The one-button voice debrief offered on close | [Bench capture](bench-capture.md#voice-debrief) |
| Record a voice memo without unlocking the phone | An iOS Shortcut or Android automation from **Devices → Hands-free shortcut** | [Bench capture](bench-capture.md#hands-free-voice-shortcut) |
| Save a protocol, paper, or vendor page from a desktop browser | The **Desktop bookmarklet** on the Devices page | [Bench capture](bench-capture.md#desktop-bookmarklet) |
| Get lot numbers and session links from photos | Photograph the reagent barcode or the session's `LT-<code>` label (server needs the `decode` extra) | [Decoded labels and file headers](decoded-labels-and-file-headers.md) |

## In notebooks and scripts

| You want to... | Set up once | Guide |
| --- | --- | --- |
| Capture matplotlib figures you save in IPython or Jupyter, and the ones a cell only displays | `lt setup autotrack --yes` | [Notebook and script capture](notebook-and-script-capture.md) |
| Keep a daily page of each notebook's reasoning (markdown and a code summary) | `lt setup autotrack --jupyter --yes`, then restart Jupyter | [Notebook and script capture](notebook-and-script-capture.md) |
| Capture figures from plain `python script.py` runs, including `plt.show()` | `lt setup autotrack --scripts --yes` in that environment | [Notebook and script capture](notebook-and-script-capture.md) |
| Capture figures from R (`ggsave()` and file graphics devices) | `lt setup autotrack --r --yes` | [R integration](lab-tracker-r.md) |
| Capture figures from MATLAB, even offline | `labtracker.savefig` in the MATLAB package | [MATLAB integration](lab-tracker-matlab.md) |
| Capture one saved file from any language or tool | `lt capture file PATH` | [Watch-folder capture](watch-folder-capture.md#capturing-one-saved-file-from-any-runtime) |
| Save figures from code explicitly, with run context | `savefig()`, `capture_figures()`, `run_context()` in `lab_tracker_client` | [Notebook and script capture](notebook-and-script-capture.md) |

## Runs, pipelines, and clusters

| You want to... | Use | Guide |
| --- | --- | --- |
| Record what an analysis command did and the files it wrote | `lt run --output results -- python analyze.py` | [Run capture](run-capture.md) |
| Record every commit of an analysis repository | `lt hooks install --project <id> --yes` | [Repo report capture](repo-report-capture.md) |
| Record commits from CI for every collaborator | The `lab-tracker-repo-report` GitHub Action | [Pipeline capture](pipeline-capture.md) |
| Record Snakemake, Nextflow, Kedro, or DVC runs | `lt pipeline report`, `lt pipeline nextflow`, `lt pipeline dvc`, or the adapters in `lab_tracker_client.integrations` | [Pipeline capture](pipeline-capture.md) |
| Record Slurm jobs | `lt hpc submit -- sbatch job.sh`; admins can add the TaskEpilog so jobs finish on their own | [HPC analysis capture](hpc-analysis-capture.md) |
| Link a figure made from uncommitted code to the commit that later holds that code | Nothing extra: captures record the working copy's git tree and the review proposes the link | [Run capture](run-capture.md) |

## Watched folders and instruments

| You want to... | Use | Guide |
| --- | --- | --- |
| Capture results or instrument exports as they appear in a folder | `lt watch add <folder> --include <glob>`, then `lt setup schedule --yes` | [Watch-folder capture](watch-folder-capture.md) |
| Keep acquisition time, instrument, and counts from FCS, OME-TIFF, and NWB files | Nothing extra: `lt watch` reads bounded file headers | [Decoded labels and file headers](decoded-labels-and-file-headers.md) |
| Attach a folder's files to a session | Put the session's `LT-<code>` in the folder or file name, or run `lt session use <code>` | [Watch-folder capture](watch-folder-capture.md) |
| Capture files from a machine you cannot install software on | Ask your operator for a registered-store scan | [Server capture channels](server-capture-channels.md) |

## Coding agents

| You want to... | Use | Guide |
| --- | --- | --- |
| Let an assistant read the record before research decisions | Connect the MCP server | [Agent setup](agent-setup.md) |
| Keep the reasoning from coding-agent sessions | `lt setup agent-hooks --yes` (your personal settings only) | [Agent session capture](agent-session-capture.md) |
| Have files an agent writes into a watched folder captured at once | Included in the agent hooks (`lt watch touch`) | [Agent session capture](agent-session-capture.md) |

## Server capture channels (operators)

These run on the Lab Tracker server and are off until an operator configures
them. See [Server capture channels](server-capture-channels.md) for setup, the
threat model, and operations, and [Configuration](configuration.md) for every
variable.

| Channel | What it captures |
| --- | --- |
| Slack | A slash command or a **Save to Lab Tracker** message shortcut, as the mapped person |
| Email | Mail sent to a person's private capture address, shown to them under **Devices → Email capture** |
| Instrument calendars | Bookings from ICS feeds, so sessions can be suggested for them |
| Registered-store scans | New files in a registered data store, as `store://` pointers |
| Photo decoding | QR codes and barcodes in uploaded photos, with the optional `decode` extra |

## After capture

Captures meet you in the daily review. Some help arrives there without any
setup:

- **Proposed links.** The review proposes links a person can accept or reject:
  captures with identical bytes, captures that name a session or commit, a
  figure made from the same code as a commit, and a capture made during
  exactly one of your own sessions. See [Scheduled daily review](scheduled-daily-review.md).
- **Session suggestions.** The review page and the sessions list suggest
  closing a quiet session, recording a bench day you forgot to open a session
  for, or covering an instrument booking. See [Session suggestions](session-suggestions.md).
- **Day logs.** A heavy bench day in one session arrives as one proposed
  timestamped log instead of many separate items.
- **Capture-setup tips.** When the drafter could not place or interpret some
  of your captures because of how they were made (phone or web captures and
  shortcut memos that reached no session, watched files whose folder names no
  session, a closed session with no debrief, NWB headers left unread), the
  review page's **Help future captures** block names the setup that would
  have recorded what was missing. It is advice only and changes nothing in the
  draft. See [Capture-setup tips](session-suggestions.md#capture-setup-tips-in-the-daily-review).

## Turning things off

Each hook has one switch. Set it to `0`, `false`, `no`, or `off`.

| Switch | Turns off |
| --- | --- |
| `LAB_TRACKER_AUTOTRACK` | IPython, Jupyter, scripts, and R figure capture |
| `LAB_TRACKER_CAPTURE_OUTBOX` | Queueing figure captures offline |
| `LAB_TRACKER_WORKTREE_TREE` | Recording the working copy's git tree |
| `LAB_TRACKER_PIPELINE_CAPTURE` | Pipeline run capture |
| `LAB_TRACKER_HPC_EPILOG_ENABLED` | The Slurm epilog |
| `LAB_TRACKER_AGENT_HOOKS` | Coding-agent session and watch-touch hooks |
| `LAB_TRACKER_WATCH_FORMAT_SNIFF` | Reading instrument file headers |
| `LAB_TRACKER_DECODE_PHOTO_CODES` | Server photo decoding |

Each setup command also takes `--uninstall`. The server capture channels are
off until an operator configures them, so they need no switch.
