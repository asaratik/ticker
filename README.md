# Ticker

Your health data, on your machine, in a form AI agents can use.

Ticker gathers heart rate, HRV, sleep, steps, SpO2, weight and more from the
devices and apps you already have — BLE chest straps and Garmin watches live,
Oura and Fitbit through their APIs, Apple Health from an export — into one
SQLite file on your own disk. Then it serves that file over the
[Model Context Protocol](https://modelcontextprotocol.io), so Claude Code,
Codex, Claude Desktop or any other MCP client can answer questions like
*"how has my resting heart rate moved since I started running?"* or *"do I
sleep worse after late workouts?"* from your real numbers.

Windows, macOS, Linux. No Ticker account, no Ticker cloud.

## Asking an agent

```
pipx install ticker
ticker-setup add oura            # and/or fitbit; see "Cloud sources"
ticker-sync --once               # pull what's there (drop --once to keep syncing)
claude mcp add ticker -- ticker-mcp
```

Then ask Claude about your sleep. The agent launches `ticker-mcp` itself and
talks to it over stdin/stdout; there's nothing to keep running for it. What
*does* need to run is whatever collects the data — `ticker-sync` for cloud
accounts, the app for a strap — and they can run at the same time: the MCP
server is one more reader of the same file.

Other clients:

**Claude Code**, available in every project rather than just this one:

```
claude mcp add --scope user ticker -- ticker-mcp
```

**Codex**, in `~/.codex/config.toml`:

```toml
[mcp_servers.ticker]
command = "ticker-mcp"
```

**Claude Desktop**, in `claude_desktop_config.json`:

```json
{ "mcpServers": { "ticker": { "command": "ticker-mcp" } } }
```

If a client can't find the command — desktop apps often don't inherit your
shell's `PATH` — give it the full path from `which ticker-mcp` (`where` on
Windows). From a source checkout, `pip install -e .` puts `ticker-mcp` on
your `PATH`; `--db` points it at a database other than the default.

**An agent on another machine.** `ticker-server` serves the same tools over
HTTP at `/mcp`, behind its usual token (see "Running it on two machines"):

```
claude mcp add --transport http ticker http://homelab:8477/mcp \
  --header "Authorization: Bearer <token>"
```

### What the agent gets

| Tool | What it answers |
|---|---|
| `get_overview` | What's connected, which metrics exist, what dates they cover and how fresh they are — the agent's starting point |
| `get_daily_summary` | Day, week or month values of up to eight metrics, with typical, lowest, highest and latest |
| `get_sleep` | Night by night: bedtime, wake, time asleep, efficiency, stages, heart rate and HRV while asleep |
| `list_sessions`, `get_session` | Workouts, recordings and sleeps, and what happened during each |
| `get_timeseries` | Readings through a window — a workout, a night — bucketed to fit |
| `query_sql` | Anything else, as read-only SQL |

The tools were shaped for a model reading them rather than a chart: times
come back in your local zone, every value carries its unit, series are
bucketed to a point budget instead of dumping 600,000 rows into the
agent's context, and anything that would mislead is said outright — two
sources both counting your steps are listed side by side rather than added
together, and a cloud account that stopped syncing is flagged. Sleep is
rebuilt night by night from stage data, which is what lets Apple Health
(stages, no sessions) and Oura (sessions and stages) answer the same way.

`ticker-mcp` **cannot change your data.** It opens the database read-only,
SQLite refuses writes on that connection, and an authorizer allows nothing
but reads — so no `DELETE`, no `ATTACH`, no state-changing `PRAGMA`, whatever
SQL the agent sends. Every call also has a time limit, so a runaway query
fails rather than hangs. Connecting sources stays out of the agent's reach
on purpose: tokens are entered at a terminal with `ticker-setup` and live
in the OS keyring, never in anything a tool returns.

## The recorder app

A desktop app for reading a heart rate monitor and logging what it sees.
Works with anything that speaks the standard BLE Heart Rate Service
(Garmin, Polar, Wahoo, whatever) — no vendor SDK involved — or with a Garmin
watch pushing readings over the network, which needs no Bluetooth or ANT+
hardware on this machine at all.

