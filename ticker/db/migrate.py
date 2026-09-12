"""
Forward-only schema migrations.

One linear history, applied in order, each inside its own transaction. There
is no downgrade path, so
the safety net is a file copy taken before anything is touched, plus
verification assertions that run *inside* the transaction and roll the whole
thing back if the migrated data doesn't match the data it came from.

Version 0 is an empty file, 1 is the v1 heart-rate schema, 2 is the
multi-source schema. A pre-existing v1 database has no schema_version table;
it is adopted at version 1 rather than rebuilt, which is why 0001 is written
with IF NOT EXISTS throughout.

Why not executescript()
-----------------------
sqlite3.Connection.executescript() commits any open transaction before it
runs, which would defeat the point of wrapping a migration in one. And under
the default isolation_level the driver only opens transactions for DML, so
DDL would run in autocommit and a failure would leave half a schema behind.
Both are fixed the same way: isolation_level = None plus explicit BEGIN, and
statements fed in one at a time.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from ticker import config as tconfig
from ticker.ingest.derive import expand_rr_csv
from ticker.model import iso_utc, now_iso, parse_iso

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# AVG(heart_rate) over ints vs AVG(value) over REALs is the same arithmetic
# in a different type; allow for float representation only, not for drift.
AVG_TOLERANCE = 1e-9

# Rows per executemany. Large enough that the per-call overhead disappears,
# small enough that a long RR expansion doesn't build one huge list.
BATCH = 1000


class MigrationError(RuntimeError):
    """A migration failed and was rolled back."""


# -- statement splitting -------------------------------------------------

def split_statements(sql: str) -> Iterator[str]:
    """Yield complete SQL statements from a script.

    sqlite3.complete_statement understands string literals, so this does not
    trip over a semicolon inside quotes the way splitting on ';' would.
    """
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                yield statement
            buffer = ""
    tail = buffer.strip()
    if tail and not tail.startswith("--"):
        raise MigrationError("trailing incomplete SQL statement: {!r}".format(tail[:80]))


def _run_script(conn: sqlite3.Connection, sql: str) -> None:
    for statement in split_statements(sql):
        conn.execute(statement)


# -- v1 -> v2 data copy --------------------------------------------------

def _metric_ids(conn: sqlite3.Connection) -> Dict[str, int]:
    return {name: mid for name, mid in conn.execute("SELECT name, id FROM metrics")}


def _normalize_ts(value: Optional[str], where: str,
                  allow_null: bool = False) -> Optional[str]:
    if value is None or value == "":
        if allow_null:
            return None
        raise MigrationError("missing required timestamp at " + where)
    try:
        return iso_utc(parse_iso(value))
    except ValueError as exc:
        raise MigrationError(
            "unparseable timestamp at {}: {!r} ({})".format(where, value, exc))


def _copy_v1_data(conn: sqlite3.Connection) -> None:
    """Move sessions_v1/samples_v1 into the v2 shape.

    Done in Python rather than SQL because RR strings expand into one row
    per beat and v1 wrote
    session times at second resolution and sample times at millisecond
    resolution, both of which have to become one canonical spelling before
    they share a column with everything else.
    """
    now = now_iso()
    metrics = _metric_ids(conn)
    hr_metric = metrics["heart_rate_bpm"]
    rr_metric = metrics["rr_interval_ms"]

    # Named to match what the app looks up: it calls ensure_source() with
    # this same name, and a
    # different one there would create a second source row, splitting one
    # strap's history from its new data.
    cur = conn.execute(
        "INSERT INTO sources (kind, vendor, display_name, config_json, enabled, created_at) "
        "VALUES ('stream', 'ble', ?, '{}', 1, ?)",
        (tconfig.source_display_name("ble"), now),
    )
    source_id = cur.lastrowid

    # Devices: one per distinct address seen in v1.
    conn.executemany(
        "INSERT INTO devices (source_id, name, address) VALUES (?, ?, ?)",
        [
            (source_id, name, address)
            for name, address in conn.execute(
                "SELECT device_name, device_address FROM sessions_v1 "
                "WHERE device_address IS NOT NULL "
                "GROUP BY device_address"
            )
        ],
    )
    device_by_address = {
        address: did
        for did, address in conn.execute(
            "SELECT id, address FROM devices WHERE source_id = ?", (source_id,)
        )
    }

    # Sessions keep their v1 id. Copying samples.session_id
    # straight across, which is only correct while the new AUTOINCREMENT ids
    # happen to line up with the old ones -- one deleted v1 session and every
    # observation after it attaches to the wrong session. Carrying the id
    # explicitly makes that impossible, and the table is empty, so it's free.
    session_rows = []
    for sid, start_time, end_time, _name, device_address, label in conn.execute(
        "SELECT id, start_time, end_time, device_name, device_address, label "
        "FROM sessions_v1 ORDER BY id"
    ):
        session_rows.append((
            sid,
            source_id,
            device_by_address.get(device_address),
            _normalize_ts(start_time, "sessions_v1.id={}.start_time".format(sid)),
            _normalize_ts(end_time, "sessions_v1.id={}.end_time".format(sid),
                          allow_null=True),
            "manual",
            label,
        ))
    conn.executemany(
        "INSERT INTO sessions (id, source_id, device_id, start_ts, end_ts, kind, label) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        session_rows,
    )

    insert_obs = (
        "INSERT INTO observations "
        "(source_id, metric_id, session_id, ts, value, external_id, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    batch: List[tuple] = []
    # A second cursor, so iterating samples isn't disturbed by the inserts.
    read = conn.cursor()
    for row_id, session_id, timestamp, heart_rate, rr_csv in read.execute(
        "SELECT id, session_id, timestamp, heart_rate, rr_intervals_ms "
        "FROM samples_v1 ORDER BY id"
    ):
        ts = parse_iso(_normalize_ts(timestamp, "samples_v1.id={}".format(row_id)))
        # external_id carries the v1 row id rather than ''. Two
        # beats can genuinely land on the same millisecond -- RR timestamps
        # are reconstructed by summing backwards, not measured, so
        # consecutive notifications' windows can overlap -- and with '' the
        # natural-key unique index turns that into either an aborted
        # migration or a silently dropped beat. The v1 row id is a stable id
        # for the datum, which is what the column is for. Nothing re-ingests
        # pre-migration data, so it can't collide with a live source later.
        batch.append((source_id, hr_metric, session_id, iso_utc(ts),
                      float(heart_rate), "v1:{}".format(row_id), now))
        for beat_index, (beat_ts, rr) in enumerate(expand_rr_csv(ts, rr_csv or "")):
            batch.append((source_id, rr_metric, session_id, iso_utc(beat_ts),
                          float(rr), "v1:{}:{}".format(row_id, beat_index), now))
        if len(batch) >= BATCH:
            conn.executemany(insert_obs, batch)
            batch = []
    if batch:
        conn.executemany(insert_obs, batch)


def _verify_v1_migration(conn: sqlite3.Connection) -> None:
    """Section 3 step 5, as an assertion rather than a checklist item.

    Runs inside the migration's transaction, so a mismatch rolls the whole
    migration back and leaves a v1 database exactly as it was.
    """
    metrics = _metric_ids(conn)
    problems: List[str] = []

    def one(sql: str, params: Sequence = ()):
        return conn.execute(sql, params).fetchone()

    # Sessions: count preserved, ids preserved.
    (v1_sessions,) = one("SELECT COUNT(*) FROM sessions_v1")
    (v2_sessions,) = one("SELECT COUNT(*) FROM sessions")
    if v1_sessions != v2_sessions:
        problems.append("session count {} != v1 {}".format(v2_sessions, v1_sessions))
    (orphans,) = one(
        "SELECT COUNT(*) FROM sessions_v1 s "
        "WHERE NOT EXISTS (SELECT 1 FROM sessions n WHERE n.id = s.id)")
    if orphans:
        problems.append(
            "{} v1 sessions have no v2 row with the same id".format(orphans))

    # Heart rate: count, time bounds and mean all preserved.
    hr = metrics["heart_rate_bpm"]
    v1_n, v1_avg = one("SELECT COUNT(*), AVG(heart_rate) FROM samples_v1")
    v2_n, v2_avg = one(
        "SELECT COUNT(*), AVG(value) FROM observations WHERE metric_id = ?", (hr,))
    if v1_n != v2_n:
        problems.append(
            "heart_rate_bpm count {} != v1 samples {}".format(v2_n, v1_n))
    if v1_n and abs((v1_avg or 0.0) - (v2_avg or 0.0)) > AVG_TOLERANCE:
        problems.append(
            "heart_rate_bpm mean {!r} != v1 mean {!r}".format(v2_avg, v1_avg))

    if v1_n and v1_n == v2_n:
        v1_min, v1_max = one("SELECT MIN(timestamp), MAX(timestamp) FROM samples_v1")
        v2_min, v2_max = one(
            "SELECT MIN(ts), MAX(ts) FROM observations WHERE metric_id = ?", (hr,))
        for label, v1_value, v2_value in (("MIN(ts)", v1_min, v2_min),
                                          ("MAX(ts)", v1_max, v2_max)):
            # Compare as instants, not strings: v1 wrote two precisions and
            # v2 writes one, so '...T00:00:00+00:00' and '...T00:00:00.000+00:00'
            # are the same moment spelled differently.
            if parse_iso(v1_value) != parse_iso(v2_value):
                problems.append("heart_rate_bpm {} {} != v1 {}".format(
                    label, v2_value, v1_value))

    # RR: one observation per interval in the v1 strings.
    expected_rr = 0
    for (rr_csv,) in conn.execute(
            "SELECT rr_intervals_ms FROM samples_v1 "
            "WHERE rr_intervals_ms IS NOT NULL AND rr_intervals_ms != ''"):
        expected_rr += len([v for v in rr_csv.split(",") if v])
    (actual_rr,) = one(
        "SELECT COUNT(*) FROM observations WHERE metric_id = ?",
        (metrics["rr_interval_ms"],))
    if expected_rr != actual_rr:
        problems.append("rr_interval_ms count {} != {} v1 intervals".format(
            actual_rr, expected_rr))

    # No observation may point at a session that isn't there.
    (misattached,) = one(
        "SELECT COUNT(*) FROM observations WHERE session_id IS NOT NULL AND "
        "session_id NOT IN (SELECT id FROM sessions)")
    if misattached:
        problems.append(
            "{} observations reference a missing session".format(misattached))

    if problems:
        raise MigrationError(
            "migration verification failed, rolling back:\n  - "
            + "\n  - ".join(problems))


def _post_0002(conn: sqlite3.Connection) -> None:
    _copy_v1_data(conn)
    _verify_v1_migration(conn)


# Python work that has to happen inside a migration's transaction, after its
# SQL has run. Keyed by version.
POST_STEPS: Dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _post_0002,
}


# -- runner --------------------------------------------------------------

_FILENAME = re.compile(r"^(\d{4})_([A-Za-z0-9_]+)\.sql$")


def available() -> List[Tuple[int, str, Path]]:
    """(version, name, path) for every migration on disk, in order."""
    found = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if not match:
            raise MigrationError(
                "migration filename not NNNN_name.sql: " + path.name)
        found.append((int(match.group(1)), match.group(2), path))
    versions = [v for v, _, _ in found]
    if versions != sorted(set(versions)):
        raise MigrationError(
            "duplicate or unordered migration versions: {}".format(versions))
    return found


LATEST_VERSION = 3


def current_version(conn: sqlite3.Connection) -> int:
    """Highest applied version. 0 for an empty or unversioned database.

    A v1 database predates schema_version entirely, so 'no such table' is not
    the same as 'nothing applied' -- if the v1 tables are there, it is at 1.
    """
    tables = {
        name for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "schema_version" in tables:
        (version,) = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
        return version
    if {"sessions", "samples"} <= tables:
        return 1
    return 0


def open_db(db_path: Path) -> sqlite3.Connection:
    """Open with the required SQLite pragmas and explicit transactions.

    foreign_keys and journal_mode are per-connection or persistent settings
    that cannot be changed inside a transaction, so they are set here and
    nowhere else.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # explicit BEGIN/COMMIT; see module docstring
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous  = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def backup_path(db_path: Path, version: int = 1) -> Path:
    """The recovery snapshot taken before upgrading ``version``.

    The schema version is part of the name so a later migration never
    overwrites the only recovery point from an earlier one.
    """
    return Path(str(db_path) + ".v{}.bak".format(version))


