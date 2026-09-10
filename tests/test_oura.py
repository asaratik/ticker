"""
Tests for the Oura PullSource.

Two layers, no network. Most tests inject a transport so a response can be
anything the API might return; a couple run against a real local HTTP server
so the stdlib urllib path -- headers, status handling, error bodies -- is
exercised rather than assumed.

The canned payloads follow the shapes Oura's v2 endpoints document. If the
API changes, these are what should change with it.
"""

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ticker.model import parse_iso
from ticker.sources import base
from ticker.sources.oura import OuraSource

UTC = timezone.utc
SINCE = datetime(2026, 3, 1, tzinfo=UTC)
UNTIL = datetime(2026, 3, 3, tzinfo=UTC)


def transport_returning(*responses):
    """A transport that replays canned (status, headers, body) triples."""
    calls = []
    queued = list(responses)

    def transport(url, headers):
        calls.append((url, headers))
        status, head, body = queued.pop(0) if queued else (200, {}, b'{"data": []}')
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        return status, head, body

    transport.calls = calls
    return transport


def ok(payload):
    return (200, {}, payload)


def source(*responses, **kwargs):
    return OuraSource(token="test-token",
                      transport=transport_returning(*responses), **kwargs)


# -- protocol ------------------------------------------------------------

def test_it_satisfies_the_pull_source_protocol():
    assert isinstance(OuraSource(token="t"), base.Source)
    assert isinstance(OuraSource(token="t"), base.PullSource)
    assert OuraSource(token="t").vendor == "oura"


def test_capabilities_are_the_metrics_it_can_actually_parse():
    assert OuraSource(token="t").capabilities() == {
        "heart_rate_bpm", "spo2_pct", "steps", "active_energy_kcal",
        "sleep_duration_s", "sleep_stage"}


def test_asking_for_a_metric_it_cannot_supply_is_a_permanent_error():
    with pytest.raises(base.PermanentError):
        list(source().fetch("weight_kg", SINCE, UNTIL))


# -- parsing: one metric per value kind ---------------------------------

def test_heart_rate_becomes_point_samples():
    src = source(ok({"data": [
        {"bpm": 62, "source": "awake", "timestamp": "2026-03-01T08:00:00+00:00"},
        {"bpm": 58, "source": "rest", "timestamp": "2026-03-01T08:05:00+00:00"},
    ]}))
    got = list(src.fetch("heart_rate_bpm", SINCE, UNTIL))
    assert [o.value for o in got] == [62.0, 58.0]
    assert got[0].ts == datetime(2026, 3, 1, 8, tzinfo=UTC)
    assert got[0].end_ts is None            # instant: no end
    # Oura tags how a reading was taken, and two can share a timestamp.
    assert [o.external_id for o in got] == ["awake", "rest"]


def test_two_readings_at_one_instant_keep_distinct_external_ids():
    src = source(ok({"data": [
        {"bpm": 62, "source": "awake", "timestamp": "2026-03-01T08:00:00+00:00"},
        {"bpm": 91, "source": "workout", "timestamp": "2026-03-01T08:00:00+00:00"},
    ]}))
    got = list(src.fetch("heart_rate_bpm", SINCE, UNTIL))
    assert len({o.external_id for o in got}) == 2


def test_spo2_becomes_a_day_long_interval():
    src = source(ok({"data": [
        {"id": "abc", "day": "2026-03-01", "spo2_percentage": {"average": 96.5}},
    ]}))
    got = list(src.fetch("spo2_pct", SINCE, UNTIL))
    assert len(got) == 1
    assert got[0].value == 96.5
    assert got[0].ts == datetime(2026, 3, 1, tzinfo=UTC)
    assert got[0].end_ts == datetime(2026, 3, 2, tzinfo=UTC)
    assert got[0].external_id == "abc"


def test_a_day_the_ring_was_not_worn_yields_nothing():
    """Absence of a reading is not a reading of zero."""
    src = source(ok({"data": [
        {"id": "abc", "day": "2026-03-01", "spo2_percentage": None},
        {"id": "def", "day": "2026-03-02", "spo2_percentage": {"average": None}},
    ]}))
    assert list(src.fetch("spo2_pct", SINCE, UNTIL)) == []


def test_daily_totals_are_interval_records():
    payload = ok({"data": [
        {"id": "act1", "day": "2026-03-01", "steps": 8412, "active_calories": 430},
    ]})
    steps = list(source(payload).fetch("steps", SINCE, UNTIL))
    assert steps[0].value == 8412.0
    # Section 2.1: a daily total carries both ends.
    assert steps[0].ts == datetime(2026, 3, 1, tzinfo=UTC)
    assert steps[0].end_ts == datetime(2026, 3, 2, tzinfo=UTC)

    energy = list(source(payload).fetch("active_energy_kcal", SINCE, UNTIL))
    assert energy[0].value == 430.0


