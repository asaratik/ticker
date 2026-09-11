"""
Tests for the v1 -> v2 migration.

The migration is the one irreversible step in the project, so these lean
hard on two properties: it must be idempotent, and any failure must leave a
v1 database exactly as it was found. Several tests build their v1 fixture
through the real v1 writer (storage.AsyncSessionStore) rather than hand-
written SQL, so they break if v1's actual on-disk shape ever differs from
what the migration assumes.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

import storage
from ticker.db import migrate
from ticker.ingest.derive import expand_rr_csv
from ticker.model import parse_iso

UTC = timezone.utc


def make_v1(db_path, sessions):
    """Build a v1 database with the v1 writer.

    sessions: [(label, device_name, device_address, [(ts, hr, [rr...]), ...])]
    """
    store = storage.AsyncSessionStore(db_path)
    try:
        for i, (label, name, address, samples) in enumerate(sessions):
            store.start_session(i, name, address, label=label)
            for ts, hr, rr in samples:
                store.insert_sample(i, ts, hr, rr)
            store.end_session(i)
    finally:
        store.close()


def connect(db_path):
    return sqlite3.connect(str(db_path))


# -- fresh databases -----------------------------------------------------

def test_fresh_database_gets_the_full_schema(tmp_path):
    db = tmp_path / "fresh.sqlite3"
    assert migrate.migrate(db) == [1, 2, 3]

    conn = connect(db)
    try:
        tables = {n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"sources", "devices", "metrics", "sessions", "observations",
                "sync_state", "raw_payloads", "rollups_daily",
                "schema_version"} <= tables
        assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 15
        assert migrate.current_version(conn) == migrate.LATEST_VERSION
    finally:
        conn.close()


def test_migrating_twice_is_a_no_op(tmp_path):
    db = tmp_path / "fresh.sqlite3"
    migrate.migrate(db)
    assert migrate.migrate(db) == []


def test_no_backup_is_written_for_a_database_that_did_not_exist(tmp_path):
    db = tmp_path / "fresh.sqlite3"
    migrate.migrate(db)
    assert not migrate.backup_path(db).exists()


def test_unique_index_makes_a_repeated_insert_idempotent(tmp_path):
    db = tmp_path / "fresh.sqlite3"
    migrate.migrate(db)
    conn = migrate.open_db(db)
    try:
        conn.execute(
            "INSERT INTO sources (kind, vendor, display_name, config_json, "
            "enabled, created_at) VALUES ('pull','oura','Ring','{}',1,'x')")
        row = ("SELECT id FROM sources WHERE vendor='oura'")
        source_id = conn.execute(row).fetchone()[0]
        metric_id = conn.execute(
            "SELECT id FROM metrics WHERE name='spo2_pct'").fetchone()[0]
        args = (source_id, metric_id, "2026-01-01T00:00:00.000+00:00", 97.0, "",
                "2026-01-01T00:00:00.000+00:00")
        sql = ("INSERT INTO observations (source_id, metric_id, ts, value, "
               "external_id, ingested_at) VALUES (?,?,?,?,?,?)")
        conn.execute(sql, args)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, args)
    finally:
        conn.close()


# -- v1 data -------------------------------------------------------------

def test_v1_heart_rate_becomes_observations(tmp_path):
    db = tmp_path / "hrm.sqlite3"
    make_v1(db, [("ride", "HRM-Dual", "AA:BB", [
        ("2026-01-01T00:00:00.000+00:00", 80, []),
        ("2026-01-01T00:00:01.000+00:00", 82, []),
    ])])
    assert migrate.migrate(db) == [2, 3]

    conn = connect(db)
    try:
        rows = conn.execute(
            "SELECT o.ts, o.value FROM observations o JOIN metrics m "
            "ON m.id = o.metric_id WHERE m.name = 'heart_rate_bpm' "
            "ORDER BY o.ts").fetchall()
        assert rows == [("2026-01-01T00:00:00.000+00:00", 80.0),
                        ("2026-01-01T00:00:01.000+00:00", 82.0)]
    finally:
        conn.close()


def test_rr_intervals_expand_to_one_observation_each(tmp_path):
    db = tmp_path / "hrm.sqlite3"
    make_v1(db, [("ride", "S", "AA", [
        ("2026-01-01T00:00:10.000+00:00", 80, [750.0, 760.0, 740.0]),
    ])])
    migrate.migrate(db)

    conn = connect(db)
    try:
        rows = conn.execute(
            "SELECT o.ts, o.value FROM observations o JOIN metrics m "
            "ON m.id = o.metric_id WHERE m.name = 'rr_interval_ms' "
            "ORDER BY o.ts").fetchall()
    finally:
        conn.close()

    assert [v for _, v in rows] == [750.0, 760.0, 740.0]
    # Timestamps walk backwards from the sample time by summing intervals:
    # the newest interval lands on the sample timestamp itself.
    times = [parse_iso(ts) for ts, _ in rows]
    assert times[2] == datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC)
    assert (times[2] - times[1]).total_seconds() == pytest.approx(0.740)
    assert (times[1] - times[0]).total_seconds() == pytest.approx(0.760)


def test_expand_rr_csv_handles_empty_and_ragged_input():
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    assert expand_rr_csv(ts, "") == []
    # A trailing comma is what a v1 join of an empty tail looks like.
    assert [v for _, v in expand_rr_csv(ts, "750.0,")] == [750.0]


def test_sessions_keep_their_v1_ids_across_a_gap(tmp_path):
    """Copying samples.session_id to newly numbered sessions silently
    misattributes every observation after a deleted session. Ids are carried
    explicitly instead, so a gap changes nothing."""
    db = tmp_path / "hrm.sqlite3"
    make_v1(db, [
        ("first", "S", "AA", [("2026-01-01T00:00:00.000+00:00", 70, [])]),
        ("deleted", "S", "AA", [("2026-01-01T01:00:00.000+00:00", 80, [])]),
        ("third", "S", "AA", [("2026-01-01T02:00:00.000+00:00", 90, [])]),
    ])
    conn = connect(db)
    conn.execute("DELETE FROM samples WHERE session_id = 2")
    conn.execute("DELETE FROM sessions WHERE id = 2")
    conn.commit()
    conn.close()

    migrate.migrate(db)

    conn = connect(db)
    try:
        assert conn.execute(
            "SELECT id, label FROM sessions ORDER BY id").fetchall() == [
                (1, "first"), (3, "third")]
        # 90 bpm came from session 3 and must still be attached to it.
        assert conn.execute(
            "SELECT session_id FROM observations WHERE value = 90.0").fetchone() == (3,)
    finally:
        conn.close()


def test_devices_are_deduplicated_by_address(tmp_path):
    db = tmp_path / "hrm.sqlite3"
    make_v1(db, [
        ("a", "HRM-Dual", "AA:BB", [("2026-01-01T00:00:00.000+00:00", 70, [])]),
        ("b", "HRM-Dual", "AA:BB", [("2026-01-01T01:00:00.000+00:00", 71, [])]),
        ("c", "Polar H10", "CC:DD", [("2026-01-01T02:00:00.000+00:00", 72, [])]),
    ])
    migrate.migrate(db)

    conn = connect(db)
    try:
        devices = conn.execute(
            "SELECT name, address FROM devices ORDER BY address").fetchall()
        assert devices == [("HRM-Dual", "AA:BB"), ("Polar H10", "CC:DD")]
        # Both of the HRM-Dual sessions point at the same device row.
        linked = conn.execute(
            "SELECT COUNT(DISTINCT device_id) FROM sessions "
            "WHERE label IN ('a','b')").fetchone()[0]
        assert linked == 1
    finally:
        conn.close()


def test_v1_second_resolution_timestamps_are_normalised(tmp_path):
    """v1 wrote session times with timespec='seconds' and sample times with
    'milliseconds'. They share a column now, so they must share a spelling."""
    db = tmp_path / "hrm.sqlite3"
    make_v1(db, [("x", "S", "AA", [("2026-01-01T00:00:00.000+00:00", 70, [])])])
    migrate.migrate(db)

    conn = connect(db)
    try:
        start_ts, = conn.execute("SELECT start_ts FROM sessions").fetchone()
        assert start_ts.endswith("+00:00")
        assert len(start_ts) == len("2026-01-01T00:00:00.000+00:00")
    finally:
        conn.close()


def test_v1_tables_are_kept_not_dropped(tmp_path):
    # Section 3 step 6: dropping them is a separate release, so a bad
    # migration stays recoverable in the field.
    db = tmp_path / "hrm.sqlite3"
    make_v1(db, [("x", "S", "AA", [("2026-01-01T00:00:00.000+00:00", 70, [])])])
    migrate.migrate(db)

    conn = connect(db)
    try:
        tables = {n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"sessions_v1", "samples_v1"} <= tables
        assert conn.execute("SELECT COUNT(*) FROM samples_v1").fetchone() == (1,)
    finally:
        conn.close()


def test_a_backup_is_taken_before_touching_v1_data(tmp_path):
    db = tmp_path / "hrm.sqlite3"
    make_v1(db, [("x", "S", "AA", [("2026-01-01T00:00:00.000+00:00", 70, [])])])
    migrate.migrate(db)

    backup = migrate.backup_path(db)
    assert backup.exists()
    conn = connect(backup)
    try:
        # The backup is still a v1 database: v1 tables, no observations.
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone() == (1,)
        tables = {n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "observations" not in tables
    finally:
        conn.close()


# -- verification and rollback ------------------------------------------

def test_failed_verification_rolls_the_whole_migration_back(tmp_path, monkeypatch):
    db = tmp_path / "hrm.sqlite3"
    make_v1(db, [("x", "S", "AA", [
        ("2026-01-01T00:00:00.000+00:00", 70, [750.0]),
        ("2026-01-01T00:00:01.000+00:00", 71, []),
    ])])

    real_copy = migrate._copy_v1_data

    def lossy(conn):
        real_copy(conn)
        conn.execute("DELETE FROM observations WHERE value = 70.0")

    monkeypatch.setattr(migrate, "_copy_v1_data", lossy)
    with pytest.raises(migrate.MigrationError) as excinfo:
        migrate.migrate(db, backup=False)
    assert "count" in str(excinfo.value)

    # DDL included: the database has to be untouched, not half-converted.
    conn = connect(db)
    try:
        tables = {n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "observations" not in tables
        assert "sessions_v1" not in tables
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone() == (2,)
    finally:
        conn.close()

    # And a corrected run still works on the same file.
    monkeypatch.setattr(migrate, "_copy_v1_data", real_copy)
    assert migrate.migrate(db, backup=False) == [2, 3]


def test_verification_checks_counts_bounds_and_mean(tmp_path):
    db = tmp_path / "hrm.sqlite3"
    make_v1(db, [("x", "S", "AA", [
        ("2026-01-01T00:00:00.000+00:00", 60, [800.0, 810.0]),
        ("2026-01-01T00:30:00.000+00:00", 100, [600.0]),
    ])])
    migrate.migrate(db)

    conn = connect(db)
    try:
        # Section 3 step 5's three checks, from the outside.
        v1_n, v1_avg, v1_min, v1_max = conn.execute(
            "SELECT COUNT(*), AVG(heart_rate), MIN(timestamp), MAX(timestamp) "
            "FROM samples_v1").fetchone()
        v2_n, v2_avg, v2_min, v2_max = conn.execute(
            "SELECT COUNT(*), AVG(o.value), MIN(o.ts), MAX(o.ts) FROM observations o "
            "JOIN metrics m ON m.id = o.metric_id "
            "WHERE m.name = 'heart_rate_bpm'").fetchone()
        assert (v2_n, v2_avg) == (v1_n, v1_avg)
        assert parse_iso(v2_min) == parse_iso(v1_min)
        assert parse_iso(v2_max) == parse_iso(v1_max)
        assert conn.execute(
            "SELECT COUNT(*) FROM observations o JOIN metrics m "
            "ON m.id = o.metric_id WHERE m.name = 'rr_interval_ms'").fetchone() == (3,)
    finally:
        conn.close()


def test_unparseable_v1_timestamp_fails_loudly(tmp_path):
    db = tmp_path / "hrm.sqlite3"
    make_v1(db, [("x", "S", "AA", [("2026-01-01T00:00:00.000+00:00", 70, [])])])
    conn = connect(db)
    conn.execute("UPDATE samples SET timestamp = 'not a timestamp'")
    conn.commit()
    conn.close()

    with pytest.raises(migrate.MigrationError):
        migrate.migrate(db, backup=False)

    conn = connect(db)
    try:
        tables = {n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "observations" not in tables
    finally:
        conn.close()


def test_duplicate_v1_timestamps_survive_the_migration(tmp_path):
    """Two samples at the same instant, and RR windows that overlap, both
    collide on (source, metric, ts, external_id) if external_id is ''. The
    v1 row id is carried into it so neither can be lost."""
    db = tmp_path / "hrm.sqlite3"
    make_v1(db, [("x", "S", "AA", [
        ("2026-01-01T00:00:00.000+00:00", 70, [1000.0]),
        ("2026-01-01T00:00:00.000+00:00", 71, [1000.0]),
    ])])
    migrate.migrate(db)

    conn = connect(db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM observations o JOIN metrics m "
            "ON m.id = o.metric_id WHERE m.name = 'heart_rate_bpm'").fetchone() == (2,)
        assert conn.execute(
            "SELECT COUNT(*) FROM observations o JOIN metrics m "
            "ON m.id = o.metric_id WHERE m.name = 'rr_interval_ms'").fetchone() == (2,)
    finally:
        conn.close()


# -- runner mechanics ----------------------------------------------------

def test_current_version_adopts_an_unversioned_v1_database(tmp_path):
    db = tmp_path / "hrm.sqlite3"
    make_v1(db, [])
    conn = connect(db)
    try:
        # No schema_version table at all, but the v1 tables are there.
        assert migrate.current_version(conn) == 1
    finally:
        conn.close()


def test_current_version_of_an_empty_file_is_zero(tmp_path):
    conn = migrate.open_db(tmp_path / "empty.sqlite3")
    try:
        assert migrate.current_version(conn) == 0
    finally:
        conn.close()


def test_every_migration_is_recorded_with_a_timestamp(tmp_path):
    db = tmp_path / "fresh.sqlite3"
    migrate.migrate(db)
    conn = connect(db)
    try:
        rows = conn.execute(
            "SELECT version, applied_at FROM schema_version ORDER BY version").fetchall()
    finally:
        conn.close()
    assert [v for v, _ in rows] == [1, 2, 3]
    for _, applied_at in rows:
        parse_iso(applied_at)   # must be a canonical timestamp, not datetime('now')


def test_migrations_on_disk_are_numbered_in_order():
    versions = [v for v, _, _ in migrate.available()]
    assert versions == sorted(set(versions))
    assert versions[-1] == migrate.LATEST_VERSION


def test_split_statements_does_not_break_on_a_quoted_semicolon():
    sql = "INSERT INTO t VALUES ('a;b');\nSELECT 1;\n"
    assert list(migrate.split_statements(sql)) == [
        "INSERT INTO t VALUES ('a;b');", "SELECT 1;"]


def test_split_statements_rejects_a_truncated_script():
    with pytest.raises(migrate.MigrationError):
        list(migrate.split_statements("CREATE TABLE t (\n"))
