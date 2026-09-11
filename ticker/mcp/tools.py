"""
The MCP tools: what an agent can ask Ticker, and what comes back.

The reader here is a language model rather than a chart, which changes three
things compared with the read API:

* **Bounded answers.** Every tool caps what it returns. A week of 1 Hz heart
  rate is 600,000 rows and a model's context is no place for them, so series
  are bucketed to a point budget, tables are row-capped, and the answer says
  when either happened.
* **Answers that interpret themselves.** Units travel with values, times come
  back in the user's zone with their offset, and anything that would
  mislead -- two sources both counting steps, days whose rollups were never
  built -- is stated in `notes` instead of left to be discovered.
* **Errors the model can act on.** A bad argument raises ToolError naming the
  fix (the known metric names, the accepted time forms). The protocol layer
  returns that as a tool result, so the agent can correct itself and retry.

Every tool reads through `ticker.mcp.readonly`, so none of them can change
anything, and each call runs against a time budget.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Callable, Dict, List, Optional, Tuple

from ticker import config as tconfig
from ticker.db import queries
from ticker.mcp import sleep as sleeplib
from ticker.mcp.readonly import DatabaseUnavailable, QueryTimeout
from ticker.model import iso_utc, now_utc, parse_iso

INSTRUCTIONS = """\
Ticker is the user's own health-data store: heart rate, HRV, sleep, steps, \
SpO2, weight and more, gathered from their devices and apps (BLE chest \
straps, Garmin watches, Oura, Fitbit, Apple Health) into one SQLite database \
on their machine. Everything here is read-only.

Start with get_overview to learn which sources are connected, which metrics \
exist, what dates they cover and how fresh they are. Then use \
get_daily_summary for day-by-day trends, get_sleep for nights, \
list_sessions and get_session for workouts and recordings, get_timeseries \
for detail within a day, and query_sql for anything the others don't cover.

Times in results are in the user's local zone with a UTC offset unless a \
tool says otherwise, and days are local calendar days. The data comes from \
consumer wearables: expect dropouts, spikes and gaps, say so when data is \
thin, and don't present it as a clinical measurement or a diagnosis."""

# -- limits ----------------------------------------------------------------

MAX_POINTS_DEFAULT = 300
MAX_POINTS_LIMIT = 2000
MAX_DAYS = 3660                 # ten years; any longer is not a question
MAX_DAY_ROWS = 400              # group_by=day past this is a wall of numbers
LIVE_FALLBACK_DAYS = 400        # days computed from raw rows when unrolled
MAX_SLEEP_DAYS = 366
SESSIONS_DEFAULT = 50
SESSIONS_MAX = 500
SQL_DEFAULT_ROWS = 200
SQL_MAX_ROWS = 2000
SQL_MAX_CHARS = 20000
SQL_BUDGET_SEC = 20.0
STALE_PULL = timedelta(days=2)
MIN_SLEEP = timedelta(minutes=10)   # shorter 'sleeps' are tracker noise

# Bucket widths an automatic choice picks from, in seconds. Round numbers,
# so a chart's x-axis and a model's description of it both come out sane.
_NICE_BUCKETS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600,
                 7200, 10800, 21600, 43200)

_SOURCE_TYPES = {"stream": "live device", "pull": "cloud sync",
                 "import": "file import"}

_RELATIVE = re.compile(r"^-\s*(\d+(?:\.\d+)?)\s*([smhdw])$")
_RELATIVE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}

TIME_FORMS = ("an ISO date or time (2026-09-01, 2026-09-01T06:30, "
              "2026-09-01T13:30:00Z), 'now', 'today', 'yesterday', or a "
              "relative time such as -6h, -7d or -2w")

SLEEP_COLUMNS = ["night", "source", "bedtime", "wake", "in_bed_min",
                 "asleep_min", "efficiency_pct", "deep_min", "light_min",
                 "rem_min", "awake_min", "unstaged_min", "vendor_sleep_min",
                 "avg_hr", "min_hr", "avg_hrv_ms", "main"]


class ToolError(Exception):
    """A call that can't be answered as asked, with what to do instead."""


# -- tool specs ------------------------------------------------------------

@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    properties: Dict[str, dict]
    required: Tuple[str, ...] = ()

    def definition(self, compact: bool = False) -> dict:
        properties, description = self.properties, self.description
        if compact:
            description = COMPACT_TOOLS[self.name]
            properties = {name: _compact_property(name, prop)
                          for name, prop in self.properties.items()}
        schema: Dict[str, Any] = {"type": "object",
                                  "properties": properties,
                                  "additionalProperties": False}
        if self.required:
            schema["required"] = list(self.required)
        return {
            "name": self.name,
            "title": self.title,
            "description": description,
            "inputSchema": schema,
            "annotations": {"title": self.title, "readOnlyHint": True,
                            "destructiveHint": False, "idempotentHint": True,
                            "openWorldHint": False},
        }


def _when(what: str, default: str) -> dict:
    return {"type": "string",
            "description": "{}. Default: {}. Accepts {}. A date or time "
                           "without an offset is local.".format(
                               what, default, TIME_FORMS)}


_SOURCE_ID = {"type": "integer",
              "description": "Only this source (ids from get_overview). "
                             "Default: every source."}

