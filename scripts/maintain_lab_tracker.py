#!/usr/bin/env python3
"""Bootstrap lt maintenance from a checkout, even before installed lt has it."""

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "src/lab_tracker_client/maintenance.py"),
        run_name="__main__",
    )