Live bpm on screen, a Start/Stop button to control what actually gets
logged, and everything lands in the same SQLite file the agents read.

### Running it

```
pip install -r requirements.txt
python -m ticker.ui.app
```

If `python3 -c "import tkinter"` fails: on macOS, `brew install python-tk`
(Homebrew's Python doesn't bundle it) or use a python.org build instead; on
Debian/Ubuntu, `sudo apt install python3-tk`.

## Where the heart rate comes from

Set `HRM_SOURCE` to pick one. Both produce the same live display and the
same session log.

### `ble` (default) — a strap, or a watch in broadcast mode

Scans for the standard Heart Rate Service and reads it. This covers chest
straps and **Garmin watches with Broadcast Heart Rate turned on**: a
broadcasting watch advertises exactly the same service a strap does, so
there's nothing extra to configure here.

On the watch: hold the button → *Sensors & Accessories* → *Wrist Heart Rate*
→ *Broadcast Heart Rate*, or add *Broadcast Heart Rate* to the controls
menu. Then start Ticker and it'll find it.

Worth knowing: "broadcast mode" is a Bluetooth/ANT+ feature, not a way
around them. The watch becomes a heart rate peripheral and something
connects to it, exactly as with a strap. If you want the PC out of the radio
business entirely, that's the other source.

### `http` — the watch pushes over the network

```
HRM_SOURCE=http python -m ticker.ui.app
```

Ticker listens on a port and a Connect IQ app on the watch posts readings to
it. The status line shows the URL to point the watch at. Nothing pairs with
or is scanned for by this machine — it just listens.

The watch app lives in [`connectiq/`](connectiq/), along with how to build
and sideload it. It's written but untested on real hardware; the README
there says what to check first.

Anything that can make an HTTP request works just as well — useful for
testing without a watch:

```
curl "http://localhost:8476/hr?hr=142&device=test"
```

## Building a standalone app

```
python build.py
```

Produces `dist/Ticker` — a folder containing `Ticker.exe` (Windows),
`Ticker.app` (macOS), or a `Ticker` binary (Linux), with the Python runtime
and everything else beside it. No Python install needed to run it.

A folder rather than a single file on purpose: PyInstaller's one-file
bootloader unpacks itself to a temp directory and runs from there, which
antivirus heuristics treat as dropper behaviour. The installer wraps the
folder so it makes no difference to anyone installing it.

