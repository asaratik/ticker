"""
Everything that might differ between machines or users lives here, and can
be overridden with an environment variable -- nothing device- or
machine-specific is hardcoded elsewhere in the codebase.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Where the SQLite database lives. Override with the HRM_DB_PATH env var.
# Defaults to a per-user app-data folder, following each OS's own
# convention, so it works the same whether you run this from source or as
# a packaged app placed anywhere on disk -- never next to the script/exe
# itself, since a packaged app's own folder may not be writable and a
# onefile build's __file__ isn't stable across runs.
def _default_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "Ticker"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Ticker"
    else:  # Linux and other Unix-likes: XDG Base Directory spec
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return Path(base) / "Ticker"


def _default_db_path() -> Path:
    override = os.environ.get("HRM_DB_PATH")
    if override:
        return Path(override)
    return _default_data_dir() / "hrm_data.sqlite3"


DB_PATH = _default_db_path()

# Pin a specific strap by BLE address to skip scanning -- handy if more than
# one heart rate device is in range. Leave unset to auto-discover the first
# device advertising the standard Heart Rate Service.
DEVICE_ADDRESS = os.environ.get("HRM_DEVICE_ADDRESS") or None

SCAN_TIMEOUT_SEC = float(os.environ.get("HRM_SCAN_TIMEOUT_SEC", "10"))
RECONNECT_DELAY_SEC = float(os.environ.get("HRM_RECONNECT_DELAY_SEC", "5"))

POLL_INTERVAL_MS = 200
GRAPH_WINDOW_SEC = int(os.environ.get("HRM_GRAPH_WINDOW_SEC", str(5 * 60)))
GRAPH_WIDTH = 480
GRAPH_HEIGHT = 150

# "Segoe UI" only exists on Windows; Tk silently substitutes something else
# if asked for a font that isn't installed, but picking a font that's
# actually native on each OS looks a lot better than leaving it to chance.
if sys.platform == "win32":
    FONT_FAMILY = "Segoe UI"
elif sys.platform == "darwin":
    FONT_FAMILY = "Helvetica Neue"
else:
    FONT_FAMILY = "DejaVu Sans"

# Dark theme
BG = "#1e1e1e"
PANEL_BG = "#2a2a2a"
TEXT = "#f0f0f0"
SUBTEXT = "#9a9a9a"
ACCENT = "#4fc3f7"
GOOD = "#66bb6a"
WARN = "#ffb74d"
BAD = "#ef5350"
GRAPH_LINE = "#4fc3f7"
