"""
The v2 write path.

Keeps the shape v1 got right -- the caller never touches SQLite, a dedicated
writer thread owns the connection, nothing the caller does can block the UI
thread -- and adds:

* **Batch commits.** insert_observations() takes a whole batch and issues one
  executemany + one commit. v1's per-row commit is fine at 1 Hz and hopeless
  at 300k rows of backfill.
* **Coalescing.** Live streams don't arrive in batches, so the writer holds
  rows back until COALESCE_ROWS or COALESCE_MS, whichever comes first, and
  commits once. A crash costs at most COALESCE_MS of live data -- and never
  sync progress: a checkpoint commits in the same transaction as every row
  queued before it, so a watermark can't outrun the rows it covers.
* **Rollup invalidation.** Triggers (migration 0004) mark the days an
  insert, update or delete changed, in the same transaction, from any
  connection; the rollup task drains them. An identical re-fetch changes no
  row, fires no trigger, and schedules no work.
* **Truthful outcomes.** A storage failure latches a fault: the writer logs
  it, refuses further data until retry() proves a real commit works again,
  and flush(), close() and every checkpoint report it instead of success.

Setup-time operations (registering a source or a device) are synchronous and
use their own connection: they happen once, they need to return an id, and
making the caller round-trip through the writer queue for them would buy
nothing. Under WAL that's a second connection reading while the writer
writes, which is exactly what WAL is for.
"""

from __future__ import annotations

import gzip
import logging
import queue
import sqlite3
import threading
import time
from concurrent.futures import Future, InvalidStateError, TimeoutError as FutureTimeout
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ticker import config as tconfig
from ticker.db import migrate, queries
from ticker.model import Observation, SessionRecord, iso_utc, now_iso

log = logging.getLogger(__name__)

# The DO UPDATE ... WHERE is what makes an amended
# vendor value overwrite while an identical re-fetch writes nothing -- and
# what keeps the rollup triggers quiet for a re-fetch that changed nothing.
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

# SQLite's primary result codes for contention between connections.
_SQLITE_BUSY, _SQLITE_LOCKED = 5, 6

# A write that meets contention is retried this many times -- each attempt
# already waits out BUSY_TIMEOUT_MS -- before it counts as a storage fault.
BUSY_ATTEMPTS = 3

# An import may have at most this many work units queued or being written.
IMPORT_IN_FLIGHT = 2

# Rollup markers rebuilt per transaction, so a large backlog (the first start
# after migration 0004 marks every day) commits in pieces.
ROLLUP_CHUNK = 500


class StorageFault(RuntimeError):
    """The writer could not save data, and refuses more until retry()."""


class ImportCancelled(RuntimeError):
    """An import was stopped early. Everything it had admitted was written."""


def _transient(exc: BaseException) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    return code is not None and (code & 0xFF) in (_SQLITE_BUSY, _SQLITE_LOCKED)


def _storage_error(exc: BaseException) -> bool:
    """A failure of the database itself -- I/O, a full disk, a read-only or
    corrupt file -- rather than of one batch's data (IntegrityError) or of a
    single operation's code."""
    return (isinstance(exc, sqlite3.Error)
            and not isinstance(exc, sqlite3.IntegrityError))


class _Batch:
    """Rows from one insert_observations() call, waiting to commit."""

    __slots__ = ("rows", "done", "valid")

    def __init__(self, rows: List[tuple], done: Optional[Callable[[bool], None]],
                 valid: bool):
        self.rows = rows
        self.done = done
        self.valid = valid