SPECS = (
    ToolSpec(
        "get_overview", "What data is there",
        "Start here. Lists every connected source (live devices, cloud "
        "syncs, file imports) with when it last delivered data and any "
        "sync error; every metric with its unit, the dates it covers and "
        "which sources report it; how many sessions of each kind exist; and "
        "the timezone the other tools use for days and local times.",
        {}),
    ToolSpec(
        "get_daily_summary", "Daily values and trends",
        "Day-by-day values of one or more metrics over a date range, with a "
        "summary per metric (typical day, lowest, highest, latest). The "
        "daily value is the average for point readings (heart rate, HRV, "
        "SpO2, weight) and the total for accumulating ones (steps, active "
        "energy, sleep duration). Ranges over about three months are grouped "
        "by week and over two years by month unless group_by says "
        "otherwise. When several sources count the same accumulating metric "
        "each is shown separately rather than summed. For sleep stages use "
        "get_sleep.",
        {
            "metrics": {"type": "array", "items": {"type": "string"},
                        "minItems": 1, "maxItems": 8,
                        "description": "Metric names from get_overview, e.g. "
                                       "[\"heart_rate_bpm\", \"steps\"]."},
            "start": _when("First day", "30 days before end"),
            "end": _when("Last day, inclusive", "today"),
            "group_by": {"type": "string",
                         "enum": ["auto", "day", "week", "month"],
                         "description": "Row per day, per week (Monday start) "
                                        "or per month. Default: auto."},
            "source_id": _SOURCE_ID,
        },
        ("metrics",)),
    ToolSpec(
        "get_timeseries", "Readings over time",
        "Readings of one metric across a time window -- heart rate through a "
        "workout, SpO2 overnight -- bucketed so that at most max_points come "
        "back, plus count, mean, min and max over the whole window. Each "
        "bucket holds avg/min/max, or a total for steps and energy. Omit "
        "bucket to have one sized automatically; 'raw' returns individual "
        "readings when they fit. For day-level trends prefer "
        "get_daily_summary.",
        {
            "metric": {"type": "string",
                       "description": "One metric name from get_overview."},
            "start": _when("Window start", "24 hours before end"),
            "end": _when("Window end", "now"),
            "bucket": {"type": "string",
                       "description": "Bucket width such as 30s, 5m, 1h or "
                                      "1d (local days), or 'raw'. Default: "
                                      "sized to fit max_points."},
            "max_points": {"type": "integer", "minimum": 10,
                           "maximum": MAX_POINTS_LIMIT,
                           "description": "Most points to return. Default "
                                          "{}.".format(MAX_POINTS_DEFAULT)},
            "source_id": _SOURCE_ID,
        },
        ("metric",)),
    ToolSpec(
        "list_sessions", "Workouts, recordings and sleeps",
        "Sessions overlapping a window, newest first: recordings started in "
        "the Ticker app (kind 'manual'), vendor workouts ('workout') and "
        "sleep periods ('sleep'), with source, device, local start and end, "
        "duration and how many readings each holds. An end of null means "
        "still in progress. Use get_session for what happened inside one.",
        {
            "start": _when("Window start", "30 days before end"),
            "end": _when("Window end", "now"),
            "kind": {"type": "string",
                     "description": "Only this kind: manual, workout or sleep."},
            "limit": {"type": "integer", "minimum": 1,
                      "maximum": SESSIONS_MAX,
                      "description": "Most sessions to return. Default "
                                     "{}.".format(SESSIONS_DEFAULT)},
        }),
    ToolSpec(
        "get_session", "One session in detail",
        "One session in detail: its bounds; count, mean, min and max of "
        "every metric its source recorded while it ran; minutes in each "
        "sleep stage when it is a sleep; and a bucketed heart-rate trace.",
        {
            "session_id": {"type": "integer",
                           "description": "An id from list_sessions."},
            "max_points": {"type": "integer", "minimum": 10, "maximum": 1000,
                           "description": "Most heart-rate points. Default 120."},
        },
        ("session_id",)),
    ToolSpec(
        "get_sleep", "Sleep by night",
        "Sleep in a date range, one row per sleep period per source: local "
        "bedtime and wake time (HH:MM), time in bed, time asleep, "
        "efficiency, minutes of deep, light, REM and awake, the vendor's own "
        "sleep total when it reports one, and heart rate and HRV while "
        "asleep. Nights are labelled with the date of waking; 'main' marks "
        "each night's longest sleep, and the per-source summary covers "
        "those.",
        {
            "start": _when("First night (by wake date)", "13 days before end"),
            "end": _when("Last night (by wake date)", "today"),
            "source_id": _SOURCE_ID,
        }),
    ToolSpec(
        "query_sql", "Read-only SQL",
        "Run one read-only SQL SELECT against the Ticker SQLite database, for "
        "questions the other tools don't answer: correlations, custom "
        "groupings, joins. Writes, ATTACH and state-changing PRAGMAs are "
        "refused; a query stops after {:g} s; at most max_rows rows come "
        "back.\n\n"
        "Schema:\n"
        "  observations(id, source_id, metric_id, session_id, ts, end_ts, "
        "value REAL, text_value, external_id, ingested_at)\n"
        "    ts, end_ts: ISO 8601 UTC text such as "
        "'2026-09-01T06:30:00.000+00:00'; compare as strings. end_ts is "
        "NULL for point readings.\n"
        "    text_value: the stage for sleep_stage rows (deep, light, rem, "
        "awake, asleep); value holds seconds or a vendor code there.\n"
        "  metrics(id, name, unit, value_kind, description)  -- value_kind: "
        "instant | cumulative | interval | categorical\n"
        "  sources(id, kind, vendor, display_name, enabled)  -- kind: stream "
        "(live device) | pull (cloud API) | import (file)\n"
        "  sessions(id, source_id, device_id, start_ts, end_ts, kind, label)"
        "  -- kind: manual | workout | sleep\n"
        "  devices(id, source_id, name, address, model)\n"
        "  rollups_daily(metric_id, day, n, sum_value, min_value, max_value, "
        "avg_value, p50_value)  -- day is the LOCAL date 'YYYY-MM-DD', all "
        "sources combined\n"
        "  sync_state(source_id, metric_id, watermark_ts, last_attempt, "
        "last_success, last_error)\n\n"
        "Indexed: observations(metric_id, ts) and (source_id, metric_id, ts), "
        "so filter on metric_id and a ts range. Results are exactly as "
        "stored: times in UTC, unlike the other tools.".format(SQL_BUDGET_SEC),
        {
            "sql": {"type": "string", "description": "One SELECT statement."},
            "max_rows": {"type": "integer", "minimum": 1,
                         "maximum": SQL_MAX_ROWS,
                         "description": "Most rows to return. Default "
                                        "{}.".format(SQL_DEFAULT_ROWS)},
        },
        ("sql",)),
)


# -- the compact profile ------------------------------------------------------
#
# What a small local model is shown. The full definitions are written for a
# frontier model and cost a few thousand tokens before a question is asked --
# a real share of a 8k context. Compact keeps the tools that answer most
# questions, in a sentence each, and drops query_sql: correct SQL against an
# unfamiliar schema is where small models fail most, and its schema text is
# the single largest definition. Calls also default to smaller answers.

PROFILES = ("full", "compact")

COMPACT_TOOLS = {
    "get_overview": "Start here: connected sources, which metrics exist with "
                    "units and dates, and how fresh the data is.",
    "get_daily_summary": "Per-day (or week/month) values of up to 8 metrics over "
                         "a date range, with typical, lowest and highest day.",
    "get_sleep": "Sleep per night: bedtime, wake, minutes asleep, stages, "
                 "efficiency, heart rate while asleep.",
    "get_timeseries": "Readings of one metric through a time window, bucketed.",
    "list_sessions": "Workouts, recordings and sleeps in a time window, newest first.",
    "get_session": "One session in detail, by its id from list_sessions.",
}

_COMPACT_PARAMS = {
    "start": "e.g. -7d, 2026-09-01, yesterday",
    "end": "default: now",
    "metrics": "metric names from get_overview",
    "metric": "a metric name from get_overview",
    "bucket": "e.g. 5m, 1h, 1d",
    "source_id": "optional",
    "kind": "manual, workout or sleep",
    "session_id": "an id from list_sessions",
}

