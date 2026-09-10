"""
Read helpers and rollup computation.

Everything here takes a connection rather than opening one, so a caller can
run several of these inside one transaction and so tests can point them at a
temporary database without touching config.

Local-day bucketing
-------------------
Observations are stored in UTC but rollups_daily.day is a
local calendar day, and a local day is not a fixed 24 hours -- across a DST
transition it is 23 or 25. So the UTC window for a day is computed in Python
from the configured zone and handed to SQL as two bounds, rather than trying
to express a tz-aware date() in SQLite, which has no IANA database.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ticker import config as tconfig
from ticker.model import iso_utc, now_iso, parse_iso


def list_metrics(conn: sqlite3.Connection) -> List[dict]:
    return [
        {"id": mid, "name": name, "unit": unit, "value_kind": kind,
         "description": description}
        for mid, name, unit, kind, description in conn.execute(
            "SELECT id, name, unit, value_kind, description FROM metrics ORDER BY name")
    ]


def metric_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM metrics WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise KeyError("no such metric: {!r}".format(name))
    return row[0]


def observations(conn: sqlite3.Connection, metric: str, since: datetime,
                 until: datetime, source_id: Optional[int] = None
                 ) -> List[Tuple[str, float, Optional[str]]]:
    """Raw (ts, value, text_value) in [since, until), oldest first.

    Hits idx_obs_metric_ts directly. For anything spanning more than about a
    week the dashboard should be reading rollups_daily instead.
    """
    sql = ("SELECT ts, value, text_value FROM observations "
           "WHERE metric_id = ? AND ts >= ? AND ts < ?")
    params: List[object] = [metric_id(conn, metric), iso_utc(since), iso_utc(until)]
    if source_id is not None:
        sql += " AND source_id = ?"
        params.append(source_id)
    return conn.execute(sql + " ORDER BY ts", params).fetchall()


def list_sessions(conn: sqlite3.Connection, since: Optional[datetime] = None,
                  until: Optional[datetime] = None) -> List[dict]:
    """Sessions overlapping the window, newest first, with a sample count."""
    sql = [
        "SELECT s.id, s.start_ts, s.end_ts, s.kind, s.label, s.external_id,",
        "       src.vendor, src.display_name, d.name,",
        "       (SELECT COUNT(*) FROM observations o WHERE o.session_id = s.id)",
        "FROM sessions s",
        "JOIN sources src ON src.id = s.source_id",
        "LEFT JOIN devices d ON d.id = s.device_id",
    ]
    where, params = [], []
    if until is not None:
        where.append("s.start_ts < ?")
        params.append(iso_utc(until))
    if since is not None:
        # An open session (end_ts NULL) is still running, so it overlaps
        # any window that hasn't ended.
        where.append("(s.end_ts IS NULL OR s.end_ts >= ?)")
        params.append(iso_utc(since))
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append("ORDER BY s.start_ts DESC")
    rows = conn.execute("\n".join(sql), params).fetchall()
    return [
        {"id": r[0], "start_ts": r[1], "end_ts": r[2], "kind": r[3], "label": r[4],
         "external_id": r[5], "vendor": r[6], "source": r[7], "device": r[8],
         "n_observations": r[9]}
        for r in rows
    ]


# -- rollups -------------------------------------------------------------

def day_bounds(day: str, zone: Optional[tzinfo] = None) -> Tuple[str, str]:
    """UTC [start, end) covering one local calendar day.

    Built from local midnight to the next local midnight so a DST day is
    correctly 23 or 25 hours rather than an assumed 24.
    """
    zone = zone or tconfig.local_zone()
    d = date.fromisoformat(day)
    start = datetime.combine(d, time(0, 0), tzinfo=zone)
    end = datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=zone)
    return iso_utc(start), iso_utc(end)


def rebuild_days(conn: sqlite3.Connection,
                 dirty: Iterable[Tuple[int, str]],
                 zone: Optional[tzinfo] = None) -> int:
    """Recompute rollups_daily for the given (metric_id, day) pairs.

    Never touches a day that wasn't listed -- 'never recompute the whole
    table' is the point of tracking dirty days at all. A day whose
    observations have all been deleted has its rollup row removed rather
    than left stale at the old numbers.
    """
    zone = zone or tconfig.local_zone()
    computed = now_iso()
    written = 0
    for mid, day in sorted(set(dirty)):
        start, end = day_bounds(day, zone)
        row = conn.execute(
            "SELECT COUNT(*), SUM(value), MIN(value), MAX(value), AVG(value) "
            "FROM observations WHERE metric_id = ? AND ts >= ? AND ts < ?",
            (mid, start, end)).fetchone()
        n = row[0]
        if not n:
            conn.execute("DELETE FROM rollups_daily WHERE metric_id = ? AND day = ?",
                         (mid, day))
            continue
        # SQLite has no median; the lower of the two middle values for an
        # even count is close enough for a dashboard and costs one sort.
        p50 = conn.execute(
            "SELECT value FROM observations WHERE metric_id = ? AND ts >= ? AND ts < ? "
            "ORDER BY value LIMIT 1 OFFSET ?",
            (mid, start, end, (n - 1) // 2)).fetchone()
        conn.execute(
            "INSERT INTO rollups_daily "
            "(metric_id, day, n, sum_value, min_value, max_value, avg_value, "
            " p50_value, computed_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (metric_id, day) DO UPDATE SET "
            "  n = excluded.n, sum_value = excluded.sum_value, "
            "  min_value = excluded.min_value, max_value = excluded.max_value, "
            "  avg_value = excluded.avg_value, p50_value = excluded.p50_value, "
            "  computed_at = excluded.computed_at",
            (mid, day, n, row[1], row[2], row[3], row[4],
             p50[0] if p50 else None, computed))
        written += 1
    return written


def all_days(conn: sqlite3.Connection, metric_id_: Optional[int] = None,
             zone: Optional[tzinfo] = None) -> Set[Tuple[int, str]]:
    """Every (metric_id, day) that has observations. For a first build or a
    deliberate full rebuild -- not for the incremental path."""
    zone = zone or tconfig.local_zone()
    sql = "SELECT metric_id, ts FROM observations"
    params: Sequence = ()
    if metric_id_ is not None:
        sql += " WHERE metric_id = ?"
        params = (metric_id_,)
    return {
        (mid, tconfig.local_day(parse_iso(ts), zone))
        for mid, ts in conn.execute(sql, params)
    }


def daily(conn: sqlite3.Connection, metric: str, since: str, until: str,
          source_id: Optional[int] = None) -> List[dict]:
    """Rollup rows for a metric over a local-day range, oldest first."""
    if source_id is not None:
        return _daily_for_source(conn, metric, since, until, source_id)
    rows = conn.execute(
        "SELECT day, n, sum_value, min_value, max_value, avg_value, p50_value "
        "FROM rollups_daily WHERE metric_id = ? AND day >= ? AND day <= ? "
        "ORDER BY day",
        (metric_id(conn, metric), since, until)).fetchall()
    return [
        {"day": r[0], "n": r[1], "sum": r[2], "min": r[3], "max": r[4],
         "avg": r[5], "p50": r[6]}
        for r in rows
    ]


def _daily_for_source(conn: sqlite3.Connection, metric: str, since: str,
                      until: str, source_id: int) -> List[dict]:
    """Compute per-source days because stored rollups combine all sources."""
    mid = metric_id(conn, metric)
    current, end = date.fromisoformat(since), date.fromisoformat(until)
    rows = []
    while current <= end:
        day = current.isoformat()
        start, stop = day_bounds(day)
        aggregate = conn.execute(
            "SELECT COUNT(*), SUM(value), MIN(value), MAX(value), AVG(value) "
            "FROM observations WHERE metric_id = ? AND source_id = ? "
            "AND ts >= ? AND ts < ?",
            (mid, source_id, start, stop)).fetchone()
        if aggregate[0]:
            rows.append({"day": day, "n": aggregate[0], "sum": aggregate[1],
                         "min": aggregate[2], "max": aggregate[3],
                         "avg": aggregate[4], "p50": None})
        current += timedelta(days=1)
    return rows


# -- server-side bucketing -----------------------------------------------

# Suffixes accepted in a `bucket` parameter, in seconds.
_BUCKET_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# A day bucket is served from rollups_daily instead of being computed, which
# is the entire reason the rollups exist. Anything coarser than this over a
# long window is asking for the daily rows.
DAY_BUCKETS = frozenset({"1d", "d", "day", "daily"})


def parse_bucket(text: Optional[str]) -> Optional[int]:
    """'30s' | '5m' | '1h' -> seconds. None/'raw' means no bucketing.

    Raises ValueError rather than falling back to a default, so a typo in a
    dashboard URL shows up as a 400 instead of a silently different chart.
    """
    if text is None:
        return None
    text = text.strip().lower()
    if not text or text == "raw":
        return None
    unit = _BUCKET_UNITS.get(text[-1:])
    if unit is None:
        raise ValueError("bucket must end in s, m, h or d: {!r}".format(text))
    try:
        count = float(text[:-1] or "1")
    except ValueError:
        raise ValueError("bucket is not a number: {!r}".format(text))
    seconds = int(count * unit)
    if seconds <= 0:
        raise ValueError("bucket must be positive: {!r}".format(text))
    return seconds


def series(conn: sqlite3.Connection, metric: str, since: datetime,
           until: datetime, bucket: Optional[str] = None,
           source_id: Optional[int] = None) -> List[dict]:
    """Points for one metric over a window, bucketed server-side.

    Bucketing here rather than in the client keeps large raw series local: a
    week of 1 Hz heart rate is 600k rows, and no dashboard wants them over
    the wire when it has 800 pixels to draw them in. Buckets are aligned to
    the epoch, not to the window, so panning a chart doesn't reshuffle which
    samples land together and make the line wobble.

    A day bucket reads rollups_daily; everything else aggregates the raw
    rows. Empty buckets are omitted rather than emitted as gaps -- Grafana
    draws a break either way, and a night of no data shouldn't cost 28,800
    null points.
    """
    if bucket is not None and bucket.strip().lower() in DAY_BUCKETS:
        rows = daily(conn, metric, tconfig.local_day(since),
                     tconfig.local_day(until), source_id=source_id)
        return [{"ts": r["day"], "n": r["n"], "value": r["avg"], "min": r["min"],
                 "max": r["max"], "sum": r["sum"]} for r in rows]

    width = parse_bucket(bucket)
    mid = metric_id(conn, metric)
    params: List[object] = [mid, iso_utc(since), iso_utc(until)]
    where = "metric_id = ? AND ts >= ? AND ts < ?"
    if source_id is not None:
        where += " AND source_id = ?"
        params.append(source_id)

    if width is None:
        return [
            {"ts": ts, "value": value, "text": text}
            for ts, value, text in conn.execute(
                "SELECT ts, value, text_value FROM observations "
                "WHERE {} ORDER BY ts".format(where), params)
        ]

    # strftime('%s') parses the stored ISO form directly, so the aggregation
    # runs entirely in SQLite -- the rows never cross into Python. Integer
    # division floors toward the bucket start for the positive epochs we
    # store, and pre-1970 timestamps are not a case this project has.
    key = "(CAST(strftime('%s', ts) AS INTEGER) / {0}) * {0}".format(width)
    rows = conn.execute(
        "SELECT {key} AS bucket, COUNT(*), AVG(value), MIN(value), MAX(value), "
        "       SUM(value) "
        "FROM observations WHERE {where} "
        "GROUP BY bucket ORDER BY bucket".format(key=key, where=where),
        params).fetchall()
    return [
        {"ts": iso_utc(datetime.fromtimestamp(r[0], timezone.utc)),
         "n": r[1], "value": r[2], "min": r[3], "max": r[4], "sum": r[5]}
        for r in rows
    ]


# -- sources -------------------------------------------------------------

def last_observation(conn: sqlite3.Connection, source_id: int,
                     metric_ids: Optional[Sequence[int]] = None
                     ) -> Optional[str]:
    """When a source last delivered anything.

    `SELECT MAX(ts) WHERE source_id = ?` would be the obvious spelling and
    is a full scan of everything that source has ever written: ux_obs_natural
    is (source_id, metric_id, ts, external_id), so ts is only ordered
    *within* a metric. Asking per metric instead lets SQLite seek to the end
    of each (source_id, metric_id) range and stop -- a dozen O(log n) seeks
    rather than one scan of millions of rows.

    Measured on two million observations: 0.3s becomes immeasurable.
    """
    if metric_ids is None:
        metric_ids = [mid for (mid,) in conn.execute("SELECT id FROM metrics")]
    latest = None
    for metric in metric_ids:
        row = conn.execute(
            "SELECT ts FROM observations WHERE source_id = ? AND metric_id = ? "
            "ORDER BY ts DESC LIMIT 1", (source_id, metric)).fetchone()
        if row and (latest is None or row[0] > latest):
            latest = row[0]
    return latest


def list_sources(conn: sqlite3.Connection, counts: bool = False) -> List[dict]:
    """Configured sources with their sync health.

    Health is read from the database rather than asked of the connectors:
    the server process may not be the one running them -- that is the whole
    point of the agent/server split -- so what it can honestly report is
    when each source last delivered, not whether its adapter is connected
    right now.

    `counts` is off by default because a row count per source is a scan of
    every row that source has written, and this endpoint exists to be cheap
    enough for a UI to poll. A caller that wants the numbers asks for them
    and pays for them.
    """
    metrics = [mid for (mid,) in conn.execute("SELECT id FROM metrics")]
    out = []
    for row in conn.execute(
            "SELECT id, kind, vendor, display_name, auth_ref, config_json, "
            "       enabled, created_at FROM sources ORDER BY id"):
        source_id = row[0]
        state = sync_state(conn, source_id)
        errors = [s["last_error"] for s in state.values() if s["last_error"]]
        entry = {
            "id": source_id, "kind": row[1], "vendor": row[2],
            "display_name": row[3],
            # auth_ref names a keyring entry; the secret itself never leaves
            # the keyring, let alone this endpoint.
            "auth_ref": row[4], "config_json": row[5],
            "enabled": bool(row[6]), "created_at": row[7],
            "last_observation": last_observation(conn, source_id, metrics),
            "sync": state,
            "last_error": errors[0] if errors else None,
        }
        if counts:
            entry["n_observations"] = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE source_id = ?",
                (source_id,)).fetchone()[0]
        out.append(entry)
    return out


# -- retention -----------------------------------------------------------

def sweep_raw_payloads(conn: sqlite3.Connection, keep_days: Optional[int] = None,
                       now: Optional[datetime] = None) -> int:
    """Delete raw_payloads older than the retention window; returns the count.

    0 (or a negative) keeps everything. Deleting rows doesn't
    shrink the file -- that needs a VACUUM, which has to run outside a
    transaction and is the scheduler's business, not this function's.
    """
    keep = tconfig.RAW_RETENTION_DAYS if keep_days is None else keep_days
    if keep <= 0:
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=keep)
    cur = conn.execute("DELETE FROM raw_payloads WHERE fetched_at < ?",
                       (iso_utc(cutoff),))
    return cur.rowcount


def sync_state(conn: sqlite3.Connection, source_id: int) -> Dict[str, dict]:
    """Per-metric watermarks for one source, keyed by metric name."""
    rows = conn.execute(
        "SELECT m.name, s.cursor, s.watermark_ts, s.last_attempt, s.last_success, "
        "       s.last_error "
        "FROM sync_state s JOIN metrics m ON m.id = s.metric_id "
        "WHERE s.source_id = ?", (source_id,)).fetchall()
    return {
        r[0]: {"cursor": r[1], "watermark_ts": r[2], "last_attempt": r[3],
               "last_success": r[4], "last_error": r[5]}
        for r in rows
    }
