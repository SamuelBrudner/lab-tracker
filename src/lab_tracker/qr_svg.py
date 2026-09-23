"""Phone-scannable QR codes and the public URL they should point at.

Shared by device enrollment (pair a phone) and session capture links (scan a
session's QR at the bench so captures land in that session).
"""

from __future__ import annotations

import segno
from starlette.requests import Request

from lab_tracker.config import get_settings
from lab_tracker.instance_url import build_instance_url

QR_ERROR = "l"
QR_MODULE_SIZE = 8
QR_BORDER = 6
QR_DARK = "#000000"
QR_LIGHT = "#ffffff"


def resolve_public_base_url(request: Request) -> str:
    """Pick the base URL a phone will hit.

    Setting beats inference. Falls back to the request's own host so a
    desktop browser opened at the laptop's LAN IP automatically generates
    a phone-reachable URL; only 127.0.0.1/localhost desktops need the
    explicit LAB_TRACKER_BASE_URL override.
    """
    settings = getattr(request.app.state, "settings", None) or get_settings()
    configured = settings.resolved_base_url()
    if configured:
        return configured
    return build_instance_url(str(request.base_url), "")


def build_qr_svg(url: str) -> str:
    """Render ``url`` as a crisp black-on-white SVG QR code for phone cameras."""

    qr = segno.make(url, error=QR_ERROR)
    module_size = QR_MODULE_SIZE
    border = QR_BORDER
    matrix = tuple(tuple(row) for row in qr.matrix)
    matrix_size = len(matrix)
    svg_size = (matrix_size + (border * 2)) * module_size
    dark_rects: list[str] = []
    for y, row in enumerate(matrix):
        run_start: int | None = None
        for x, module in enumerate((*row, 0)):
            if module and run_start is None:
                run_start = x
            if not module and run_start is not None:
                rect_x = (run_start + border) * module_size
                rect_y = (y + border) * module_size
                rect_width = (x - run_start) * module_size
                dark_rects.append(
                    f'<rect x="{rect_x}" y="{rect_y}" '
                    f'width="{rect_width}" height="{module_size}" />'
                )
                run_start = None
    dark_markup = "".join(dark_rects)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_size}" '
        f'height="{svg_size}" viewBox="0 0 {svg_size} {svg_size}" '
        'shape-rendering="crispEdges">'
        f'<rect width="{svg_size}" height="{svg_size}" fill="{QR_LIGHT}" />'
        f'<g fill="{QR_DARK}">{dark_markup}</g>'
        "</svg>"
    )


__all__ = [
    "QR_BORDER",
    "QR_DARK",
    "QR_ERROR",
    "QR_LIGHT",
    "QR_MODULE_SIZE",
    "build_qr_svg",
    "resolve_public_base_url",
]