On Windows, `python build.py --installer` additionally builds an installer
(needs [Inno Setup](https://jrsoftware.org/isdl.php)) — per-user, no admin
rights, and it appears in Add/Remove Programs. Uninstalling leaves your
database alone.

On Linux, `python build.py --appimage-only --version 1.2.3` wraps the folder
in an AppImage: one executable file that needs nothing installed on the
target. It needs `appimagetool` on your `PATH` (or in `$APPIMAGETOOL`);
the build script deliberately won't download it for you.

On macOS, `--dmg-only` wraps the signed bundle in a disk image.

If you'd rather not deal with downloads and warnings at all:

```
pipx install ticker
```

which also gives you `ticker-mcp`, `ticker-sync`, `ticker-import`,
`ticker-setup`, `ticker-server`, `ticker-rollup` and `ticker-backfill` on
your PATH.

Every release publishes `SHA256SUMS-<platform>.txt` so a download can be
verified independently. Note that released binaries are **not currently code
signed** — see `packaging/README.md` for what that means and what setting it
up requires.

## How it works

Sources are pluggable. Each one runs on its own thread(s) and pushes the
same three message shapes — status, sample, error — onto a queue the UI
drains; the UI never knows which source it got them from. The protocol and
the factory live in `hr_source.py`:

```
ble_source.py    BLE strap or broadcasting watch (bleak, own asyncio loop)
http_source.py   HTTP server the watch posts to (stdlib, no dependencies)
      ↓
   queue.Queue  → ticker/ui/app.py →  SessionLogger  →  ticker/db/store.py
                                                   (own writer thread)
```

`connected` means different mechanics per source — a live GATT link for
BLE, a reading that arrived recently for HTTP — but the same thing to the
UI: readings are, or aren't, coming in.

A session is separate from the connection itself — you decide when logging
starts and stops with the button in the app, and a session survives a brief
disconnect (it just leaves a gap in the data). The live bpm number updates
whenever something's connected, session running or not, mostly so you know
it's actually reading something.

All the disk writing happens on its own thread, never the UI thread. An
earlier version of this froze under Windows Defender scanning the SQLite
journal file on every write; moving writes off the UI thread and switching
to WAL mode fixed it. Details in the comments at the top of `storage.py`.

Each reading becomes several rows rather than one: heart rate, one per RR
interval in the packet, and HRV (RMSSD) computed from those beats. The strap
never sends HRV — it's derived in the pipeline, so any source that supplies
RR intervals gets it for free.

### Upgrading from an earlier version

The first run migrates the database in place to the multi-source schema.
It copies the file to `hrm_data.sqlite3.v1.bak`
first, and verifies the migrated data against the original — matching row
counts, time bounds and mean heart rate — before committing. If that check
fails the whole migration rolls back and the database is left exactly as it
was. The old `sessions`/`samples` tables are kept, renamed to `sessions_v1`
and `samples_v1`; a later release drops them.

There is no downgrade path, which is what the `.bak` file is for.

## Configuration

Environment variables, all optional:

| Variable | Default | What it does |
|---|---|---|
| `HRM_SOURCE` | `ble` | Where heart rate comes from: `ble` or `http` |
| `HRM_DB_PATH` | see below | Where the database lives |
| `HRM_GRAPH_WINDOW_SEC` | `300` | How much history the live graph shows |
| `TICKER_TZ` | OS timezone | IANA timezone used to assign observations to local calendar days in rollups; stored timestamps remain UTC |
| `TICKER_RAW_RETENTION_DAYS` | `90` | Days to keep compressed vendor API responses; `0` keeps them indefinitely |
| `TICKER_BUSY_TIMEOUT_MS` | `5000` | How long a database writer waits for another connection's lock |

Advanced ingest tuning:

| Variable | Default | What it does |
|---|---|---|
| `TICKER_COALESCE_ROWS` | `200` | Live rows accumulated before a write is committed |
| `TICKER_COALESCE_MS` | `2000` | Maximum milliseconds live rows wait for that commit |
| `TICKER_NORMALIZE_BATCH` | `1000` | Observations normalized and handed to the writer at once |

BLE source only:

| Variable | Default | What it does |
|---|---|---|
| `HRM_DEVICE_ADDRESS` | auto-discover | Pin a specific device by BLE address, skip scanning. Pinned means pinned: if it isn't there, Ticker says so rather than connecting to some other strap in range |
| `HRM_SCAN_TIMEOUT_SEC` | `10` | How long each scan attempt runs |
| `HRM_RECONNECT_DELAY_SEC` | `5` | Delay before retrying a dropped connection |

HTTP source only:

| Variable | Default | What it does |
|---|---|---|
| `HRM_HTTP_HOST` | `0.0.0.0` | Interface to listen on. The default is what a watch on the LAN needs; `127.0.0.1` only ever hears from this machine |
| `HRM_HTTP_PORT` | `8476` | Port to listen on |
| `HRM_HTTP_TOKEN` | unset | Shared secret the watch must send. Unset means anything that can reach the port can post readings — fine on a home LAN, worth setting anywhere else |
| `HRM_HTTP_TIMEOUT_SEC` | `15` | Silence before the watch stops counting as connected |

Read API (`ticker-server`), only when you run it:

| Variable | Default | What it does |
|---|---|---|
| `TICKER_API_HOST` | `127.0.0.1` | Interface to listen on. Anything but loopback also needs the flag below *and* a token |
| `TICKER_API_PORT` | `8477` | Port to listen on |
| `TICKER_API_TOKEN` | unset | Shared secret. Optional on loopback, required off it |
| `TICKER_API_ALLOW_REMOTE` | `0` | Permit binding off loopback. Without it the server refuses rather than starting open |
| `TICKER_API_MAX_BATCH` | `5000` | Most observations one `/api/ingest` call may carry |

Agent (`ticker-agent`), only when you run it:

| Variable | Default | What it does |
|---|---|---|
| `TICKER_SERVER_URL` | `http://127.0.0.1:8477` | Where the agent posts |
| `TICKER_SPOOL_PATH` | beside the database | The agent's local spool file |
| `TICKER_AGENT_BATCH` | `500` | Observations per uplink POST |
| `TICKER_AGENT_TIMEOUT_SEC` | `10` | How long a post may take before it counts as unreachable |
| `TICKER_SPOOL_MAX_ROWS` | `2000000` | Spool cap; past it the oldest rows go first. `0` means no limit |

Default database location, per OS:

| OS | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\Ticker\hrm_data.sqlite3` |
| macOS | `~/Library/Application Support/Ticker/hrm_data.sqlite3` |
| Linux | `$XDG_DATA_HOME/Ticker/hrm_data.sqlite3` (or `~/.local/share/...`) |

## Data and privacy

Ticker is single-user and local-first: it does not host your database or
send observations to a Ticker-operated service. Cloud connectors contact the
vendor you configure, and the optional agent sends data only to the server
URL you give it.

An AI agent is the one exception worth spelling out. `ticker-mcp` runs on
your machine and only reads, but whatever a tool returns becomes part of
the agent's conversation, and so goes to the model provider behind that
agent — Anthropic for Claude, OpenAI for Codex — under that provider's
terms. The tools return summaries and bounded series rather than whole
tables, but treat an agent session over your health data the way you would
pasting the same numbers into a chat, and don't connect an agent you
wouldn't show them to.

The database contains health observations and compressed vendor responses;
it is not encrypted at rest, so protect it and its backups like any other
sensitive file. Vendor tokens are stored in the OS keyring, never in the
database or environment, and raw responses are removed after 90 days by
default (`TICKER_RAW_RETENTION_DAYS` changes that).

Ticker is data plumbing, not a medical device: it does not diagnose,
interpret clinical thresholds, or make medical claims.

## Looking at the data afterward

Everything lands in one `observations` table, so a query doesn't care which
device produced a row — only which metric it is:

```python
import sqlite3, pandas as pd
import config
conn = sqlite3.connect(str(config.DB_PATH))

sessions = pd.read_sql_query("SELECT * FROM sessions", conn)
hr = pd.read_sql_query("""
    SELECT o.ts, o.value AS bpm, o.session_id
    FROM observations o JOIN metrics m ON m.id = o.metric_id
    WHERE m.name = 'heart_rate_bpm'
    ORDER BY o.ts
""", conn)
```

`SELECT name FROM metrics` lists what's available — `heart_rate_bpm`,
`rr_interval_ms`, `hrv_rmssd_ms` and the rest of the registry. Timestamps
are ISO 8601 UTC at millisecond resolution, stored as text, so they sort
correctly as strings.

`ticker/db/queries.py` has helpers for the common reads. A quick summary
from the command line:

```
python storage.py
```

Scripts written against the old two-table schema keep working:
`storage.list_sessions()` and `storage.get_samples()` read either schema and
return the same shape, reassembling RR intervals back into the v1
comma-separated string.

## Cloud sources

Beyond the strap, Ticker can pull from vendor APIs. Oura and Fitbit are
implemented, and other pull sources can implement the same source protocol
without writing to SQLite directly.

```
pip install keyring
python -m ticker.auth.setup add oura          # prompts for the token
python -m ticker.auth.setup add fitbit        # opens a browser
python -m ticker.ingest.sync                  # syncs until interrupted
```

The two vendors authenticate differently, which is why `setup` has two
paths. Oura takes a personal access token you paste once. Fitbit needs
OAuth, so `add fitbit` opens a browser, waits on a loopback redirect bound
to `127.0.0.1` only, and exchanges the code with PKCE. Set
`TICKER_FITBIT_CLIENT_ID` first, from an application registered at
<https://dev.fitbit.com>; the client id is not a secret and lives in config,
while the tokens it obtains go to the keyring like everything else.

Get an Oura personal access token from
<https://cloud.ouraring.com/personal-access-tokens>. The prompt hides your
typing, and the token goes straight into the OS keyring — Credential Manager
on Windows, Keychain on macOS, Secret Service on Linux. The database stores
only the *name* of the keyring entry, never the token. Deliberately, there is
no environment-variable option: those are inherited by every child process
and end up in crash reports.

`python -m ticker.auth.setup list` shows what's configured;
`remove oura` forgets the token and disables the source without touching any
data it already collected.

What Oura contributes:

| Metric | From |
|---|---|
| `heart_rate_bpm` | `heartrate`, point samples |
| `spo2_pct` | `daily_spo2`, one average per day |
| `steps`, `active_energy_kcal` | `daily_activity`, daily totals |
| `sleep_duration_s` | `sleep`, one figure per sleep period |
| `sleep_stage` | `sleep`, a five-minute block per phase |

Sleep periods also become sessions, so they show up in the dashboards next to
the ones you started by hand.

What Fitbit contributes:

| Metric | From |
|---|---|
| `heart_rate_bpm` | intraday series, one point per minute |
| `steps`, `active_energy_kcal` | the daily activity summary |
| `sleep_stage`, `sleep_duration_s` | sleep logs |
| `spo2_pct`, `respiratory_rate_bpm` | the SpO2 and breathing-rate series |
| `skin_temp_delta_c` | nightly relative skin temperature |
| `weight_kg`, `body_fat_pct` | weight logs |

Two Fitbit-specific things worth knowing. **Set `TICKER_FITBIT_TZ`** to the
IANA zone your Fitbit account reports in (`America/New_York`, say). Fitbit
answers with local wall-clock times carrying no offset, so this is what makes
them interpretable; it defaults to your OS zone when that resolves to a real
IANA name, and to UTC when it does not, which will visibly misplace things
rather than quietly misplace them. And **intraday heart rate needs Fitbit to
approve your application** — until they do, that one metric returns a
permission error while everything else works. Fitbit also rate limits to 150
requests per hour per user, and the daily endpoints spend one request per
day, so backfilling a year takes a while.

### Apple Health

No API exists, so this one is a file import: export from the Health app on
your phone, then

```
python -m ticker.ingest.importer apple_health export.zip
```

The zip is read in place and parsed as a stream — exports run past a
gigabyte. Re-importing a later export that overlaps an earlier one is the
expected way to use it and does not duplicate anything.

Two things Apple records are deliberately *not* imported: HRV, because Apple
measures SDNN and the strap measures RMSSD and mixing them in one series
would corrupt both, and wrist temperature, because Apple's is absolute while
the metric here is a deviation from baseline. Anything in a unit the importer
doesn't recognise is skipped and counted rather than assumed — it tells you
what it skipped when it finishes.

### How syncing behaves

`sync` wakes every 15 minutes and asks for everything since its last
watermark, minus a 24-hour overlap. The overlap is deliberate: vendors amend
recent data after the fact — a sleep score gets rewritten hours later — and
re-fetching is free because the unique index turns an unchanged row into a
no-op and an amended one into an update.

A window that fails leaves the watermark where it was, so the next run
re-fetches rather than skipping. On a fresh source it also walks backwards
30 days at a time to fill in history, at lower priority than the live window,
resuming where it left off if interrupted.

`--once` runs a single cycle, which is what you want from Task Scheduler or
cron. `--verbose` shows each window as it goes.

## Running it on two machines

Bluetooth is physically local. Cloud pulls are not. If the strap is near a
laptop but the data should live on the box that's always on, Ticker splits
along exactly that line:

```
[ machine near the strap ]          [ homelab box ]
  ticker-agent                        ticker-server
  - BLE scan/connect          --->    - read API + ingest
  - local spool (SQLite)      HTTP    - the database
  - drains on reconnect               - pull connectors (ticker-sync)
                                    [ Grafana ]
```

On the box with the disk:

```
ticker-server --host 0.0.0.0 --allow-remote --token "$(openssl rand -hex 16)"
```

On the machine near the strap:

```
ticker-agent --server http://homelab:8477 --token <the same token>
```

That plain-HTTP example is suitable only for loopback or a trusted private
LAN. A token authenticates requests but does not encrypt health data or the
token itself. Across an untrusted network, put the server behind TLS (for
example with a reverse proxy) or carry the connection through a trusted VPN.

That's the whole configuration. The agent has no database, no dashboards and
no window — it scans, connects, and posts.

**Nothing about this is mandatory.** Running the app on one machine is
unchanged and needs none of it: no agent, no server, no token. The split is
something you turn on, not something you migrate to.

### When the server is unreachable

The agent spools to a local SQLite file and drains it when the server comes
back. Server rebooting, Wi-Fi gone, laptop lid closed mid-run — the strap
keeps recording and the rows arrive later, in order.

Everything is written to the spool *before* the network is attempted and
deleted only once the server has acknowledged it, so a crash mid-post costs
nothing and a replay is harmless: observations upsert on their natural key
and sessions carry a stable id, so sending the same batch three times leaves
exactly one copy.

```
ticker-agent --status      # what's spooled, and whether the server answers
ticker-agent --drain       # push what's waiting and exit
```

The spool has a row cap (`TICKER_SPOOL_MAX_ROWS`, two million by default,
about three weeks of 1 Hz heart rate with RR intervals). Past it the oldest
rows go first, so a machine that's been offline for a month stops growing
rather than filling the disk with the thing the spool exists to protect.

### The read API

The server is worth running on its own machine too, since it's also the
programmatic way into the data:

```
GET  /api/metrics                                  the registry
GET  /api/observations?metric=&from=&to=&bucket=   series, bucketed server-side
GET  /api/sessions?from=&to=                       session list
GET  /api/sources                                  configuration and sync health
POST /api/ingest                                   agent push
POST /api/sync/{source_id}                         sync a pull source now
POST /mcp                                          MCP for agents (read-only)
GET  /health                                       liveness, no token needed
```

The token can arrive as an `X-Ticker-Token` header, `Authorization: Bearer`,
or a `?token=` parameter; MCP clients know the middle one.

```
curl 'http://127.0.0.1:8477/api/observations?metric=heart_rate_bpm&from=-6h&bucket=5m'
```

`from` and `to` take an ISO timestamp, an epoch, or a relative `-6h`.
`bucket` takes `30s`, `5m`, `1h` — or `1d`, which reads `rollups_daily`
rather than recomputing. Bucketing happens in SQLite, because a week of
1 Hz heart rate is 600,000 rows and no chart wants them over the wire when
it has 800 pixels to draw them in. Buckets align to the epoch, so panning a
chart doesn't reshuffle which samples land together.

`/api/sources` is built to be cheap enough to poll — it reports when each
source last delivered, which is a handful of index seeks. Add `?counts=1`
for row counts per source, which is a scan per source and therefore opt-in.

It binds to loopback by default. Binding anywhere else needs both
`--allow-remote` and a token, and it refuses to start without them rather
than warning — `/api/ingest` writes to the database, so the failure mode of
getting that wrong isn't a leak, it's an open ingest port.

Grafana doesn't need any of this: it reads the SQLite file directly.

## Dashboards

`grafana/` has a provisioned Grafana setup — datasource, two dashboards, and
a compose file — so there's nothing to click through to get a working view:

```
cd grafana
TICKER_DB_DIR="$LOCALAPPDATA/Ticker" docker compose up
```

PowerShell:

```powershell
cd grafana
$env:TICKER_DB_DIR = "$env:LOCALAPPDATA\Ticker"; docker compose up
```

Then <http://localhost:3000> — anonymous, no login. Point `TICKER_DB_DIR` at
the directory holding `hrm_data.sqlite3` (see the table above for your OS).
It's the directory, not the file: SQLite in WAL mode needs its `-wal` and
`-shm` siblings, which is also why that mount isn't read-only.

**Ticker — Overview** is the long view. It reads `rollups_daily` and shows
daily average heart rate with a min/max band, HRV, and how much data each day
holds. The **Resolution** variable switches the same panel between the daily
rollups and raw `observations` — rollups for long spans, raw for detail.

**Ticker — Session detail** reads raw `observations` over whatever window is
selected: every sample as the strap reported it, plus RR intervals or any
other metric.

Dashboards are JSON files in the repo, versioned with the code that produces
the data they read. Editing a panel in Grafana works, but save the change
back to the file or the next provisioning reload overwrites it.

### Filling in the rollups

The dashboards read `rollups_daily`, which is maintained automatically at the
end of every session. Data that predates that — anything migrated from v1 —
needs one pass:

```
python -m ticker.db.backfill --hrv     # derive HRV from stored RR intervals
python -m ticker.db.rollup --all       # then build the daily rollups
```

Both are idempotent, so re-running them costs nothing and changes nothing.
`--hrv` is worth running once on migrated data: HRV is computed as readings
arrive, so v1 data has RR intervals but no HRV until this fills it in.

## Tests

```
pip install -r requirements-dev.txt
pytest
```

Everything's hardware- and OS-independent (no real BLE connection, no real
window opened), so it runs the same everywhere. This is what CI runs too,
on `windows-latest`, `macos-latest` and `ubuntu-latest`.

The HTTP tests are the exception to "no real I/O": the watch endpoint, the
read API, `/mcp` and the agent-to-server round trip each start a real server
on `127.0.0.1` with port `0` (the OS picks a free one) and make real requests
to it, because the thing worth testing there is the wire contract something
else will actually hit. `ticker-mcp` likewise runs once as a real subprocess
over pipes, which is how an agent launches it. Still no fixed port, no
network peer, no hardware.

Everything either side of those wires is tested without a socket: the API's
endpoints take a parsed request and return a status, and the agent's uplink
runs against a poster that can be made to fail on demand — which is what
makes "the spool drains in order when the server comes back" a test rather
than a hope.

## Releasing

Push a tag like `v1.0.0` and the release workflow builds all three platforms
and attaches the binaries to a GitHub Release. It can also be triggered by
hand from the Actions tab, but the tag has to exist already — the build
checks out the tag itself, so a manual run can't ship untagged code under a
version number.

On Windows it also builds an installer, publishes checksums, and attaches
ready-to-submit winget manifests — filled in with the release's version, URL
and installer hash, so publishing to winget is a copy of those three files
into a `winget-pkgs` fork with nothing to edit by hand.

On macOS it signs the app with the hardened runtime, notarizes it, staples
the ticket, and does the same again for the `.dmg` it wraps it in — two
submissions, because stapling the disk image doesn't staple the app inside
it and both get downloaded. Order matters more than usual here: every way of
getting it wrong produces a green build and an app that won't open, so
`tests/test_macos_packaging.py` asserts the sequence.

Code signing is wired into the workflow on both platforms but inactive:
Windows signing is skipped unless the Azure credentials are configured, and
macOS signing and notarization unless a Developer ID certificate is, so
releases build unsigned rather than failing. `packaging/README.md` covers
what turning either on involves — and is honest that signing alone doesn't
buy instant SmartScreen trust, though notarization *does* buy a clean first
open on macOS.

## Notes

- Strap or watch won't connect over BLE: most only allow 1–2 simultaneous
  connections, so check it isn't already paired with Garmin Connect, Zwift,
  a phone, etc. A strap also has to be on skin — a lot of them sleep
  otherwise.
- "Could not listen on ... permission denied" on Windows, on a port nothing
  is using: Hyper-V and WSL reserve whole blocks of ports at boot, and
  binding inside one fails as a permission error rather than "in use". This
  machine had 8594–9093 reserved, which is why the default is 8476 rather
  than something in the 8700s. `netsh interface ipv4 show excludedportrange
  protocol=tcp` lists them; pick an `HRM_HTTP_PORT` outside.
- Nothing arrives over HTTP: check `http://<pc-ip>:8476/health` from a
  browser first. If that fails the watch was never getting through either,
  and it's a firewall or wrong-IP problem on this end. Windows will prompt
  to allow Python through the firewall on first run; if that got dismissed,
  add a rule for TCP 8476 on private networks.
- Packaged app doing nothing visible: check `ticker.log` next to the
  database. A windowed build has no console, so errors go there instead.
- Want ANT+: different protocol, needs a USB dongle and the `openant`
  library. Not implemented, but it's the obvious third `HRSource` — it
  would drop in next to the other two without touching the UI.
