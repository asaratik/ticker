"""
Read-only database access for the MCP server.

An agent-facing surface gets a stronger guarantee than the rest of Ticker:
nothing reachable through MCP can change the data. Three layers, each
enough on its own for what it covers:

* The file is opened `mode=rw` rather than the default `rwc`, so a database
  that isn't there yet is reported rather than silently created empty.
* `PRAGMA query_only` refuses every write the connection could attempt.
* An authorizer allows reads and nothing else -- no ATTACH (which could
  create a file anywhere), no PRAGMA that changes state, no temp tables.

Why not `mode=ro`: a read-only connection to a WAL database needs the -shm
file to exist already, and it doesn't whenever no other process has the
database open -- which is exactly when an agent is most likely to be asking.

Every call also runs against a deadline enforced by a progress handler, so
one expensive query -- an agent's hand-written SQL, or a year of 1 Hz heart
rate -- can't wedge the server for everyone else.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Tuple

from ticker import config as tconfig
from ticker.db import migrate

# How long one tool call may spend in SQLite. Generous for anything the
# indexes serve; short enough that a runaway query fails while the agent is
# still waiting for it.
DEFAULT_BUDGET_SEC = 10.0

# SQLite VM instructions between deadline checks. Cheap enough to be
# invisible, frequent enough that a timeout lands within milliseconds.
PROGRESS_STEPS = 10000

_ALLOWED_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    # WITH RECURSIVE. Not exported by every Python's sqlite3 module.
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
})

# Pragmas that only describe the schema. Everything else -- including ones
# that merely read a setting -- is refused, because several of them write
# when given an argument and the authorizer can't always tell which form it
# is looking at.
_SCHEMA_PRAGMAS = frozenset({
    "table_info", "table_xinfo", "index_list", "index_info", "index_xinfo",
    "foreign_key_list",
})


class DatabaseUnavailable(Exception):
    """No database yet, or one this version can't serve."""


class QueryTimeout(Exception):
    """A call ran past its time budget and was interrupted."""


def _authorize(action, arg1, arg2, dbname, source):
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() in _SCHEMA_PRAGMAS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class _Clock:
    """The deadline a connection's progress handler checks."""

    def __init__(self):
        self.deadline: Optional[float] = None
        self.expired = False

    def check(self) -> int:
        if self.deadline is not None and time.monotonic() > self.deadline:
            self.expired = True
            return 1                    # non-zero interrupts the statement
        return 0


def open_readonly(path: Path) -> Tuple[sqlite3.Connection, _Clock]:
    """Open `path` for reading only, or say why it can't be."""
    path = Path(path)
    if not path.exists():
        raise DatabaseUnavailable(
            "No Ticker database at {} yet. Connect a source first -- run the "
            "Ticker app, `ticker-setup add oura`, or `ticker-import` -- or "
            "point ticker-mcp at another file with --db.".format(path))
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)
    except sqlite3.Error as exc:
        raise DatabaseUnavailable("cannot open {}: {}".format(path, exc))
    try:
        conn.isolation_level = None
        conn.execute("PRAGMA busy_timeout = {}".format(tconfig.BUSY_TIMEOUT_MS))
        conn.execute("PRAGMA query_only = ON")
        version = migrate.current_version(conn)
    except sqlite3.Error as exc:
        conn.close()
        raise DatabaseUnavailable("cannot read {}: {}".format(path, exc))
    if version < migrate.LATEST_VERSION:
        conn.close()
        # Upgrading is a write, and this process doesn't make those. Any
        # other Ticker command migrates on start, with its backup and checks.
        raise DatabaseUnavailable(
            "The database at {} is at schema version {} and needs upgrading "
            "to {}. Run the Ticker app or any ticker command once (for "
            "example `ticker-setup list`); it migrates in place after taking "
            "a backup.".format(path, version, migrate.LATEST_VERSION))
    clock = _Clock()
    conn.set_progress_handler(clock.check, PROGRESS_STEPS)
    # Last: from here on the connection can only read.
    conn.set_authorizer(_authorize)
    return conn, clock


class ReadOnlyDatabase:
    """Lazily opened, per-thread read-only connections to one file.

    Lazy so that the server starts -- and can tell the agent what's wrong --
    even before the database exists; each call retries the open until it
    succeeds. Per-thread because the HTTP transport answers on several
    threads and sqlite3 connections must not be shared across them.
    """

    def __init__(self, path: Optional[Path] = None,
                 budget_sec: float = DEFAULT_BUDGET_SEC):
        self.path = Path(path) if path else tconfig.DB_PATH
        self.budget_sec = budget_sec
        self._local = threading.local()

    @contextmanager
    def session(self, budget_sec: Optional[float] = None
                ) -> Iterator[sqlite3.Connection]:
        """A connection whose statements are interrupted past the budget."""
        conn, clock = self._connection()
        clock.expired = False
        seconds = self.budget_sec if budget_sec is None else budget_sec
        clock.deadline = time.monotonic() + seconds
        try:
            yield conn
        except sqlite3.OperationalError as exc:
            if clock.expired:
                raise QueryTimeout(
                    "stopped after {:g} s -- narrow the time window, add a "
                    "metric_id and ts range to the WHERE clause, or ask for "
                    "coarser buckets".format(seconds)) from exc
            raise
        finally:
            clock.deadline = None

    def close(self) -> None:
        """Close this thread's connection, if it has one."""
        held = getattr(self._local, "held", None)
        if held is not None:
            held[0].close()
            self._local.held = None

    def _connection(self) -> Tuple[sqlite3.Connection, _Clock]:
        held = getattr(self._local, "held", None)
        if held is None:
            held = open_readonly(self.path)
            self._local.held = held
        return held
