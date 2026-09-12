# Ticker

Your health data, on your machine, in one app your AI agents can read.

Ticker gathers heart rate, HRV, sleep, steps, stress, SpO2, weight and more
from the devices and apps you already have — a BLE chest strap or Garmin
watch live; Oura, Fitbit and Garmin Connect through your accounts; Apple
Health and Garmin from their exports — into one SQLite file on your own
disk. It shows you what it has on a page in your browser, and serves the
same data over the [Model Context Protocol](https://modelcontextprotocol.io),
so Claude Code, Codex, Claude Desktop or any other MCP client — or a model
you run yourself with Ollama — can answer questions like *"how has my
resting heart rate moved since I started running?"* or *"do I sleep worse
after late workouts?"* from your real numbers.

Windows, macOS, Linux. No Ticker account, no Ticker cloud.

## Getting started

```
pipx install ticker
ticker
```

That starts Ticker and opens its page at <http://127.0.0.1:8477>. Everything
happens there:

- **Ask** a question, answered from your own data by a model you run
  yourself (Ollama, LM Studio) — without any of it leaving the machine.
- **Connect** an Oura ring, Fitbit or Garmin account (sign
  in), or an Apple Health or Garmin export (give the path).
- **Live heart rate**: turn on a Bluetooth strap or a Garmin watch, watch the
  reading, and start a session to record it.
- **Ask an agent**: copy one line into Claude Code, Codex or Claude Desktop.
- **What's recorded**: every metric, the dates it covers, and anything that
  needs attention — a sync that's failing, an account that went quiet.

Ticker keeps cloud accounts in sync for as long as it runs. Running `ticker`
again while it's up just opens the page: one database, one Ticker.

## One app

```
strap or watch (live) ──────┐
Oura, Fitbit (cloud sync) ──┼─► one writer ─► one SQLite file ─┬─► the page       /
Apple Health (file import) ─┘                                  ├─► read API       /api
                                                               └─► agents (MCP)   /mcp, ticker mcp
```

`ticker` is one process that owns all of that: the live source, cloud sync on
a schedule, imports, the daily rollups, the read API, MCP, and the page. The
rest of the command line is subcommands of the same thing:

| Command | What it does |
|---|---|
| `ticker` | Start Ticker and open its page |
| `ticker --headless` | Start it without opening a browser — a server, a login item |
| `ticker mcp` | MCP over stdio: what an agent launches (`--compact` for small models) |
| `ticker ask "QUESTION"` | Ask your data from the terminal, answered by a local model |
| `ticker status` | What's connected and how fresh, in the terminal (`--json` too) |
| `ticker import FILE` | Import an Apple Health or Garmin export, or a `.fit` file, without the page |
| `ticker agent` | Stream a strap to a Ticker on another machine |
| `ticker sync --once` | One cloud sync without the app, for cron |
| `ticker rollup`, `ticker backfill` | Maintenance |

`ticker-app` is the same app from a launcher with no terminal behind it —
what to put in a Start menu or login item.

### Keeping it running

Syncing and live recording happen while Ticker runs; agents can read your
data either way (see below). To start it with your session, run
`ticker --headless` from whatever your OS uses:

- **Windows**: a Task Scheduler task "At log on" running `ticker-app`, or a
  shortcut to it in `shell:startup`.
- **macOS**: the packaged `Ticker.app` in Login Items.
- **Linux**: a systemd user unit —

  ```ini
  # ~/.config/systemd/user/ticker.service
  [Unit]
  Description=Ticker

  [Service]
  ExecStart=%h/.local/bin/ticker --headless
  Restart=on-failure

  [Install]
  WantedBy=default.target
  ```

  then `systemctl --user enable --now ticker`.

## Asking an agent

```
claude mcp add ticker -- ticker mcp
```

Then ask Claude about your sleep. The page's **Ask an agent** card has this
and the others ready to copy:

**Claude Code**, in every project rather than just this one:
`claude mcp add --scope user ticker -- ticker mcp`

**Codex**, in `~/.codex/config.toml`:

```toml
[mcp_servers.ticker]
command = "ticker"
args = ["mcp"]
```

**Claude Desktop**, in `claude_desktop_config.json`:

```json
{ "mcpServers": { "ticker": { "command": "ticker", "args": ["mcp"] } } }
```

If a client can't find the command — desktop apps often don't inherit your
shell's `PATH` — give it the full path from `which ticker` (`where` on
Windows).

