"""
Guards on the schema itself.

db/schema.sql is generated from the migrations, so the first test here is
what stops it becoming a description of a database that no longer exists.
The rest assert the properties the data model depends on -- the
idempotency index, the CHECK constraints, the cascade behaviour -- rather
than re-stating the DDL.
"""

import sqlite3

import pytest

from ticker.db import migrate, schema_snapshot, store


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "s.sqlite3")
    yield connection
    connection.close()


def test_schema_sql_matches_the_migrations():
    # If this fails: python -m ticker.db.schema_snapshot
    assert schema_snapshot.main(["--check"]) == 0


def test_the_natural_key_index_is_unique(conn):
    indexes = {
        name: unique for name, unique in conn.execute(
            "SELECT name, [unique] FROM pragma_index_list('observations')")
    }
    assert indexes["ux_obs_natural"] == 1
    columns = [r[2] for r in conn.execute(
        "SELECT * FROM pragma_index_info('ux_obs_natural')")]
    assert columns == ["source_id", "metric_id", "ts", "external_id"]


def test_external_id_defaults_to_empty_string_not_null(conn):
    """NULLs are distinct from each other in a unique index, so a NULL
    external_id would quietly disable the idempotency guarantee."""
    source_id = store.ensure_source(conn, "stream", "ble", "S")
    metric_id = conn.execute(
        "SELECT id FROM metrics WHERE name = 'heart_rate_bpm'").fetchone()[0]
    conn.execute(
        "INSERT INTO observations (source_id, metric_id, ts, value, ingested_at) "
        "VALUES (?,?,'2026-01-01T00:00:00.000+00:00',70,'x')", (source_id, metric_id))
    assert conn.execute("SELECT external_id FROM observations").fetchone() == ("",)


def test_foreign_keys_are_enforced(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO observations (source_id, metric_id, ts, value, ingested_at) "
            "VALUES (9999, 1, '2026-01-01T00:00:00.000+00:00', 70, 'x')")


def test_deleting_a_source_takes_its_observations_with_it(conn):
    source_id = store.ensure_source(conn, "stream", "ble", "S")
    metric_id = conn.execute(
        "SELECT id FROM metrics WHERE name = 'heart_rate_bpm'").fetchone()[0]
    conn.execute(
        "INSERT INTO observations (source_id, metric_id, ts, value, ingested_at) "
        "VALUES (?,?,'2026-01-01T00:00:00.000+00:00',70,'x')", (source_id, metric_id))
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone() == (0,)


def test_deleting_a_session_keeps_its_observations(conn):
    """ON DELETE SET NULL: a session is a grouping, not the data itself."""
    source_id = store.ensure_source(conn, "stream", "ble", "S")
    metric_id = conn.execute(
        "SELECT id FROM metrics WHERE name = 'heart_rate_bpm'").fetchone()[0]
    conn.execute("INSERT INTO sessions (source_id, start_ts) VALUES (?,?)",
                 (source_id, "2026-01-01T00:00:00.000+00:00"))
    conn.execute(
        "INSERT INTO observations (source_id, metric_id, session_id, ts, value, "
        "ingested_at) VALUES (?,?,1,'2026-01-01T00:00:00.000+00:00',70,'x')",
        (source_id, metric_id))
    conn.execute("DELETE FROM sessions WHERE id = 1")
    assert conn.execute("SELECT session_id FROM observations").fetchone() == (None,)


@pytest.mark.parametrize("kind", ["stream", "pull", "import"])
def test_valid_source_kinds_are_accepted(conn, kind):
    store.ensure_source(conn, kind, "v", "d-" + kind)


def test_an_invalid_source_kind_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        store.ensure_source(conn, "telepathy", "v", "d")


def test_an_invalid_value_kind_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        store.ensure_metric(conn, "m", "u", "vibes")


def test_wal_mode_is_on(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_a_source_cannot_be_configured_twice(conn):
    store.ensure_source(conn, "pull", "oura", "Ring")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sources (kind, vendor, display_name, config_json, "
            "enabled, created_at) VALUES ('pull','oura','Ring','{}',1,'x')")


def test_the_session_index_is_partial(conn):
    # idx_obs_session covers only non-NULL session_ids; most rows from pull
    # sources have none, and indexing those would be pure overhead.
    sql, = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idx_obs_session'").fetchone()
    assert "WHERE session_id IS NOT NULL" in sql


def test_the_dashboard_index_exists(conn):
    names = {n for (n,) in conn.execute(
        "SELECT name FROM pragma_index_list('observations')")}
    assert "idx_obs_metric_ts" in names


def test_latest_version_matches_the_migrations_on_disk():
    assert migrate.available()[-1][0] == migrate.LATEST_VERSION