def test_sleep_duration_spans_the_sleep_period():
    src = source(ok({"data": [{
        "id": "sleep1", "day": "2026-03-01",
        "bedtime_start": "2026-02-28T23:10:00+00:00",
        "bedtime_end": "2026-03-01T07:20:00+00:00",
        "total_sleep_duration": 26000,
    }]}))
    got = list(src.fetch("sleep_duration_s", SINCE, UNTIL))
    assert got[0].value == 26000.0
    assert got[0].ts == parse_iso("2026-02-28T23:10:00+00:00")
    assert got[0].end_ts == parse_iso("2026-03-01T07:20:00+00:00")
    assert got[0].external_id == "sleep1"


def test_sleep_phases_become_categorical_five_minute_blocks():
    src = source(ok({"data": [{
        "id": "sleep1", "day": "2026-03-01",
        "bedtime_start": "2026-03-01T00:00:00+00:00",
        "bedtime_end": "2026-03-01T00:20:00+00:00",
        "total_sleep_duration": 1200,
        "sleep_phase_5_min": "4213",
    }]}))
    got = list(src.fetch("sleep_stage", SINCE, UNTIL))
    assert [o.text_value for o in got] == ["awake", "light", "deep", "rem"]
    assert got[0].ts == datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    assert got[0].end_ts == datetime(2026, 3, 1, 0, 5, tzinfo=UTC)
    assert got[1].ts == datetime(2026, 3, 1, 0, 5, tzinfo=UTC)
    # Every block of one night would otherwise collide on the natural key.
    assert [o.external_id for o in got] == [
        "sleep1:0", "sleep1:1", "sleep1:2", "sleep1:3"]


def test_an_unknown_sleep_phase_code_is_skipped_not_guessed():
    src = source(ok({"data": [{
        "id": "s", "day": "2026-03-01",
        "bedtime_start": "2026-03-01T00:00:00+00:00",
        "bedtime_end": "2026-03-01T00:15:00+00:00",
        "sleep_phase_5_min": "1X3",
    }]}))
    got = list(src.fetch("sleep_stage", SINCE, UNTIL))
    assert [o.text_value for o in got] == ["deep", "rem"]


def test_sleep_periods_are_offered_as_sessions():
    """Section 2.2: vendor-side sleeps land in the sessions table. The
    connector doesn't write them -- it hands them over."""
    seen = []
    src = source(ok({"data": [{
        "id": "sleep1", "day": "2026-03-01",
        "bedtime_start": "2026-02-28T23:10:00+00:00",
        "bedtime_end": "2026-03-01T07:20:00+00:00",
        "total_sleep_duration": 26000,
    }]}), on_session=seen.append)
    got = list(src.fetch("sleep_duration_s", SINCE, UNTIL))

    assert len(seen) == 1
    assert seen[0].kind == "sleep"
    assert seen[0].external_id == "sleep1"
    assert seen[0].end_ts == parse_iso("2026-03-01T07:20:00+00:00")
    # The observation is bound to that session.
    assert got[0].session_key == seen[0].key


def test_malformed_items_are_skipped_rather_than_crashing_the_sync():
    src = source(ok({"data": [
        {"bpm": None, "timestamp": "2026-03-01T08:00:00+00:00"},
        {"bpm": 62},
        {"bpm": 60, "source": "rest", "timestamp": "2026-03-01T08:05:00+00:00"},
    ]}))
    assert [o.value for o in src.fetch("heart_rate_bpm", SINCE, UNTIL)] == [60.0]


# -- windows and pagination ---------------------------------------------

def test_datetime_endpoints_get_an_instant_range():
    src = source()
    list(src.fetch("heart_rate_bpm", SINCE, UNTIL))
    url = src._transport.calls[0][0]
    assert "start_datetime=2026-03-01T00%3A00%3A00%2B00%3A00" in url
    assert "end_datetime=2026-03-03T00%3A00%3A00%2B00%3A00" in url


def test_date_endpoints_get_whole_days():
    src = source()
    list(src.fetch("steps", SINCE, UNTIL))
    url = src._transport.calls[0][0]
    assert "start_date=2026-03-01" in url
    assert "end_date=2026-03-03" in url
    assert "datetime" not in url


def test_pagination_follows_next_token_until_it_stops():
    src = source(
        ok({"data": [{"bpm": 60, "source": "a",
                      "timestamp": "2026-03-01T08:00:00+00:00"}],
            "next_token": "page2"}),
        ok({"data": [{"bpm": 61, "source": "a",
                      "timestamp": "2026-03-01T08:01:00+00:00"}],
            "next_token": "page3"}),
        ok({"data": [{"bpm": 62, "source": "a",
                      "timestamp": "2026-03-01T08:02:00+00:00"}]}),
    )
    got = list(src.fetch("heart_rate_bpm", SINCE, UNTIL))
    assert [o.value for o in got] == [60.0, 61.0, 62.0]
    assert len(src._transport.calls) == 3
    assert "next_token=page2" in src._transport.calls[1][0]


