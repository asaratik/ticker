"""
Tests for the v2 batched writer.

store.flush() commits everything queued so far and blocks until it has, so
these read back through a second connection without polling or sleeping.
The coalescing timer is the one thing that genuinely needs wall clock, and
that test keeps its window short.
"""

import queue
import time
from datetime import datetime, timedelta, timezone

import pytest

from ticker.db import store
from ticker.model import Observation, SessionRecord

UTC = timezone.utc
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    """A migrated database, plus a source to write into."""
    path = tmp_path / "ticker.sqlite3"
    conn = store.connect(path)
    source_id = store.ensure_source(conn, "stream", "ble", "Test strap")
    yield path, conn, source_id
    conn.close()


def hr(seconds, value, **kw):
    return Observation("heart_rate_bpm", T0 + timedelta(seconds=seconds),
                       float(value), **kw)


def rows(conn, metric="heart_rate_bpm"):
    return conn.execute(
        "SELECT o.ts, o.value, o.session_id FROM observations o "
        "JOIN metrics m ON m.id = o.metric_id WHERE m.name = ? ORDER BY o.ts",
        (metric,)).fetchall()


# -- setup helpers -------------------------------------------------------

def test_ensure_source_is_idempotent(db):
    _, conn, source_id = db
    assert store.ensure_source(conn, "stream", "ble", "Test strap") == source_id
    assert conn.execute(
        "SELECT COUNT(*) FROM sources WHERE display_name = 'Test strap'"
    ).fetchone() == (1,)


def test_ensure_device_is_idempotent_and_learns_a_name(db):
    _, conn, source_id = db
    first = store.ensure_device(conn, source_id, "AA:BB")
    assert store.ensure_device(conn, source_id, "AA:BB", name="HRM-Dual") == first
    assert conn.execute("SELECT name FROM devices WHERE id = ?",
                        (first,)).fetchone() == ("HRM-Dual",)


def test_ensure_device_without_an_address_makes_no_row(db):
    _, conn, source_id = db
    assert store.ensure_device(conn, source_id, None, name="Nameless") is None
    assert conn.execute("SELECT COUNT(*) FROM devices").fetchone() == (0,)


def test_ensure_metric_extends_the_registry(db):
    _, conn, _ = db
    mid = store.ensure_metric(conn, "vo2max_ml_kg_min", "ml/kg/min", "instant")
    assert store.ensure_metric(conn, "vo2max_ml_kg_min", "ml/kg/min", "instant") == mid


# -- the write path ------------------------------------------------------

def test_observations_are_written_in_one_batch(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, [hr(i, 70 + i) for i in range(5)])
        assert writer.flush()
    finally:
        writer.close()

    assert [v for _, v, _ in rows(conn)] == [70.0, 71.0, 72.0, 73.0, 74.0]


def test_an_identical_refetch_changes_nothing(db):
    """The idempotency guarantee: overlapping sync windows must be free."""
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        batch = [hr(i, 70 + i) for i in range(5)]
        writer.insert_observations(source_id, batch)
        writer.flush()
        writer.take_dirty_days()

        writer.insert_observations(source_id, batch)
        writer.flush()
        # Nothing changed, so no rollup work was scheduled.
        assert writer.take_dirty_days() == set()
    finally:
        writer.close()

    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone() == (5,)


def test_an_amended_value_overwrites_and_marks_the_day_dirty(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, [hr(0, 70)])
        writer.flush()
        writer.take_dirty_days()

        writer.insert_observations(source_id, [hr(0, 99)])
        writer.flush()
        assert writer.take_dirty_days() != set()
    finally:
        writer.close()

    assert [v for _, v, _ in rows(conn)] == [99.0]


def test_an_amended_observation_updates_metadata_and_session(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, [hr(0, 70)])
        writer.flush()
        writer.take_dirty_days()

        writer.begin_session(source_id, SessionRecord(key="run", start_ts=T0))
        writer.insert_observations(source_id, [hr(
            0, 70, end_ts=T0 + timedelta(seconds=1), text_value="corrected",
            session_key="run")])
        writer.flush()
        assert writer.take_dirty_days()
    finally:
        writer.close()

    session_id, end_ts, text = conn.execute(
        "SELECT session_id, end_ts, text_value FROM observations").fetchone()
    assert session_id is not None
    assert end_ts == "2026-03-01T12:00:01.000+00:00"
    assert text == "corrected"


def test_two_sources_at_the_same_instant_do_not_collide(db):
    path, conn, source_id = db
    other = store.ensure_source(conn, "pull", "oura", "Ring")
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, [hr(0, 70)])
        writer.insert_observations(other, [hr(0, 55)])
        writer.flush()
    finally:
        writer.close()

    assert sorted(v for _, v, _ in rows(conn)) == [55.0, 70.0]


def test_external_id_separates_two_values_at_one_instant(db):
    """'a source that emits two values for the same metric at the same
    instant must supply one' -- with distinct ids, both survive."""
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, [
            hr(0, 70, external_id="a"),
            hr(0, 71, external_id="b"),
        ])
        writer.flush()
    finally:
        writer.close()

    assert sorted(v for _, v, _ in rows(conn)) == [70.0, 71.0]


def test_interval_observations_keep_their_end_and_text(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, [Observation(
            "sleep_stage", T0, 3.0, end_ts=T0 + timedelta(minutes=20),
            text_value="deep", external_id="s1")])
        writer.flush()
    finally:
        writer.close()

    ts, end_ts, text = conn.execute(
        "SELECT ts, end_ts, text_value FROM observations").fetchone()
    assert text == "deep"
    assert end_ts == "2026-03-01T12:20:00.000+00:00"


