"""Generate PWA app icons using only the Python standard library.

Run from the repo root:

    uv run python scripts/generate_pwa_icons.py

Writes solid teal squares with a white "LT" monogram into the committed
frontend bundle at src/lab_tracker/frontend/. Output sizes match the manifest
and the iOS apple-touch-icon. Re-run this script when changing the icon mark.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

PNG_SIG = b"\x89PNG\r\n\x1a\n"

BG = (0x0D, 0x8B, 0x6F)  # --accent teal
FG = (0xFF, 0xFF, 0xFF)

# Monogram strokes in a 512x512 design canvas; scaled per target size.
# L: tall vertical bar at left + short horizontal bar at bottom.
# T: short horizontal bar at top + tall vertical bar centered under it.
_DESIGN_RECTS_512 = (
    (96, 96, 144, 376),   # L vertical
    (96, 328, 240, 376),  # L bottom
    (264, 96, 408, 144),  # T top
    (312, 96, 360, 376),  # T vertical
)


def _chunk(type_bytes: bytes, data: bytes) -> bytes:
    length = struct.pack(">I", len(data))
    crc = struct.pack(">I", zlib.crc32(type_bytes + data) & 0xFFFFFFFF)
    return length + type_bytes + data + crc


def _render_rows(size: int) -> list[bytes]:
    scale = size / 512.0
    rects = [
        (
            int(round(x1 * scale)),
            int(round(y1 * scale)),
            int(round(x2 * scale)),
            int(round(y2 * scale)),
        )
        for x1, y1, x2, y2 in _DESIGN_RECTS_512
    ]
    bg_row = bytes(BG) * size
    rows: list[bytes] = []
    for y in range(size):
        spans = [(rx1, rx2) for rx1, ry1, rx2, ry2 in rects if ry1 <= y < ry2]
        if not spans:
            rows.append(bg_row)
            continue
        row = bytearray(bg_row)
        for rx1, rx2 in spans:
            for x in range(rx1, rx2):
                base = x * 3
                row[base : base + 3] = bytes(FG)
        rows.append(bytes(row))
    return rows


def write_png(path: Path, size: int) -> None:
    rows = _render_rows(size)
    raw = b"".join(b"\x00" + row for row in rows)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    body = (
        PNG_SIG
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )
    path.write_bytes(body)


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    out_dir = repo_root / "src" / "lab_tracker" / "frontend"
    out_dir.mkdir(parents=True, exist_ok=True)
    for size, name in ((192, "icon-192.png"), (512, "icon-512.png"), (180, "icon-180.png")):
        target = out_dir / name
        write_png(target, size)
        print(f"wrote {target.relative_to(repo_root)} ({size}x{size})")


if __name__ == "__main__":
    main()
