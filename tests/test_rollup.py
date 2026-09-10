"""
Tests for rollup maintenance and derived-metric backfill.

The incremental path (AsyncStore.rebuild_rollups) and the bulk path
(python -m ticker.db.rollup --all) have to agree: the whole point of tracking
dirty days is that the cheap path produces the same answer as recomputing
everything.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from ticker.db import backfill, queries, rollup, store
from ticker.model import Observation

UTC = timezone.utc
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "r.sqlite3"
    conn = store.connect(path)
    source_id = store.ensure_source(conn, "stream", "ble", "Strap")
    yield path, conn, source_id
    conn.close()


def hr(seconds, value):
    return Observation("heart_rate_bpm", T0 + timedelta(seconds=seconds), float(value))


def daily(conn, metric="heart_rate_bpm"):
    return queries.daily(conn, metric, "2020-01-01", "2099-12-31")


# -- the incremental path ------------------------------------------------

def test_the_writer_rebuilds_only_the_days_it_touched(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, [hr(0, 60), hr(1, 80)])
        writer.rebuild_rollups()
        assert writer.flush()
    finally:
        writer.close()

    rows = daily(conn)
    assert len(rows) == 1
    assert (rows[0]["n"], rows[0]["min"], rows[0]["max"]) == (2, 60.0, 80.0)


def test_rebuilding_with_nothing_dirty_does_no_work(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, [hr(0, 60)])
        writer.rebuild_rollups()
        writer.flush()
        # Re-inserting identical data changes nothing, so nothing is dirty
        # and the rollup must not be recomputed.
        before = conn.execute(
            "SELECT computed_at FROM rollups_daily").fetchone()[0]
        writer.insert_observations(source_id, [hr(0, 60)])
        writer.rebuild_rollups()
        writer.flush()
        after = conn.execute("SELECT computed_at FROM rollups_daily").fetchone()[0]
    finally:
        writer.close()
    assert before == after


def test_an_amended_value_updates_the_rollup(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, [hr(0, 60), hr(1, 80)])
        writer.rebuild_rollups()
        writer.flush()
        writer.insert_observations(source_id, [hr(1, 200)])
        writer.rebuild_rollups()
        assert writer.flush()
    finally:
        writer.close()

    assert daily(conn)[0]["max"] == 200.0


def test_the_incremental_result_matches_a_full_rebuild(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, [
            hr(i * 3600, 60 + (i % 40)) for i in range(72)])
        writer.rebuild_rollups()
        assert writer.flush()
    finally:
        writer.close()

    incremental = daily(conn)
    conn.execute("DELETE FROM rollups_daily")
    rollup.rebuild_all(conn)
    assert daily(conn) == incremental


def test_a_failed_rebuild_leaves_the_days_dirty(db, monkeypatch):
    """Dropping a day that failed to rebuild would leave its rollup
    permanently stale, with nothing to notice it."""
    path, conn, source_id = db

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk went away")

    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, [hr(0, 60)])
        writer.flush()
        monkeypatch.setattr(queries, "rebuild_days", boom)
        writer.rebuild_rollups()
        writer.flush()
        monkeypatch.undo()
        # Still dirty, so a later rebuild picks it up.
        assert writer.take_dirty_days() != set()
    finally:
        writer.close()


# -- the bulk path -------------------------------------------------------

def test_rebuild_all_covers_every_metric_and_day(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, [
            hr(0, 60), hr(86400, 70),
            Observation("rr_interval_ms", T0, 800.0),
        ])
        assert writer.flush()
    finally:
        writer.close()

    assert rollup.rebuild_all(conn) == 3       # two HR days plus one RR day
    assert len(daily(conn)) == 2
    assert len(daily(conn, "rr_interval_ms")) == 1


def test_rebuild_all_can_be_limited_to_one_metric(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, [
            hr(0, 60), Observation("rr_interval_ms", T0, 800.0)])
        assert writer.flush()
    finally:
        writer.close()

    rollup.rebuild_all(conn, metric="heart_rate_bpm")
    assert len(daily(conn)) == 1
    assert daily(conn, "rr_interval_ms") == []


def test_the_rollup_cli_refuses_to_guess(tmp_path):
    with pytest.raises(SystemExit):
        rollup.main([])


def test_the_rollup_cli_reports_a_missing_database(tmp_path, capsys):
    assert rollup.main(["--all", "--db", str(tmp_path / "nope.sqlite3")]) == 1


def test_the_rollup_cli_runs(db, capsys):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, [hr(0, 60)])
        assert writer.flush()
    finally:
        writer.close()

    assert rollup.main(["--all", "--db", str(path)]) == 0
    assert "heart_rate_bpm" in capsys.readouterr().out


# -- derived-metric backfill ---------------------------------------------

def rr_run(source_id, count=60, session=None):
    out, offset = [], 0.0
    for i in range(count):
        interval = 810.0 if i % 2 else 790.0
        offset += interval
        out.append(Observation("rr_interval_ms",
                               T0 + timedelta(milliseconds=offset), interval,
                               external_id=str(i), session_key=session))
    return out


def test_hrv_is_derived_from_stored_rr_intervals(db):
    """Data migrated from v1 predates the pipeline that derives HRV, so the
    HRV panel would be empty over it without this."""
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, rr_run(source_id))
        assert writer.flush()
    finally:
        writer.close()

    assert conn.execute(
        "SELECT COUNT(*) FROM observations o JOIN metrics m ON m.id = o.metric_id "
        "WHERE m.name = 'hrv_rmssd_ms'").fetchone() == (0,)

    written = backfill.backfill_hrv(conn)
    assert written >= 1
    value, = conn.execute(
        "SELECT value FROM observations o JOIN metrics m ON m.id = o.metric_id "
        "WHERE m.name = 'hrv_rmssd_ms' LIMIT 1").fetchone()
    # Alternating +/-10 ms gives successive differences of 20 ms.
    assert value == pytest.approx(20.0, abs=1.0)


def test_backfilling_twice_writes_nothing_the_second_time(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, rr_run(source_id))
        assert writer.flush()
    finally:
        writer.close()

    first = backfill.backfill_hrv(conn)
    assert first >= 1
    assert backfill.backfill_hrv(conn) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM observations o JOIN metrics m ON m.id = o.metric_id "
        "WHERE m.name = 'hrv_rmssd_ms'").fetchone() == (first,)


def test_the_hrv_window_does_not_span_two_sessions(db):
    """A window crossing a session boundary would difference two beats hours
    apart and call the result heart rate variability."""
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, rr_run(source_id, 10))
        assert writer.flush()
    finally:
        writer.close()

    # Ten beats is below the window minimum, so nothing should come out --
    # if the groups were merged with anything else, something would.
    assert backfill.backfill_hrv(conn) == 0


def test_backfill_can_be_limited_to_one_source(db):
    path, conn, source_id = db
    other = store.ensure_source(conn, "pull", "oura", "Ring")
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, rr_run(source_id))
        writer.insert_observations(other, rr_run(other))
        assert writer.flush()
    finally:
        writer.close()

    backfill.backfill_hrv(conn, source_id=other)
    sources = {s for (s,) in conn.execute(
        "SELECT DISTINCT source_id FROM observations o "
        "JOIN metrics m ON m.id = o.metric_id WHERE m.name = 'hrv_rmssd_ms'")}
    assert sources == {other}


def test_the_backfill_cli_refuses_to_guess():
    with pytest.raises(SystemExit):
        backfill.main([])