**Over HTTP.** The running app also serves MCP at `/mcp`. That's how an agent
reaches the packaged app, which has no console for `ticker mcp`, and how it
reaches a Ticker on another machine:

```
claude mcp add --transport http ticker http://127.0.0.1:8477/mcp
claude mcp add --transport http ticker http://homelab:8477/mcp \
  --header "Authorization: Bearer <token>"
```

`ticker mcp` bridges into the running app, so an agent sees exactly what the
page does. When nothing is running it answers from the database directly,
read-only, so an agent still works before Ticker has ever been started.
`--server http://homelab:8477` points it at a Ticker elsewhere.

### What the agent gets

| Tool | What it answers |
|---|---|
| `get_overview` | What's connected, which metrics exist, what dates they cover and how fresh they are — the agent's starting point |
| `get_daily_summary` | Day, week or month values of up to eight metrics, with typical, lowest, highest and latest |
| `get_sleep` | Night by night: bedtime, wake, time asleep, efficiency, stages, heart rate and HRV while asleep |
| `list_sessions`, `get_session` | Workouts, recordings and sleeps, and what happened during each |
| `get_timeseries` | Readings through a window — a workout, a night — bucketed to fit |
| `query_sql` | Anything else, as read-only SQL |

The tools are shaped for a model reading them rather than a chart: times
come back in your local zone, every value carries its unit, series are
bucketed to a point budget instead of dumping 600,000 rows into the agent's
context, and anything that would mislead is said outright — two sources both
counting your steps are listed side by side rather than added together, and
an account that stopped syncing is flagged. Sleep is rebuilt night by night
from stage data, which is what lets Apple Health (stages, no sessions) and
Oura (sessions and stages) answer the same way.

**Agents can't change your data.** The MCP tools open the database
read-only, SQLite refuses writes on that connection, and an authorizer
allows nothing but reads — no `DELETE`, no `ATTACH`, no state-changing
`PRAGMA`, whatever SQL the agent sends. Every call has a time limit, so a
runaway query fails rather than hangs. Connecting accounts stays out of the
agent's reach on purpose: that happens on the page, and credentials go to the
OS keyring, never into anything a tool returns.

## Asking a local model

The **Ask your data** box at the top of the page answers questions with a
model you run yourself, so neither the question nor your data leaves the
machine. Ticker runs the model's tool calls itself, read-only, and lists
each one under the answer.

