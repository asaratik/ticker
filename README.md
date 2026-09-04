# Ticker

A little desktop app for reading a Bluetooth heart rate strap and logging
what it sees. Works with anything that speaks the standard BLE Heart Rate
Service (Garmin, Polar, Wahoo, whatever) — no vendor SDK involved. Windows,
macOS, Linux.

Live bpm on screen, a Start/Stop button to control what actually gets
logged, and a SQLite file afterward you can point pandas at.

## Running it

```
pip install -r requirements.txt
python hrm_app.py
```

If `python3 -c "import tkinter"` fails: on macOS, `brew install python-tk`
(Homebrew's Python doesn't bundle it) or use a python.org build instead; on
Debian/Ubuntu, `sudo apt install python3-tk`.

## Building a standalone app

```
python build.py
```

Installs dependencies, runs the tests, then packages with PyInstaller.
Produces `dist/Ticker.exe` on Windows, `dist/Ticker.app` on macOS, or
`dist/Ticker` on Linux — one file, no Python install needed to run it.
Pass `--skip-tests` to skip straight to packaging.

macOS heads-up: PyInstaller's default bundle doesn't set
`NSBluetoothAlwaysUsageDescription` in `Info.plist`, which recent macOS
requires for Bluetooth permission. Untested since I don't have a Mac to
check it on — if the built `.app` can't see any straps but
`python hrm_app.py` can, this is almost certainly why.

## How it works

Straps broadcast the Bluetooth Heart Rate Service directly, so `hr_ble.py`
just scans for that and reads it, reconnecting on its own if the connection
drops.

A session is separate from the connection itself — you decide when logging
starts and stops with the button in the app, and a session survives a brief
disconnect (it just leaves a gap in the data). The live bpm number updates
whenever a strap is connected, session running or not, mostly so you know
it's actually reading something.

All the disk writing happens on its own thread, never the UI thread. An
earlier version of this froze under Windows Defender scanning the SQLite
journal file on every write; moving writes off the UI thread and switching
to WAL mode fixed it. Details in the comments at the top of `storage.py`.

## Configuration

Environment variables, all optional:

| Variable | Default | What it does |
|---|---|---|
| `HRM_DB_PATH` | see below | Where the database lives |
| `HRM_DEVICE_ADDRESS` | auto-discover | Pin a specific strap by BLE address, skip scanning |
| `HRM_SCAN_TIMEOUT_SEC` | `10` | How long each scan attempt runs |
| `HRM_RECONNECT_DELAY_SEC` | `5` | Delay before retrying a dropped connection |
| `HRM_GRAPH_WINDOW_SEC` | `300` | How much history the live graph shows |

Default database location, per OS:

| OS | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\Ticker\hrm_data.sqlite3` |
| macOS | `~/Library/Application Support/Ticker/hrm_data.sqlite3` |
| Linux | `$XDG_DATA_HOME/Ticker/hrm_data.sqlite3` (or `~/.local/share/...`) |

## Looking at the data afterward

```python
import sqlite3, pandas as pd
import config
conn = sqlite3.connect(str(config.DB_PATH))
sessions = pd.read_sql_query("SELECT * FROM sessions", conn)
samples = pd.read_sql_query("SELECT * FROM samples", conn)
```

`rr_intervals_ms` is a comma-separated string per sample, when the strap
sends it — beat-to-beat intervals, useful later for HRV stuff like RMSSD.

## Tests

```
pip install -r requirements-dev.txt
pytest
```

Everything's hardware- and OS-independent (no real BLE connection, no real
window opened), so it runs the same everywhere. This is what CI runs too,
on `windows-latest`, `macos-latest` and `ubuntu-latest`.

## Releasing

Push a tag like `v1.0.0` and the release workflow builds all three platforms
and attaches the binaries to a GitHub Release. It can also be triggered by
hand from the Actions tab, but the tag has to exist already — the build
checks out the tag itself, so a manual run can't ship untagged code under a
version number.

## Notes

- Strap won't connect: most only allow 1–2 simultaneous BLE connections, so
  check it isn't already paired with Garmin Connect, Zwift, a watch, etc.
  Also make sure it's actually on skin — a lot of them sleep otherwise.
- Packaged app doing nothing visible: check `hrm_app.log` next to the
  database. A windowed build has no console, so errors go there instead.
- Want ANT+ instead of BLE: different protocol, needs a USB dongle and the
  `openant` library, not implemented here.
