"""
P0 write-path integrity, against the real AsyncStore.

The scheduler's own tests use a recording fake for record_sync, which is
exactly what let a checkpoint commit ahead of its rows, and live windows
wipe the backfill cursor, without anything noticing. These drive the real
writer and read back through a second connection, the way a restart would.
"""

import asyncio
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from ticker import config as tconfig
from ticker.db import queries, store
from ticker.ingest import importer, scheduler, sync
from ticker.ingest.normalizer import Normalizer
from ticker.model import Observation
from ticker.sources import base

UTC = timezone.utc
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
HR = "heart_rate_bpm"
BROKEN_UPSERT = "INSERT INTO no_such_table VALUES (?,?,?,?,?,?,?,?,?)"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "ticker.sqlite3"
    conn = store.connect(path)
    source_id = store.ensure_source(conn, "pull", "fake", "Fake")
    yield path, conn, source_id
    conn.close()


@pytest.fixture
def run():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop.run_until_complete
    finally:
        asyncio.set_event_loop(None)
        loop.close()


class Pull:
    """`per_window` readings per window, nothing before `floor`."""

    vendor = "fake"

    def __init__(self, per_window=50, floor=None):
        self.per_window = per_window
        self.floor = floor
        self.windows = []

    def capabilities(self):
        return frozenset([HR])

    def health(self):
        return base.SourceHealth(ok=True, state="ok")

    def fetch(self, metric, since, until):
        self.windows.append((metric, since, until))
        if self.floor is not None and until <= self.floor:
            return []
        return [Observation(metric, since + timedelta(seconds=i + 1), 60.0 + i % 30)
                for i in range(self.per_window)]


async def no_sleep(seconds):
    pass


def sync_cycle(run, source, writer, state, cycles=1, **kwargs):
    run(scheduler.run_pull_source(
        source, Normalizer(writer, state.source_id, derive_hrv=False), writer, state,
        now=lambda: NOW, sleep=no_sleep, max_cycles=cycles, **kwargs))


def count(conn):
    return conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]


def saved_state(conn, source_id):
    return queries.sync_state(conn, source_id).get(HR, {})


# -- checkpoints never outrun their rows ---------------------------------------

def test_a_checkpoint_commits_with_the_rows_it_covers(db, run):
    """50 readings sit below the 200-row coalescing threshold, under a
    one-minute coalescing window. The old writer committed the watermark at
    once and the rows up to two seconds later; a crash in between skipped
    them for good. Reading back before any flush or close is that crash."""
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=200, coalesce_ms=60_000,
                              migrate_first=False)
    try:
        state = scheduler.PullState(source_id, [HR])
        sync_cycle(run, Pull(per_window=50), writer, state)
        saved = saved_state(conn, source_id)
        assert saved["watermark_ts"] is not None and saved["cursor"] is not None
        # Both windows (live and one backfill chunk) were acknowledged, so
        # all of their rows are already durable.
        assert count(conn) == 100
        assert state.watermarks[HR] == NOW
    finally:
        writer.close()


def test_cancelled_checkpoint_waiter_does_not_fault_the_writer(db, monkeypatch):
    path, conn, source_id = db
    writer = store.AsyncStore(path, migrate_first=False)
    entered = threading.Event()
    release = threading.Event()
    record_sync = writer._record_sync

    def wait_before_checkpoint(*args):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("checkpoint was not released")
        return record_sync(*args)

    monkeypatch.setattr(writer, "_record_sync", wait_before_checkpoint)
    try:
        writer.insert_observations(source_id, [Observation(HR, NOW, 70.0)])
        future = writer.record_sync(source_id, HR, watermark_ts=NOW)
        assert entered.wait(5)
        assert future.cancel()
        release.set()
        assert writer.flush(5)
        assert writer.fault is None
        assert count(conn) == 1
        assert saved_state(conn, source_id)["watermark_ts"] is not None
    finally:
        release.set()
        writer.close()


def test_a_failed_commit_leaves_progress_alone_and_retries_the_window(db, run, monkeypatch):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=200, migrate_first=False)
    source = Pull(per_window=50)
    state = scheduler.PullState(source_id, [HR])
    errors = []
    try:
        monkeypatch.setattr(store, "UPSERT_OBSERVATION", BROKEN_UPSERT)
        sync_cycle(run, source, writer, state, on_error=errors.append)
        # Nothing moved: not in memory, not on disk.
        assert state.watermarks == {} and state.backfill == {}
        assert saved_state(conn, source_id).get("watermark_ts") is None
        assert count(conn) == 0
        assert writer.fault and any("progress not saved" in e for e in errors)
        first_live = source.windows[0]

        monkeypatch.undo()
        assert writer.retry() and writer.fault is None
        sync_cycle(run, source, writer, state)
        # The same live window again, and now it lands.
        assert source.windows[2] == first_live
        assert saved_state(conn, source_id)["watermark_ts"] is not None
        assert count(conn) == 100
    finally:
        writer.close()


