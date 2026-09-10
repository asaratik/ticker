"""
The agent's local spool.

The machine near the strap does not have the database. What it has is this:
a small SQLite file that everything is written to *before* the network is
attempted, drained in order once the server answers, and deleted only once
the server has acknowledged it. Server down, laptop lid closed, Wi-Fi gone
-- the strap keeps recording and the rows arrive later.

Why SQLite for something this simple
------------------------------------
A JSONL file with an offset would be smaller code and would lose data on
exactly the failure this exists for: a half-written line at the moment the
power goes. The spool's whole job is to survive an ungraceful stop, so it
gets a transaction rather than a flush.

Ordering and idempotency
------------------------
Rows drain in insertion order, which is arrival order, which is the order
the server's writer wants -- a session is opened before the observations
that reference it. A batch is deleted only after the server has taken it,
so a crash between POST and acknowledgement re-sends; the server's upsert
makes the replay a no-op. Section 10's 'a replayed spool is harmless' is
that property, and it is the reason this can be careless in the safe
direction.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ticker import config as tconfig
from ticker.api import wire
from ticker.model import Observation, SessionRecord, iso_utc, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS spool (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,          -- 'obs' | 'session' | 'session_end'
    payload   TEXT NOT NULL,          -- the JSON this becomes on the wire
    queued_at TEXT NOT NULL
);
"""

# Kinds, spelled once.
OBS = "obs"
SESSION = "session"
SESSION_END = "session_end"


class Spool:
    """A durable FIFO of things waiting to reach the server.

    Thread-safe: the stream thread appends while the uplink thread drains,
    and both go through one connection under one lock. That is enough --
    the spool is written at a few hundred rows a second at worst, and a
    lock held for one executemany is not what limits it.
    """

    def __init__(self, path: Optional[Path] = None,
                 max_rows: Optional[int] = None):
        self.path = Path(path) if path else tconfig.AGENT_SPOOL_PATH
        self.max_rows = (tconfig.AGENT_SPOOL_MAX_ROWS if max_rows is None
                         else max_rows)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                     isolation_level=None)
        # WAL for the same reason the main database uses it: an append while
        # a drain is reading should not block on it. synchronous=NORMAL is
        # the durability the spool needs -- a commit survives a process
        # crash, which is the case this exists for; surviving a power cut
        # mid-commit would cost an fsync per batch at 1 Hz.
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA busy_timeout = {}".format(
            tconfig.BUSY_TIMEOUT_MS))
        self._conn.executescript(SCHEMA)
        self.dropped = 0

    # -- writing ---------------------------------------------------------

    def add_observations(self, batch: Sequence[Observation]) -> int:
        return self._append(OBS, [wire.observation_to_json(o) for o in batch])

    def add_session(self, record: SessionRecord,
                    device: Optional[Dict[str, Any]] = None) -> int:
        payload = wire.session_to_json(record)
        if device:
            payload["device"] = device
        return self._append(SESSION, [payload])

    def add_session_end(self, key: str, end_ts=None) -> int:
        payload: Dict[str, Any] = {"key": key}
        if end_ts is not None:
            payload["end_ts"] = iso_utc(end_ts)
        return self._append(SESSION_END, [payload])

    def _append(self, kind: str, payloads: Sequence[Dict[str, Any]]) -> int:
        if not payloads:
            return 0
        queued = now_iso()
        rows = [(kind, json.dumps(p, separators=(",", ":")), queued)
                for p in payloads]
        with self._lock:
            self._conn.execute("BEGIN")
            self._conn.executemany(
                "INSERT INTO spool (kind, payload, queued_at) VALUES (?,?,?)",
                rows)
            self._conn.execute("COMMIT")
            self._trim_locked()
        return len(rows)

    def _trim_locked(self) -> None:
        """Enforce the row cap, oldest first.

        A cap is not optional on a machine that may be offline for a week:
        without one the spool grows until the disk does not have room for
        the thing the spool exists to protect. Dropping the oldest rather
        than refusing new ones keeps the recent past, which is the part
        anyone will look at.
        """
        if self.max_rows <= 0:
            return
        total = self._conn.execute("SELECT COUNT(*) FROM spool").fetchone()[0]
        excess = total - self.max_rows
        if excess <= 0:
            return
        self._conn.execute(
            "DELETE FROM spool WHERE id IN "
            "(SELECT id FROM spool ORDER BY id LIMIT ?)", (excess,))
        self.dropped += excess

    # -- draining --------------------------------------------------------

    def take(self, limit: int) -> Tuple[List[int], Dict[str, Any]]:
        """The next batch to post, as (ids, payload body).

        The batch is cut after a session end rather than running past one.
        Within one POST the server opens sessions, writes observations, then
        closes sessions -- so observations that belong *after* a close must
        not share a request with it, or they would be filed under a session
        that request had already ended.

        Nothing is deleted here. ack() does that, once the server has it.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, payload FROM spool ORDER BY id LIMIT ?",
                (limit,)).fetchall()

        ids: List[int] = []
        observations: List[Dict[str, Any]] = []
        sessions: List[Dict[str, Any]] = []
        closed: List[str] = []
        device: Optional[Dict[str, Any]] = None

        for row_id, kind, payload in rows:
            item = json.loads(payload)
            ids.append(row_id)
            if kind == OBS:
                observations.append(item)
            elif kind == SESSION:
                # The device rides along with whichever session announced
                # it; the server keys the devices row off it.
                device = item.pop("device", None) or device
                sessions.append(item)
            elif kind == SESSION_END:
                closed.append(item["key"])
                break

        body: Dict[str, Any] = {}
        if observations:
            body["observations"] = observations
        if sessions:
            body["sessions"] = sessions
        if closed:
            body["closed_sessions"] = closed
        if device:
            body["device"] = device
        return ids, body

    def ack(self, ids: Sequence[int]) -> int:
        """Forget rows the server has taken. Called only after a 2xx."""
        if not ids:
            return 0
        with self._lock:
            self._conn.execute("BEGIN")
            self._conn.executemany("DELETE FROM spool WHERE id = ?",
                                   [(i,) for i in ids])
            self._conn.execute("COMMIT")
        return len(ids)

    # -- status ----------------------------------------------------------

    def pending(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM spool").fetchone()[0]

    def oldest(self) -> Optional[str]:
        """When the oldest undelivered row was queued, for the status line."""
        with self._lock:
            row = self._conn.execute(
                "SELECT queued_at FROM spool ORDER BY id LIMIT 1").fetchone()
        return row[0] if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
