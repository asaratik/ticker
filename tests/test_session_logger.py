"""
Tests for the app's write path.

This is what the Tk app does when you press Start, get readings, and press
Stop -- exercised without a window, which is why the logic lives in
SessionLogger rather than inline in the Tk app.
"""

import queue
import sqlite3

import pytest

import storage
from ticker import config as tconfig
from ticker.db import migrate, queries
from ticker.ingest.session_logger import SessionLogger

TS = "2026-03-01T12:00:00.000+00:00"


def sample(hr, rr=None, ts=TS):
    return {"type": "sample", "timestamp": ts, "hr": hr,
            "rr_intervals_ms": rr or []}


def rows(db_path, metric):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT o.ts, o.value, o.session_id FROM observations o "
            "JOIN metrics m ON m.id = o.metric_id WHERE m.name = ? ORDER BY o.ts",
            (metric,)).fetchall()
    finally:
        conn.close()


@pytest.fixture
def logger(tmp_path):
    made = SessionLogger("ble", db_path=tmp_path / "app.sqlite3")
    yield made
    if made.store is not None:
        made.close()


# -- opening ------------------------------------------------------------

def test_it_creates_and_migrates_the_database(tmp_path):
    db_path = tmp_path / "new.sqlite3"
    made = SessionLogger("ble", db_path=db_path)
    try:
        assert made.available is True
        assert made.error is None
        conn = sqlite3.connect(str(db_path))
        assert migrate.current_version(conn) == migrate.LATEST_VERSION
        conn.close()
    finally:
        made.close()


def test_it_migrates_an_existing_v1_database_in_place(tmp_path):
    """The upgrade path a real user takes: v1 data recorded by the old app,
    then the new one starts."""
    db_path = tmp_path / "hrm_data.sqlite3"
    v1 = storage.AsyncSessionStore(db_path)
    v1.start_session("t", "HRM-Dual", "AA:BB", label="old ride")
    v1.insert_sample("t", TS, 80, [750.0])
    v1.end_session("t")
    v1.close()

    made = SessionLogger("ble", db_path=db_path)
    try:
        assert made.available is True
    finally:
        made.close()

    # The old ride survived, as observations.
    assert [v for _, v, _ in rows(db_path, "heart_rate_bpm")] == [80.0]
    assert migrate.backup_path(db_path).exists()


def test_new_data_joins_the_migrated_source_rather_than_a_second_one(tmp_path):
    """The migration creates the 'ble' source row and the app looks it up by
    the same name; a mismatch would split one strap across two sources."""
    db_path = tmp_path / "hrm_data.sqlite3"
    v1 = storage.AsyncSessionStore(db_path)
    v1.start_session("t", "HRM-Dual", "AA:BB", label="old")
    v1.insert_sample("t", TS, 80, [])
    v1.end_session("t")
    v1.close()

    made = SessionLogger("ble", db_path=db_path)
    try:
        made.start_session(label="new")
        made.log_sample(sample(90, ts="2026-03-01T13:00:00.000+00:00"))
        made.end_session()
    finally:
        made.close()

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sources WHERE vendor = 'ble'").fetchone() == (1,)
        # Old and new data share the one source row.
        assert conn.execute(
            "SELECT COUNT(DISTINCT source_id) FROM observations").fetchone() == (1,)
    finally:
        conn.close()


def test_an_unopenable_database_leaves_the_app_usable(tmp_path):
    blocker = tmp_path / "this-is-a-file"
    blocker.write_text("")
    errors = queue.Queue()

    made = SessionLogger("ble", db_path=blocker / "sub" / "app.sqlite3",
                         error_queue=errors)
    assert made.available is False
    assert made.error is not None
    # None of these may raise: the window still has to show live bpm.
    assert made.start_session(label="x") is None
    made.log_sample(sample(70))
    made.end_session()
    made.close()

    assert str(blocker) in errors.get_nowait()["message"]


# -- session lifecycle ---------------------------------------------------