def test_a_rejected_batch_cannot_advance_a_checkpoint(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, coalesce_ms=60_000,
                              migrate_first=False)
    try:
        writer.insert_observations(source_id, [Observation(HR, NOW, 70.0)])
        writer.insert_observations(9999, [Observation(HR, NOW, 71.0)])
        with pytest.raises(store.StorageFault):
            writer.record_sync(source_id, HR, watermark_ts=NOW).result(5)
        assert count(conn) == 1
        assert saved_state(conn, source_id) == {}
        assert writer.fault is None
        assert writer.flush(5) is False

        writer.insert_observations(source_id, [
            Observation(HR, NOW + timedelta(seconds=1), 72.0)])
        assert writer.record_sync(source_id, HR, watermark_ts=NOW).result(5)
        assert saved_state(conn, source_id)["watermark_ts"] is not None
    finally:
        assert writer.close() is True


def test_an_unusable_observation_cannot_advance_a_checkpoint(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, [Observation("not_registered", NOW, 70.0)])
        with pytest.raises(store.StorageFault):
            writer.record_sync(source_id, HR, watermark_ts=NOW).result(5)
        assert saved_state(conn, source_id) == {}
        assert writer.flush(5) is False
    finally:
        assert writer.close() is True


def test_close_reports_a_rejected_batch_without_an_intervening_flush(db):
    path, _, _ = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    writer.insert_observations(9999, [Observation(HR, NOW, 70.0)])
    assert writer.close() is False


def test_terminal_sync_exits_nonzero_on_a_fetch_failure(db, monkeypatch, capsys):
    path, _, source_id = db

    class FailingPull(Pull):
        def fetch(self, metric, since, until):
            raise base.PermanentError("vendor unavailable")

    monkeypatch.setattr(sync, "build_sources",
                        lambda conn, writer: [(source_id, FailingPull())])
    assert sync.main(["--once", "--db", str(path)]) == 1
    assert "did not complete successfully" in capsys.readouterr().err


# -- backfill progress survives live syncs, failures and restarts --------------

def test_live_and_failed_windows_keep_the_backfill_cursor(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, migrate_first=False)
    try:
        cursor = "2026-01-01T00:00:00.000+00:00"
        writer.record_sync(source_id, HR, cursor=cursor).result(5)
        writer.record_sync(source_id, HR, watermark_ts=NOW).result(5)
        writer.record_sync(source_id, HR, last_error="boom", succeeded=False).result(5)
        saved = saved_state(conn, source_id)
        assert saved["cursor"] == cursor
        assert saved["last_error"] == "boom"
    finally:
        writer.close()


def test_a_finished_backfill_is_remembered_across_restarts(db, run):
    path, conn, source_id = db
    # A row as an older version left it: a watermark, no cursor, no `done`.
    conn.execute("INSERT INTO sync_state (source_id, metric_id, watermark_ts) "
                 "VALUES (?, (SELECT id FROM metrics WHERE name = ?), ?)",
                 (source_id, HR, "2026-03-09T12:00:00.000+00:00"))
    writer = store.AsyncStore(path, migrate_first=False)
    try:
        state = scheduler.PullState.load(conn, source_id, [HR])
        assert state.done == set()          # unknown: replay once
        source = Pull(per_window=1, floor=NOW - timedelta(days=60))
        sync_cycle(run, source, writer, state, cycles=6)
        assert state.done == {HR}
        saved = saved_state(conn, source_id)
        assert saved["backfill_done"] is True

        restarted = scheduler.PullState.load(conn, source_id, [HR])
        assert restarted.done == {HR}
        again = Pull(per_window=1)
        sync_cycle(run, again, writer, restarted, cycles=3)
        # Live windows only: completed history is not walked again.
        assert all(until == NOW for _, _, until in again.windows)
    finally:
        writer.close()


def test_marking_backfill_done_leaves_live_sync_state_alone(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, migrate_first=False)
    try:
        writer.mark_backfill_done(source_id, HR).result(5)
        saved = saved_state(conn, source_id)
        assert saved["backfill_done"] is True
        assert saved["last_success"] is None and saved["last_attempt"] is None
    finally:
        writer.close()


# -- storage faults are reported, latched, and cleared only by a real write -----

