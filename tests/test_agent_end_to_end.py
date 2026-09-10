"""
Agent to server to database, over a real socket.

The unit tests either side of the wire use fakes, which is what makes them
fast and exhaustive -- and exactly why this file exists too. It runs the
real server, the real uplink and the real store against a temporary
database, because the properties the split actually promises are end-to-end
ones: a replayed spool is harmless, an unreachable server costs nothing,
and the rows land under one source no matter how many times they are sent.
"""

from datetime import datetime, timedelta, timezone

import pytest

from ticker.agent import main as agent_main
from ticker.agent.spool import Spool
from ticker.agent.uplink import Uplink
from ticker.api import server as apiserver
from ticker.db import store
from ticker.model import Observation, SessionRecord

UTC = timezone.utc
START = datetime(2026, 8, 15, 7, 0, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "server.sqlite3"
    store.connect(path).close()
    return path


@pytest.fixture
def server(db):
    running = apiserver.build(db, host="127.0.0.1", port=0)
    running.start()
    yield running
    running.stop()
    running.api.writer.close()


@pytest.fixture
def spool(tmp_path):
    s = Spool(tmp_path / "spool.sqlite3", max_rows=0)
    yield s
    try:
        s.close()
    except Exception:
        pass


def agent(server_url, spool, token=None):
    return Uplink(server_url=server_url, token=token, spool=spool, start=False)


def observations(count, start=START):
    return [Observation("heart_rate_bpm", start + timedelta(seconds=i),
                        60.0 + i) for i in range(count)]


def rows(db, sql, params=()):
    conn = store.connect(db, migrate_first=False)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def settle(server):
    assert server.api.writer.flush(timeout=10)


# -- the round trip -------------------------------------------------------

def test_observations_reach_the_database(server, db, spool):
    link = agent(server.url, spool)
    link.insert_observations(None, observations(3))
    assert link.flush(timeout=10)
    settle(server)

    stored = rows(db, "SELECT value FROM observations ORDER BY ts")
    assert [r[0] for r in stored] == [60, 61, 62]


def test_a_session_and_its_observations_arrive_linked(server, db, spool):
    link = agent(server.url, spool)
    link.begin_session(None, SessionRecord(key="1", start_ts=START, label="run"))
    link.insert_observations(None, [
        Observation("heart_rate_bpm", START, 142.0, session_key="1")])
    link.end_session(None, "1", START + timedelta(minutes=20))
    assert link.flush(timeout=10)
    settle(server)

    session, = rows(db, "SELECT id, label, end_ts FROM sessions")
    assert session[1] == "run" and session[2] is not None
    linked, = rows(db, "SELECT session_id FROM observations")
    assert linked[0] == session[0]


def test_the_strap_is_registered_as_a_device(server, db, spool):
    link = agent(server.url, spool)
    link.device = {"address": "AA:BB:CC:DD", "name": "H10"}
    link.begin_session(None, SessionRecord(key="1", start_ts=START))
    link.insert_observations(None, observations(1))
    assert link.flush(timeout=10)
    settle(server)

    device, = rows(db, "SELECT name, address FROM devices")
    assert device == ("H10", "AA:BB:CC:DD")


# -- the guarantees the split rests on ------------------------------------

def test_replaying_the_same_data_changes_nothing(server, db, spool):
    """Section 10: 'a replayed spool is harmless'."""
    link = agent(server.url, spool)
    for _ in range(3):
        link.begin_session(None, SessionRecord(key="1", start_ts=START))
        link.insert_observations(None, observations(5))
        link.end_session(None, "1")
        assert link.flush(timeout=10)
    settle(server)

    assert rows(db, "SELECT COUNT(*) FROM observations")[0][0] == 5
    assert rows(db, "SELECT COUNT(*) FROM sessions")[0][0] == 1
    assert rows(db, "SELECT COUNT(*) FROM sources WHERE vendor = 'ble'")[0][0] == 1


def test_data_recorded_while_the_server_was_down_arrives_afterwards(db, spool,
                                                                    tmp_path):
    # The agent records against a server that isn't listening yet, then the
    # server starts. Nothing is lost and nothing needs re-recording.
    link = agent("http://127.0.0.1:9", spool)          # discard port
    link.insert_observations(None, observations(4))
    assert link.flush(timeout=0.2) is False
    assert spool.pending() == 4

    running = apiserver.build(db, host="127.0.0.1", port=0)
    running.start()
    try:
        link.server_url = running.url
        assert link.flush(timeout=10)
        assert running.api.writer.flush(timeout=10)
        assert rows(db, "SELECT COUNT(*) FROM observations")[0][0] == 4
        assert spool.pending() == 0
    finally:
        running.stop()
        running.api.writer.close()


def test_a_spool_written_by_an_earlier_run_is_drained_by_a_later_one(server, db,
                                                                     tmp_path):
    # The agent stopped -- crash, reboot, lid closed -- with rows still
    # waiting. A new process picks them up off disk.
    path = tmp_path / "carried.sqlite3"
    first = Spool(path, max_rows=0)
    first.add_observations(observations(3))
    first.close()

    second = Spool(path, max_rows=0)
    link = Uplink(server_url=server.url, spool=second, start=False)
    try:
        assert link.flush(timeout=10)
        settle(server)
        assert rows(db, "SELECT COUNT(*) FROM observations")[0][0] == 3
    finally:
        second.close()


def test_a_token_protected_server_rejects_an_agent_without_one(db, spool):
    running = apiserver.build(db, host="127.0.0.1", port=0, token="secret")
    running.start()
    try:
        link = agent(running.url, spool, token=None)
        link.insert_observations(None, observations(2))
        assert link.flush(timeout=0.5) is False
        # A 401 is not permanent: the rows wait for the token to be fixed
        # rather than being discarded.
        assert spool.pending() == 2

        link.token = "secret"
        assert link.flush(timeout=10)
        assert running.api.writer.flush(timeout=10)
        assert rows(db, "SELECT COUNT(*) FROM observations")[0][0] == 2
    finally:
        running.stop()
        running.api.writer.close()


# -- the CLI --------------------------------------------------------------

def test_status_reports_the_spool_without_needing_a_server(tmp_path, capsys):
    spool_path = tmp_path / "s.sqlite3"
    spool = Spool(spool_path, max_rows=0)
    spool.add_observations(observations(2))
    spool.close()

    code = agent_main.main(["--status", "--spool", str(spool_path),
                            "--server", "http://127.0.0.1:9"])
    out = capsys.readouterr().out
    assert code == 0
    assert "unreachable" in out
    assert "pending:  2" in out


def test_status_can_answer_in_json(tmp_path, capsys):
    spool_path = tmp_path / "s.sqlite3"
    Spool(spool_path, max_rows=0).close()
    code = agent_main.main(["--status", "--json", "--spool", str(spool_path)])
    assert code == 0
    assert '"pending": 0' in capsys.readouterr().out


def test_drain_pushes_what_is_spooled_and_exits(server, db, tmp_path, capsys):
    spool_path = tmp_path / "s.sqlite3"
    spool = Spool(spool_path, max_rows=0)
    spool.add_observations(observations(6))
    spool.close()

    code = agent_main.main(["--drain", "--spool", str(spool_path),
                            "--server", server.url])
    assert code == 0
    assert "drained 6" in capsys.readouterr().out
    settle(server)
    assert rows(db, "SELECT COUNT(*) FROM observations")[0][0] == 6


def test_drain_says_what_is_left_when_it_cannot_finish(tmp_path, capsys):
    spool_path = tmp_path / "s.sqlite3"
    spool = Spool(spool_path, max_rows=0)
    spool.add_observations(observations(3))
    spool.close()

    code = agent_main.main(["--drain", "--spool", str(spool_path),
                            "--server", "http://127.0.0.1:9"])
    err = capsys.readouterr().err
    assert code == 1
    assert "3 rows still spooled" in err


def test_check_server_notices_a_live_one(server):
    assert agent_main.check_server(server.url) is True


def test_check_server_does_not_raise_on_an_unreachable_one():
    assert agent_main.check_server("http://127.0.0.1:9", timeout=0.5) is False
