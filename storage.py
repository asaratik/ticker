"""
SQLite storage for heart rate sessions/samples.

Writes happen on a dedicated background thread (AsyncSessionStore) so disk
I/O can never block the UI thread -- important on Windows, where SQLite's
default rollback-journal mode creates/deletes a small sidecar file on every
commit, which real-time antivirus scanning can turn into multi-second
stalls if that happens on the same thread that's pumping window messages.
We also turn on WAL mode, which avoids that per-commit file churn entirely.

This is the v1 writer. The app writes through ticker.db.store now; what
remains useful here is the read side, which reads either schema (see below),
and the v1 schema itself, which the migration tests build fixtures from.

Schema:
    sessions(id, start_time, end_time, device_name, device_address, label)
    samples(id, session_id, timestamp, heart_rate, rr_intervals_ms)

rr_intervals_ms is stored as a comma-separated string (often empty -- not
every notification carries RR data). Read it back later with e.g.:

    import sqlite3, pandas as pd
    conn = sqlite3.connect(str(config.DB_PATH))
    df = pd.read_sql_query("SELECT * FROM samples", conn)
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TEXT NOT NULL,
    end_time TEXT,
    device_name TEXT,
    device_address TEXT,
    label TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    timestamp TEXT NOT NULL,
    heart_rate INTEGER NOT NULL,
    rr_intervals_ms TEXT
);

CREATE INDEX IF NOT EXISTS idx_samples_session ON samples(session_id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _open(db_path: Path) -> sqlite3.Connection:
    """Open for writing, creating the file and schema if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _open_existing(db_path: Path) -> Optional[sqlite3.Connection]:
    """Open an existing database, or return None if there isn't one yet.

    The read helpers go through this rather than _open so that merely
    listing sessions never creates a database file or runs the schema as a
    side effect -- pointing them at the wrong path should come back empty,
    not quietly leave a new empty database there.
    """
    if not db_path.exists():
        return None
    return sqlite3.connect(str(db_path))


class AsyncSessionStore:
    """Fire-and-forget session/sample logging on a dedicated writer thread.

    The caller never touches SQLite directly and never blocks: start_session
    / insert_sample / end_session just enqueue work and return immediately.
    A session is identified by a locally-generated `token` (any hashable,
    e.g. an incrementing int) chosen by the caller *before* the DB row
    exists, so logging samples never has to wait on a round trip.
    """

    def __init__(self, db_path: Optional[Path] = None,
                 error_queue: Optional["queue.Queue"] = None):
        self.db_path = Path(db_path) if db_path else config.DB_PATH
        # Optional: the app's message queue, so a writer-thread failure can
        # reach the UI instead of only the log file. Same message shape the
        # heart rate sources use (see hr_source), since it's the same queue.
        self._error_queue = error_queue
        self._ops: "queue.Queue" = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def start_session(self, token, device_name: Optional[str],
                       device_address: Optional[str], label: Optional[str] = None):
        self._ops.put(("start", token, device_name, device_address, label))

    def end_session(self, token):
        self._ops.put(("end", token))

    def insert_sample(self, token, timestamp: str, heart_rate: int,
                       rr_intervals_ms: Optional[List[float]] = None):
        self._ops.put(("sample", token, timestamp, heart_rate, rr_intervals_ms))

    def close(self):
        self._ops.put(("stop",))
        self._thread.join(timeout=5)

    # -- writer thread body --------------------------------------------

    def _report(self, message: str):
        if self._error_queue is not None:
            self._error_queue.put({"type": "error", "message": message})

    def _run(self):
        try:
            conn = _open(self.db_path)
        except Exception as exc:
            # An unwritable HRM_DB_PATH, a full disk, a corrupt file: the
            # thread used to die here and take all logging with it, with no
            # sign anywhere but a traceback nobody reads. Say so, then keep
            # draining ops so callers still never block and close() still
            # returns promptly.
            traceback.print_exc()
            self._report(f"Cannot log to {self.db_path} — {exc}")
            self._drain_until_stop()
            return

        token_to_row: dict = {}
        try:
            while True:
                op = self._ops.get()
                kind = op[0]
                if kind == "stop":
                    break
                try:
                    self._apply(conn, token_to_row, kind, op)
                except Exception:
                    # A single bad op should never take down the writer
                    # thread (and therefore silently stop all future
                    # logging) -- log to stderr and keep going.
                    traceback.print_exc()
        finally:
            conn.close()

    def _drain_until_stop(self):
        """Swallow queued work until close(). Used when there's no database
        to write to -- without it the queue grows for as long as the app runs.
        """
        while True:
            if self._ops.get()[0] == "stop":
                return

    @staticmethod
    def _apply(conn: sqlite3.Connection, token_to_row: dict, kind: str, op: tuple):
        if kind == "start":
            _, token, name, addr, label = op
            cur = conn.execute(
                "INSERT INTO sessions (start_time, device_name, device_address, label) "
                "VALUES (?, ?, ?, ?)",
                (_now_iso(), name, addr, label),
            )
            conn.commit()
            token_to_row[token] = cur.lastrowid

        elif kind == "end":
            _, token = op
            row_id = token_to_row.pop(token, None)
            if row_id is not None:
                conn.execute("UPDATE sessions SET end_time = ? WHERE id = ?", (_now_iso(), row_id))
                conn.commit()

        elif kind == "sample":
            _, token, timestamp, heart_rate, rr_intervals_ms = op
            row_id = token_to_row.get(token)
            if row_id is None:
                return  # session wasn't started (or already ended) -- drop it
            rr_str = ",".join(str(v) for v in rr_intervals_ms) if rr_intervals_ms else ""
            conn.execute(
                "INSERT INTO samples (session_id, timestamp, heart_rate, rr_intervals_ms) "
                "VALUES (?, ?, ?, ?)",
                (row_id, timestamp, heart_rate, rr_str),
            )
            conn.commit()


