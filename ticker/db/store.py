"""
The v2 write path.

Keeps the shape v1 got right -- the caller never touches SQLite, a dedicated
writer thread owns the connection, nothing the caller does can block the UI
thread -- and adds three things:

* **Batch commits.** insert_observations() takes a whole batch and issues one
  executemany + one commit. v1's per-row commit is fine at 1 Hz and hopeless
  at 300k rows of backfill.
* **Coalescing.** Live streams don't arrive in batches, so the writer holds
  rows back until COALESCE_ROWS or COALESCE_MS, whichever comes first, and
  commits once. A crash costs at most COALESCE_MS of live data.
* **Rollup invalidation.** Every commit records which (metric_id, day) pairs
  it actually changed; a rollup task drains that set. Days that didn't change
  are never marked, so an identical re-fetch schedules no work at all.

Setup-time operations (registering a source or a device) are synchronous and
use their own connection: they happen once, they need to return an id, and
making the caller round-trip through the writer queue for them would buy
nothing. Under WAL that's a second connection reading while the writer
writes, which is exactly what WAL is for.
"""

from __future__ import annotations

import gzip
import queue
import sqlite3
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ticker import config as tconfig
from ticker.db import migrate, queries
from ticker.model import Observation, SessionRecord, iso_utc, now_iso

# The DO UPDATE ... WHERE is what makes an amended
# vendor value overwrite while an identical re-fetch writes nothing -- and
# what makes changes() an honest signal for rollup invalidation.
UPSERT_OBSERVATION = (
    "INSERT INTO observations "
    "(source_id, metric_id, session_id, ts, end_ts, value, text_value, "
    " external_id, ingested_at) "
    "VALUES (?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT (source_id, metric_id, ts, external_id) DO UPDATE SET "
    "  session_id = excluded.session_id, "
    "  value = excluded.value, "
    "  end_ts = excluded.end_ts, "
    "  text_value = excluded.text_value, "
    "  ingested_at = excluded.ingested_at "
    "WHERE excluded.session_id IS NOT observations.session_id "
    "   OR excluded.value IS NOT observations.value "
    "   OR excluded.end_ts IS NOT observations.end_ts "
    "   OR excluded.text_value IS NOT observations.text_value"
)


def connect(db_path: Optional[Path] = None, migrate_first: bool = True) -> sqlite3.Connection:
    """Open a connection with the required pragmas, migrating if needed."""
    path = Path(db_path) if db_path else tconfig.DB_PATH
    if migrate_first:
        migrate.migrate(path)
    conn = migrate.open_db(path)
    conn.execute("PRAGMA busy_timeout = {}".format(tconfig.BUSY_TIMEOUT_MS))
    return conn


# -- synchronous setup helpers ------------------------------------------

def ensure_source(conn: sqlite3.Connection, kind: str, vendor: str,
                  display_name: str, auth_ref: Optional[str] = None,
                  config_json: str = "{}") -> int:
    """Get or create a source row, returning its id. Idempotent on
    (vendor, display_name), which is the natural key for a configured
    connector instance."""
    row = conn.execute(
        "SELECT id FROM sources WHERE vendor = ? AND display_name = ?",
        (vendor, display_name)).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO sources (kind, vendor, display_name, auth_ref, config_json, "
        "                     enabled, created_at) VALUES (?,?,?,?,?,1,?)",
        (kind, vendor, display_name, auth_ref, config_json, now_iso()))
    return cur.lastrowid


def ensure_device(conn: sqlite3.Connection, source_id: int, address: Optional[str],
                  name: Optional[str] = None, model: Optional[str] = None) -> Optional[int]:
    """Get or create a device row. Returns None for a source with no address
    to key on -- devices are optional, and a nameless one is not worth a row."""
    if not address:
        return None
    row = conn.execute(
        "SELECT id FROM devices WHERE source_id = ? AND address = ?",
        (source_id, address)).fetchone()
    if row:
        # A strap that reports a name after first being seen by address only.
        if name:
            conn.execute("UPDATE devices SET name = ? WHERE id = ? AND "
                         "(name IS NULL OR name != ?)", (name, row[0], name))
        return row[0]
    cur = conn.execute(
        "INSERT INTO devices (source_id, name, address, model) VALUES (?,?,?,?)",
        (source_id, name, address, model))
    return cur.lastrowid


def ensure_metric(conn: sqlite3.Connection, name: str, unit: str,
                  value_kind: str, description: Optional[str] = None) -> int:
    """Register a metric beyond the seeded set. The registry is extensible by
    design; nothing auto-registers, so a typo in a connector is an error
    rather than a new metric."""
    row = conn.execute("SELECT id FROM metrics WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO metrics (name, unit, value_kind, description) VALUES (?,?,?,?)",
        (name, unit, value_kind, description))
    return cur.lastrowid