class AsyncStore:
    """Observation logging on a dedicated writer thread.

    insert_observations() and the session calls enqueue and return; nothing
    the caller does waits on disk. record_sync() returns a future that
    completes once the checkpoint -- and every row queued before it -- has
    committed. flush() is the blocking call, for shutdown, backups and tests,
    and it says whether everything before it was saved.
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
        self._rejected = 0
        self._dropped = 0
        self._fault: Optional[str] = None
        self._zone = tconfig.local_zone()

        # Writer-thread state.
        self._metrics: Dict[str, int] = {}
        self._sessions: Dict[Tuple[int, str], int] = {}
        self._pending: List[_Batch] = []
        self._pending_rows = 0
        self._deadline: Optional[float] = None
        self._failed_since_flush = False
        self._failed_since_checkpoint = False
        self._drained = False

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

        While storage is faulted the rows are dropped and counted (see
        `dropped`) rather than queued without bound.
        """
        if batch:
            self._ops.put(("obs_batch", source_id, tuple(batch), None))

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
        self._ops.put(("session_begin", source_id, record, device_id, None))

    def end_session(self, source_id: int, key: str,
                    end_ts: Optional[datetime] = None) -> None:
        self._ops.put(("session_end", source_id, key, end_ts))

    def record_sync(self, source_id: int, metric: str,
                    watermark_ts: Optional[datetime] = None,
                    cursor: Optional[str] = None,
                    last_error: Optional[str] = None,
                    succeeded: bool = True) -> "Future[bool]":
        """Persist a pull source's progress for one metric.

        Commits in the same transaction as every observation queued before
        it. The returned future completes once that has happened, and fails
        if it could not; a caller must not treat the window as done before.

        The watermark only ever moves forward, and only when a window
        completed: a partial failure has to leave it where it
        was so the next run re-fetches, which the unique index makes free.
        Passing watermark_ts=None records the attempt without moving it.

        `cursor` is how far backfill has reached. None leaves the saved one
        alone: a live window or a failed fetch must never erase it.
        """
        future: "Future[bool]" = Future()
        self._ops.put(("sync_state", source_id, metric, watermark_ts, cursor,
                       last_error, succeeded, future))
        return future

    def mark_backfill_done(self, source_id: int, metric: str) -> "Future[bool]":
        """Remember that a metric's history is complete, so a restart doesn't
        walk it again. Touches nothing else in the sync_state row -- in
        particular not last_success, which belongs to live sync."""
        future: "Future[bool]" = Future()
        self._ops.put(("backfill_done", source_id, metric, future))
        return future

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

    def sweep_raw_payloads(self) -> None:
        """Delete raw vendor responses past retention. On the writer, not on
        the caller's thread, where waiting for the write lock would stall it
        -- the pull event loop, for one."""
        self._ops.put(("sweep",))

    def importer(self, cancel: Optional[threading.Event] = None) -> "ImportWriter":
        """The bounded view of this writer that an import writes through."""
        return ImportWriter(self, cancel)

    def flush(self, timeout: float = 10.0) -> bool:
        """Block until everything queued so far is committed.

        True only if it all was: False for a storage fault, for any write
        that failed since the previous flush, or if the writer didn't get
        there in time -- a timeout says nothing about whether the work will
        still land, so it is never reported as saved.
        """
        done = threading.Event()
        outcome: List[bool] = []
        self._ops.put(("flush", done, outcome))
        if not done.wait(timeout):
            log.warning("writes did not finish within %.0fs", timeout)
            return False
        return outcome == [True]

    def retry(self, timeout: float = 30.0) -> bool:
        """Retry storage after a fault. The fault clears only once a real
        write transaction commits -- reopening the database first if it could
        not be opened. True if storage is usable now."""
        future: "Future[bool]" = Future()
        self._ops.put(("probe", future))
        try:
            return future.result(timeout)
        except FutureTimeout:
            return False

    def close(self, timeout: float = 60.0, warn_after: float = 10.0) -> bool:
        """Commit what is pending and stop the writer.

        True only if storage is healthy and everything still queued was
        saved. (A batch refused earlier for bad data was reported then, and
        by the flush after it.) A slow drain is logged after `warn_after` and
        waited for up to `timeout`; returning early as if the work were done
        is exactly what this must not do.
        """
        self._ops.put(("stop",))
        self._thread.join(min(warn_after, timeout))
        if self._thread.is_alive() and timeout > warn_after:
            log.warning("still saving data; waiting up to %.0fs more",
                        timeout - warn_after)
            self._thread.join(timeout - warn_after)
        if self._thread.is_alive():
            log.error("the writer did not finish within %.0fs; queued data may "
                      "not be saved", timeout)
            return False
        return self._fault is None and self._drained and not self._failed_since_flush

    def take_dirty_days(self) -> Set[Tuple[int, str]]:
        """Drain the (metric_id, local day) pairs whose rollups are stale.

        The markers live in rollup_dirty, so this reads and clears them
        through a connection of its own."""
        conn = connect(self.db_path, migrate_first=False)
        try:
            conn.execute("BEGIN IMMEDIATE")
            markers = conn.execute(
                "SELECT source_id, metric_id, utc_day FROM rollup_dirty").fetchall()
            conn.execute("DELETE FROM rollup_dirty")
            conn.execute("COMMIT")
        finally:
            conn.close()
        return queries.local_dirty_days(markers, self._zone)

    @property
    def fault(self) -> Optional[str]:
        """Why storage is refusing writes, or None while it works."""
        return self._fault

    @property
    def rejected(self) -> int:
        """Rows the writer refused (unknown metric, bad batch). Counted, not
        silent."""
        return self._rejected

    @property
    def dropped(self) -> int:
        """Rows not saved because storage was faulted."""
        return self._dropped

    # -- writer thread ---------------------------------------------------

    def _report(self, message: str) -> None:
        if self._error_queue is not None:
            self._error_queue.put({"type": "error", "message": message})

    def _latch(self, exc: BaseException, message: Optional[str] = None) -> None:
        message = message or "Ticker could not save data: {}".format(exc)
        if self._fault is None:
            self._fault = message
            log.error("%s; refusing writes until storage is retried", message,
                      exc_info=exc)
            self._report(message)

    def _run(self) -> None:
        conn = self._open()
        while True:
            if conn is None:
                conn = self._offline()
                if conn is None:
                    return
            try:
                self._loop(conn)
                return
            except Exception as exc:
                # The loop itself failing is a bug, not a bad op. Stop
                # saving, say so, and keep answering callers.
                self._latch(exc, "Ticker's writer stopped unexpectedly: {}".format(exc))
            finally:
                conn.close()
            conn = None

    def _open(self) -> Optional[sqlite3.Connection]:
        try:
            conn = connect(self.db_path, migrate_first=self._migrate_first)
        except Exception as exc:
            # v1's lesson: an unwritable path used to kill this thread on its
            # first line and take all logging with it, silently. Say so, then
            # keep answering so callers never block and close() still returns.
            self._latch(exc, "Cannot log to {} - {}".format(self.db_path, exc))
            return None
        try:
            self._metrics = metric_ids(conn)
        except sqlite3.Error as exc:
            self._latch(exc)
            return conn
        if self._probe(conn) and self._fault is None:
            # Days marked before a crash, or by the migration, get rebuilt
            # before anything else -- no maintenance command required.
            self._guarded(conn, self._rebuild_dirty)
        return conn

    def _offline(self) -> Optional[sqlite3.Connection]:
        """No connection: refuse work promptly until a probe reconnects
        (returns the connection) or close() arrives (returns None)."""
        while True:
            op = self._ops.get()
            if op[0] == "stop":
                return None
            if op[0] == "probe":
                conn = self._open()
                self._complete_future(op[1], result=conn is not None and self._fault is None)
                if conn is not None:
                    return conn
                continue
            self._refuse(op)

    def _probe(self, conn: sqlite3.Connection) -> bool:
        """Prove storage takes a real commit. Clears a fault if it does."""
        try:
            self._write(conn, lambda: conn.execute(
                "INSERT INTO writer_probe (id, at) VALUES (1, ?) "
                "ON CONFLICT (id) DO UPDATE SET at = excluded.at", (now_iso(),)))
        except Exception as exc:
            self._latch(exc)
            return False
        if self._fault is not None:
            log.info("storage accepts writes again; resuming")
            self._fault = None
            self._failed_since_flush = False
        return True

    def _loop(self, conn: sqlite3.Connection) -> None:
        while True:
            timeout = (None if self._deadline is None
                       else max(0.0, self._deadline - time.monotonic()))
            try:
                op = self._ops.get(timeout=timeout)
            except queue.Empty:
                self._guarded(conn, self._commit_pending)   # coalescing window expired
                continue

            kind = op[0]
            if kind == "stop":
                self._drained = self._guarded(conn, self._commit_pending)
                return
            if kind == "probe":
                self._complete_future(op[1], result=self._probe(conn))
                continue
            if self._fault is not None:
                self._refuse(op)
                continue

            if kind == "obs_batch":
                _, source_id, batch, done = op
                rejected_before = self._rejected
                rows = self._resolve(source_id, batch)
                valid = self._rejected == rejected_before
                if not rows:
                    if done is not None:
                        done(valid)
                    continue
                self._pending.append(_Batch(rows, done, valid))
                self._pending_rows += len(rows)
                if self._deadline is None:
                    self._deadline = time.monotonic() + self._coalesce_ms / 1000.0
                if self._pending_rows >= self._coalesce_rows:
                    self._guarded(conn, self._commit_pending)
                continue

            if kind == "flush":
                _, done, outcome = op
                # The waiter is released whatever happens, or one bad batch
                # turns every later flush() into a full-timeout stall instead
                # of a prompt False.
                try:
                    ok = self._guarded(conn, self._commit_pending)
                    outcome.append(ok and self._fault is None
                                   and not self._failed_since_flush)
                    self._failed_since_flush = False
                finally:
                    done.set()
                continue

            if kind == "sync_state":
                _, source_id, metric, watermark, cursor, error, succeeded, future = op
                metric_id = self._checkpoint_metric(metric, future)
                if metric_id is None:
                    continue
                self._commit_checkpoint(conn, future, lambda: self._record_sync(
                    conn, source_id, metric_id, watermark, cursor, error, succeeded))
                continue

            if kind == "backfill_done":
                _, source_id, metric, future = op
                metric_id = self._checkpoint_metric(metric, future)
                if metric_id is None:
                    continue
                self._commit_checkpoint(conn, future, lambda: conn.execute(
                    "INSERT INTO sync_state (source_id, metric_id, backfill_done) "
                    "VALUES (?,?,1) ON CONFLICT (source_id, metric_id) "
                    "DO UPDATE SET backfill_done = 1", (source_id, metric_id)))
                continue

            if kind == "raw_payload":
                _, source_id, endpoint, window_from, window_to, body = op
                self._guarded(conn, lambda c: self._write(c, lambda: c.execute(
                    "INSERT INTO raw_payloads (source_id, endpoint, "
                    "window_from, window_to, fetched_at, body) "
                    "VALUES (?,?,?,?,?,?)",
                    (source_id, endpoint,
                     iso_utc(window_from) if window_from else None,
                     iso_utc(window_to) if window_to else None,
                     now_iso(), gzip.compress(body)))))
                continue

            if kind == "rollup":
                # Pending rows first, or the rebuild reads a day whose
                # newest observations haven't been committed yet.
                if self._guarded(conn, self._commit_pending):
                    self._guarded(conn, self._rebuild_dirty)
                continue

            if kind == "sweep":
                self._guarded(conn, self._sweep)
                continue

            if kind == "session_begin":
                _, source_id, record, device_id, done = op
                # Sessions are a foreign key for the rows waiting in
                # `pending`, so they have to be durable before those rows
                # reference them.
                ok = self._guarded(conn, lambda c: self._begin_session(
                    c, source_id, record, device_id))
                if not ok:
                    self._failed_since_checkpoint = True
                if done is not None:
                    done(ok)
                continue

            if kind == "session_end":
                _, source_id, key, end_ts = op
                self._guarded(conn, lambda c: self._end_session(c, source_id, key, end_ts))
                continue

    def _guarded(self, conn: sqlite3.Connection,
                 work: Callable[[sqlite3.Connection], object]) -> bool:
        """Run one operation; True if it committed.

        A storage failure latches the fault. Anything else fails only this
        operation -- one bad op must never take the writer thread down and
        silently end all logging for the life of the process -- but it is
        logged and remembered, so the next flush() doesn't report success.
        """
        try:
            work(conn)
            return True
        except Exception as exc:
            self._failed_since_flush = True
            if _storage_error(exc):
                self._latch(exc)
            else:
                log.exception("a write failed")
                self._report("A write failed: {}".format(exc))
            return False

    def _write(self, conn: sqlite3.Connection, work: Callable[[], object]) -> object:
        """`work` in one transaction, retrying contention a bounded number of
        times. BEGIN IMMEDIATE so the wait for the write lock happens up
        front, under busy_timeout, rather than midway through."""
        for attempt in range(1, BUSY_ATTEMPTS + 1):
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = work()
                conn.execute("COMMIT")
                return result
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if not _transient(exc) or attempt == BUSY_ATTEMPTS:
                    raise
                log.warning("database busy; retrying write (%d/%d)",
                            attempt, BUSY_ATTEMPTS)
        raise AssertionError("unreachable")

    def _commit_checkpoint(self, conn: sqlite3.Connection, future: Future,
                           checkpoint: Callable[[], object]) -> None:
        include_progress = not self._failed_since_checkpoint
        committed = self._guarded(
            conn, lambda c: self._commit_pending(
                c, then=checkpoint if include_progress else None))
        accepted = not self._failed_since_checkpoint
        self._failed_since_checkpoint = False
        self._settle(future, committed and include_progress and accepted)

    def _commit_pending(self, conn: sqlite3.Connection,
                        then: Optional[Callable[[], object]] = None) -> None:
        """Commit the coalesced batches -- and `then`, a checkpoint, in the
        same transaction, so progress can never be saved ahead of its rows."""
        batches, self._pending, self._pending_rows = self._pending, [], 0
        self._deadline = None
        if not batches and then is None:
            return
        rows = [row for batch in batches for row in batch.rows]

        def work():
            if rows:
                conn.executemany(UPSERT_OBSERVATION, rows)
            if then is not None:
                then()

        try:
            self._write(conn, work)
        except sqlite3.IntegrityError:
            # One producer's bad batch (say, a source deleted mid-sync) must
            # not take everyone else's rows with it: write each on its own,
            # and refuse only the ones that fail.
            self._commit_each(conn, batches, then)
        except BaseException:
            self._dropped += len(rows)
            self._settle_batches(batches, False)
            raise
        else:
            self._settle_batches(batches, True)

    def _commit_each(self, conn: sqlite3.Connection, batches: List[_Batch],
                     then: Optional[Callable[[], object]]) -> None:
        for index, batch in enumerate(batches):
            try:
                self._write(conn, lambda: conn.executemany(UPSERT_OBSERVATION, batch.rows))
            except sqlite3.IntegrityError as exc:
                self._failed_since_flush = True
                self._failed_since_checkpoint = True
                self._rejected += len(batch.rows)
                log.warning("refused a batch of %d rows: %s", len(batch.rows), exc)
                self._report("Refused {} rows: {}".format(len(batch.rows), exc))
                self._settle_batches([batch], False)
            except BaseException:
                self._dropped += sum(len(b.rows) for b in batches[index:])
                self._settle_batches(batches[index:], False)
                raise
            else:
                self._settle_batches([batch], True)
        if then is not None and not self._failed_since_checkpoint:
            self._write(conn, then)

    @staticmethod
    def _settle_batches(batches: List[_Batch], ok: bool) -> None:
        for batch in batches:
            if batch.done is not None:
                batch.done(ok and batch.valid)

    def _settle(self, future: Future, ok: bool) -> None:
        if ok:
            self._complete_future(future, result=True)
        else:
            self._complete_future(future, error=StorageFault(
                self._fault or "progress could not be saved"))

    @staticmethod
    def _complete_future(future: Future, *, result: Optional[bool] = None,
                         error: Optional[BaseException] = None) -> None:
        try:
            if error is None:
                future.set_result(result)
            else:
                future.set_exception(error)
        except InvalidStateError:
            if not future.cancelled():
                raise

    def _checkpoint_metric(self, metric: str, future: Future) -> Optional[int]:
        metric_id = self._metrics.get(metric)
        if metric_id is None:
            self._rejected += 1
            self._failed_since_flush = True
            self._report("Unknown metric {!r} in sync state".format(metric))
            self._complete_future(future, error=ValueError(
                "unknown metric {!r}".format(metric)))
        return metric_id

    def _refuse(self, op: tuple) -> None:
        """Answer an operation without doing it, because storage is faulted."""
        kind = op[0]
        if kind == "obs_batch":
            self._dropped += len(op[2])
            if op[3] is not None:
                op[3](False)
        elif kind == "session_begin":
            if op[4] is not None:
                op[4](False)
        elif kind in ("sync_state", "backfill_done"):
            self._complete_future(op[-1], error=StorageFault(
                self._fault or "storage is unavailable"))
        elif kind == "flush":
            op[2].append(False)
            op[1].set()
        elif kind == "probe":
            self._complete_future(op[1], result=False)

    def _resolve(self, source_id: int,
                 batch: Iterable[Observation]) -> List[tuple]:
        """Observation objects -> parameter tuples. A row that can't be
        written is counted and reported here, not discovered at commit."""
        ingested = now_iso()
        rows: List[tuple] = []
        for obs in batch:
            metric_id = self._metrics.get(obs.metric)
            if metric_id is None:
                self._rejected += 1
                self._failed_since_flush = True
                self._failed_since_checkpoint = True
                self._report(
                    "Unknown metric {!r} from source {} - register it with "
                    "ensure_metric() first".format(obs.metric, source_id))
                continue
            # An observation with no session is still data. v1 dropped
            # sessionless samples; a stream running without a user-started
            # session is a normal case in v2, so it gets NULL instead.
            session_id = (self._sessions.get((source_id, obs.session_key))
                          if obs.session_key else None)
            try:
                rows.append((
                    source_id, metric_id, session_id,
                    iso_utc(obs.ts),
                    iso_utc(obs.end_ts) if obs.end_ts else None,
                    float(obs.value), obs.text_value, obs.external_id, ingested,
                ))
            except (TypeError, ValueError, AttributeError) as exc:
                self._rejected += 1
                self._failed_since_flush = True
                self._failed_since_checkpoint = True
                self._report("Unusable {} row from source {}: {}".format(
                    obs.metric, source_id, exc))
        return rows

    def _record_sync(self, conn: sqlite3.Connection, source_id: int,
                     metric_id: int, watermark: Optional[datetime],
                     cursor: Optional[str], error: Optional[str],
                     succeeded: bool) -> None:
        now = now_iso()
        conn.execute(
            "INSERT INTO sync_state (source_id, metric_id, cursor, watermark_ts, "
            "                        last_attempt, last_success, last_error) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT (source_id, metric_id) DO UPDATE SET "
            # COALESCE, not assignment: a run that didn't finish must leave
            # the watermark where it was rather than blanking it, and a live
            # window or a failure must leave the backfill cursor alone.
            "  cursor       = COALESCE(excluded.cursor, sync_state.cursor), "
            "  watermark_ts = COALESCE(excluded.watermark_ts, sync_state.watermark_ts), "
            "  last_attempt = excluded.last_attempt, "
            "  last_success = COALESCE(excluded.last_success, sync_state.last_success), "
            "  last_error   = excluded.last_error",
            (source_id, metric_id, cursor,
             iso_utc(watermark) if watermark else None,
             now, now if succeeded else None, error))

    def _begin_session(self, conn: sqlite3.Connection, source_id: int,
                       record: SessionRecord, device_id: Optional[int]) -> None:
        self._commit_pending(conn)
        self._sessions[(source_id, record.key)] = self._write(
            conn, lambda: self._upsert_session(conn, source_id, record, device_id))

    def _end_session(self, conn: sqlite3.Connection, source_id: int, key: str,
                     end_ts: Optional[datetime]) -> None:
        self._commit_pending(conn)
        session_id = self._sessions.pop((source_id, key), None)
        if session_id is not None:
            self._write(conn, lambda: conn.execute(
                "UPDATE sessions SET end_ts = ? WHERE id = ?",
                (iso_utc(end_ts) if end_ts else now_iso(), session_id)))

    def _rebuild_dirty(self, conn: sqlite3.Connection) -> None:
        """Recompute rollups for the days the triggers marked, a chunk per
        transaction. A chunk's markers are cleared in the same transaction
        as its rebuild, so a failure leaves them for next time."""
        while True:
            def chunk() -> int:
                markers = conn.execute(
                    "SELECT source_id, metric_id, utc_day FROM rollup_dirty "
                    "LIMIT ?", (ROLLUP_CHUNK,)).fetchall()
                if markers:
                    queries.rebuild_days(
                        conn, queries.local_dirty_days(markers, self._zone),
                        self._zone)
                    conn.executemany(
                        "DELETE FROM rollup_dirty WHERE source_id = ? AND "
                        "metric_id = ? AND utc_day = ?", markers)
                return len(markers)
            if self._write(conn, chunk) < ROLLUP_CHUNK:
                return

    def _sweep(self, conn: sqlite3.Connection) -> None:
        removed = self._write(conn, lambda: queries.sweep_raw_payloads(conn))
        if removed:
            log.info("removed %d raw vendor responses past retention", removed)

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