def test_a_cursor_that_never_terminates_gives_up():
    def endless(url, headers):
        return 200, {}, json.dumps({"data": [], "next_token": "same"}).encode()

    src = OuraSource(token="t", transport=endless)
    with pytest.raises(base.PermanentError):
        list(src.fetch("heart_rate_bpm", SINCE, UNTIL))


def test_the_bearer_token_is_sent():
    src = source()
    list(src.fetch("heart_rate_bpm", SINCE, UNTIL))
    assert src._transport.calls[0][1]["Authorization"] == "Bearer test-token"


# -- errors --------------------------------------------------------------

def test_rate_limiting_is_raised_never_slept_on():
    """Section 4.1: the connector raises RateLimited and the scheduler
    decides. Sleeping here would make the retry policy untestable."""
    src = source((429, {"Retry-After": "42"}, {"detail": "too many"}))
    with pytest.raises(base.RateLimited) as excinfo:
        list(src.fetch("heart_rate_bpm", SINCE, UNTIL))
    assert excinfo.value.retry_after == 42.0


def test_rate_limiting_without_a_header_still_gives_a_delay():
    src = source((429, {}, {"detail": "slow down"}))
    with pytest.raises(base.RateLimited) as excinfo:
        list(src.fetch("heart_rate_bpm", SINCE, UNTIL))
    assert excinfo.value.retry_after > 0


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_is_an_auth_error(status):
    src = source((status, {}, {"detail": "bad token"}))
    with pytest.raises(base.AuthExpired):
        list(src.fetch("heart_rate_bpm", SINCE, UNTIL))
    assert src.health().ok is False


def test_a_server_error_is_transient():
    src = source((503, {}, {"detail": "maintenance"}))
    with pytest.raises(base.TransientError):
        list(src.fetch("heart_rate_bpm", SINCE, UNTIL))


def test_a_client_error_is_permanent():
    src = source((400, {}, {"detail": "bad request"}))
    with pytest.raises(base.PermanentError):
        list(src.fetch("heart_rate_bpm", SINCE, UNTIL))


def test_unparseable_json_is_transient_not_a_crash():
    src = source((200, {}, b"<html>gateway timeout</html>"))
    with pytest.raises(base.TransientError):
        list(src.fetch("heart_rate_bpm", SINCE, UNTIL))


def test_health_never_raises_and_reports_a_missing_token():
    health = OuraSource(auth_ref="oura:none").health()
    assert health.ok is False
    assert health.state == "auth_missing"


def test_fetching_without_a_token_is_an_auth_error():
    src = OuraSource(auth_ref="oura:absent", transport=transport_returning())
    with pytest.raises(base.AuthExpired):
        list(src.fetch("heart_rate_bpm", SINCE, UNTIL))


def test_a_successful_fetch_reports_healthy():
    src = source(ok({"data": []}))
    list(src.fetch("heart_rate_bpm", SINCE, UNTIL))
    assert src.health().ok is True


# -- raw payloads --------------------------------------------------------

def test_raw_bodies_are_offered_verbatim_before_parsing():
    """raw_payloads exists so the normalizer can be rewritten without
    re-fetching, which only works if what's kept is untouched."""
    captured = []
    body = json.dumps({"data": [
        {"bpm": 60, "source": "a", "timestamp": "2026-03-01T08:00:00+00:00"}]}
    ).encode("utf-8")
    src = OuraSource(token="t", transport=transport_returning((200, {}, body)),
                     on_payload=lambda *args: captured.append(args))
    list(src.fetch("heart_rate_bpm", SINCE, UNTIL))

    endpoint, window_from, window_to, raw = captured[0]
    assert endpoint == "heartrate"
    assert (window_from, window_to) == (SINCE, UNTIL)
    assert raw == body


# -- the real HTTP path --------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    payload = {"data": []}
    status = 200

    def do_GET(self):
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def live_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:{}".format(server.server_port)
    server.shutdown()
    server.server_close()


def test_the_default_transport_talks_real_http(live_server):
    _Handler.status = 200
    _Handler.payload = {"data": [
        {"bpm": 55, "source": "rest", "timestamp": "2026-03-01T08:00:00+00:00"}]}
    src = OuraSource(token="t", base_url=live_server)
    got = list(src.fetch("heart_rate_bpm", SINCE, UNTIL))
    assert [o.value for o in got] == [55.0]


def test_the_default_transport_maps_an_http_error_status(live_server):
    _Handler.status = 401
    _Handler.payload = {"detail": "Unauthorized"}
    src = OuraSource(token="t", base_url=live_server)
    with pytest.raises(base.AuthExpired):
        list(src.fetch("heart_rate_bpm", SINCE, UNTIL))
    _Handler.status = 200


def test_an_unreachable_host_is_transient():
    # Port 1 on loopback: nothing listens, and the connection is refused
    # immediately rather than hanging.
    src = OuraSource(token="t", base_url="http://127.0.0.1:1")
    with pytest.raises(base.TransientError):
        list(src.fetch("heart_rate_bpm", SINCE, UNTIL))