def metric_ids(conn: sqlite3.Connection) -> Dict[str, int]:
    return {name: mid for name, mid in conn.execute("SELECT name, id FROM metrics")}


# -- the writer ----------------------------------------------------------

class AsyncStore:
    """Fire-and-forget observation logging on a dedicated writer thread.

    insert_observations() and the session calls enqueue and return; nothing
    the caller does waits on disk. flush() is the one blocking call, for
    shutdown and for tests that need to read back what they just wrote.
    """

    def __init__(self, db_path: Optional[Path] = None,
                 error_queue: Optional["queue.Queue"] = None,
                 coalesce_rows: Optional[int] = None,
                 coalesce_ms: Optional[float] = None,
                 migrate_first: bool = True):
        self.db_path = Path(db_path) if db_path else tconfig.DB_PATH
        self._error_queue = error_queue
        self._coalesce_rows = (tconfig.COALESCE_ROWS if coalesce_rows is None
                               else coalesce_rows)
        self._coalesce_ms = (tconfig.COALESCE_MS if coalesce_ms is None
                             else coalesce_ms)
        self._migrate_first = migrate_first

        self._ops: "queue.Queue" = queue.Queue()
        self._dirty: Set[Tuple[int, str]] = set()
        self._dirty_lock = threading.Lock()
        self._rejected = 0
        self._zone = tconfig.local_zone()

        self._thread = threading.Thread(target=self._run, name="ticker-writer",
                                        daemon=True)
        self._thread.start()

    # -- caller side -----------------------------------------------------

    def insert_observations(self, source_id: int,
                            batch: Sequence[Observation]) -> None:
        """Queue a batch for upsert.

        source_id is an argument rather than part of Observation because a
        connector has no idea which configured source row it was wired up as
        -- that is the scheduler's business, not the connector's.
        """
        if batch:
            self._ops.put(("obs_batch", source_id, tuple(batch)))

    def begin_session(self, source_id: int, record: SessionRecord,
                      device_id: Optional[int] = None) -> None:
        """Open (or re-open) a session and bind its key.

        Observations carrying record.key land in this session from here on.
        A session with an external_id is matched on it, so re-syncing a
        vendor workout updates one row instead of making another.

        device_id is separate from SessionRecord because a connector has no
        idea which devices row it was registered as -- the same reason
        source_id is not on Observation. Resolve it with ensure_device().
        """
        self._ops.put(("session_begin", source_id, record, device_id))

    def end_session(self, source_id: int, key: str,
                    end_ts: Optional[datetime] = None) -> None:
        self._ops.put(("session_end", source_id, key, end_ts))

    def record_sync(self, source_id: int, metric: str,
                    watermark_ts: Optional[datetime] = None,
                    cursor: Optional[str] = None,
                    last_error: Optional[str] = None,
                    succeeded: bool = True) -> None:
        """Persist a pull source's progress for one metric.

        The watermark only ever moves forward, and only when a window
        completed: a partial failure has to leave it where it
        was so the next run re-fetches, which the unique index makes free.
        Passing watermark_ts=None records the attempt without moving it.
        """
        self._ops.put(("sync_state", source_id, metric, watermark_ts, cursor,
                       last_error, succeeded))

    def insert_raw_payload(self, source_id: int, endpoint: str,
                           window_from: Optional[datetime],
                           window_to: Optional[datetime], body: bytes) -> None:
        """Store a verbatim API response so the normalizer can be rewritten
        without re-fetching. Gzipped on the writer thread."""
        self._ops.put(("raw_payload", source_id, endpoint, window_from,
                       window_to, body))

    def rebuild_rollups(self) -> None:
        """Recompute the daily rollups for whatever has changed.

        Section 6's 'low-priority task recomputes dirty days': it runs on the
        writer thread, which already owns a connection and is not the UI
        thread, and it only ever touches days a commit actually changed --
        never the whole table.
        """
        self._ops.put(("rollup",))

    def flush(self, timeout: float = 10.0) -> bool:
        """Block until everything queued so far is committed.

        Returns False if the writer didn't get there in time (a dead writer
        thread, say) rather than hanging the caller.
        """
        done = threading.Event()
        self._ops.put(("flush", done))
        return done.wait(timeout)

    def close(self, timeout: float = 10.0) -> None:
        self._ops.put(("stop",))
        self._thread.join(timeout=timeout)

    def take_dirty_days(self) -> Set[Tuple[int, str]]:
        """Drain the (metric_id, day) pairs whose data changed since the last
        call. The rollup task's input; see queries.rebuild_days."""
        with self._dirty_lock:
            dirty, self._dirty = self._dirty, set()
        return dirty

    @property
    def rejected(self) -> int:
        """Rows the writer refused (unknown metric). Counted, not silent."""
        return self._rejected

    # -- writer thread ---------------------------------------------------

    def _report(self, message: str) -> None:
        if self._error_queue is not None:
            self._error_queue.put({"type": "error", "message": message})

    def _run(self) -> None:
        try:
            conn = connect(self.db_path, migrate_first=self._migrate_first)
        except Exception as exc:
            # v1's lesson: an unwritable path used to kill this thread on its
            # first line and take all logging with it, silently. Say so, then
            # keep draining so callers never block and close() still returns.
            traceback.print_exc()
            self._report("Cannot log to {} - {}".format(self.db_path, exc))
            self._drain_until_stop()
            return

        try:
            self._loop(conn)
        finally:
            conn.close()

    def _loop(self, conn: sqlite3.Connection) -> None:
        metrics = metric_ids(conn)
        sessions: Dict[Tuple[int, str], int] = {}
        pending: List[tuple] = []
        touched: Set[Tuple[int, str]] = set()
        deadline: Optional[float] = None

        while True:
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                op = self._ops.get(timeout=timeout)
            except queue.Empty:
                self._commit(conn, pending, touched)   # coalescing window expired
                pending, touched, deadline = [], set(), None
                continue

            kind = op[0]
            try:
                if kind == "stop":
                    self._commit(conn, pending, touched)
                    return

                if kind == "flush":
                    # The waiter has to be released even when the commit
                    # fails, or one bad batch turns every later flush() into
                    # a full-timeout stall instead of a prompt False.
                    try:
                        self._commit(conn, pending, touched)
                    finally:
                        pending, touched, deadline = [], set(), None
                        op[1].set()
                    continue

                if kind == "obs_batch":
                    _, source_id, batch = op
                    rows, days = self._resolve(metrics, sessions, source_id, batch)
                    pending.extend(rows)
                    touched |= days
                    if deadline is None:
                        deadline = time.monotonic() + self._coalesce_ms / 1000.0
                    if len(pending) >= self._coalesce_rows:
                        self._commit(conn, pending, touched)
                        pending, touched, deadline = [], set(), None
                    continue

                if kind == "sync_state":
                    _, source_id, metric, watermark, cursor, error, ok = op
                    self._record_sync(conn, metrics, source_id, metric,
                                      watermark, cursor, error, ok)
                    continue

                if kind == "raw_payload":
                    _, source_id, endpoint, window_from, window_to, body = op
                    conn.execute(
                        "INSERT INTO raw_payloads (source_id, endpoint, "
                        "window_from, window_to, fetched_at, body) "
                        "VALUES (?,?,?,?,?,?)",
                        (source_id, endpoint,
                         iso_utc(window_from) if window_from else None,
                         iso_utc(window_to) if window_to else None,
                         now_iso(), gzip.compress(body)))
                    continue

                if kind == "rollup":
                    # Pending rows first, or the rebuild reads a day whose
                    # newest observations haven't been committed yet.
                    self._commit(conn, pending, touched)
                    pending, touched, deadline = [], set(), None
                    dirty = self.take_dirty_days()
                    if dirty:
                        conn.execute("BEGIN")
                        try:
                            queries.rebuild_days(conn, dirty, self._zone)
                            conn.execute("COMMIT")
                        except Exception:
                            conn.execute("ROLLBACK")
                            # Put them back: a day that failed to rebuild is
                            # still dirty, and dropping it would leave the
                            # rollup permanently stale.
                            with self._dirty_lock:
                                self._dirty |= dirty
                            raise
                    continue

                if kind == "session_begin":
                    _, source_id, record, device_id = op
                    # Sessions are a foreign key for the rows waiting in
                    # `pending`, so they have to be durable before those rows
                    # reference them.
                    self._commit(conn, pending, touched)
                    pending, touched, deadline = [], set(), None
                    sessions[(source_id, record.key)] = self._upsert_session(
                        conn, source_id, record, device_id)
                    continue

                if kind == "session_end":
                    _, source_id, key, end_ts = op
                    self._commit(conn, pending, touched)
                    pending, touched, deadline = [], set(), None
                    session_id = sessions.pop((source_id, key), None)
                    if session_id is not None:
                        # Single statements commit themselves; the connection
                        # is in autocommit, so an explicit COMMIT here would
                        # raise "no transaction is active".
                        conn.execute("UPDATE sessions SET end_ts = ? WHERE id = ?",
                                     (iso_utc(end_ts) if end_ts else now_iso(),
                                      session_id))
                    continue

            except Exception:
                # One bad op must never take the writer thread down and
                # silently end all logging for the life of the process.
                traceback.print_exc()
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                pending, touched, deadline = [], set(), None

    def _drain_until_stop(self) -> None:
        """Swallow queued work until close(), so the queue can't grow without
        bound when there is no database to write to."""
        while True:
            op = self._ops.get()
            if op[0] == "stop":
                return
            if op[0] == "flush":
                op[1].set()

    def _resolve(self, metrics: Dict[str, int],
                 sessions: Dict[Tuple[int, str], int],
                 source_id: int, batch: Iterable[Observation]
                 ) -> Tuple[List[tuple], Set[Tuple[int, str]]]:
        """Observation objects -> parameter tuples, plus the days they touch."""
        ingested = now_iso()
        rows: List[tuple] = []
        days: Set[Tuple[int, str]] = set()
        for obs in batch:
            metric_id = metrics.get(obs.metric)
            if metric_id is None:
                self._rejected += 1
                self._report(
                    "Unknown metric {!r} from source {} - register it with "
                    "ensure_metric() first".format(obs.metric, source_id))
                continue
            # An observation with no session is still data. v1 dropped
            # sessionless samples; a stream running without a user-started
            # session is a normal case in v2, so it gets NULL instead.
            session_id = (sessions.get((source_id, obs.session_key))
                          if obs.session_key else None)
            rows.append((
                source_id, metric_id, session_id,
                iso_utc(obs.ts),
                iso_utc(obs.end_ts) if obs.end_ts else None,
                float(obs.value), obs.text_value, obs.external_id, ingested,
            ))
            days.add((metric_id, tconfig.local_day(obs.ts, self._zone)))
        return rows, days

    def _commit(self, conn: sqlite3.Connection, pending: List[tuple],
                touched: Set[Tuple[int, str]]) -> None:
        if not pending:
            return
        # Explicit BEGIN: the connection runs in autocommit (isolation_level
        # None, so migrations can control their own transactions), and
        # without it executemany would commit once per row, which is exactly
        # the cost batching exists to avoid.
        conn.execute("BEGIN")
        cur = conn.executemany(UPSERT_OBSERVATION, pending)
        changed = cur.rowcount
        conn.execute("COMMIT")
        # Only mark days dirty when something actually changed. An identical
        # re-fetch -- the normal case for an overlapping sync window -- takes
        # the upsert's WHERE branch, changes nothing, and schedules no
        # rollup work. Within a batch that did change something we can't tell
        # which rows, so the whole batch's days are marked: a superset costs
        # a recompute, a subset would leave a rollup wrong.
        if changed:
            with self._dirty_lock:
                self._dirty |= touched

    def _record_sync(self, conn: sqlite3.Connection, metrics: Dict[str, int],
                     source_id: int, metric: str,
                     watermark: Optional[datetime], cursor: Optional[str],
                     error: Optional[str], succeeded: bool) -> None:
        metric_id = metrics.get(metric)
        if metric_id is None:
            self._rejected += 1
            self._report("Unknown metric {!r} in sync state".format(metric))
            return
        now = now_iso()
        conn.execute(
            "INSERT INTO sync_state (source_id, metric_id, cursor, watermark_ts, "
            "                        last_attempt, last_success, last_error) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT (source_id, metric_id) DO UPDATE SET "
            # COALESCE, not assignment: a run that didn't finish must leave
            # the watermark where it was rather than blanking it.
            "  cursor       = excluded.cursor, "
            "  watermark_ts = COALESCE(excluded.watermark_ts, sync_state.watermark_ts), "
            "  last_attempt = excluded.last_attempt, "
            "  last_success = COALESCE(excluded.last_success, sync_state.last_success), "
            "  last_error   = excluded.last_error",
            (source_id, metric_id, cursor,
             iso_utc(watermark) if watermark else None,
             now, now if succeeded else None, error))

    @staticmethod
    def _upsert_session(conn: sqlite3.Connection, source_id: int,
                        record: SessionRecord,
                        device_id: Optional[int] = None) -> int:
        if record.external_id is not None:
            row = conn.execute(
                "SELECT id FROM sessions WHERE source_id = ? AND external_id = ?",
                (source_id, record.external_id)).fetchone()
            if row:
                conn.execute(
                    "UPDATE sessions SET start_ts = ?, end_ts = ?, kind = ?, "
                    "label = ?, device_id = COALESCE(?, device_id) WHERE id = ?",
                    (iso_utc(record.start_ts),
                     iso_utc(record.end_ts) if record.end_ts else None,
                     record.kind, record.label, device_id, row[0]))
                return row[0]
        cur = conn.execute(
            "INSERT INTO sessions (source_id, device_id, external_id, start_ts, "
            "                      end_ts, kind, label) VALUES (?,?,?,?,?,?,?)",
            (source_id, device_id, record.external_id, iso_utc(record.start_ts),
             iso_utc(record.end_ts) if record.end_ts else None,
             record.kind, record.label))
        return cur.lastrowid
