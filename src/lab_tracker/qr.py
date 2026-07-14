"""Shared QR SVG rendering helpers."""

from __future__ import annotations

import segno

QR_ERROR = "l"
QR_MODULE_SIZE = 8
QR_BORDER = 6
QR_DARK = "#000000"
QR_LIGHT = "#ffffff"


def build_qr_svg(url: str) -> str:
    """Render a scanner-friendly monochrome QR code as inline SVG."""

    qr = segno.make(url, error=QR_ERROR)
    matrix = tuple(tuple(row) for row in qr.matrix)
    matrix_size = len(matrix)
    svg_size = (matrix_size + (QR_BORDER * 2)) * QR_MODULE_SIZE
    dark_rects: list[str] = []
    for y, row in enumerate(matrix):
        run_start: int | None = None
        for x, module in enumerate((*row, 0)):
            if module and run_start is None:
                run_start = x
            if not module and run_start is not None:
                rect_x = (run_start + QR_BORDER) * QR_MODULE_SIZE
                rect_y = (y + QR_BORDER) * QR_MODULE_SIZE
                rect_width = (x - run_start) * QR_MODULE_SIZE
                dark_rects.append(
                    f'<rect x="{rect_x}" y="{rect_y}" '
                    f'width="{rect_width}" height="{QR_MODULE_SIZE}" />'
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