def test_an_unknown_metric_is_counted_and_reported_not_dropped(db):
    path, _, source_id = db
    errors = queue.Queue()
    writer = store.AsyncStore(path, error_queue=errors, coalesce_rows=1000,
                              migrate_first=False)
    try:
        writer.insert_observations(source_id, [
            Observation("not_a_metric", T0, 1.0), hr(0, 70)])
        writer.flush()
        assert writer.rejected == 1
    finally:
        writer.close()

    message = errors.get_nowait()
    assert message["type"] == "error"
    assert "not_a_metric" in message["message"]


def test_a_bad_op_does_not_kill_the_writer(db):
    """v1's hard-won lesson: one bad row must never silently end all logging
    for the rest of the process's life."""
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        # A source id that violates the foreign key: the batch fails, the
        # writer survives, and the next batch still lands.
        writer.insert_observations(9999, [hr(0, 70)])
        # A failed commit still has to release the flush waiter promptly
        # rather than stalling it for the whole timeout.
        started = time.monotonic()
        writer.flush(timeout=5)
        assert time.monotonic() - started < 2
        writer.insert_observations(source_id, [hr(1, 71)])
        assert writer.flush()
    finally:
        writer.close()

    assert [v for _, v, _ in rows(conn)] == [71.0]


def test_an_unopenable_database_is_reported_and_never_blocks(tmp_path):
    blocker = tmp_path / "this-is-a-file"
    blocker.write_text("")
    unusable = blocker / "subdir" / "ticker.sqlite3"

    errors = queue.Queue()
    writer = store.AsyncStore(unusable, error_queue=errors)
    try:
        writer.insert_observations(1, [hr(0, 70)])
        # flush() has to return rather than hang on a dead writer.
        assert writer.flush(timeout=5) is True
    finally:
        writer.close()

    assert str(unusable) in errors.get_nowait()["message"]


# -- coalescing ----------------------------------------------------------

def test_a_full_batch_commits_without_waiting_for_the_timer(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=3, coalesce_ms=60000,
                              migrate_first=False)
    try:
        writer.insert_observations(source_id, [hr(i, 70 + i) for i in range(3)])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 3:
                break
            time.sleep(0.01)
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone() == (3,)
    finally:
        writer.close()


def test_a_partial_batch_commits_when_the_timer_expires(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, coalesce_ms=50,
                              migrate_first=False)
    try:
        writer.insert_observations(source_id, [hr(0, 70)])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]:
                break
            time.sleep(0.01)
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone() == (1,)
    finally:
        writer.close()


def test_close_commits_what_is_still_pending(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, coalesce_ms=60000,
                              migrate_first=False)
    writer.insert_observations(source_id, [hr(0, 70)])
    writer.close()
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone() == (1,)


# -- sessions ------------------------------------------------------------

def test_observations_attach_to_an_open_session(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.begin_session(source_id, SessionRecord(
            key="run", start_ts=T0, label="tempo"))
        writer.insert_observations(source_id, [hr(0, 70, session_key="run")])
        writer.end_session(source_id, "run", T0 + timedelta(minutes=30))
        writer.flush()
    finally:
        writer.close()

    session_id, start_ts, end_ts, label = conn.execute(
        "SELECT id, start_ts, end_ts, label FROM sessions").fetchone()
    assert label == "tempo"
    assert end_ts == "2026-03-01T12:30:00.000+00:00"
    assert rows(conn)[0][2] == session_id


def test_an_observation_with_no_session_is_kept(db):
    """v1 dropped sessionless samples. In v2 a stream running without a
    user-started session is normal, so they land with a NULL session."""
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.insert_observations(source_id, [hr(0, 70)])
        writer.insert_observations(source_id, [hr(1, 71, session_key="never-opened")])
        writer.flush()
    finally:
        writer.close()

    assert [(v, s) for _, v, s in rows(conn)] == [(70.0, None), (71.0, None)]


def test_resyncing_a_vendor_session_updates_one_row(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, migrate_first=False)
    try:
        writer.begin_session(source_id, SessionRecord(
            key="w1", start_ts=T0, kind="workout", external_id="oura-123"))
        writer.flush()
        # The same vendor workout, fetched again in an overlapping window,
        # now with an end time.
        writer.begin_session(source_id, SessionRecord(
            key="w1", start_ts=T0, end_ts=T0 + timedelta(hours=1),
            kind="workout", external_id="oura-123"))
        writer.flush()
    finally:
        writer.close()

    sessions = conn.execute(
        "SELECT external_id, end_ts FROM sessions").fetchall()
    assert sessions == [("oura-123", "2026-03-01T13:00:00.000+00:00")]


def test_rows_queued_before_a_session_opens_still_commit(db):
    path, conn, source_id = db
    writer = store.AsyncStore(path, coalesce_rows=1000, coalesce_ms=60000,
                              migrate_first=False)
    try:
        writer.insert_observations(source_id, [hr(0, 70)])
        writer.begin_session(source_id, SessionRecord(key="run", start_ts=T0))
        writer.insert_observations(source_id, [hr(1, 71, session_key="run")])
        writer.flush()
    finally:
        writer.close()

    assert [(v, s is not None) for _, v, s in rows(conn)] == [(70.0, False), (71.0, True)]