def test_a_storage_fault_is_reported_everywhere(db, monkeypatch, caplog):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        monkeypatch.setattr(store, "UPSERT_OBSERVATION", BROKEN_UPSERT)
        writer.insert_observations(source_id, [Observation(HR, NOW, 70.0)])
        with caplog.at_level(logging.ERROR, logger="ticker.db.store"):
            assert writer.flush(5) is False
        assert writer.fault and "could not save" in writer.fault
        # Logged through logging, so the packaged app's ticker.log has it.
        assert any("refusing writes" in r.getMessage() for r in caplog.records)

        # Latched: later work is refused, not silently attempted.
        monkeypatch.undo()
        writer.insert_observations(source_id, [Observation(HR, NOW, 71.0)])
        assert writer.flush(5) is False
        assert writer.dropped == 2
        with pytest.raises(store.StorageFault):
            writer.record_sync(source_id, HR, watermark_ts=NOW).result(5)
        assert count(conn) == 0

        assert writer.retry() is True
        writer.insert_observations(source_id, [Observation(HR, NOW, 72.0)])
        assert writer.flush(5) is True
    finally:
        assert writer.close() is True
    assert count(conn) == 1


def test_retry_keeps_the_fault_while_storage_still_fails(db, monkeypatch):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        monkeypatch.setattr(store, "UPSERT_OBSERVATION", BROKEN_UPSERT)
        writer.insert_observations(source_id, [Observation(HR, NOW, 70.0)])
        writer.flush(5)
        # The probe is a real commit; make it impossible too.
        conn.execute("DROP TABLE writer_probe")
        assert writer.retry() is False
        assert writer.fault
    finally:
        assert writer.close() is False


def hold_write_lock(path, seconds):
    """Another connection holding the write lock, on a thread of its own
    (SQLite connections are bound to the thread that made them)."""
    held, done = threading.Event(), threading.Event()

    def hold():
        blocker = store.connect(path, migrate_first=False)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            held.set()
            done.wait(seconds)
            blocker.execute("COMMIT")
        finally:
            blocker.close()

    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(5)
    return thread, done


def test_lock_contention_is_retried_rather_than_latched(db, monkeypatch):
    path, conn, source_id = db
    monkeypatch.setattr(tconfig, "BUSY_TIMEOUT_MS", 100)
    monkeypatch.setattr(store, "BUSY_ATTEMPTS", 5)
    writer = store.AsyncStore(path, coalesce_rows=1, migrate_first=False)
    try:
        assert writer.flush(5)                  # started, probe done
        thread, _ = hold_write_lock(path, 0.25)
        writer.insert_observations(source_id, [Observation(HR, NOW, 70.0)])
        assert writer.flush(10) is True
        assert writer.fault is None
        thread.join()
    finally:
        writer.close()
    assert count(conn) == 1


def test_contention_that_never_ends_latches_and_says_so(db, monkeypatch):
    path, conn, source_id = db
    monkeypatch.setattr(tconfig, "BUSY_TIMEOUT_MS", 50)
    writer = store.AsyncStore(path, coalesce_rows=1, migrate_first=False)
    try:
        assert writer.flush(5)
        thread, release = hold_write_lock(path, 30)
        writer.insert_observations(source_id, [Observation(HR, NOW, 70.0)])
        assert writer.flush(10) is False
        assert writer.fault and "locked" in writer.fault
        release.set()
        thread.join()
        assert writer.retry() is True
    finally:
        writer.close()


def test_one_bad_batch_does_not_take_another_producers_rows(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, coalesce_ms=60_000,
                              migrate_first=False)
    try:
        writer.insert_observations(source_id, [Observation(HR, NOW, 70.0)])
        writer.insert_observations(9999, [Observation(HR, NOW, 71.0)])   # no such source
        assert writer.flush(5) is False          # something failed: say so
        assert writer.fault is None              # but it was data, not storage
        assert writer.rejected == 1
    finally:
        writer.close()
    assert [v for (v,) in conn.execute("SELECT value FROM observations")] == [70.0]


# -- imports are bounded, and finish only once committed ---------------------------

class StalledStore:
    """Takes work and never writes it, so admission can be watched."""

    fault = None

    def __init__(self):
        self._ops = queue.Queue()
        self._thread = threading.current_thread()


def batch():
    return [Observation(HR, NOW, 70.0)]


def test_an_import_has_at_most_two_units_in_flight():
    stalled = StalledStore()
    writer = store.ImportWriter(stalled)
    writer.insert_observations(1, batch())
    writer.begin_session(1, object())          # sessions count against it too
    third = threading.Thread(target=writer.insert_observations, args=(1, batch()))
    third.start()
    time.sleep(0.3)
    assert third.is_alive() and stalled._ops.qsize() == 2
    stalled._ops.get()[-1](True)               # the first unit commits
    third.join(2)
    assert not third.is_alive() and stalled._ops.qsize() == 2