COMPACT_INSTRUCTIONS = """\
Ticker holds the user's own health data (heart rate, HRV, sleep, steps, \
SpO2, weight and more). Call get_overview first to see what exists. Times \
are local. Say when data is thin, and don't present it as medical advice."""


def _compact_property(name: str, prop: dict) -> dict:
    slim = {key: value for key, value in prop.items() if key != "description"}
    if name in _COMPACT_PARAMS:
        slim["description"] = _COMPACT_PARAMS[name]
    return slim


# -- argument helpers -------------------------------------------------------

def _check_arguments(spec: ToolSpec, args: Dict[str, Any]) -> None:
    unknown = sorted(set(args) - set(spec.properties))
    if unknown:
        raise ToolError("{} does not take {}; its arguments are: {}".format(
            spec.name, ", ".join(unknown),
            ", ".join(spec.properties) or "none"))
    missing = [name for name in spec.required
               if args.get(name) is None or args.get(name) in ("", [])]
    if missing:
        raise ToolError("{} needs {}".format(spec.name, ", ".join(missing)))


def _int(args: Dict[str, Any], name: str, default: Optional[int],
         low: int, high: int) -> Optional[int]:
    raw = args.get(name)
    if raw is None:
        return default
    if isinstance(raw, str) and re.fullmatch(r"\s*-?\d+\s*", raw):
        raw = int(raw)
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ToolError("{} must be a whole number".format(name))
    if not low <= raw <= high:
        raise ToolError("{} must be between {} and {}".format(name, low, high))
    return raw


def _str(args: Dict[str, Any], name: str, default: Optional[str] = None,
         choices: Optional[Tuple[str, ...]] = None) -> Optional[str]:
    raw = args.get(name)
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise ToolError("{} must be a string".format(name))
    raw = raw.strip()
    if not raw:
        return default
    if choices is not None:
        if raw.lower() not in choices:
            raise ToolError("{} must be one of: {}".format(
                name, ", ".join(choices)))
        return raw.lower()
    return raw


def _num(value) -> Any:
    """A value rounded for reading: whole numbers as ints, the rest to two
    decimals (one past 100). Precision past that is noise from averaging,
    and costs tokens in every row."""
    if value is None:
        return None
    value = float(value)
    if value.is_integer():
        return int(value)
    return round(value, 1) if abs(value) >= 100 else round(value, 2)


def _minutes(seconds: Optional[float]) -> Optional[int]:
    return None if seconds is None else int(round(seconds / 60.0))


