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

# Where heart rate comes from. "ble" reads a strap -- or a Garmin watch with
# Broadcast Heart Rate on -- over Bluetooth. "http" runs a small server this
# machine listens on and lets the watch push readings to it over the
# network, which needs no Bluetooth or ANT+ hardware here at all. See
# hr_source.create_source.
HR_SOURCE = (os.environ.get("HRM_SOURCE") or "ble").strip().lower()

# -- BLE source -------------------------------------------------------------

# Pin a specific strap by BLE address to skip scanning -- handy if more than
# one heart rate device is in range. Leave unset to auto-discover the first
# device advertising the standard Heart Rate Service.
DEVICE_ADDRESS = os.environ.get("HRM_DEVICE_ADDRESS") or None

SCAN_TIMEOUT_SEC = float(os.environ.get("HRM_SCAN_TIMEOUT_SEC", "10"))
RECONNECT_DELAY_SEC = float(os.environ.get("HRM_RECONNECT_DELAY_SEC", "5"))

# -- HTTP source ------------------------------------------------------------

# 0.0.0.0 listens on every interface, which is what a watch on the LAN needs
# -- 127.0.0.1 would only ever hear from this machine itself.
HTTP_HOST = os.environ.get("HRM_HTTP_HOST", "0.0.0.0")

# 8476 is "HRM" on a phone keypad, and -- more to the point -- it sits below
# the blocks Windows reserves for Hyper-V/WSL, which on a dev machine
# routinely swallow the whole 8600-9100 region. Binding inside one of those
# fails with a permission error, not "address in use"; see the hint in
# http_source.start().
HTTP_PORT = int(os.environ.get("HRM_HTTP_PORT", "8476"))

# Shared secret the watch must send. Unset means anything that can reach the
# port can post readings -- fine on a home LAN, worth setting anywhere else.
HTTP_TOKEN = os.environ.get("HRM_HTTP_TOKEN") or None

# How long without a reading before the UI stops calling the watch connected.
HTTP_SAMPLE_TIMEOUT_SEC = float(os.environ.get("HRM_HTTP_TIMEOUT_SEC", "15"))

# How much history the page's live chart shows.
GRAPH_WINDOW_SEC = int(os.environ.get("HRM_GRAPH_WINDOW_SEC", str(5 * 60)))