def snapshot(conn: sqlite3.Connection, destination: Path) -> Path:
    """Make a complete SQLite snapshot, including committed WAL pages."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = destination.with_name(destination.name + ".tmp")
    if scratch.exists():
        scratch.unlink()
    target = sqlite3.connect(str(scratch))
    try:
        conn.backup(target)
        if target.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise MigrationError("backup integrity check failed")
    finally:
        target.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(destination) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    scratch.replace(destination)
    return destination


def migrate(db_path: Path, backup: bool = True) -> List[int]:
    """Bring the database at db_path up to the latest version.

    Returns the versions applied, newest last; an empty list means it was
    already current. Each migration runs in one transaction and rolls back
    whole on any failure, including a failed verification assertion.
    """
    db_path = Path(db_path)
    existed = db_path.exists()
    conn = open_db(db_path)
    try:
        version = current_version(conn)
        if version > LATEST_VERSION:
            raise MigrationError(
                "database schema version {} is newer than this Ticker "
                "supports ({}); upgrade Ticker before opening it".format(
                    version, LATEST_VERSION))
        pending = [m for m in available() if m[0] > version]
        if not pending:
            return []

        # Section 3 step 1: copy the file before touching anything. Only
        # worth doing when there was already data in it.
        if backup and existed and version >= 1:
            snapshot(conn, backup_path(db_path, version))

        applied = []
        for number, name, path in pending:
            _apply_one(conn, number, name, path)
            applied.append(number)
        return applied
    finally:
        conn.close()


def _apply_one(conn: sqlite3.Connection, version: int, name: str, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    conn.execute("BEGIN")
    try:
        _run_script(conn, sql)
        # schema_version is part of the v2 schema, but 0001 has to record
        # itself too, so ensure it exists before the first INSERT into it.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "    version     INTEGER NOT NULL,"
            "    applied_at  TEXT    NOT NULL)")
        post = POST_STEPS.get(version)
        if post is not None:
            post(conn)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, now_iso()))
        conn.execute("COMMIT")
    except Exception as exc:
        conn.execute("ROLLBACK")
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError("migration {:04d}_{} failed: {}".format(
            version, name, exc)) from exc