class ImportWriter:
    """The writer as an import sees it.

    Blocks the import thread -- and only the import thread -- once
    IMPORT_IN_FLIGHT of its work units are queued or being written, so a
    gigabyte export can neither grow the queue without bound nor put more
    than that in front of sync and live writes. Sessions count against the
    same budget as observation batches.
    """

    def __init__(self, store: AsyncStore, cancel: Optional[threading.Event] = None,
                 limit: int = IMPORT_IN_FLIGHT):
        self._store = store
        self._cancel = cancel or threading.Event()
        self._limit = limit
        self._slots = threading.Semaphore(limit)
        self._failures = 0

    def insert_observations(self, source_id: int,
                            batch: Sequence[Observation]) -> None:
        if batch:
            self._admit()
            self._store._ops.put(("obs_batch", source_id, tuple(batch), self._done))

    def begin_session(self, source_id: int, record: SessionRecord,
                      device_id: Optional[int] = None) -> None:
        self._admit()
        self._store._ops.put(("session_begin", source_id, record, device_id,
                              self._done))

    def finish(self, timeout: float = 600.0) -> None:
        """Return once everything this import admitted is committed; raise
        StorageFault if any of it wasn't."""
        deadline = time.monotonic() + timeout
        # Commits the last partial batch now rather than at the coalescing
        # deadline. Its result covers other producers too, so the import's
        # own outcome comes from its slots instead.
        self._store.flush(timeout)
        for taken in range(self._limit):
            if not self._slots.acquire(timeout=max(0.0, deadline - time.monotonic())):
                for _ in range(taken):
                    self._slots.release()
                raise StorageFault("the import's last writes did not finish in time")
        for _ in range(self._limit):
            self._slots.release()
        if self._failures:
            raise StorageFault(self._store.fault
                               or "part of the import could not be saved")

    def _admit(self) -> None:
        while True:
            if self._cancel.is_set():
                raise ImportCancelled("import canceled")
            fault = self._store.fault
            if fault:
                raise StorageFault(fault)
            if not self._store._thread.is_alive():
                raise StorageFault("the writer has stopped")
            if self._slots.acquire(timeout=0.1):
                return

    def _done(self, ok: bool) -> None:
        if not ok:
            self._failures += 1
        self._slots.release()
