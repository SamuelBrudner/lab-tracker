# Windows Fresh Clone Setup

These notes cover the Windows-specific setup details that are easy to miss on a
new checkout.

## Prerequisites

- Python 3.10 or newer
- Node.js 20 or newer and npm
- `bd` (Beads) for issue tracking

Install or update Beads with the Windows PowerShell installer:

```powershell
irm https://raw.githubusercontent.com/steveyegge/beads/main/install.ps1 | iex
```

You can verify the installation with:

```powershell
bd version
bd context
```

## Application Setup

The README uses Unix activation commands. In PowerShell, the pip/venv path is:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[test,lint]"
.\.venv\Scripts\python.exe -m alembic upgrade head
```

PowerShell execution policy may block `npm.ps1`. Use `npm.cmd` from PowerShell:

```powershell
npm.cmd install
npm.cmd run test:frontend
npm.cmd run lint:frontend
npm.cmd run build
```

Start the local API with:

```powershell
.\.venv\Scripts\python.exe -m uvicorn lab_tracker.asgi:app --host 127.0.0.1 --port 8000
```

Then open `http://127.0.0.1:8000/app`.

## Beads Bootstrap

Fresh clones may have `.beads/issues.jsonl` but no local embedded Dolt database
yet. Run:

```powershell
bd bootstrap --yes
bd ready
```

If bootstrap finds an existing empty or broken local embedded database, keep a
backup of `.beads/`, then reinitialize from the tracked JSONL:

```powershell
bd init --prefix lab-tracker --reinit-local --skip-agents --skip-hooks --non-interactive
bd import
```

This repository sets `dolt.database: "beads"` and `sync.branch: "beads-sync"` in
`.beads/config.yaml`, so Beads should use the expected local database name and
sync branch setting. Use `bd dolt show` to confirm the active database. This
repo currently syncs Beads state through the git-tracked `.beads/issues.jsonl`;
`bd dolt push` requires a separately configured Dolt remote:

```powershell
bd dolt remote list
bd export
```
