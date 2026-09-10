"""
Tests for the agent's local spool.

The spool is the reason an unreachable server costs nothing, so what is
tested here is mostly the awkward cases: ordering across kinds, the cut
that keeps a session end from stranding the rows behind it, the row cap,
and surviving a process that stopped without being asked to.
"""

from datetime import datetime, timedelta, timezone

import pytest

from ticker.agent.spool import Spool
from ticker.model import Observation, SessionRecord, iso_utc

UTC = timezone.utc
START = datetime(2026, 7, 4, 6, 0, 0, tzinfo=UTC)


@pytest.fixture
def spool(tmp_path):
    s = Spool(tmp_path / "spool.sqlite3", max_rows=0)
    yield s
    s.close()


def observations(count, start=START):
    return [Observation("heart_rate_bpm", start + timedelta(seconds=i),
                        60.0 + i) for i in range(count)]


def session(key="1", start=START):
    return SessionRecord(key=key, start_ts=start, label="run")


# -- the basics -----------------------------------------------------------

def test_an_empty_spool_has_nothing_to_send(spool):
    ids, body = spool.take(100)
    assert ids == [] and body == {}
    assert spool.pending() == 0


def test_observations_come_back_in_the_order_they_went_in(spool):
    spool.add_observations(observations(3))
    ids, body = spool.take(100)
    assert len(ids) == 3
    assert [o["value"] for o in body["observations"]] == [60, 61, 62]


def test_taking_does_not_remove_anything(spool):
    # Rows are deleted only once the server has acknowledged them; a crash
    # between the POST and the ack has to re-send, not lose.
    spool.add_observations(observations(2))
    spool.take(100)
    assert spool.pending() == 2


def test_acking_removes_exactly_what_was_taken(spool):
    spool.add_observations(observations(5))
    ids, _ = spool.take(2)
    spool.ack(ids)
    assert spool.pending() == 3
    _, body = spool.take(100)
    assert [o["value"] for o in body["observations"]] == [62, 63, 64]


def test_a_batch_is_capped_at_the_limit(spool):
    spool.add_observations(observations(10))
    ids, body = spool.take(4)
    assert len(ids) == 4 and len(body["observations"]) == 4


def test_acking_nothing_is_harmless(spool):
    assert spool.ack([]) == 0


# -- ordering across kinds ------------------------------------------------

def test_a_session_travels_with_the_observations_that_reference_it(spool):
    spool.add_session(session())
    spool.add_observations(observations(2))
    _, body = spool.take(100)
    assert body["sessions"][0]["key"] == "1"
    assert len(body["observations"]) == 2


def test_a_batch_is_cut_after_a_session_end(spool):
    # Within one POST the server opens sessions, writes observations, then
    # closes them. Rows queued *after* a close must not ride along, or they
    # would be filed under a session that request had already ended.
    spool.add_session(session())
    spool.add_observations(observations(2))
    spool.add_session_end("1")
    spool.add_observations(observations(2, START + timedelta(minutes=5)))

    ids, body = spool.take(100)
    assert body["closed_sessions"] == ["1"]
    assert len(body["observations"]) == 2
    assert len(ids) == 4                       # session, obs, obs, end

    spool.ack(ids)
    _, later = spool.take(100)
    assert len(later["observations"]) == 2
    assert "closed_sessions" not in later


def test_two_sessions_in_a_row_are_cut_apart(spool):
    for key in ("1", "2"):
        spool.add_session(session(key))
        spool.add_observations(observations(1))
        spool.add_session_end(key)
    ids, body = spool.take(100)
    assert body["closed_sessions"] == ["1"]
    spool.ack(ids)
    _, second = spool.take(100)
    assert second["closed_sessions"] == ["2"]


def test_a_session_end_carries_its_time_when_given_one(spool):
    end = START + timedelta(minutes=30)
    spool.add_session_end("1", end)
    _, body = spool.take(100)
    assert body["closed_sessions"] == ["1"]


def test_a_device_rides_along_with_the_session(spool):
    spool.add_session(session(), {"address": "AA:BB", "name": "H10"})
    _, body = spool.take(100)
    assert body["device"] == {"address": "AA:BB", "name": "H10"}
    # ...and not inside the session itself, which the server parses as a
    # SessionRecord and would reject an unknown field on.
    assert "device" not in body["sessions"][0]


def test_a_body_only_carries_the_keys_it_has(spool):
    spool.add_observations(observations(1))
    _, body = spool.take(100)
    assert set(body) == {"observations"}


# -- durability -----------------------------------------------------------

def test_a_spool_survives_the_process_that_wrote_it(tmp_path):
    path = tmp_path / "spool.sqlite3"
    first = Spool(path, max_rows=0)
    first.add_observations(observations(3))
    first.close()                              # as if the process had stopped

    second = Spool(path, max_rows=0)
    try:
        assert second.pending() == 3
        _, body = second.take(100)
        assert [o["value"] for o in body["observations"]] == [60, 61, 62]
    finally:
        second.close()


def test_the_spool_directory_is_created_if_it_is_missing(tmp_path):
    spool = Spool(tmp_path / "nested" / "deeper" / "spool.sqlite3", max_rows=0)
    try:
        assert spool.add_observations(observations(1)) == 1
    finally:
        spool.close()


def test_the_oldest_queued_time_is_reported(spool):
    assert spool.oldest() is None
    spool.add_observations(observations(1))
    assert spool.oldest() is not None


# -- the row cap ----------------------------------------------------------

def test_the_oldest_rows_go_first_when_the_cap_is_reached(tmp_path):
    # A machine offline for a week must stop growing rather than fill the
    # disk with the thing the spool exists to protect.
    spool = Spool(tmp_path / "s.sqlite3", max_rows=5)
    try:
        spool.add_observations(observations(8))
        assert spool.pending() == 5
        _, body = spool.take(100)
        assert [o["value"] for o in body["observations"]] == [63, 64, 65, 66, 67]
        assert spool.dropped == 3
    finally:
        spool.close()


def test_a_cap_of_zero_means_no_limit(tmp_path):
    spool = Spool(tmp_path / "s.sqlite3", max_rows=0)
    try:
        spool.add_observations(observations(50))
        assert spool.pending() == 50
        assert spool.dropped == 0
    finally:
        spool.close()


# -- the wire format ------------------------------------------------------

def test_optional_fields_are_omitted_rather_than_sent_as_null(spool):
    # At 1 Hz with RR intervals this is most of the bytes on the wire.
    spool.add_observations([Observation("heart_rate_bpm", START, 60.0)])
    _, body = spool.take(100)
    assert set(body["observations"][0]) == {"metric", "ts", "value"}


def test_what_is_set_survives_the_round_trip(spool):
    spool.add_observations([Observation(
        metric="rr_interval_ms", ts=START, value=812.5,
        end_ts=START + timedelta(seconds=1), text_value="deep",
        external_id="3", session_key="1")])
    _, body = spool.take(100)
    row = body["observations"][0]
    assert row["value"] == 812.5
    assert row["external_id"] == "3"
    assert row["session_key"] == "1"
    assert row["text_value"] == "deep"
    assert row["end_ts"] == iso_utc(START + timedelta(seconds=1))
