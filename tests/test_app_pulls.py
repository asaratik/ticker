"""
Tests for the app's cloud-sync supervisor.

The scheduler's own behaviour -- windows, watermarks, backoff -- belongs to
test_pull_scheduler.py. What is tested here is what the supervisor adds for
an app that stays up: accounts come and go while it runs, "Sync now" can be
asked for from another thread, and rollups are rebuilt as it goes. Real
temporary database, fake connectors, short intervals.
"""

import threading
import time

import pytest

from ticker.app.pulls import PullSupervisor
from ticker.db import store


class FakePull:
    vendor = "fake"

    def __init__(self, gate=None, **wiring):
        self.gate = gate
        self.wiring = wiring
        self.fetches = []

    def capabilities(self):
        return frozenset({"heart_rate_bpm"})

    def fetch(self, metric, since, until):
        self.fetches.append((metric, since, until))
        if self.gate is not None:
            self.gate.wait(5)
        return []


class FakeWriter:
    def __init__(self):
        self.rollups = 0

    def record_sync(self, *args, **kwargs):
        pass

    def insert_observations(self, source_id, batch):
        pass

    def rebuild_rollups(self):
        self.rollups += 1


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "pulls.sqlite3"
    conn = store.connect(path)
    yield path, conn
    conn.close()


@pytest.fixture
def made():
    return []


@pytest.fixture
def start(db, made):
    running = []

    def begin(gate=None, **options):
        def build(**wiring):
            connector = FakePull(gate=gate, **wiring)
            made.append(connector)
            return connector

        options.setdefault("interval", 3600)
        options.setdefault("refresh_sec", 0.1)
        writer = FakeWriter()
        supervisor = PullSupervisor(db[0], writer, builders={"fake": build},
                                    **options)
        supervisor.start()
        running.append(supervisor)
        return supervisor, writer

    yield begin
    for supervisor in running:
        supervisor.stop()


def add_source(conn, name="One", vendor="fake"):
    return store.ensure_source(conn, "pull", vendor, name)


def idle(supervisor, source_id):
    status = supervisor.status().get(source_id)
    return status is not None and status["running"] and not status["syncing"]


def test_an_enabled_source_starts_syncing(start, db, made):
    source_id = add_source(db[1])
    supervisor, _ = start()
    wait_for(lambda: made and made[0].fetches)
    assert source_id in supervisor.running()
    # Wired to the writer, as `ticker sync` wires it.
    assert "on_payload" in made[0].wiring and "on_session" in made[0].wiring


def test_an_account_connected_while_running_is_picked_up(start, db, made):
    supervisor, _ = start()
    source_id = add_source(db[1])
    wait_for(lambda: source_id in supervisor.running())


def test_a_disabled_account_is_dropped(start, db):
    source_id = add_source(db[1])
    supervisor, _ = start()
    wait_for(lambda: source_id in supervisor.running())
    db[1].execute("UPDATE sources SET enabled = 0 WHERE id = ?", (source_id,))
    wait_for(lambda: source_id not in supervisor.running())


def test_sync_now_wakes_an_idle_source(start, db, made):
    source_id = add_source(db[1])
    supervisor, _ = start()
    wait_for(lambda: idle(supervisor, source_id))
    before = len(made[0].fetches)
    assert supervisor.request_sync(source_id) is True
    wait_for(lambda: len(made[0].fetches) > before)     # not an hour later


def test_sync_now_mid_sync_says_so(start, db, made):
    gate = threading.Event()
    source_id = add_source(db[1])
    supervisor, _ = start(gate=gate)
    wait_for(lambda: made and made[0].fetches)          # blocked in fetch
    try:
        assert supervisor.request_sync(source_id) is False
        assert supervisor.status()[source_id]["syncing"] is True
    finally:
        gate.set()


def test_sync_now_for_an_account_just_connected_starts_it(start, db, made):
    supervisor, _ = start(refresh_sec=3600)            # no periodic refresh
    # A round trip through the loop, so its startup refresh has run: added
    # before that, the account would be found at startup instead, and the
    # sync asked for below would rightly say it is already under way.
    assert supervisor.request_sync(999) is False
    source_id = add_source(db[1])
    assert supervisor.request_sync(source_id) is True
    wait_for(lambda: made and made[0].fetches)


def test_sync_now_for_something_that_is_not_running_is_false(start):
    supervisor, _ = start()
    assert supervisor.request_sync(999) is False


def test_restart_rebuilds_the_connector(start, db, made):
    source_id = add_source(db[1])
    supervisor, _ = start()
    wait_for(lambda: len(made) == 1)
    assert supervisor.restart(source_id) is True
    wait_for(lambda: len(made) == 2 and made[1].fetches)


def test_a_vendor_this_version_cannot_sync_is_reported(start, db):
    source_id = add_source(db[1], vendor="whoop")
    supervisor, _ = start()
    wait_for(lambda: source_id in supervisor.status())
    status = supervisor.status()[source_id]
    assert status["running"] is False
    assert "whoop" in status["problem"]


def test_rollups_are_rebuilt_while_running(start):
    supervisor, writer = start(rollup_sec=0.2)
    wait_for(lambda: writer.rollups >= 1)


def test_stopping_is_prompt_and_can_be_repeated(start, db):
    add_source(db[1])
    supervisor, _ = start()
    began = time.monotonic()
    supervisor.stop()
    supervisor.stop()
    assert time.monotonic() - began < 5
    assert supervisor.request_sync(1) is False
