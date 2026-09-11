"""
Tests for the live heart-rate source inside the app.

No Bluetooth and no watch: the source factory hands back a fake that posts
v1 protocol messages onto the monitor's queue when told to, exactly as
ble_source and http_source do from their own threads. The session logger is
the real one, writing through a real writer into a temporary database,
because "a session started on the page lands in the database" is the
property worth pinning.
"""

import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from ticker.app.live import WINDOW_SEC, LiveError, LiveMonitor
from ticker.app.settings import Settings
from ticker.db import store
from ticker.model import iso_utc, now_utc


class FakeSource:
    def __init__(self, kind, out_queue, address):
        self.kind = kind
        self.address = address
        self.out = out_queue
        self.started = False
        self.stopped = False
        # A second apart, like a real source. Two samples in the same
        # millisecond share a natural key, and the store rightly keeps one.
        self._clock = now_utc()

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def status(self, status, name=None, address=None, message=None):
        self.out.put({"type": "status", "status": status, "device_name": name,
                      "device_address": address, "message": message})

    def sample(self, hr, rr=()):
        self._clock += timedelta(seconds=1)
        self.out.put({"type": "sample", "timestamp": iso_utc(self._clock),
                      "hr": hr, "rr_intervals_ms": list(rr)})

    def error(self, message):
        self.out.put({"type": "error", "message": message})


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "live.sqlite3"
    store.connect(path).close()
    writer = store.AsyncStore(path, migrate_first=False)
    yield path, writer
    writer.close()


@pytest.fixture
def make(db, tmp_path, monkeypatch):
    """Build and start a monitor; every one made is closed afterwards."""
    monkeypatch.delenv("HRM_SOURCE", raising=False)
    monkeypatch.delenv("HRM_DEVICE_ADDRESS", raising=False)
    path, writer = db
    built = []

    def factory(kind, out_queue, address):
        if kind not in ("ble", "http"):
            raise ValueError("Unknown heart rate source {!r}".format(kind))
        source = FakeSource(kind, out_queue, address)
        made.append(source)
        return source

    made = []

    def build(**kwargs):
        settings = Settings(tmp_path / "settings.json")
        monitor = LiveMonitor(settings, writer, db_path=path,
                              make_source=factory, **kwargs)
        monitor.start()
        built.append(monitor)
        return SimpleNamespace(monitor=monitor, made=made, settings=settings)

    yield build
    for monitor in built:
        monitor.close()


def connected(env, name="H10"):
    """Switch to a strap and bring it up; returns the fake."""
    env.monitor.set_source("ble")
    source = env.made[-1]
    source.status("connected", name=name, address="AA:BB")
    wait_for(lambda: env.monitor.snapshot()["status"] == "connected")
    return source


def count(path, sql):
    conn = store.connect(path, migrate_first=False)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


# -- choosing a source -------------------------------------------------------

def test_it_starts_off_and_scans_for_nothing(make):
    env = make()
    snap = env.monitor.snapshot()
    assert (snap["source"], snap["status"], snap["bpm"]) == ("off", "off", None)
    assert env.made == []


def test_turning_a_source_on_starts_it_and_is_remembered(make, tmp_path):
    env = make()
    env.monitor.set_source("http")
    assert env.made[-1].kind == "http" and env.made[-1].started
    # Survives a restart: the choice was written beside the database.
    assert Settings(tmp_path / "settings.json").live_source == "http"


def test_an_unknown_source_is_refused(make):
    with pytest.raises(LiveError, match="off, ble, http"):
        make().monitor.set_source("ant")


def test_a_pinned_source_cannot_be_changed_from_the_page(make, monkeypatch):
    monkeypatch.setenv("HRM_SOURCE", "http")
    env = make()
    assert env.monitor.snapshot()["pinned"] is True
    with pytest.raises(LiveError, match="HRM_SOURCE"):
        env.monitor.set_source("ble")


def test_a_source_that_cannot_start_is_shown_not_raised(make, monkeypatch):
    monkeypatch.setenv("HRM_SOURCE", "bogus")
    snap = make().monitor.snapshot()         # start() did not raise
    assert snap["status"] == "error"
    assert "Unknown" in snap["error"]


