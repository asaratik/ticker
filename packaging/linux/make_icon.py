#!/usr/bin/env python3
"""Generate the shared Ticker icon for Linux, Windows, macOS, and the app."""

import math
import struct
import zlib
from pathlib import Path

SIZE = 256
ROOT = Path(__file__).resolve().parents[2]
LINUX_ICON = Path(__file__).with_name("ticker.png")
APP_ICON = ROOT / "ticker" / "app" / "static" / "ticker.png"
WINDOWS_ICON = ROOT / "packaging" / "windows" / "ticker.ico"
MACOS_ICON = ROOT / "packaging" / "macos" / "ticker.icns"

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


def render(size=SIZE):
    """Render the icon as RGBA rows.

    Coverage is computed as a smooth distance rather than a hard test, which
    is enough antialiasing to keep the diagonal strokes from looking like a
    staircase at 256px.
    """
    scale = size / SIZE
    points = [(ax * scale, ay * scale) for ax, ay in TRACE_POINTS]
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            px, py = x + 0.5, y + 0.5

            background_cover = _clamp(
                _rounded_rect_coverage(px, py, size, CORNER_RADIUS * scale) + 0.5)

            nearest = min(
                _distance_to_segment(px, py, ax, ay, bx, by)
                for (ax, ay), (bx, by) in zip(points, points[1:]))
            trace_cover = _clamp(TRACE_WIDTH * scale / 2.0 - nearest + 0.5)

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


def png_bytes(rows, size=SIZE) -> bytes:
    """Encode 8-bit RGBA rows as a PNG.

    Filter type 0 (none) on every scanline: the image is tiny and zlib
    handles it fine, and a filter-free file is one less thing to get wrong.
    """
    raw = b"".join(b"\x00" + row for row in rows)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b""))


def write_png(rows, path: Path) -> None:
    path.write_bytes(png_bytes(rows))


def ico_bytes(images: dict[int, bytes]) -> bytes:
    entries = []
    payloads = []
    offset = 6 + 16 * len(images)
    for size, png in images.items():
        entries.append(struct.pack("<BBBBHHII", size if size < 256 else 0,
                                   size if size < 256 else 0, 0, 0, 1, 32,
                                   len(png), offset))
        payloads.append(png)
        offset += len(png)
    return struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries + payloads)


def icns_bytes(images: dict[int, bytes]) -> bytes:
    kinds = {16: b"icp4", 32: b"icp5", 64: b"icp6", 128: b"ic07",
             256: b"ic08", 512: b"ic09", 1024: b"ic10"}
    chunks = [kinds[size] + struct.pack(">I", len(png) + 8) + png
              for size, png in images.items()]
    return b"icns" + struct.pack(">I", 8 + sum(map(len, chunks))) + b"".join(chunks)


def main() -> int:
    sizes = (16, 24, 32, 48, 64, 128, 256, 512, 1024)
    images = {size: png_bytes(render(size), size) for size in sizes}
    LINUX_ICON.write_bytes(images[SIZE])
    APP_ICON.write_bytes(images[SIZE])
    WINDOWS_ICON.write_bytes(ico_bytes({size: images[size] for size in sizes if size <= 256}))
    MACOS_ICON.write_bytes(icns_bytes({size: images[size] for size in sizes if size != 24 and size != 48}))
    for path in (LINUX_ICON, APP_ICON, WINDOWS_ICON, MACOS_ICON):
        print("wrote {} ({} bytes)".format(path, path.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
