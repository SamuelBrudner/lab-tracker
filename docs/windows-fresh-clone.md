# Windows Fresh Clone Setup

These notes cover the Windows-specific setup details that are easy to miss on a
new checkout.

## Prerequisites

- Python 3.10 or newer
- Node.js 20 or newer and npm
- Dolt for Beads issue tracking

If Dolt is installed but `bd` cannot find it, add the Dolt binary directory to
the current PowerShell session before running Beads commands:

```powershell
$env:PATH = 'C:\Program Files\Dolt\bin;' + $env:PATH
```

New terminals should pick up the user PATH if Dolt was installed with winget.
You can verify the installation with:

```powershell
dolt version
bd doctor
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

Fresh clones may have `.beads/issues.jsonl` but no local Dolt database yet.
Run:

```powershell
bd doctor
bd init --prefix lab-tracker --from-jsonl --skip-agents --skip-hooks
bd import
bd ready
```

This repository sets `dolt.database: "beads"` in `.beads/config.yaml`, so Beads
should use the expected local Dolt database name. If `bd doctor` reports schema
or project identity issues after initialization, run:

```powershell
bd doctor --fix --yes
bd import
```

Use `bd dolt show` to confirm the active database, host, and port. If the Dolt
database has local uncommitted table changes after Beads configuration updates,
commit them with:

```powershell
bd dolt commit -m "Update beads configuration"
```