# -- readings ----------------------------------------------------------------

def test_readings_show_once_connected(make):
    env = make()
    source = connected(env)
    source.sample(72)
    wait_for(lambda: env.monitor.snapshot()["bpm"] == 72)
    snap = env.monitor.snapshot()
    assert snap["device"] == "H10"
    assert [p[1] for p in snap["points"]] == [72]


def test_the_reading_clears_when_the_connection_drops(make):
    env = make()
    source = connected(env)
    source.sample(72)
    wait_for(lambda: env.monitor.snapshot()["bpm"] == 72)
    source.status("reconnecting")
    wait_for(lambda: env.monitor.snapshot()["bpm"] is None)


def test_old_readings_fall_out_of_the_window(make):
    now = {"t": 1000.0}
    env = make(clock=lambda: now["t"])
    source = connected(env)
    source.sample(70)
    wait_for(lambda: env.monitor.snapshot()["points"])
    now["t"] += WINDOW_SEC + 1
    assert env.monitor.snapshot()["points"] == []


def test_messages_from_a_replaced_source_are_ignored(make):
    env = make()
    env.monitor.set_source("ble")
    old = env.made[-1]
    env.monitor.set_source("http")
    assert old.stopped
    # A BLE thread's last words, arriving after the switch.
    old.status("connected", name="Old strap")
    old.sample(99)
    time.sleep(0.3)
    snap = env.monitor.snapshot()
    assert snap["status"] == "starting" and snap["bpm"] is None


def test_errors_from_the_source_are_shown(make):
    env = make()
    env.monitor.set_source("ble")
    env.made[-1].error("Bluetooth is off")
    wait_for(lambda: env.monitor.snapshot()["error"] == "Bluetooth is off")


# -- sessions ----------------------------------------------------------------

def test_a_session_needs_a_connection(make):
    env = make()
    env.monitor.set_source("ble")
    with pytest.raises(LiveError, match="nothing is connected"):
        env.monitor.start_session("ride")


def test_a_session_lands_in_the_database(make, db):
    path, writer = db
    env = make()
    source = connected(env)
    snap = env.monitor.start_session("ride")
    assert snap["session"]["label"] == "ride"
    source.sample(120, rr=[500.0, 498.0])
    source.sample(130)
    wait_for(lambda: env.monitor.snapshot()["session"]["n"] == 2)
    running = env.monitor.snapshot()["session"]
    assert (running["avg"], running["min"], running["max"]) == (125, 120, 130)

    assert env.monitor.stop_session()["session"] is None
    assert writer.flush()
    assert count(path, "SELECT COUNT(*) FROM sessions WHERE label = 'ride' "
                       "AND end_ts IS NOT NULL") == 1
    # Two heart-rate rows and two beats, all inside the session.
    assert count(path, "SELECT COUNT(*) FROM observations "
                       "WHERE session_id IS NOT NULL") == 4


def test_readings_outside_a_session_are_shown_but_not_saved(make, db):
    path, writer = db
    env = make()
    connected(env).sample(80)
    wait_for(lambda: env.monitor.snapshot()["bpm"] == 80)
    assert writer.flush()
    assert count(path, "SELECT COUNT(*) FROM observations") == 0


def test_a_second_start_is_refused(make):
    env = make()
    connected(env)
    env.monitor.start_session(None)
    with pytest.raises(LiveError, match="already running"):
        env.monitor.start_session(None)


def test_switching_source_ends_the_running_session(make, db):
    path, writer = db
    env = make()
    connected(env)
    env.monitor.start_session("swap")
    env.monitor.set_source("off")
    assert writer.flush()
    assert count(path, "SELECT COUNT(*) FROM sessions WHERE label = 'swap' "
                       "AND end_ts IS NOT NULL") == 1


def test_closing_ends_the_running_session(make, db):
    path, writer = db
    env = make()
    connected(env)
    env.monitor.start_session("closing")
    env.monitor.close()
    assert writer.flush()
    assert count(path, "SELECT COUNT(*) FROM sessions WHERE label = 'closing' "
                       "AND end_ts IS NOT NULL") == 1