1. Install [Ollama](https://ollama.com) and pull a model that can call
   tools: `ollama pull qwen3:14b`, or `llama3.1:8b` on a smaller machine.
2. Open **Model settings** on the page and pick it. Ollama at
   `http://127.0.0.1:11434` is the default; for LM Studio, llama.cpp's
   server or vLLM, give their OpenAI-compatible URL, ending in `/v1`.

The same from a terminal:

```
ticker ask "How did I sleep this week?"
ticker ask --model llama3.1:8b "Compare my steps this week with last week"
```

The model needs tool calling — Qwen 3, Llama 3.1/3.3, Mistral and Gemma 4
all have it — and around 14B parameters is where answers get dependable.
Ticker shows it a **compact** tool set, six tools described in a sentence
each and no SQL tool, and asks Ollama for an 8k context window per request
(`TICKER_LLM_CONTEXT`), because Ollama's own default is too small for tool
use.

### Other local-model clients

Anything that runs an Ollama model and speaks MCP can use Ticker as well;
point it at the compact profile.

- **Open WebUI** has native MCP since v0.6.31. Add an MCP server, Streamable
  HTTP, at `http://host.docker.internal:8477/mcp?profile=compact`. Open
  WebUI usually runs in Docker and reaches this machine by that name, so on
  Docker Desktop start Ticker with
  `TICKER_API_ALLOWED_HOSTS=host.docker.internal`. On Linux, where a
  container can't reach 127.0.0.1 at all, run `ticker --host 0.0.0.0
  --allow-remote --token <token>` and give Open WebUI the token as a Bearer
  header.
- **Terminal clients** such as [MCPHost](https://github.com/mark3labs/mcphost)
  or [ollmcp](https://github.com/jonigl/mcp-client-for-ollama): configure
  `ticker mcp --compact` as a stdio server.

## Connecting your data

### A strap or a watch, live

On the page, **Live heart rate** → **Bluetooth strap** or **Watch over
Wi-Fi**. The choice is remembered. It's off by default: Ticker runs all
day, and a Bluetooth scan running all day with no strap in range drains a
laptop for nothing.

**Bluetooth strap** covers anything that speaks the standard BLE Heart Rate
Service (Garmin, Polar, Wahoo, whatever) — no vendor SDK involved — and
**Garmin watches with Broadcast Heart Rate turned on**, which advertise
exactly the same service a strap does. On the watch: hold the button →
*Sensors & Accessories* → *Wrist Heart Rate* → *Broadcast Heart Rate*.
Broadcast mode is a Bluetooth feature, not a way around it: the watch
becomes a heart rate peripheral that this machine connects to.

**Watch over Wi-Fi** takes the machine out of the radio business entirely.
Ticker listens on a port and a Connect IQ app on the watch posts readings to
it; the page shows the address to point the watch at. The watch app lives in
[`connectiq/`](connectiq/), along with how to build and sideload it. It's
written but untested on real hardware; the README there says what to check
first. Anything that can make an HTTP request works just as well, which is
useful for testing without a watch:

```
curl "http://localhost:8476/hr?hr=142&device=test"
```

**Sessions** are yours to start and stop, separate from the connection. The
live number shows whenever something's connected; readings are saved only
while a session runs, and a session survives a brief dropout (it leaves a
gap). Each reading becomes several rows: heart rate, one per RR interval in
the packet, and HRV (RMSSD) derived from those beats — the strap never sends
HRV itself, so any source with RR intervals gets it for free.

### Oura

Create an OAuth application at <https://cloud.ouraring.com/user/applications>
and register `http://127.0.0.1:8478/callback` as its redirect URI. Under
**Connect** → **Oura ring**, enter its client id and secret, then approve the
requested access in Oura. The refresh token and application credentials go
into the OS keyring — Credential Manager on Windows, Keychain on macOS,
Secret Service on Linux — and the database stores only the *name* of the
keyring entry. Oura retired personal access tokens in December 2025, so Ticker
uses the supported authorization-code flow. Syncing starts after approval.

| Metric | From |
|---|---|
| `heart_rate_bpm` | `heartrate`, point samples |
| `spo2_pct` | `daily_spo2`, one average per day |
| `steps`, `active_energy_kcal` | `daily_activity`, daily totals |
| `sleep_duration_s` | `sleep`, one figure per sleep period |
| `sleep_stage` | `sleep`, a five-minute block per phase |

Sleep periods also become sessions, next to the ones you record yourself.

### Fitbit

Fitbit needs an application of your own: register one at
<https://dev.fitbit.com>, set `TICKER_FITBIT_CLIENT_ID` (a client id is not
a secret), and restart Ticker. Then **Connect** → **Fitbit** → **Sign in
with Fitbit** opens Fitbit's sign-in; approve it and Ticker catches the
redirect on a loopback port, exchanges the code with PKCE, and keeps the
tokens in the keyring. The redirect comes back to the machine Ticker runs
on, so connect Fitbit from a browser on that machine.

| Metric | From |
|---|---|
| `heart_rate_bpm` | intraday series, one point per minute |
| `steps`, `active_energy_kcal` | the daily activity summary |
| `sleep_stage`, `sleep_duration_s` | sleep logs |
| `spo2_pct`, `respiratory_rate_bpm` | the SpO2 and breathing-rate series |
| `skin_temp_delta_c` | nightly relative skin temperature |
| `weight_kg`, `body_fat_pct` | weight logs |