# -- read-only helpers (used by the CLI summary below and by analysis scripts) --
#
# These read v1 or v2 databases and return the same shape either way, so an
# analysis script written against v1 keeps working after the migration. The
# v2 schema splits one v1 sample into several rows -- heart rate and one per
# RR interval -- so the v2 path reassembles them back into the v1 shape.
# Anything new should read ticker.db.queries directly instead.


def _is_v2(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'observations'"
    ).fetchone() is not None


def list_sessions(db_path: Optional[Path] = None):
    """Return all sessions with a sample count and bpm summary, newest first."""
    conn = _open_existing(Path(db_path) if db_path else config.DB_PATH)
    if conn is None:
        return []
    try:
        if _is_v2(conn):
            return conn.execute(
                """
                SELECT s.id, s.start_ts, s.end_ts, d.name, s.label,
                       COUNT(o.id) AS n_samples,
                       AVG(o.value) AS avg_hr,
                       MIN(o.value) AS min_hr,
                       MAX(o.value) AS max_hr
                FROM sessions s
                LEFT JOIN devices d ON d.id = s.device_id
                LEFT JOIN observations o
                       ON o.session_id = s.id
                      AND o.metric_id = (SELECT id FROM metrics
                                          WHERE name = 'heart_rate_bpm')
                GROUP BY s.id
                ORDER BY s.id DESC
                """
            ).fetchall()
        return conn.execute(
            """
            SELECT s.id, s.start_time, s.end_time, s.device_name, s.label,
                   COUNT(m.id) AS n_samples,
                   AVG(m.heart_rate) AS avg_hr,
                   MIN(m.heart_rate) AS min_hr,
                   MAX(m.heart_rate) AS max_hr
            FROM sessions s
            LEFT JOIN samples m ON m.session_id = s.id
            GROUP BY s.id
            ORDER BY s.id DESC
            """
        ).fetchall()
    finally:
        conn.close()


def get_samples(session_id: int, db_path: Optional[Path] = None):
    conn = _open_existing(Path(db_path) if db_path else config.DB_PATH)
    if conn is None:
        return []
    try:
        if _is_v2(conn):
            return _v2_samples(conn, session_id)
        return conn.execute(
            "SELECT timestamp, heart_rate, rr_intervals_ms FROM samples "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()


def _v2_samples(conn: sqlite3.Connection, session_id: int):
    """Rebuild v1-shaped (timestamp, heart_rate, rr_csv) rows from v2.

    Sessions recorded before the migration are read straight out of
    samples_v1, which the migration leaves untouched, so historical data
    comes back exactly as it did before -- including its original timestamp
    precision and RR grouping.

    For anything recorded since, RR intervals are their own observations and
    are grouped back by timestamp: each beat belongs to the first heart rate
    sample at or after it, which is the inverse of how the beats were placed
    (see ticker.ingest.derive.expand_rr). That grouping is exact for v2 data,
    whose timestamps are millisecond resolution.
    """
    if _has_v1_samples(conn, session_id):
        return conn.execute(
            "SELECT timestamp, heart_rate, rr_intervals_ms FROM samples_v1 "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()

    rows = conn.execute(
        "SELECT m.name, o.ts, o.value FROM observations o "
        "JOIN metrics m ON m.id = o.metric_id "
        "WHERE o.session_id = ? AND m.name IN ('heart_rate_bpm', 'rr_interval_ms') "
        "ORDER BY o.ts, o.id",
        (session_id,),
    ).fetchall()

    samples = [(ts, value) for name, ts, value in rows if name == "heart_rate_bpm"]
    beats = [(ts, value) for name, ts, value in rows if name == "rr_interval_ms"]

    out, beat_index = [], 0
    for position, (ts, hr) in enumerate(samples):
        is_last = position == len(samples) - 1
        carried = []
        while beat_index < len(beats) and (is_last or beats[beat_index][0] <= ts):
            carried.append(beats[beat_index][1])
            beat_index += 1
        out.append((ts, int(hr), ",".join(str(v) for v in carried)))
    return out


def _has_v1_samples(conn: sqlite3.Connection, session_id: int) -> bool:
    """True if this session predates the migration and its original rows are
    still there. The migration preserves session ids, so they line up."""
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'samples_v1'"
    ).fetchone()
    if not present:
        return False
    return conn.execute(
        "SELECT 1 FROM samples_v1 WHERE session_id = ? LIMIT 1", (session_id,)
    ).fetchone() is not None


if __name__ == "__main__":
    print(f"Database: {config.DB_PATH}\n")
    rows = list_sessions()
    if not rows:
        print("No sessions logged yet.")
    for r in rows:
        sid, start, end, name, label, n, avg_hr, min_hr, max_hr = r
        end_str = end or "(in progress)"
        avg_str = f"{avg_hr:.0f}" if avg_hr is not None else "-"
        label_str = f"  \"{label}\"" if label else ""
        print(f"#{sid}  {start} -> {end_str}  device={name}{label_str}  "
              f"samples={n}  avg={avg_str}  min={min_hr}  max={max_hr}")