def _mean(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def zone_name(zone: tzinfo) -> str:
    return getattr(zone, "key", None) or str(zone)


# -- the tools --------------------------------------------------------------

class Tools:
    """The tool implementations over a read-only database.

    `db` needs a `session(budget_sec)` context manager yielding a
    connection -- ticker.mcp.readonly.ReadOnlyDatabase in real use. `zone`
    and `now` are injectable so tests can pin both.
    """

    def __init__(self, db, zone: Optional[tzinfo] = None,
                 now: Callable[[], datetime] = now_utc,
                 sql_budget_sec: float = SQL_BUDGET_SEC,
                 profile: str = "full"):
        if profile not in PROFILES:
            raise ValueError("profile must be one of: {}".format(", ".join(PROFILES)))
        self.db = db
        self.zone = zone or tconfig.local_zone()
        self.now = now
        self.sql_budget_sec = sql_budget_sec
        self.profile = profile
        self.compact = profile == "compact"
        self._specs = {spec.name: spec for spec in SPECS
                       if not self.compact or spec.name in COMPACT_TOOLS}
        # Defaults for what a call leaves unsaid. Compact answers are sized
        # for a small model's context; the full ones for a large model's.
        self.default_points = 60 if self.compact else MAX_POINTS_DEFAULT
        self.default_trace_points = 40 if self.compact else 120
        self.default_sessions = 10 if self.compact else SESSIONS_DEFAULT
        self.default_nights = 7 if self.compact else 14
        self.auto_day_rows = 31 if self.compact else 92
        self.auto_week_rows = 365 if self.compact else 730

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def definitions(self) -> List[dict]:
        return [spec.definition(self.compact) for spec in SPECS
                if spec.name in self._specs]

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        spec = self._specs.get(name)
        if spec is None:
            raise ToolError("unknown tool: {}".format(name))
        _check_arguments(spec, arguments)
        handler = getattr(self, "_" + name)
        budget = self.sql_budget_sec if name == "query_sql" else None
        try:
            with self.db.session(budget) as conn:
                return handler(conn, arguments)
        except (DatabaseUnavailable, QueryTimeout) as exc:
            raise ToolError(str(exc))

    # -- time ------------------------------------------------------------

    def _today(self) -> date:
        return self.now().astimezone(self.zone).date()

    def _midnight(self, day: date) -> datetime:
        return datetime.combine(day, time(0), tzinfo=self.zone).astimezone(
            timezone.utc)

    def _instant(self, args: Dict[str, Any], name: str,
                 default: datetime) -> datetime:
        """An argument as an aware UTC instant. Naive means local."""
        raw = args.get(name)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return default
        if not isinstance(raw, str):
            raise ToolError("{} must be a string: {}".format(name, TIME_FORMS))
        text = raw.strip()
        lowered = text.lower()
        if lowered == "now":
            return self.now()
        if lowered in ("today", "yesterday"):
            day = self._today() - timedelta(days=lowered == "yesterday")
            return self._midnight(day)
        match = _RELATIVE.match(lowered)
        if match:
            seconds = float(match.group(1)) * _RELATIVE_UNITS[match.group(2)]
            return self.now() - timedelta(seconds=seconds)
        try:
            parsed = datetime.fromisoformat(
                text.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            raise ToolError("could not read {}={!r}: use {}".format(
                name, raw, TIME_FORMS))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.zone)
        return parsed.astimezone(timezone.utc)

    def _day(self, args: Dict[str, Any], name: str, default: date) -> date:
        """An argument as a local calendar day."""
        if args.get(name) is None or args.get(name) == "":
            return default
        instant = self._instant(args, name, self._midnight(default))
        return instant.astimezone(self.zone).date()

    def _local(self, value) -> Optional[str]:
        if value is None:
            return None
        instant = parse_iso(value) if isinstance(value, str) else value
        return instant.astimezone(self.zone).isoformat(timespec="seconds")

    def _clock(self, instant: datetime) -> str:
        return instant.astimezone(self.zone).strftime("%H:%M")

    # -- lookups ---------------------------------------------------------

    @staticmethod
    def _metric(conn: sqlite3.Connection, name: Optional[str]) -> dict:
        row = conn.execute(
            "SELECT id, name, unit, value_kind FROM metrics WHERE name = ?",
            (name,)).fetchone()
        if row is None:
            known = [n for (n,) in conn.execute(
                "SELECT name FROM metrics ORDER BY name")]
            raise ToolError("no metric named {!r}. Known metrics: {}".format(
                name, ", ".join(known)))
        return {"id": row[0], "name": row[1], "unit": row[2], "kind": row[3]}

    @staticmethod
    def _source_names(conn: sqlite3.Connection) -> Dict[int, str]:
        return {sid: name for sid, name in conn.execute(
            "SELECT id, display_name FROM sources ORDER BY id")}

    @staticmethod
    def _source_arg(conn: sqlite3.Connection,
                    args: Dict[str, Any]) -> Optional[int]:
        source_id = _int(args, "source_id", None, 1, 2 ** 62)
        if source_id is not None and conn.execute(
                "SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone() is None:
            raise ToolError("no source {}; get_overview lists the sources "
                            "and their ids".format(source_id))
        return source_id

    @staticmethod
    def _has_data(conn: sqlite3.Connection, metric_id: int, lo: str, hi: str,
                  source_id: Optional[int] = None) -> bool:
        # One index seek either way: (metric_id, ts), or ux_obs_natural's
        # (source_id, metric_id, ts) when a source is named.
        sql = ("SELECT 1 FROM observations WHERE metric_id = ? "
               "AND ts >= ? AND ts < ?")
        params: List[Any] = [metric_id, lo, hi]
        if source_id is not None:
            sql += " AND source_id = ?"
            params.append(source_id)
        return conn.execute(sql + " LIMIT 1", params).fetchone() is not None

    @staticmethod
    def _window_stats(conn: sqlite3.Connection, metric_id: int,
                      lo: str, hi: str, source_id: Optional[int] = None
                      ) -> Tuple[int, Any, Any, Any, Any]:
        """(n, avg, min, max, sum) of one metric over [lo, hi)."""
        sql = ("SELECT COUNT(*), AVG(value), MIN(value), MAX(value), "
               "SUM(value) FROM observations WHERE metric_id = ? "
               "AND ts >= ? AND ts < ?")
        params: List[Any] = [metric_id, lo, hi]
        if source_id is not None:
            sql += " AND source_id = ?"
            params.append(source_id)
        return conn.execute(sql, params).fetchone()

    # -- get_overview ----------------------------------------------------

    def _get_overview(self, conn: sqlite3.Connection,
                      args: Dict[str, Any]) -> Dict[str, Any]:
        now = self.now()
        metrics = conn.execute(
            "SELECT id, name, unit, value_kind, description FROM metrics "
            "ORDER BY name").fetchall()
        sources = conn.execute(
            "SELECT id, kind, vendor, display_name, enabled FROM sources "
            "ORDER BY id").fetchall()
        rolled = {mid: n for mid, n in conn.execute(
            "SELECT metric_id, COUNT(*) FROM rollups_daily GROUP BY metric_id")}
        notes: List[str] = []

        metric_rows, empty, unrolled = [], [], []
        for mid, name, unit, kind, description in metrics:
            # Two seeks on (metric_id, ts); MIN and MAX in one statement
            # would defeat SQLite's min/max optimisation and scan instead.
            first = conn.execute(
                "SELECT ts FROM observations WHERE metric_id = ? "
                "ORDER BY ts LIMIT 1", (mid,)).fetchone()
            if first is None:
                empty.append(name)
                continue
            last = conn.execute(
                "SELECT ts FROM observations WHERE metric_id = ? "
                "ORDER BY ts DESC LIMIT 1", (mid,)).fetchone()
            reporting = [
                display for sid, _kind, _vendor, display, _on in sources
                if conn.execute(
                    "SELECT 1 FROM observations WHERE source_id = ? AND "
                    "metric_id = ? LIMIT 1", (sid, mid)).fetchone()]
            if mid not in rolled:
                unrolled.append(name)
            metric_rows.append({
                "metric": name, "unit": unit, "kind": kind,
                "description": description,
                "first": self._local(first[0]), "last": self._local(last[0]),
                "days_with_data": rolled.get(mid), "sources": reporting,
            })
        if unrolled:
            notes.append(
                "No daily rollups yet for {}: get_daily_summary computes those "
                "days from raw readings instead, which is slower over long "
                "ranges. `ticker rollup --all` builds them.".format(
                    ", ".join(unrolled)))

        metric_ids = [row[0] for row in metrics]
        source_rows = []
        for sid, kind, vendor, display, enabled in sources:
            last = queries.last_observation(conn, sid, metric_ids)
            entry: Dict[str, Any] = {
                "id": sid, "name": display, "vendor": vendor,
                "type": _SOURCE_TYPES.get(kind, kind),
                "enabled": bool(enabled), "last_data": self._local(last),
            }
            devices = [name or model for name, model in conn.execute(
                "SELECT name, model FROM devices WHERE source_id = ? "
                "ORDER BY id", (sid,)) if name or model]
            if devices:
                entry["devices"] = devices
            if kind == "pull":
                state = queries.sync_state(conn, sid)
                successes = [s["last_success"] for s in state.values()
                             if s["last_success"]]
                entry["last_sync"] = self._local(max(successes)) if successes else None
                errors = {metric: s["last_error"] for metric, s in state.items()
                          if s["last_error"]}
                if errors:
                    entry["sync_errors"] = errors
                    notes.append("{}'s last sync failed: {}".format(
                        display, next(iter(errors.values()))))
                if enabled and last and now - parse_iso(last) > STALE_PULL:
                    notes.append(
                        "{} has delivered nothing since {}; if that's "
                        "unexpected, check the account on Ticker's page, and "
                        "that Ticker is running (`ticker status`).".format(
                            display, self._local(last)))
            source_rows.append(entry)

        sessions = {
            (kind or "unknown"): {"count": count, "latest": self._local(latest)}
            for kind, count, latest in conn.execute(
                "SELECT kind, COUNT(*), MAX(start_ts) FROM sessions "
                "GROUP BY kind ORDER BY kind")}

        if not metric_rows:
            notes.append(
                "No data yet. Sources are connected on Ticker's page, not "
                "through an agent: start `ticker`, then use Connect for Oura, "
                "Fitbit or an Apple Health export, or turn on a strap or "
                "watch under Live heart rate.")
        result = {
            "now": self._local(now),
            "timezone": zone_name(self.zone),
            "sources": source_rows,
            "metrics": metric_rows,
            "sessions": sessions,
            "notes": notes,
        }
        if self.compact:
            # A small model's context is better spent on data than on prose.
            for row in metric_rows:
                row.pop("description", None)
        else:
            result["metrics_without_data"] = empty
        return result

    # -- get_daily_summary -----------------------------------------------

    def _get_daily_summary(self, conn: sqlite3.Connection,
                           args: Dict[str, Any]) -> Dict[str, Any]:
        names = args.get("metrics")
        if isinstance(names, str):
            # A model will sometimes send "steps,heart_rate_bpm"; there is
            # only one thing it can have meant.
            names = [part for part in names.split(",")]
        if (not isinstance(names, list)
                or not all(isinstance(n, str) and n.strip() for n in names)):
            raise ToolError('metrics must be a list of metric names, such as '
                            '["heart_rate_bpm", "steps"]')
        if len(names) > 8:
            raise ToolError("at most 8 metrics per call")

        today = self._today()
        until = self._day(args, "end", today)
        since = self._day(args, "start", until - timedelta(days=29))
        if since > until:
            raise ToolError("start ({}) is after end ({})".format(since, until))
        span = (until - since).days + 1
        if span > MAX_DAYS:
            raise ToolError("{} days is more than the {} allowed per "
                            "call".format(span, MAX_DAYS))
        group = _str(args, "group_by", "auto",
                     choices=("auto", "day", "week", "month"))
        if group == "auto":
            group = ("day" if span <= self.auto_day_rows else
                     "week" if span <= self.auto_week_rows else "month")
        if group == "day" and span > MAX_DAY_ROWS:
            raise ToolError("{} days is too many to list one by one; use "
                            "group_by 'week' or 'month'".format(span))
        source_id = self._source_arg(conn, args)

        days = [since + timedelta(days=i) for i in range(span)]
        lo = queries.day_bounds(since.isoformat(), self.zone)[0]
        hi = queries.day_bounds(until.isoformat(), self.zone)[1]
        sources = self._source_names(conn)
        results = [self._daily_metric(conn, name.strip(), days, lo, hi, group,
                                      source_id, sources, today)
                   for name in names]
        return {"from": since.isoformat(), "to": until.isoformat(),
                "group_by": group, "timezone": zone_name(self.zone),
                "results": results}

    def _daily_metric(self, conn, name, days, lo, hi, group, source_id,
                      sources, today) -> Dict[str, Any]:
        metric = self._metric(conn, name)
        out: Dict[str, Any] = {"metric": name, "unit": metric["unit"],
                               "kind": metric["kind"]}
        if metric["kind"] == "categorical":
            out["note"] = ("{} is categorical and has no daily value; "
                           "get_sleep breaks sleep down by stage".format(name))
            return out
        total = metric["kind"] != "instant"
        out["daily_value"] = "total" if total else "average"
        notes: List[str] = []

        if source_id is not None:
            series = [(source_id, sources[source_id],
                       self._live_days(conn, metric["id"], days, source_id))]
        else:
            reporting = [(sid, label) for sid, label in sources.items()
                         if self._has_data(conn, metric["id"], lo, hi, sid)]
            if total and len(reporting) > 1:
                series = [(sid, label,
                           self._live_days(conn, metric["id"], days, sid))
                          for sid, label in reporting]
                notes.append(
                    "{} sources report {}; each is listed separately because "
                    "adding them would count the same activity twice.".format(
                        len(reporting), name))
            else:
                values, note = self._combined_days(conn, metric["id"], days,
                                                   lo, today)
                if note:
                    notes.append(note)
                if len(reporting) == 1:
                    series = [(reporting[0][0], reporting[0][1], values)]
                else:
                    series = [(None, "all sources", values)]
        if name == "sleep_duration_s":
            notes.append("sleep_duration_s counts each sleep toward the day "
                         "it began; get_sleep groups sleep by night.")

        out["series"] = [self._shape_days(values, group, total, sid, label,
                                          len(days))
                         for sid, label, values in series]
        if notes:
            out["notes"] = notes
        return out

    def _live_days(self, conn, metric_id: int, days: List[date],
                   source_id: Optional[int] = None) -> Dict[str, tuple]:
        """day -> (n, avg, min, max, sum), computed from raw readings."""
        out = {}
        for day in days:
            lo, hi = queries.day_bounds(day.isoformat(), self.zone)
            row = self._window_stats(conn, metric_id, lo, hi, source_id)
            if row[0]:
                out[day.isoformat()] = row
        return out

    def _combined_days(self, conn, metric_id: int, days: List[date], lo: str,
                       today: date) -> Tuple[Dict[str, tuple], Optional[str]]:
        """Every source together: rollups for settled days, raw for recent.

        Rollups are rebuilt when a session ends or a sync finishes, so
        today's -- and yesterday's, until the overnight syncs land -- can
        lag. Those two are always computed live; they're cheap, and they are
        what 'how am I doing today' is asking about.
        """
        live_from = today - timedelta(days=1)
        settled = [d for d in days if d < live_from]
        recent = [d for d in days if d >= live_from]
        values: Dict[str, tuple] = {}
        note = None
        if settled:
            for day, n, avg, low, high, total in conn.execute(
                    "SELECT day, n, avg_value, min_value, max_value, sum_value "
                    "FROM rollups_daily WHERE metric_id = ? AND day >= ? "
                    "AND day <= ?", (metric_id, settled[0].isoformat(),
                                     settled[-1].isoformat())):
                values[day] = (n, avg, low, high, total)
            settled_hi = queries.day_bounds(settled[-1].isoformat(), self.zone)[1]
            if not values and self._has_data(conn, metric_id, lo, settled_hi):
                if len(settled) <= LIVE_FALLBACK_DAYS:
                    values.update(self._live_days(conn, metric_id, settled))
                    note = ("Daily rollups haven't been built for this range, "
                            "so days were computed from raw readings; "
                            "`ticker rollup --all` builds them.")
                else:
                    note = ("Daily rollups haven't been built for this range "
                            "and it is too long to compute live; run "
                            "`ticker rollup --all`, or ask for a shorter "
                            "range.")
        values.update(self._live_days(conn, metric_id, recent))
        return values, note

    @staticmethod
    def _shape_days(values: Dict[str, tuple], group: str, total: bool,
                    source_id: Optional[int], label: str,
                    span: int) -> Dict[str, Any]:
        daily = sorted(values.items())

        def headline(v):
            return v[4] if total else v[1]

        series: Dict[str, Any] = {"source": label}
        if source_id is not None:
            series["source_id"] = source_id
        if group == "day":
            if total:
                series["columns"] = ["day", "total", "n"]
                series["rows"] = [[d, _num(v[4]), v[0]] for d, v in daily]
            else:
                series["columns"] = ["day", "avg", "min", "max", "n"]
                series["rows"] = [[d, _num(v[1]), _num(v[2]), _num(v[3]), v[0]]
                                  for d, v in daily]
        else:
            buckets: Dict[str, List[tuple]] = {}
            for d, v in daily:
                day = date.fromisoformat(d)
                key = ((day - timedelta(days=day.weekday())).isoformat()
                       if group == "week" else d[:7])
                buckets.setdefault(key, []).append(v)
            period = "week_of" if group == "week" else "month"
            if total:
                series["columns"] = [period, "per_day", "total", "days"]
                series["rows"] = [
                    [k, _num(_mean([v[4] for v in vs])),
                     _num(sum(v[4] for v in vs)), len(vs)]
                    for k, vs in buckets.items()]
            else:
                series["columns"] = [period, "avg", "min", "max", "days"]
                series["rows"] = [
                    [k, _num(_mean([v[1] for v in vs])),
                     _num(min(v[2] for v in vs)), _num(max(v[3] for v in vs)),
                     len(vs)]
                    for k, vs in buckets.items()]

        summary: Dict[str, Any] = {"days_with_data": len(daily),
                                   "days_in_range": span}
        if daily:
            points = [(d, headline(v)) for d, v in daily]
            lowest = min(points, key=lambda p: p[1])
            highest = max(points, key=lambda p: p[1])
            summary.update({
                "mean_per_day": _num(_mean([p[1] for p in points])),
                "lowest": {"day": lowest[0], "value": _num(lowest[1])},
                "highest": {"day": highest[0], "value": _num(highest[1])},
                "latest": {"day": points[-1][0], "value": _num(points[-1][1])},
            })
        series["summary"] = summary
        return series

    # -- get_timeseries --------------------------------------------------

    def _get_timeseries(self, conn: sqlite3.Connection,
                        args: Dict[str, Any]) -> Dict[str, Any]:
        metric = self._metric(conn, _str(args, "metric"))
        until = self._instant(args, "end", self.now())
        since = self._instant(args, "start", until - timedelta(hours=24))
        if since >= until:
            raise ToolError("start ({}) is not before end ({})".format(
                self._local(since), self._local(until)))
        if until - since > timedelta(days=MAX_DAYS):
            raise ToolError("windows over {} days aren't served; use "
                            "get_daily_summary".format(MAX_DAYS))
        max_points = _int(args, "max_points", self.default_points, 10,
                          MAX_POINTS_LIMIT)
        source_id = self._source_arg(conn, args)
        out: Dict[str, Any] = {
            "metric": metric["name"], "unit": metric["unit"],
            "kind": metric["kind"], "from": self._local(since),
            "to": self._local(until),
            "source": (self._source_names(conn)[source_id]
                       if source_id is not None else "all sources"),
        }
        out.update(self._series(conn, metric, since, until, source_id,
                                _str(args, "bucket"), max_points))
        return out

    def _series(self, conn, metric: dict, since: datetime, until: datetime,
                source_id: Optional[int], bucket: Optional[str],
                max_points: int) -> Dict[str, Any]:
        """Window stats plus at most max_points points of one metric."""
        lo, hi = iso_utc(since), iso_utc(until)
        n, avg, low, high, total = self._window_stats(conn, metric["id"], lo,
                                                      hi, source_id)
        accumulating = metric["kind"] in ("cumulative", "interval")
        stats: Dict[str, Any] = {"n": n}

        if metric["kind"] == "categorical":
            # Averaging stage codes means nothing; return the segments.
            sql = ("SELECT ts, end_ts, text_value FROM observations "
                   "WHERE metric_id = ? AND ts >= ? AND ts < ?")
            params: List[Any] = [metric["id"], lo, hi]
            if source_id is not None:
                sql += " AND source_id = ?"
                params.append(source_id)
            rows = conn.execute(sql + " ORDER BY ts LIMIT ?",
                                params + [max_points + 1]).fetchall()
            result = {"bucket": "raw", "stats": stats,
                      "columns": ["start", "end", "value"],
                      "rows": [[self._local(ts), self._local(end), text]
                               for ts, end, text in rows[:max_points]]}
            if len(rows) > max_points:
                result["truncated"] = True
                result["notes"] = ["More segments than max_points; get_sleep "
                                   "summarises sleep stages by night."]
            return result

        if n:
            stats.update({"mean": _num(avg), "min": _num(low), "max": _num(high)})
            if accumulating:
                stats["total"] = _num(total)
        else:
            return {"bucket": bucket or "raw", "stats": stats, "columns": [],
                    "rows": []}

        if bucket is None:
            bucket = ("raw" if n <= max_points else
                      _auto_bucket((until - since).total_seconds(), max_points))
        elif bucket.lower() == "raw" and n > max_points:
            raise ToolError(
                "{} readings in this window, more than max_points={}; omit "
                "bucket to have one chosen, or narrow the window".format(
                    n, max_points))
        try:
            points = queries.series(conn, metric["name"], since, until, bucket,
                                    source_id)
        except ValueError as exc:
            raise ToolError("{}; use a width like 30s, 5m, 1h or 1d, or "
                            "'raw'".format(exc))
        if len(points) > max_points:
            raise ToolError(
                "bucket {} gives {} points, more than max_points={}; use a "
                "wider bucket, omit bucket, or raise max_points (up to "
                "{})".format(bucket, len(points), max_points, MAX_POINTS_LIMIT))

        label = bucket.strip().lower()
        if label == "raw":
            return {"bucket": "raw", "stats": stats, "columns": ["time", "value"],
                    "rows": [[self._local(p["ts"]), _num(p["value"])]
                             for p in points]}
        if label in queries.DAY_BUCKETS:
            def when(p):
                return p["ts"]                 # already a local day
        else:
            def when(p):
                return self._local(p["ts"])
        if accumulating:
            columns = ["time", "total", "n"]
            rows = [[when(p), _num(p["sum"]), p["n"]] for p in points]
        else:
            columns = ["time", "avg", "min", "max", "n"]
            rows = [[when(p), _num(p["value"]), _num(p["min"]), _num(p["max"]),
                     p["n"]] for p in points]
        return {"bucket": label, "stats": stats, "columns": columns, "rows": rows}

    # -- sessions --------------------------------------------------------

    def _list_sessions(self, conn: sqlite3.Connection,
                       args: Dict[str, Any]) -> Dict[str, Any]:
        until = self._instant(args, "end", self.now())
        since = self._instant(args, "start", until - timedelta(days=30))
        if since > until:
            raise ToolError("start is after end")
        kind = _str(args, "kind")
        limit = _int(args, "limit", self.default_sessions, 1, SESSIONS_MAX)

        where = ["s.start_ts < ?", "(s.end_ts IS NULL OR s.end_ts >= ?)"]
        params: List[Any] = [iso_utc(until), iso_utc(since)]
        if kind:
            where.append("s.kind = ?")
            params.append(kind.lower())
        clause = " AND ".join(where)
        (total,) = conn.execute("SELECT COUNT(*) FROM sessions s WHERE " + clause,
                                params).fetchone()
        now = self.now()
        rows = []
        for sid, k, label, source, device, start_ts, end_ts, n in conn.execute(
                "SELECT s.id, s.kind, s.label, src.display_name, d.name, "
                "       s.start_ts, s.end_ts, "
                "       (SELECT COUNT(*) FROM observations o "
                "        WHERE o.session_id = s.id) "
                "FROM sessions s JOIN sources src ON src.id = s.source_id "
                "LEFT JOIN devices d ON d.id = s.device_id "
                "WHERE " + clause + " ORDER BY s.start_ts DESC LIMIT ?",
                params + [limit]):
            start = parse_iso(start_ts)
            end = parse_iso(end_ts) if end_ts else now
            rows.append([sid, k, label, source, device, self._local(start),
                         self._local(end_ts),
                         _num((end - start).total_seconds() / 60.0), n])
        out: Dict[str, Any] = {
            "from": self._local(since), "to": self._local(until),
            "total": total,
            "columns": ["id", "kind", "label", "source", "device", "start",
                        "end", "duration_min", "readings"],
            "rows": rows,
        }
        if total > len(rows):
            out["note"] = ("showing the newest {} of {}; narrow the window or "
                           "raise limit".format(len(rows), total))
        return out

    def _get_session(self, conn: sqlite3.Connection,
                     args: Dict[str, Any]) -> Dict[str, Any]:
        session_id = _int(args, "session_id", None, 1, 2 ** 62)
        max_points = _int(args, "max_points", self.default_trace_points, 10, 1000)
        row = conn.execute(
            "SELECT s.source_id, s.kind, s.label, s.start_ts, s.end_ts, "
            "       src.display_name, d.name, d.model, "
            "       (SELECT COUNT(*) FROM observations o "
            "        WHERE o.session_id = s.id) "
            "FROM sessions s JOIN sources src ON src.id = s.source_id "
            "LEFT JOIN devices d ON d.id = s.device_id WHERE s.id = ?",
            (session_id,)).fetchone()
        if row is None:
            raise ToolError("no session {}; list_sessions shows the "
                            "ids".format(session_id))
        source_id, kind, label, start_ts, end_ts, source, device, model, attached = row
        start = parse_iso(start_ts)
        end = parse_iso(end_ts) if end_ts else self.now()
        lo, hi = iso_utc(start), iso_utc(end)

        # Everything the session's own source recorded while it ran, not
        # just rows tagged with it: a vendor's sleep carries its stages,
        # but the heart rate during it arrives from a separate endpoint and
        # is tagged with nothing.
        metrics, stages = [], None
        for mid, name, unit, value_kind in conn.execute(
                "SELECT id, name, unit, value_kind FROM metrics "
                "ORDER BY name").fetchall():
            if value_kind == "categorical":
                found = conn.execute(
                    "SELECT text_value, SUM(julianday(COALESCE(end_ts, ts)) "
                    "                       - julianday(ts)) * 1440.0 "
                    "FROM observations WHERE source_id = ? AND metric_id = ? "
                    "AND ts >= ? AND ts < ? GROUP BY text_value "
                    "ORDER BY text_value", (source_id, mid, lo, hi)).fetchall()
                if found and name == "sleep_stage":
                    stages = {text or "unknown": int(round(minutes or 0))
                              for text, minutes in found}
                continue
            n, avg, low, high, total = self._window_stats(conn, mid, lo, hi,
                                                          source_id)
            if not n:
                continue
            entry = {"metric": name, "unit": unit, "n": n, "mean": _num(avg),
                     "min": _num(low), "max": _num(high)}
            if value_kind != "instant":
                entry["total"] = _num(total)
            metrics.append(entry)

        result: Dict[str, Any] = {
            "session": {
                "id": session_id, "kind": kind, "label": label,
                "source": source, "device": device or model,
                "start": self._local(start), "end": self._local(end_ts),
                "in_progress": end_ts is None,
                "duration_min": _num((end - start).total_seconds() / 60.0),
                "readings_attached": attached,
            },
            "metrics": metrics,
        }
        if stages:
            result["sleep_stages_min"] = stages
        trace = self._series(conn, self._metric(conn, "heart_rate_bpm"), start,
                             end, source_id, None, max_points)
        if trace["stats"]["n"]:
            result["heart_rate"] = {key: trace[key]
                                    for key in ("bucket", "columns", "rows")}
        result["notes"] = ["Statistics cover everything {} recorded between "
                           "the session's start and {}.".format(
                               source, "end" if end_ts else "now")]
        return result

    # -- get_sleep -------------------------------------------------------

    def _get_sleep(self, conn: sqlite3.Connection,
                   args: Dict[str, Any]) -> Dict[str, Any]:
        today = self._today()
        until = self._day(args, "end", today)
        since = self._day(args, "start",
                          until - timedelta(days=self.default_nights - 1))
        if since > until:
            raise ToolError("start ({}) is after end ({})".format(since, until))
        if (until - since).days + 1 > MAX_SLEEP_DAYS:
            raise ToolError("at most {} nights per call".format(MAX_SLEEP_DAYS))
        source_id = self._source_arg(conn, args)
        names = self._source_names(conn)
        stage = self._metric(conn, "sleep_stage")
        duration = self._metric(conn, "sleep_duration_s")
        hr = self._metric(conn, "heart_rate_bpm")
        hrv = self._metric(conn, "hrv_rmssd_ms")

        # A night labelled with its wake date began the evening before --
        # or earlier still, for a long one.
        lo = iso_utc(self._midnight(since) - timedelta(hours=18))
        hi = queries.day_bounds(until.isoformat(), self.zone)[1]
        where = "metric_id = ? AND ts >= ? AND ts < ?"
        extra: List[Any] = []
        if source_id is not None:
            where += " AND source_id = ?"
            extra = [source_id]
        segments = [
            (sid, parse_iso(ts), parse_iso(end) if end else parse_iso(ts),
             text or "asleep")
            for sid, ts, end, text in conn.execute(
                "SELECT source_id, ts, end_ts, text_value FROM observations "
                "WHERE " + where + " ORDER BY source_id, ts",
                [stage["id"], lo, hi] + extra)]
        reported = []
        for sid, ts, end, seconds in conn.execute(
                "SELECT source_id, ts, end_ts, value FROM observations "
                "WHERE " + where + " ORDER BY ts", [duration["id"], lo, hi] + extra):
            began = parse_iso(ts)
            reported.append((sid, began, parse_iso(end) if end else
                             began + timedelta(seconds=seconds), seconds))
        periods = sleeplib.attach_reported(sleeplib.group_segments(segments),
                                           reported)

        kept = []
        for period in periods:
            night = period.end.astimezone(self.zone).date()
            if (since <= night <= until
                    and period.asleep_s >= MIN_SLEEP.total_seconds()):
                kept.append((night, period))
        main: Dict[Tuple[date, int], sleeplib.Period] = {}
        for night, period in kept:
            key = (night, period.source_id)
            if key not in main or period.asleep_s > main[key].asleep_s:
                main[key] = period

        rows, by_source = [], {}
        for night, p in kept:
            span_lo, span_hi = iso_utc(p.start), iso_utc(p.end)
            _n, hr_avg, hr_min, _hr_max, _sum = self._window_stats(
                conn, hr["id"], span_lo, span_hi, p.source_id)
            hrv_avg = self._window_stats(conn, hrv["id"], span_lo, span_hi,
                                         p.source_id)[1]
            is_main = main[(night, p.source_id)] is p
            efficiency = (round(100.0 * p.asleep_s / p.in_bed_s, 1)
                          if p.staged and p.in_bed_s > 0 else None)
            rows.append([
                night.isoformat(), names.get(p.source_id),
                self._clock(p.start), self._clock(p.end),
                _minutes(p.in_bed_s), _minutes(p.asleep_s), efficiency,
                _stage_minutes(p, "deep"), _stage_minutes(p, "light"),
                _stage_minutes(p, "rem"), _stage_minutes(p, "awake"),
                _stage_minutes(p, "asleep"), _minutes(p.reported_s),
                _num(hr_avg), _num(hr_min), _num(hrv_avg), is_main,
            ])
            if is_main:
                by_source.setdefault(p.source_id, []).append((night, p, hr_avg))

        summary = []
        for sid, items in by_source.items():
            efficiencies = [100.0 * p.asleep_s / p.in_bed_s
                            for _, p, _ in items if p.staged and p.in_bed_s > 0]
            heart = [h for _, _, h in items if h is not None]
            shortest = min(items, key=lambda item: item[1].asleep_s)
            longest = max(items, key=lambda item: item[1].asleep_s)
            summary.append({
                "source": names.get(sid), "nights": len(items),
                "avg_asleep_min": _minutes(_mean([p.asleep_s for _, p, _ in items])),
                "avg_efficiency_pct": (round(_mean(efficiencies), 1)
                                       if efficiencies else None),
                "avg_hr": _num(_mean(heart)),
                "shortest": {"night": shortest[0].isoformat(),
                             "asleep_min": _minutes(shortest[1].asleep_s)},
                "longest": {"night": longest[0].isoformat(),
                            "asleep_min": _minutes(longest[1].asleep_s)},
            })

        notes = ["Times are local ({}); each night is labelled with the date "
                 "of waking.".format(zone_name(self.zone))]
        if len(by_source) > 1:
            notes.append("Nights appear once per source ({}); they measure "
                         "independently and won't agree exactly.".format(
                             ", ".join(names.get(s, "?") for s in by_source)))
        if not rows:
            notes.append("No sleep recorded between {} and {}. get_overview "
                         "shows which sources report sleep_stage or "
                         "sleep_duration_s.".format(since, until))
        return {"from": since.isoformat(), "to": until.isoformat(),
                "columns": SLEEP_COLUMNS, "rows": rows, "summary": summary,
                "notes": notes}

    # -- query_sql -------------------------------------------------------

    def _query_sql(self, conn: sqlite3.Connection,
                   args: Dict[str, Any]) -> Dict[str, Any]:
        sql = _str(args, "sql")
        if len(sql) > SQL_MAX_CHARS:
            raise ToolError("query is {} characters; the limit is {}".format(
                len(sql), SQL_MAX_CHARS))
        max_rows = _int(args, "max_rows", SQL_DEFAULT_ROWS, 1, SQL_MAX_ROWS)
        try:
            cursor = conn.execute(sql)
            columns = [column[0] for column in cursor.description or ()]
            rows = cursor.fetchmany(max_rows + 1) if columns else []
            cursor.close()
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc):
                raise                       # the time budget; readonly says so
            raise ToolError(_sql_message(exc))
        except (sqlite3.Error, sqlite3.Warning) as exc:
            # sqlite3.Warning is what Python < 3.12 raises for more than one
            # statement, and it is not a subclass of sqlite3.Error.
            raise ToolError(_sql_message(exc))
        truncated = len(rows) > max_rows
        result: Dict[str, Any] = {
            "columns": columns,
            "rows": [[_cell(value) for value in row] for row in rows[:max_rows]],
            "truncated": truncated,
        }
        if truncated:
            result["note"] = ("more than {} rows; aggregate, add a LIMIT, or "
                              "raise max_rows (up to {})".format(
                                  max_rows, SQL_MAX_ROWS))
        return result


def _stage_minutes(period: sleeplib.Period, stage: str) -> Optional[int]:
    """Minutes in one stage; None when the period has no stage breakdown,
    so 'not measured' never reads as 'zero'."""
    if not period.staged:
        return None
    return _minutes(period.stages.get(stage, 0.0))


def _auto_bucket(span_seconds: float, max_points: int) -> str:
    """The narrowest round bucket that keeps a window within max_points.

    One point of headroom: buckets align to the epoch rather than to the
    window, so a window can straddle one more bucket than it spans.
    """
    needed = span_seconds / max(1, max_points - 1)
    for width in _NICE_BUCKETS:
        if width >= needed:
            if width % 3600 == 0:
                return "{}h".format(width // 3600)
            if width % 60 == 0:
                return "{}m".format(width // 60)
            return "{}s".format(width)
    if needed <= 86400:
        return "1d"
    raise ToolError("this window needs buckets wider than a day to fit "
                    "max_points; use get_daily_summary with group_by 'week' "
                    "or 'month'")


def _cell(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        # raw_payloads.body is gzipped vendor JSON: bytes that mean nothing
        # to a model and could be megabytes of its context.
        return "<{} bytes>".format(len(value))
    return value


def _sql_message(exc: Exception) -> str:
    text = str(exc)
    if "not authorized" in text or "readonly" in text:
        return ("refused: this connection is read-only, so only SELECT (and "
                "WITH ... SELECT) statements run. Writes, ATTACH and "
                "state-changing PRAGMAs are blocked.")
    if "one statement" in text:
        return "send one statement at a time"
    return "SQL error: {}".format(text)