Two things worth knowing. **Set `TICKER_FITBIT_TZ`** to the IANA zone your
Fitbit account reports in (`America/New_York`, say): Fitbit answers with
local wall-clock times carrying no offset, so this is what makes them
interpretable. It defaults to your OS zone when that resolves to a real IANA
name, and to UTC when it does not, which will visibly misplace things rather
than quietly misplace them. And **intraday heart rate needs Fitbit to
approve your application** — until they do, that one metric returns a
permission error while everything else works. Fitbit rate limits to 150
requests per hour, and the daily endpoints spend one request per day, so
backfilling a year takes a while.

### Garmin

A Garmin watch reaches Ticker three ways, and they stack.

**Live**, as above: Broadcast Heart Rate over Bluetooth, or the Connect IQ
app over Wi-Fi.

**Garmin Connect sign-in**, for everything the watch syncs to Garmin: sleep,
HRV, resting heart rate, stress, Body Battery, steps, SpO2, breathing rate
and all-day heart rate, every 15 minutes like Oura. Garmin offers
individuals no API, so this signs in the way Garmin's own app does, through
the community [garminconnect](https://github.com/cyberjunky/python-garminconnect)
library. That makes it unofficial: it can break when Garmin changes its
login, as it did in March 2026. Garmin support is included in the normal
install. Use **Connect** → **Garmin Connect** on the page, with your Garmin email
and password and, if Garmin asks, the code it sends you. The password is
used for that one sign-in and never stored; the session goes to the OS
keyring. On a box with no browser, `python -m ticker.auth.setup add garmin`
does the same at a terminal.

**Garmin's files**, official, and immune to login changes: request your
data at <https://www.garmin.com/en-US/account/datamanagement/exportdata/>
and import the zip Garmin emails you — or `.fit` files from the watch's
`GARMIN` folder, or Garmin Connect's *Export Original* — under **Connect** →
**Import a file**, or with `ticker import garmin.zip`.

| From | You get |
|---|---|
| Workout `.fit` files, and every activity in the export | Workouts as sessions, heart rate and beat-to-beat intervals — with HRV derived from them, as for a strap |
| Monitoring `.fit` files | All-day heart rate |
| Sleep `.fit` files | Sleep stages: awake, light, deep, REM |
| The export's `sleepData.json` | Each night's window and time asleep |
| The export's `UDSFile` daily summaries | Steps, active energy, resting heart rate, average stress |

Garmin documents none of the archive's JSON, so the import summary names
any file it found but couldn't read — if yours does, that's worth an issue.
Files land as their own source, "Garmin files", apart from a signed-in
account, so a day both report is shown side by side rather than added
together.

### Apple Health

No API exists, so this one is a file: on your iPhone, Health → your picture
→ *Export All Health Data*, copy `export.zip` to this computer, and give its
path under **Connect** → **Import a file** — or run `ticker import
export.zip`. The zip is read in place and parsed as a stream; exports run
past a gigabyte. Importing a later export that overlaps an earlier one is
the expected way to use it and duplicates nothing.

Two things Apple records are deliberately *not* imported: HRV, because Apple
measures SDNN and the strap measures RMSSD and mixing them in one series
would corrupt both, and wrist temperature, because Apple's is absolute while
the metric here is a deviation from baseline. Anything in a unit the
importer doesn't recognise is skipped and counted rather than assumed.

### How syncing behaves

Each cloud account wakes every 15 minutes and asks for everything since its
last watermark, minus a 24-hour overlap. The overlap is deliberate: vendors
amend recent data after the fact — a sleep score gets rewritten hours later
— and re-fetching is free because the unique index turns an unchanged row
into a no-op and an amended one into an update. A window that fails leaves
the watermark where it was, so the next run re-fetches rather than skipping.
On a fresh account it also walks backwards 30 days at a time to fill in
history, at lower priority than the live window, resuming where it left off.

**Sync now** on the page (or `POST /api/sync/{id}`) starts a cycle at once
instead of waiting out the 15 minutes — though never inside a vendor's
rate-limit back-off. An account connected while Ticker runs starts syncing
immediately, and daily rollups are rebuilt every few minutes over whatever
changed. `ticker sync --once` runs a single cycle without the app, for cron
or Task Scheduler.

## Running Ticker on a server

On the box that's always on:

```
ticker --headless --host 0.0.0.0 --allow-remote --token "$(openssl rand -hex 16)"
```

Then open `http://homelab:8477/?token=<token>` from anywhere on your network
— the page keeps the token for that tab and takes it out of the address bar
— and point agents at `http://homelab:8477/mcp` with the token as a Bearer
header. Binding anywhere but loopback needs both `--allow-remote` and a
token, and Ticker refuses to start without them rather than warning.

That plain-HTTP setup is suitable only for a trusted private LAN. A token
authenticates requests but does not encrypt health data or the token itself.
Across an untrusted network, put Ticker behind TLS (a reverse proxy, say) or
reach it through a VPN.

### A strap near a laptop, data on the server

Bluetooth is physically local; the database doesn't have to be. On the
machine near the strap:

```
ticker agent --server http://homelab:8477 --token <the same token>
```

The agent has no database and no page — it scans, connects, and posts. When
the server is unreachable it spools to a local SQLite file and drains when
the server comes back: server rebooting, Wi-Fi gone, laptop lid closed
mid-run, the strap keeps recording and the rows arrive later, in order.
Everything is spooled *before* the network is tried and deleted only once
the server acknowledges it, so a crash mid-post costs nothing and a replay
is harmless — observations upsert on their natural key and sessions carry a
stable id.

```
ticker agent --status      # what's spooled, and whether the server answers
ticker agent --drain       # push what's waiting and exit
```

The spool has a row cap (`TICKER_SPOOL_MAX_ROWS`, two million by default,
about three weeks of 1 Hz heart rate with RR intervals); past it the oldest
rows go first.

### The read API

The programmatic way into the data, served by the same process:

```
GET  /api/metrics                                  the registry
GET  /api/observations?metric=&from=&to=&bucket=   series, bucketed server-side
GET  /api/sessions?from=&to=                       session list
GET  /api/sources                                  configuration and sync health
POST /api/ingest                                   agent push
POST /api/sync/{source_id}                         sync a cloud account now
POST /mcp                                          MCP for agents (read-only)
POST /mcp?profile=compact                          the same, sized for small models
GET  /                                             the page (its data is under /ui)
GET  /health                                       liveness, no token needed
```

```
curl 'http://127.0.0.1:8477/api/observations?metric=heart_rate_bpm&from=-6h&bucket=5m'
```

`from` and `to` take an ISO timestamp, an epoch, or a relative `-6h`.
`bucket` takes `30s`, `5m`, `1h` — or `1d`, which reads `rollups_daily`
rather than recomputing. Bucketing happens in SQLite, because a week of
1 Hz heart rate is 600,000 rows and no chart wants them over the wire.
Buckets align to the epoch, so panning a chart doesn't reshuffle them.
`/api/sources` is cheap enough to poll; `?counts=1` adds row counts, which
cost a scan per source.

The token can arrive as an `X-Ticker-Token` header, `Authorization: Bearer`,
or a `?token=` parameter. Because the page can store credentials and stop
the app, every route but `/health` also refuses what a hostile web page
would send: on a loopback server the `Host` header must name loopback, which
defeats DNS rebinding, a foreign `Origin` is refused, and the page's actions
accept only JSON bodies, which a cross-site form can't send. Agents, curl and
Grafana send none of that and aren't affected.

## Configuration

Choices made on the page (the live source and the Ask box's model) are kept
in `settings.json` beside the database. Environment variables configure the
rest, and an environment variable that is set always wins over the page.

| Variable | Default | What it does |
|---|---|---|
| `HRM_DB_PATH` | see below | Where the database lives |
| `HRM_SOURCE` | unset | Pins the live source (`ble`, `http` or `off`); unset lets the page choose |
| `HRM_GRAPH_WINDOW_SEC` | `300` | How much history the live chart shows |
| `TICKER_TZ` | OS timezone | IANA zone for local calendar days and the times agents see; stored timestamps stay UTC |
| `TICKER_RAW_RETENTION_DAYS` | `90` | Days to keep compressed vendor API responses; `0` keeps them indefinitely |
| `TICKER_BUSY_TIMEOUT_MS` | `5000` | How long a database writer waits for another connection's lock |
| `TICKER_FITBIT_CLIENT_ID` | unset | Your Fitbit application's client id |
| `TICKER_FITBIT_TZ` | OS timezone | The zone your Fitbit account reports in |
| `TICKER_OURA_REDIRECT_PORT` | `8478` | Fixed loopback port registered as the Oura OAuth redirect |

The app's server:

| Variable | Default | What it does |
|---|---|---|
| `TICKER_API_HOST` | `127.0.0.1` | Interface to listen on (`--host`). Anything but loopback also needs the flag below *and* a token |
| `TICKER_API_PORT` | `8477` | Port for the page, the API and MCP (`--port`) |
| `TICKER_API_TOKEN` | unset | Shared secret (`--token`). Optional on loopback, required off it |
| `TICKER_API_ALLOW_REMOTE` | `0` | Permit listening off loopback (`--allow-remote`) |
| `TICKER_API_ALLOWED_HOSTS` | unset | Extra host names a token-less loopback server answers to, comma-separated — `host.docker.internal` for Open WebUI in Docker |
| `TICKER_API_MAX_BATCH` | `5000` | Most observations one `/api/ingest` call may carry |

The Ask box:

| Variable | Default | What it does |
|---|---|---|
| `TICKER_LLM_URL` | chosen on the page | The model server, `http://127.0.0.1:11434` until chosen; a URL ending in `/v1` is spoken to as OpenAI-compatible. Setting it pins it |
| `TICKER_LLM_MODEL` | chosen on the page | The model. Setting it pins it |
| `TICKER_LLM_CONTEXT` | `8192` | Context window asked of Ollama per request |
| `TICKER_LLM_TIMEOUT_SEC` | `300` | How long one model reply may take |

Bluetooth strap:

| Variable | Default | What it does |
|---|---|---|
| `HRM_DEVICE_ADDRESS` | auto-discover | Pin a specific device by BLE address, skip scanning. Pinned means pinned: if it isn't there, Ticker says so rather than connecting to some other strap in range |
| `HRM_SCAN_TIMEOUT_SEC` | `10` | How long each scan attempt runs |
| `HRM_RECONNECT_DELAY_SEC` | `5` | Delay before retrying a dropped connection |

Watch over Wi-Fi:

| Variable | Default | What it does |
|---|---|---|
| `HRM_HTTP_HOST` | `0.0.0.0` | Interface to listen on. The default is what a watch on the LAN needs; `127.0.0.1` only ever hears from this machine |
| `HRM_HTTP_PORT` | `8476` | Port to listen on |
| `HRM_HTTP_TOKEN` | unset | Shared secret the watch must send. When listening on the LAN, Ticker generates a pairing token if this is unset and shows it in the live-source status |
| `HRM_HTTP_TIMEOUT_SEC` | `15` | Silence before the watch stops counting as connected |

`ticker agent`:

| Variable | Default | What it does |
|---|---|---|
| `TICKER_SERVER_URL` | `http://127.0.0.1:8477` | Where the agent posts |
| `TICKER_SPOOL_PATH` | beside the database | The agent's local spool file |
| `TICKER_AGENT_BATCH` | `500` | Observations per uplink POST |
| `TICKER_AGENT_TIMEOUT_SEC` | `10` | How long a post may take before it counts as unreachable |
| `TICKER_SPOOL_MAX_ROWS` | `2000000` | Spool cap; past it the oldest rows go first. `0` means no limit |

Advanced ingest tuning: `TICKER_COALESCE_ROWS` (`200`) and
`TICKER_COALESCE_MS` (`2000`) bound how long live rows wait before a commit;
`TICKER_NORMALIZE_BATCH` (`1000`) is how many observations are handed to the
writer at once.

Default database location:

| OS | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\Ticker\hrm_data.sqlite3` |
| macOS | `~/Library/Application Support/Ticker/hrm_data.sqlite3` |
| Linux | `$XDG_DATA_HOME/Ticker/hrm_data.sqlite3` (or `~/.local/share/...`) |

## Data and privacy

Ticker is single-user and local-first: it does not host your database or
send observations to a Ticker-operated service. Cloud connectors contact the
vendor you configure, and `ticker agent` sends data only to the server you
give it. The page is served on 127.0.0.1 unless you choose otherwise.

An AI agent is the one exception worth spelling out. The MCP tools run on
your machine and only read, but whatever a tool returns becomes part of the
agent's conversation, and so goes to the model provider behind that agent —
Anthropic for Claude, OpenAI for Codex — under that provider's terms. The
tools return summaries and bounded series rather than whole tables, but
treat an agent session over your health data the way you would pasting the
same numbers into a chat. The Ask box is the way round that: with the model
server on this machine, questions and data never leave it.

The database contains health observations and compressed vendor responses;
it is not encrypted at rest, so protect it and its backups like any other
sensitive file. Vendor tokens live in the OS keyring, never in the database
or the environment, and raw responses are removed after 90 days by default.

Ticker is data plumbing, not a medical device: it does not diagnose,
interpret clinical thresholds, or make medical claims.

## Looking at the data yourself

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

`SELECT name FROM metrics` lists what's available. Timestamps are ISO 8601
UTC at millisecond resolution, stored as text, so they sort correctly as
strings. `ticker/db/queries.py` has helpers for the common reads.

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

Then <http://localhost:3000> — anonymous Viewer access, bound to loopback so
it is reachable only from this computer. Point `TICKER_DB_DIR` at
the directory holding `hrm_data.sqlite3`. It's the directory, not the file:
SQLite in WAL mode needs its `-wal` and `-shm` siblings, which is also why
that mount isn't read-only.

**Ticker — Overview** reads `rollups_daily` and shows daily heart rate with
a min/max band, HRV, and how much data each day holds; its **Resolution**
variable switches the same panel to raw `observations`. **Ticker — Session
detail** reads raw `observations` over whatever window is selected.
Dashboards are JSON files in the repo; edit a panel in Grafana and save the
change back to the file, or the next provisioning reload overwrites it.

Rollups are maintained while Ticker runs. Data that predates that —
anything migrated from v1 — needs one pass:

```
ticker backfill --hrv     # derive HRV from stored RR intervals
ticker rollup --all       # then build the daily rollups
```

Both are idempotent.

## Building a standalone app

```
python build.py
```

Produces `dist/Ticker` — a folder containing `Ticker.exe` (Windows),
`Ticker.app` (macOS), or a `Ticker` binary (Linux), with the Python runtime
beside it. Opening it starts Ticker and opens the page; being a windowed
build it has no console, so it logs to `ticker.log` beside the database, and
agents reach it over HTTP (`claude mcp add --transport http ticker
http://127.0.0.1:8477/mcp`) rather than through `ticker mcp`.

A folder rather than a single file on purpose: PyInstaller's one-file
bootloader unpacks itself to a temp directory and runs from there, which
antivirus heuristics treat as dropper behaviour. On Windows, `python build.py
--installer` also builds a per-user installer (needs
[Inno Setup](https://jrsoftware.org/isdl.php)); uninstalling leaves your
database alone. On Linux, `--appimage-only --version 1.2.3` wraps the folder
in an AppImage (needs `appimagetool`); on macOS, `--dmg-only` wraps the
signed bundle in a disk image.

Every release publishes uniquely named archives/installers, a platform
checksum file and a build manifest containing its version, commit, sizes and
signing status. The workflow signs Windows and macOS when credentials are
configured, and can enforce signing with `SIGN_RELEASES=true`. It creates a
draft only after all three platform builds pass, verifies uploaded sizes, and
then publishes. `pipx install ticker` remains available for Python users.

### Backup, restore, and updates

The page's **Backup and support** card creates a verified SQLite snapshot,
checks GitHub for a newer release only when you ask, and downloads diagnostics
without recordings, credential references, device identifiers, or local file
paths. The command-line equivalents are:

```
ticker backup create
ticker backup create D:\safe\ticker.sqlite3
ticker backup restore D:\safe\ticker.sqlite3
```

Stop Ticker before restoring. Restore first preserves the current database as
a timestamped snapshot. Ticker refuses to open a database created by a newer
schema, preventing an older binary from silently damaging it.

### Upgrading from an earlier version

**From 0.2**, the separate programs are now one app:

| Before | Now |
|---|---|
| The Tk window (`ticker`, `python -m ticker.ui.app`) | `ticker` — the page |
| `ticker-setup add oura` / `add fitbit` | **Connect** on the page |
| `ticker-import apple_health FILE` | **Connect** → Apple Health, or `ticker import FILE` |
| `ticker-sync` | Runs inside `ticker`; `ticker sync --once` for cron |
| `ticker-server` | `ticker --headless` |
| `ticker-mcp` | `ticker mcp` |
| `ticker-agent`, `ticker-rollup`, `ticker-backfill` | `ticker agent`, `ticker rollup`, `ticker backfill` |

Update agent configs that named `ticker-mcp`. The live source is now chosen
on the page and is off until you choose; `HRM_SOURCE` still pins it. On a
box with no browser, `python -m ticker.auth.setup add oura` still connects
an account from a terminal.

**From v1**, the first run migrates the database in place to the
multi-source schema. It copies the file to `hrm_data.sqlite3.v1.bak` first
and verifies the migrated data against the original — matching row counts,
time bounds and mean heart rate — before committing; if that check fails the
whole migration rolls back. The old tables are kept as `sessions_v1` and
`samples_v1`. There is no downgrade path, which is what the `.bak` is for.

## How it works

```
ticker/app/       the runtime: live source, cloud-sync supervisor, jobs, the page
ticker/api/       the HTTP server: /api, /mcp, the page's routes
ticker/mcp/       MCP: protocol, read-only tools, stdio transport
ticker/ingest/    scheduler, normalizer, importer, derived metrics
ticker/sources/   connectors: BLE, Oura, Fitbit, Apple Health
ticker/db/        schema, migrations, the writer, queries, rollups
```

Connectors never touch SQLite: they yield observations, and one writer
thread owns the connection and commits them in batches. Live sources speak a
small message protocol (status, sample, error) onto a queue that one thread
drains, which is also where the page's Start and Stop land — so a Stop can
never race the sample before it. Cloud accounts each run on their own task
in one event loop. The MCP tools and the page's overview read through
separate read-only connections.

All disk writing happens off any thread that serves a request. An early
version froze under Windows Defender scanning the SQLite journal on every
write; moving writes onto their own thread and switching to WAL mode fixed
it.

## Tests

```
pip install -r requirements-dev.txt
pytest
```

Everything's hardware- and OS-independent — no Bluetooth, no browser — so it
runs the same everywhere, and it's what CI runs on `windows-latest`,
`macos-latest` and `ubuntu-latest`.

The exceptions to "no real I/O" are deliberate: the watch endpoint, the read
API, `/mcp`, the page, and the agent-to-server round trip each start a real
server on `127.0.0.1` port `0` and make real requests to it, because the
wire contract is what something else will actually hit; and the MCP server
runs once as a real subprocess over pipes, which is how an agent launches
it. The runtime is tested end to end the same way: started against a
temporary database with fake connectors and a fake strap, and driven over
HTTP exactly as the page drives it.

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
what turning either on involves.

## Notes

- Strap or watch won't connect over Bluetooth: most allow only one or two
  simultaneous connections, so check it isn't already paired with Garmin
  Connect, Zwift, a phone, etc. A strap also has to be on skin — a lot of
  them sleep otherwise.
- "Could not listen on ... permission denied" on Windows, on a port nothing
  is using: Hyper-V and WSL reserve whole blocks of ports at boot, and
  binding inside one fails as a permission error rather than "in use".
  `netsh interface ipv4 show excludedportrange protocol=tcp` lists them; pick
  a `TICKER_API_PORT` or `HRM_HTTP_PORT` outside.
- Nothing arrives from the watch: check `http://<pc-ip>:8476/health` from a
  browser first. If that fails the watch was never getting through either,
  and it's a firewall or wrong-IP problem on this end. Windows will prompt to
  allow Python through the firewall the first time the watch source starts;
  if that got dismissed, add a rule for TCP 8476 on private networks.
- The packaged app opened no page: open <http://127.0.0.1:8477> yourself,
  and check `ticker.log` next to the database.
- Want ANT+: different protocol, needs a USB dongle and the `openant`
  library. Not implemented, but it's the obvious third live source — it
  would drop in next to the other two without touching the page.
