#!/usr/bin/env python3
"""
Generate packaging/linux/ticker.png, the AppImage's icon.

The AppImage format requires an icon that matches the desktop entry's
`Icon=` key -- appimagetool refuses to build without one -- and this project
had no icon asset at all, having used PyInstaller's default everywhere else.

Written as a generator rather than a committed-once binary so the icon is
reproducible and reviewable: a PNG in a diff is opaque, and nobody can tell
whether the one in the tree is the one this script would produce. Run it and
compare if you ever need to know.

Pure stdlib on purpose. Pillow would render this more nicely, but adding an
imaging dependency to build a 256px icon -- one that would then have to be
excluded from the frozen app -- is a bad trade. zlib and struct can write a
PNG perfectly well.

    python packaging/linux/make_icon.py
"""

import math
import struct
import zlib
from pathlib import Path

SIZE = 256
OUT = Path(__file__).with_name("ticker.png")

BACKGROUND = (27, 36, 48)      # slate, dark enough for a light or dark shelf
TRACE = (229, 72, 77)          # the same red the live reading uses
CORNER_RADIUS = 56
TRACE_WIDTH = 11.0

# A single QRS complex: flat baseline, the small P bump, the tall spike, the
# deep trough, and back to flat. Drawn in icon coordinates.
TRACE_POINTS = [
    (24, 132), (72, 132), (88, 132), (98, 116), (108, 132),
    (124, 132), (134, 74), (148, 190), (160, 132),
    (176, 132), (188, 120), (198, 132), (232, 132),
]


def _distance_to_segment(px, py, ax, ay, bx, by):
    """Shortest distance from a point to a line segment."""
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _rounded_rect_coverage(x, y, size, radius):
    """Signed coverage of a rounded square, smoothed at the edge."""
    half = size / 2.0
    cx, cy = x - half, y - half
    # Distance from the rounded-rectangle boundary, negative inside.
    qx = abs(cx) - (half - radius)
    qy = abs(cy) - (half - radius)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return -(outside + inside - radius)


def _blend(bottom, top, alpha):
    return tuple(int(round(b + (t - b) * alpha)) for b, t in zip(bottom, top))


def render():
    """Render the icon as RGBA rows.

    Coverage is computed as a smooth distance rather than a hard test, which
    is enough antialiasing to keep the diagonal strokes from looking like a
    staircase at 256px.
    """
    rows = []
    for y in range(SIZE):
        row = bytearray()
        for x in range(SIZE):
            px, py = x + 0.5, y + 0.5

            background_cover = _clamp(
                _rounded_rect_coverage(px, py, SIZE, CORNER_RADIUS) + 0.5)

            nearest = min(
                _distance_to_segment(px, py, ax, ay, bx, by)
                for (ax, ay), (bx, by) in zip(TRACE_POINTS, TRACE_POINTS[1:]))
            trace_cover = _clamp(TRACE_WIDTH / 2.0 - nearest + 0.5)

            colour = _blend(BACKGROUND, TRACE, trace_cover)
            alpha = int(round(255 * background_cover))
            row += bytes((colour[0], colour[1], colour[2], alpha))
        rows.append(bytes(row))
    return rows


def _clamp(value):
    return max(0.0, min(1.0, value))


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))


def write_png(rows, path: Path) -> None:
    """Write 8-bit RGBA rows as a PNG.

    Filter type 0 (none) on every scanline: the image is tiny and zlib
    handles it fine, and a filter-free file is one less thing to get wrong.
    """
    raw = b"".join(b"\x00" + row for row in rows)
    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", header)
           + _chunk(b"IDAT", zlib.compress(raw, 9))
           + _chunk(b"IEND", b""))
    path.write_bytes(png)


def main() -> int:
    write_png(render(), OUT)
    print("wrote {} ({} bytes)".format(OUT, OUT.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
