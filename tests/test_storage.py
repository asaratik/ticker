"""
Tests for the async SQLite session store. Uses pytest's tmp_path fixture so
nothing touches the real database. store.close() joins the writer thread,
so reads after it are deterministic -- no polling/sleeping needed.
"""

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


def test_reading_a_missing_database_creates_nothing(tmp_path):
    # The read helpers are for analysis scripts and the CLI summary; being
    # pointed at a path with no database should come back empty rather than
    # leaving a new empty database behind.
    missing = tmp_path / "nope" / "missing.sqlite3"
    assert storage.list_sessions(missing) == []
    assert storage.get_samples(1, missing) == []
    assert not missing.exists()
    assert not missing.parent.exists()
