"""
Tests for the agent's uplink.

The uplink is what makes 'the server is down' a non-event, so these drive
it against a poster that can be made to fail on demand rather than against
a socket: every retry, backoff and reconnect case is then exercised without
a wall-clock wait, and 'the spool drains in order when the server comes
back' is a test rather than a hope.

Nothing here starts the background thread (start=False). The drain is
driven explicitly so a test never depends on when a thread happened to wake.
"""

from datetime import datetime, timedelta, timezone

import pytest

from ticker.agent.spool import Spool
from ticker.agent.uplink import (Uplink, UplinkError, backoff_delay,
                                 stable_session_id)
from ticker.model import Observation, SessionRecord

UTC = timezone.utc
START = datetime(2026, 7, 4, 6, 0, 0, tzinfo=UTC)


class FakeServer:
    """A poster that records what it was sent, and can be made to fail."""

    def __init__(self):
        self.posts = []
        self.fail_with = None

    def __call__(self, url, body, token=None, timeout=None):
        if self.fail_with is not None:
            raise self.fail_with
        self.posts.append((url, body, token))
        return {"ok": True, "accepted": len(body.get("observations", ()))}

    @property
    def observations(self):
        return [o for _, body, _ in self.posts
                for o in body.get("observations", ())]

    def offline(self):
        self.fail_with = UplinkError("connection refused")

    def online(self):
        self.fail_with = None


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def uplink(tmp_path, server):
    link = Uplink(server_url="http://server:8477", token="secret",
                  spool=Spool(tmp_path / "spool.sqlite3", max_rows=0),
                  poster=server, sleep=lambda _: None, start=False)
    yield link
    link.spool.close()


def observations(count, start=START):
    return [Observation("heart_rate_bpm", start + timedelta(seconds=i),
                        60.0 + i) for i in range(count)]


# -- backoff --------------------------------------------------------------

def test_backoff_doubles_and_then_stops_growing():
    assert backoff_delay(0) == 0
    assert [backoff_delay(n) for n in (1, 2, 3, 4)] == [1, 2, 4, 8]
    assert backoff_delay(50) == 60.0


# -- the happy path -------------------------------------------------------

def test_observations_reach_the_server(uplink, server):
    uplink.insert_observations(None, observations(3))
    assert uplink.flush(timeout=1)
    assert [o["value"] for o in server.observations] == [60, 61, 62]
    assert uplink.spool.pending() == 0


def test_the_source_travels_with_every_batch(uplink, server):
    # The agent cannot know its source_id -- that is the knowledge the split
    # takes away -- so it names the vendor and lets the server resolve it.
    uplink.insert_observations(None, observations(1))
    uplink.flush(timeout=1)
    _, body, _ = server.posts[0]
    assert body["source"] == {"vendor": "ble", "kind": "stream",
                              "display_name": "BLE strap"}


def test_the_token_is_sent(uplink, server):
    uplink.insert_observations(None, observations(1))
    uplink.flush(timeout=1)
    assert server.posts[0][2] == "secret"


def test_the_ingest_endpoint_is_the_target(uplink, server):
    uplink.insert_observations(None, observations(1))
    uplink.flush(timeout=1)
    assert server.posts[0][0] == "http://server:8477/api/ingest"


def test_an_empty_batch_is_not_a_post(uplink, server):
    uplink.insert_observations(None, [])
    assert uplink.flush(timeout=1)
    assert server.posts == []


def test_batches_are_split_at_the_configured_size(tmp_path, server):
    link = Uplink(server_url="http://s", spool=Spool(tmp_path / "s.sqlite3",
                                                     max_rows=0),
                  batch=2, poster=server, sleep=lambda _: None, start=False)
    try:
        link.insert_observations(None, observations(5))
        assert link.flush(timeout=1)
        assert [len(b.get("observations", ())) for _, b, _ in server.posts] == [2, 2, 1]
    finally:
        link.close()


# -- offline and back -----------------------------------------------------

def test_nothing_is_lost_while_the_server_is_down(uplink, server):
    server.offline()
    uplink.insert_observations(None, observations(3))
    assert not uplink.flush(timeout=0.05)
    assert uplink.spool.pending() == 3
    assert uplink.connected is False


def test_the_spool_drains_in_order_when_the_server_returns(uplink, server):
    server.offline()
    uplink.insert_observations(None, observations(3))
    uplink.flush(timeout=0.05)
    uplink.insert_observations(None, observations(2, START + timedelta(minutes=1)))
    uplink.flush(timeout=0.05)

    server.online()
    assert uplink.flush(timeout=1)
    assert [o["value"] for o in server.observations] == [60, 61, 62, 60, 61]
    assert uplink.spool.pending() == 0
    assert uplink.connected is True


def test_a_failed_post_leaves_the_rows_to_be_re_sent(uplink, server):
    # Not acked means not deleted: a crash between the POST and the ack has
    # to re-send, and the server's upsert makes the replay harmless.
    server.offline()
    uplink.insert_observations(None, observations(2))
    uplink.flush(timeout=0.05)
    server.online()
    uplink.flush(timeout=1)
    assert len(server.observations) == 2


