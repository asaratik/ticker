"""
Tests for the async SQLite session store. Uses pytest's tmp_path fixture so
nothing touches the real database. store.close() joins the writer thread,
so reads after it are deterministic -- no polling/sleeping needed.
"""

import queue
import sqlite3

import storage


def test_full_session_lifecycle(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    store = storage.AsyncSessionStore(db_path)
    try:
        store.start_session("tok", "Test Strap", "AA:BB:CC:DD:EE:FF", label="unit test")
        store.insert_sample("tok", "2026-01-01T00:00:00+00:00", 80, [750.0, 760.0])
        store.insert_sample("tok", "2026-01-01T00:00:01+00:00", 82, [])
        store.end_session("tok")
    finally:
        store.close()

    sessions = storage.list_sessions(db_path)
    assert len(sessions) == 1
    sid, start, end, name, label, n, avg_hr, min_hr, max_hr = sessions[0]
    assert name == "Test Strap"
    assert label == "unit test"
    assert n == 2
    assert min_hr == 80
    assert max_hr == 82
    assert end is not None

    samples = storage.get_samples(sid, db_path)
    assert len(samples) == 2
    assert samples[0][1] == 80
    assert samples[0][2] == "750.0,760.0"
    assert samples[1][2] == ""  # no RR data on that sample


def test_sample_without_active_session_is_dropped_not_errored(tmp_path):
    db_path = tmp_path / "test2.sqlite3"
    store = storage.AsyncSessionStore(db_path)
    try:
        # No start_session for this token -- must be silently ignored, not
        # crash the writer thread (which would silently kill all future
        # logging for the rest of the app's life).
        store.insert_sample("ghost-token", "2026-01-01T00:00:00+00:00", 80, [])
    finally:
        store.close()

    assert storage.list_sessions(db_path) == []


def test_multiple_sessions_are_independent(tmp_path):
    db_path = tmp_path / "test3.sqlite3"
    store = storage.AsyncSessionStore(db_path)
    try:
        store.start_session(1, "Strap", "AA:AA", label="first")
        store.insert_sample(1, "2026-01-01T00:00:00+00:00", 70, [])
        store.end_session(1)

        store.start_session(2, "Strap", "AA:AA", label="second")
        store.insert_sample(2, "2026-01-01T00:01:00+00:00", 90, [])
        store.insert_sample(2, "2026-01-01T00:01:01+00:00", 91, [])
        store.end_session(2)
    finally:
        store.close()

    sessions = {row[4]: row for row in storage.list_sessions(db_path)}  # keyed by label
    assert sessions["first"][5] == 1  # n_samples
    assert sessions["second"][5] == 2


def test_empty_database_reports_no_sessions(tmp_path):
    db_path = tmp_path / "empty.sqlite3"
    store = storage.AsyncSessionStore(db_path)
    store.close()
    assert storage.list_sessions(db_path) == []


def test_an_unopenable_database_is_reported_and_never_blocks(tmp_path):
    """A database that can't be opened used to kill the writer thread on its
    first line, silently ending all logging for the life of the app. It has
    to surface, and the caller still has to be able to log and close without
    blocking.
    """
    blocker = tmp_path / "this-is-a-file"
    blocker.write_text("")
    unusable = blocker / "subdir" / "hrm.sqlite3"  # a file can't be a parent

    errors: "queue.Queue" = queue.Queue()
    store = storage.AsyncSessionStore(unusable, error_queue=errors)
    try:
        store.start_session("tok", "Strap", "AA:BB")
        store.insert_sample("tok", "2026-01-01T00:00:00+00:00", 80, [])
        store.end_session("tok")
    finally:
        store.close()  # must return, not time out on a dead thread

    message = errors.get_nowait()
    assert message["type"] == "error"
    assert str(unusable) in message["message"]


def test_reading_a_missing_database_creates_nothing(tmp_path):
    # The read helpers are for analysis scripts and the CLI summary; being
    # pointed at a path with no database should come back empty rather than
    # leaving a new empty database behind.
    missing = tmp_path / "nope" / "missing.sqlite3"
    assert storage.list_sessions(missing) == []
    assert storage.get_samples(1, missing) == []
    assert not missing.exists()
    assert not missing.parent.exists()


# -- reading across the migration ---------------------------------------
#
# The app migrates the database on first v2 run. Analysis scripts and the CLI
# summary go through these helpers, so they have to keep working afterwards --
# on the same data, in the same shape.

def _v1_fixture(db_path):
    store = storage.AsyncSessionStore(db_path)
    store.start_session("t", "HRM-Dual", "AA:BB:CC", label="ride")
    store.insert_sample("t", "2026-01-01T00:00:10.000+00:00", 80, [750.0, 760.0])
    store.insert_sample("t", "2026-01-01T00:00:11.000+00:00", 82, [])
    store.insert_sample("t", "2026-01-01T00:00:12.000+00:00", 84, [800.0])
    store.end_session("t")
    store.close()


def test_list_sessions_reads_the_same_summary_after_migration(tmp_path):
    from ticker.db import migrate

    db_path = tmp_path / "hrm.sqlite3"
    _v1_fixture(db_path)
    before = storage.list_sessions(db_path)
    migrate.migrate(db_path)
    after = storage.list_sessions(db_path)

    assert len(after) == len(before) == 1
    # (id, start, end, device_name, label, n_samples, avg, min, max)
    assert after[0][3] == before[0][3] == "HRM-Dual"
    assert after[0][4] == before[0][4] == "ride"
    assert after[0][5] == before[0][5] == 3
    assert after[0][6] == before[0][6]
    assert (after[0][7], after[0][8]) == (before[0][7], before[0][8]) == (80, 84)


def test_get_samples_reassembles_rr_intervals_after_migration(tmp_path):
    """v2 splits one v1 sample into several rows. The legacy reader puts them
    back together rather than reporting RR data as absent."""
    from ticker.db import migrate

    db_path = tmp_path / "hrm.sqlite3"
    _v1_fixture(db_path)
    before = storage.get_samples(1, db_path)
    migrate.migrate(db_path)
    after = storage.get_samples(1, db_path)

    assert [hr for _, hr, _ in after] == [hr for _, hr, _ in before] == [80, 82, 84]
    assert [rr for _, _, rr in after] == [rr for _, _, rr in before]
    assert after[0][2] == "750.0,760.0"
    assert after[1][2] == ""


def test_reading_a_v2_database_the_app_wrote(tmp_path):
    from ticker.ingest.session_logger import SessionLogger

    db_path = tmp_path / "v2only.sqlite3"
    logger = SessionLogger("ble", db_path=db_path)
    try:
        logger.start_session(label="tempo", device_name="Polar H10",
                             device_address="11:22")
        logger.log_sample({"type": "sample", "hr": 70,
                           "timestamp": "2026-03-01T12:00:00.000+00:00",
                           "rr_intervals_ms": [800.0]})
    finally:
        logger.close()

    sessions = storage.list_sessions(db_path)
    assert sessions[0][3] == "Polar H10"
    assert sessions[0][4] == "tempo"
    assert sessions[0][5] == 1              # one heart rate sample, not the RR row
    assert storage.get_samples(sessions[0][0], db_path) == [
        ("2026-03-01T12:00:00.000+00:00", 70, "800.0")]


def test_second_resolution_v1_data_reads_back_unchanged(tmp_path):
    """Real v1 databases were written at second resolution, so several
    samples share one timestamp -- at ~93 bpm a strap notifies faster than
    once a second. Beats cannot be attributed to a sample by timestamp when
    the timestamps repeat, so migrated sessions are read from the preserved
    v1 rows instead.
    """
    from ticker.db import migrate

    db_path = tmp_path / "hrm.sqlite3"
    store = storage.AsyncSessionStore(db_path)
    store.start_session("t", "HRM 600", "EC:1C", label="ride")
    # Three notifications inside the same second, as the real data has.
    store.insert_sample("t", "2026-09-04T23:38:36+00:00", 93, [658.2])
    store.insert_sample("t", "2026-09-04T23:38:36+00:00", 94, [658.2])
    store.insert_sample("t", "2026-09-04T23:38:36+00:00", 95, [632.8])
    store.insert_sample("t", "2026-09-04T23:38:37+00:00", 96, [640.0])
    store.end_session("t")
    store.close()

    before = storage.get_samples(1, db_path)
    migrate.migrate(db_path)
    after = storage.get_samples(1, db_path)

    assert after == before
    assert [rr for _, _, rr in after] == ["658.2", "658.2", "632.8", "640.0"]


def test_duplicate_v1_timestamps_all_survive_as_observations(tmp_path):
    """The natural key is (source, metric, ts, external_id). A blank
    external_id would make these four samples collide into one."""
    from ticker.db import migrate

    db_path = tmp_path / "hrm.sqlite3"
    store = storage.AsyncSessionStore(db_path)
    store.start_session("t", "HRM 600", "EC:1C")
    for hr in (93, 94, 95, 96):
        store.insert_sample("t", "2026-09-04T23:38:36+00:00", hr, [658.2])
    store.end_session("t")
    store.close()
    migrate.migrate(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM observations o JOIN metrics m "
            "ON m.id = o.metric_id WHERE m.name = 'heart_rate_bpm'").fetchone() == (4,)
        assert conn.execute(
            "SELECT COUNT(*) FROM observations o JOIN metrics m "
            "ON m.id = o.metric_id WHERE m.name = 'rr_interval_ms'").fetchone() == (4,)
    finally:
        conn.close()
