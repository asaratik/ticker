# Native desktop application

Ticker's native desktop interface uses Qt Widgets through PySide6, with the
compact toolbar navigation selected in the design review. It shares the
existing Python runtime, database, recording, synchronization, and agent
services. It does not render HTML or use an embedded browser.

## Run locally

From the repository, install dependencies once and launch:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m ticker.app.main
```

The existing `ticker` and `ticker-app` launchers also open the native window.
`ticker desktop` is an explicit alias. `--web` retains the old browser UI;
`--headless` retains service-only operation. Stop an existing browser/headless
Ticker before opening the native app; the app refuses a duplicate service on
its API port. A second native launch for the same database activates the first
window instead of starting another writer. Different databases need different
API ports (or `--port 0`).

The default database and OS-keyring credentials stay in their existing locations.
For isolated experimentation, use `--db PATH_TO_NEW_DIRECTORY/preview.sqlite3`
and `--port 0`. Settings are stored beside the database, so use a new directory,
not just a new filename, to avoid sharing production settings.

## First working slice

- Overview: latest measurements show actual values, dates, and sources. Trends
  explicitly select a source; missing data stays empty rather than becoming zero.
- Live recording: choose Bluetooth/watch HTTP, scan or enter a Bluetooth address,
  start a named recording, see live readings, and stop/save. Recording continues
  while navigating between screens.
- History: inspect the newest 50 sessions and their heart-rate traces; export a
  bounded CSV trace. Long sessions are bucketed to 300 points, not exported raw.
- Connections: existing Oura, Fitbit, and Garmin sign-in flows, sync, disconnect,
  import file selection, and job status. Fitbit still requires the configured
  `TICKER_FITBIT_CLIENT_ID`. Oura/Fitbit authorization opens the system browser;
  ordinary app use does not. Garmin codes are entered by activating its waiting
  sign-in job in Activity.
- Ask: configure a local model, submit questions, and follow results using the
  existing read-only assistant. Choosing a remote model sends the question and
  retrieved health context to that server; the settings dialog warns about this.
- Settings: System/Light/Dark appearance, keep-running preference, verified
  backups, on-demand update checks, and the optional MCP endpoint.

Native menus, file pickers, dialogs, text controls, and keyboard shortcuts use
Qt Widgets. Appearance follows the platform palette. There is no HTML renderer,
WebView, separate service launch, or new database. Provider credentials are
submitted to the existing keyring flow, not saved in desktop settings.
Linux desktop use needs the system libraries listed in
[Qt's platform requirements](https://doc.qt.io/qt-6/linux-requirements.html).
CI installs the Qt runtime dependencies and runs native tests offscreen.

Closing the window offers keep-running, quit, or cancel. Keep-running is enabled
only with a usable tray/menu-bar indicator; active recordings always prompt.
Quit ends a recording, drains the writer, and releases readers. Startup and
shutdown failures remain visible instead of forcibly terminating the writer.

## Implementation

`desktop.py` owns the application and per-database lock/activation channel.
`desktop_backend.py` keeps runtime commands and read-only queries on a dedicated
worker so provider sign-in, scans, imports, and database work do not block the
window. `desktop_window.py` owns navigation and native controls;
`desktop_chart.py` paints palette-aware charts; `desktop_data.py` reads bounded
data through the existing read-only database and tools.

The existing HTTP API/MCP service stays available for integrations, but native
controls call the runtime directly. Existing remote-binding/auth protections and
the one-writer database architecture remain intact. IPC is current-user-only and
carries only an activation request, never health data or credentials.

## Validation and next work

```powershell
.\.venv\Scripts\python.exe -m pyflakes ticker
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ticker.app.main --smoke-test RESULT.json
python build.py
```

The smoke test renders all six screens against disposable data with no hardware
or provider connections. Packaging runs that same check against the executable.
Native tests additionally cover source-separated reads, honest empty states,
stale-result rejection, saved preferences, safe backup destinations, a real second
process activating the first instance, and recording into temporary SQLite data.

Still required before release:

- Real Bluetooth and each cloud provider's sign-in, permissions, failure, retry,
  and reconnection paths on Windows, macOS, and Linux.
- Native display/keyboard/accessibility checks, system light/dark changes, tray
  availability and close behavior, high DPI, and small screens on all platforms.
- Signed installers, notarization, Qt/PySide license notices and distribution
  obligations. Qt's current macOS wheels require macOS 13+.

This is a desktop foundation, not a mobile app. Start-at-login management,
in-app restore/support export, richer history filtering, and final visual polish
remain follow-up work. Restore is still available through the existing CLI with
Ticker stopped; the optional browser UI retains its support export.