def test_failures_are_counted_for_the_backoff(uplink, server):
    server.offline()
    uplink.insert_observations(None, observations(1))
    for _ in range(3):
        uplink.flush(timeout=0.01)
    assert uplink.failures >= 1
    assert uplink.last_error


def test_a_successful_post_resets_the_failure_count(uplink, server):
    server.offline()
    uplink.insert_observations(None, observations(1))
    uplink.flush(timeout=0.01)
    server.online()
    uplink.flush(timeout=1)
    assert uplink.failures == 0
    assert uplink.last_error is None


def test_flush_says_no_rather_than_blocking_forever(uplink, server):
    server.offline()
    uplink.insert_observations(None, observations(1))
    assert uplink.flush(timeout=0.05) is False
    # The rows are still spooled, so 'not delivered yet' is not 'lost' --
    # the caller can stop without losing anything.
    assert uplink.spool.pending() == 1


# -- permanent rejections -------------------------------------------------

def test_a_batch_the_server_will_never_accept_is_dropped(uplink, server):
    # Otherwise one malformed row wedges everything queued behind it, which
    # loses far more than dropping the row does.
    server.fail_with = UplinkError("bad request", status=400, permanent=True)
    uplink.insert_observations(None, observations(2))
    assert uplink.flush(timeout=1)
    assert uplink.spool.pending() == 0
    assert uplink.discarded == 2


def test_an_auth_failure_is_not_permanent(uplink, server):
    # A token fixed while the agent is running should drain what piled up,
    # not find it discarded.
    server.fail_with = UplinkError("unauthorized", status=401)
    uplink.insert_observations(None, observations(2))
    uplink.flush(timeout=0.05)
    assert uplink.spool.pending() == 2
    assert uplink.discarded == 0


# -- sessions -------------------------------------------------------------

def test_a_session_is_given_an_id_that_survives_a_replay():
    record = SessionRecord(key="1", start_ts=START)
    stamped = stable_session_id(record)
    assert stamped.external_id
    # The key cannot be that id: it is a per-process counter, so a restarted
    # agent would call two different sessions '1'.
    assert stamped.external_id != record.key
    assert stable_session_id(record).external_id == stamped.external_id


def test_a_session_that_already_has_an_id_keeps_it():
    record = SessionRecord(key="1", start_ts=START, external_id="oura-123")
    assert stable_session_id(record).external_id == "oura-123"


def test_sessions_and_ends_reach_the_server(uplink, server):
    uplink.begin_session(None, SessionRecord(key="1", start_ts=START))
    uplink.insert_observations(None, observations(1))
    uplink.end_session(None, "1")
    assert uplink.flush(timeout=1)
    _, body, _ = server.posts[0]
    assert body["sessions"][0]["external_id"]
    assert body["closed_sessions"] == ["1"]


def test_the_device_is_announced_when_the_agent_knows_it(uplink, server):
    uplink.device = {"address": "AA:BB", "name": "H10"}
    uplink.insert_observations(None, observations(1))
    uplink.flush(timeout=1)
    assert server.posts[0][1]["device"] == {"address": "AA:BB", "name": "H10"}


# -- the store-shaped surface ---------------------------------------------

def test_the_pull_only_calls_are_accepted_and_ignored(uplink):
    # The Normalizer and scheduler talk to a store; an agent that raised
    # AttributeError on one of these would fail far from the cause.
    uplink.record_sync(1, "heart_rate_bpm")
    uplink.insert_raw_payload(1, "/x", None, None, b"{}")
    uplink.rebuild_rollups()


def test_status_reports_what_is_waiting(uplink, server):
    server.offline()
    uplink.insert_observations(None, observations(4))
    uplink.flush(timeout=0.01)
    status = uplink.status()
    assert status["pending"] == 4
    assert status["connected"] is False
    assert status["server"] == "http://server:8477"
    assert status["oldest"]


def test_delivered_counts_only_what_landed(uplink, server):
    server.offline()
    uplink.insert_observations(None, observations(3))
    uplink.flush(timeout=0.01)
    assert uplink.status()["delivered"] == 0
    server.online()
    uplink.flush(timeout=1)
    assert uplink.status()["delivered"] == 3


# -- the drain thread -----------------------------------------------------

def test_the_background_thread_drains_without_being_asked(tmp_path, server):
    link = Uplink(server_url="http://s", spool=Spool(tmp_path / "s.sqlite3",
                                                     max_rows=0),
                  poster=server, sleep=lambda _: None)
    try:
        link.insert_observations(None, observations(2))
        # close() makes its own last attempt, which is the guarantee that a
        # clean stop with the server up leaves nothing spooled.
        link.close(timeout=5)
        assert len(server.observations) == 2
    finally:
        pass


def test_a_concurrent_flush_and_drain_do_not_double_post(tmp_path, server):
    # Both take() the same rows unless one drain runs at a time, and a
    # session posted twice is two session rows rather than one.
    link = Uplink(server_url="http://s", spool=Spool(tmp_path / "s.sqlite3",
                                                     max_rows=0),
                  poster=server, sleep=lambda _: None)
    try:
        link.insert_observations(None, observations(200))
        link.flush(timeout=5)
        link.close(timeout=5)
        assert len(server.observations) == 200
    finally:
        pass