def test_a_waiting_import_is_released_by_a_fault_or_a_cancel():
    stalled = StalledStore()
    cancel = threading.Event()
    writer = store.ImportWriter(stalled, cancel)
    writer.insert_observations(1, batch())
    writer.insert_observations(1, batch())
    stalled.fault = "disk full"
    with pytest.raises(store.StorageFault, match="disk full"):
        writer.insert_observations(1, batch())
    stalled.fault = None
    cancel.set()
    with pytest.raises(store.ImportCancelled):
        writer.insert_observations(1, batch())


def test_an_import_fails_if_the_store_rejects_part_of_its_batch(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        imported = writer.importer()
        imported.insert_observations(source_id, [
            Observation(HR, NOW, 70.0),
            Observation("not_registered", NOW, 71.0),
        ])
        with pytest.raises(store.StorageFault):
            imported.finish()
        assert count(conn) == 1
    finally:
        writer.close()


APPLE_EXPORT = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Watch" unit="count/min"
  value="61" startDate="2026-05-01 07:00:00 +0000" endDate="2026-05-01 07:00:00 +0000"/>
</HealthData>
"""


def test_an_import_returns_only_once_its_rows_are_committed(db, tmp_path):
    path, conn, _ = db
    export = tmp_path / "export.xml"
    export.write_text(APPLE_EXPORT, encoding="utf-8")
    writer = store.AsyncStore(path, coalesce_ms=60_000, migrate_first=False)
    try:
        importer.run_import(conn, writer, "apple_health", export)
        # No flush, under a one-minute coalescing window: already durable.
        assert count(conn) == 1
    finally:
        writer.close()


def test_a_terminal_import_that_cannot_save_exits_non_zero(db, tmp_path, monkeypatch, capsys):
    path, conn, _ = db
    export = tmp_path / "export.xml"
    export.write_text(APPLE_EXPORT, encoding="utf-8")
    monkeypatch.setattr(store, "UPSERT_OBSERVATION", BROKEN_UPSERT)
    assert importer.main([str(export), "--db", str(path), "--quiet"]) == 1
    captured = capsys.readouterr()
    assert "imported into" not in captured.out
    assert "could not save" in captured.err
    assert count(conn) == 0


# -- rollups catch up without a maintenance command --------------------------------

def daily(conn):
    return conn.execute("SELECT day, n, sum_value FROM rollups_daily ORDER BY day").fetchall()


def test_rollups_catch_up_after_a_crash(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, migrate_first=False)
    writer.insert_observations(source_id, [Observation(HR, NOW, 70.0),
                                           Observation(HR, NOW + timedelta(minutes=1), 80.0)])
    assert writer.flush(5)
    writer.close()                              # no rebuild_rollups(): the crash
    assert daily(conn) == []
    # The next writer rebuilds what the triggers marked, unprompted.
    writer = store.AsyncStore(path, migrate_first=False)
    try:
        assert writer.flush(5)
        assert [(n, total) for _, n, total in daily(conn)] == [(2, 150.0)]
        assert conn.execute("SELECT COUNT(*) FROM rollup_dirty").fetchone() == (0,)
    finally:
        writer.close()


def test_a_delete_through_another_connection_marks_its_day(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, migrate_first=False)
    try:
        writer.insert_observations(source_id, [Observation(HR, NOW, 70.0)])
        writer.rebuild_rollups()
        assert writer.flush(5) and len(daily(conn)) == 1
        conn.execute("DELETE FROM observations")
        writer.rebuild_rollups()
        assert writer.flush(5)
        assert daily(conn) == []
    finally:
        writer.close()


def test_the_upgrade_marks_existing_days_for_one_rebuild(tmp_path):
    from ticker.db import migrate
    path = tmp_path / "old.sqlite3"
    conn = migrate.open_db(path)
    for version, name, migration in migrate.available()[:3]:
        migrate._apply_one(conn, version, name, migration)
    source_id = store.ensure_source(conn, "pull", "fake", "Fake")
    conn.execute("INSERT INTO observations (source_id, metric_id, ts, value, ingested_at) "
                 "VALUES (?, (SELECT id FROM metrics WHERE name = ?), ?, 60, ?)",
                 (source_id, HR, "2026-03-01T12:00:00.000+00:00",
                  "2026-03-01T12:00:00.000+00:00"))
    conn.close()
    assert migrate.migrate(path) == [4]
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("SELECT utc_day FROM rollup_dirty").fetchall() == [("2026-03-01",)]
    finally:
        conn.close()