def test_a_session_records_heart_rate_rr_and_derived_hrv(logger):
    logger.start_session(label="tempo", device_name="HRM-Dual",
                         device_address="AA:BB")
    ts = 0
    for i in range(40):
        rr = 810.0 if i % 2 else 790.0
        ts += rr
        stamp = "2026-03-01T12:{:02d}:{:02d}.{:03d}+00:00".format(
            int(ts // 60000), int(ts // 1000) % 60, int(ts) % 1000)
        logger.log_sample(sample(75, [rr], ts=stamp))
    logger.end_session()
    logger.store.flush()

    assert len(rows(logger.db_path, "heart_rate_bpm")) == 40
    assert len(rows(logger.db_path, "rr_interval_ms")) == 40
    # HRV the strap never sent.
    assert len(rows(logger.db_path, "hrv_rmssd_ms")) >= 1


def test_samples_outside_a_session_are_not_logged(logger):
    """The Start button is what starts logging -- the app shows live bpm
    without recording it, exactly as v1 did."""
    logger.log_sample(sample(70))
    logger.store.flush()
    assert rows(logger.db_path, "heart_rate_bpm") == []


def test_observations_are_attached_to_their_session(logger):
    logger.start_session(label="ride")
    logger.log_sample(sample(70))
    logger.end_session()
    logger.store.flush()

    conn = sqlite3.connect(str(logger.db_path))
    try:
        session_id, label, end_ts = conn.execute(
            "SELECT id, label, end_ts FROM sessions").fetchone()
        assert label == "ride"
        assert end_ts is not None
    finally:
        conn.close()
    assert rows(logger.db_path, "heart_rate_bpm")[0][2] == session_id


def test_the_device_is_recorded_and_linked_to_the_session(logger):
    logger.start_session(device_name="HRM-Dual", device_address="AA:BB")
    logger.log_sample(sample(70))
    logger.end_session()
    logger.store.flush()

    conn = sqlite3.connect(str(logger.db_path))
    try:
        assert conn.execute(
            "SELECT name, address FROM devices").fetchall() == [("HRM-Dual", "AA:BB")]
        assert conn.execute(
            "SELECT device_id FROM sessions").fetchone() == (1,)
    finally:
        conn.close()


def test_a_source_without_a_device_address_still_logs(logger):
    # The HTTP source has no BLE address to record.
    logger.start_session(label="watch push")
    logger.log_sample(sample(70))
    logger.end_session()
    logger.store.flush()

    conn = sqlite3.connect(str(logger.db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM devices").fetchone() == (0,)
        assert conn.execute("SELECT device_id FROM sessions").fetchone() == (None,)
    finally:
        conn.close()
    assert len(rows(logger.db_path, "heart_rate_bpm")) == 1


def test_consecutive_sessions_are_independent(logger):
    logger.start_session(label="first")
    logger.log_sample(sample(70))
    logger.end_session()
    logger.start_session(label="second")
    logger.log_sample(sample(80, ts="2026-03-01T13:00:00.000+00:00"))
    logger.log_sample(sample(81, ts="2026-03-01T13:00:01.000+00:00"))
    logger.end_session()
    logger.store.flush()

    counts = {}
    conn = sqlite3.connect(str(logger.db_path))
    try:
        for label, n in conn.execute(
                "SELECT s.label, COUNT(o.id) FROM sessions s "
                "LEFT JOIN observations o ON o.session_id = s.id "
                "GROUP BY s.id"):
            counts[label] = n
    finally:
        conn.close()
    assert counts == {"first": 1, "second": 2}


def test_an_out_of_range_reading_is_rejected_and_counted(logger):
    logger.start_session()
    logger.log_sample(sample(70))
    logger.log_sample(sample(900, ts="2026-03-01T12:00:01.000+00:00"))
    logger.end_session()
    logger.store.flush()

    assert len(rows(logger.db_path, "heart_rate_bpm")) == 1
    assert logger.normalizer.rejected == 1


def test_close_commits_a_session_left_running(logger):
    # Closing the window mid-session must not lose the readings.
    logger.start_session(label="interrupted")
    logger.log_sample(sample(70))
    logger.close()

    assert len(rows(logger.db_path, "heart_rate_bpm")) == 1
    conn = sqlite3.connect(str(logger.db_path))
    try:
        assert conn.execute("SELECT end_ts FROM sessions").fetchone()[0] is not None
    finally:
        conn.close()


def test_the_http_vendor_gets_its_own_source_row(tmp_path):
    db_path = tmp_path / "both.sqlite3"
    for vendor in ("ble", "http"):
        made = SessionLogger(vendor, db_path=db_path)
        try:
            made.start_session()
            made.log_sample(sample(70))
            made.end_session()
        finally:
            made.close()

    conn = sqlite3.connect(str(db_path))
    try:
        vendors = {v for (v,) in conn.execute(
            "SELECT vendor FROM sources WHERE id IN "
            "(SELECT DISTINCT source_id FROM observations)")}
    finally:
        conn.close()
    assert vendors == {"ble", "http"}


def test_display_names_come_from_one_place():
    assert tconfig.source_display_name("ble") == "BLE strap"
    assert tconfig.source_display_name("unknown-vendor") == "unknown-vendor"


# -- the v2 data is queryable -------------------------------------------

def test_what_the_app_wrote_reads_back_through_the_v2_queries(logger):
    logger.start_session(label="ride")
    logger.log_sample(sample(70))
    logger.end_session()
    logger.store.flush()

    conn = sqlite3.connect(str(logger.db_path))
    try:
        sessions = queries.list_sessions(conn)
        assert sessions[0]["label"] == "ride"
        assert sessions[0]["vendor"] == "ble"
        assert sessions[0]["n_observations"] == 1
    finally:
        conn.close()
