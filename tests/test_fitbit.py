"""
Tests for the Fitbit pull source.

The parsing is the easy half. What is actually load-bearing here, and what
most of these cover, is that Fitbit's unlabelled local timestamps come out
the other side as correct UTC instants -- including across a DST boundary,
where a "day" is not 24 hours -- and that the rate limit and scope failures
are distinguished from an expired token, because the three want completely
different responses from the scheduler.

No network: the transport is injected, exactly as in the Oura tests.
"""

import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from ticker.auth import oauth
from ticker.sources import base
from ticker.sources.fitbit import FitbitSource, _chunks, _entries

NY = "America/New_York"


def _response(payload, status=200, headers=None):
    body = json.dumps(payload).encode("utf-8")
    return (status, headers or {}, body)


def _source(payload=None, status=200, headers=None, calls=None, tz=NY,
            only_date=None, **kwargs):
    """A source wired to a transport that answers with one canned payload.

    `only_date` matters more than it looks: a UTC window converts to *two*
    local days in a western zone (midnight UTC on the 14th is 19:00 on the
    13th in New York), so a transport that answers every request with the
    same body hands back each observation twice. Real endpoints answer per
    date, and tests that assert on counts need the same.
    """
    empty = [] if isinstance(payload, list) else {}

    def transport(url, request_headers):
        if calls is not None:
            calls.append(url)
        if only_date is not None and only_date not in url:
            return _response(empty, status, headers)
        return _response(payload if payload is not None else {},
                         status, headers)

    return FitbitSource(
        tokens=oauth.TokenSet(access_token="at"), profile_tz=tz,
        transport=transport, base_url="https://fitbit.test", **kwargs)


def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# -- Protocol ---------------------------------------------------------------

def test_it_satisfies_the_pull_source_protocol():
    assert isinstance(_source(), base.PullSource)


def test_capabilities_are_the_metrics_it_can_actually_parse():
    source = _source()
    for metric in source.capabilities():
        assert source._parser_for(metric) is not None


def test_asking_for_a_metric_it_cannot_supply_is_a_permanent_error():
    with pytest.raises(base.PermanentError):
        list(_source().fetch("weight_lb", _utc(2026, 1, 1), _utc(2026, 1, 2)))


# -- Local time, which is the whole problem ---------------------------------

def test_intraday_heart_rate_is_read_in_the_profile_zone():
    """07:03 in New York is 12:03 UTC, not 07:03 UTC."""
    source = _source({"activities-heart-intraday": {
        "dataset": [{"time": "07:03:00", "value": 61}]}},
        only_date="2026-01-14")

    got = list(source.fetch("heart_rate_bpm", _utc(2026, 1, 14),
                            _utc(2026, 1, 15)))

    assert len(got) == 1
    assert got[0].ts == datetime(2026, 1, 14, 12, 3, tzinfo=timezone.utc)
    assert got[0].value == 61.0


def test_every_observation_is_timezone_aware():
    """Section 2.4: naive timestamps are never stored."""
    source = _source({"activities-heart-intraday": {
        "dataset": [{"time": "07:03:00", "value": 61}]}})
    for observation in source.fetch("heart_rate_bpm", _utc(2026, 1, 14),
                                    _utc(2026, 1, 15)):
        assert observation.ts.tzinfo is not None


def test_a_sleep_log_keeps_its_local_wall_clock_meaning():
    source = _source({"sleep": [{
        "logId": 99, "startTime": "2026-01-14T23:41:30.000",
        "endTime": "2026-01-15T07:12:00.000", "minutesAsleep": 430}]})

    got = list(source.fetch("sleep_duration_s", _utc(2026, 1, 14),
                            _utc(2026, 1, 16)))

    assert len(got) == 1
    # 23:41:30 EST is 04:41:30 UTC the following day.
    assert got[0].ts == datetime(2026, 1, 15, 4, 41, 30, tzinfo=timezone.utc)
    assert got[0].value == 430 * 60


def test_fractional_seconds_are_handled_on_every_supported_python():
    """3.9's fromisoformat cannot read the milliseconds Fitbit sends."""
    source = _source(tz="UTC")
    assert source._parse_local("2026-01-14T23:41:30.000") == datetime(
        2026, 1, 14, 23, 41, 30, tzinfo=timezone.utc)


def test_a_daily_total_spans_the_local_day_not_the_utc_day():
    source = _source({"summary": {"steps": 8412}}, only_date="2026-01-14")

    got = list(source.fetch("steps", _utc(2026, 1, 14), _utc(2026, 1, 15)))

    assert len(got) == 1
    # Local midnight in New York is 05:00 UTC in January.
    assert got[0].ts == datetime(2026, 1, 14, 5, tzinfo=timezone.utc)
    assert got[0].end_ts == datetime(2026, 1, 15, 5, tzinfo=timezone.utc)
    assert got[0].value == 8412


def test_a_day_that_loses_an_hour_to_dst_is_twenty_three_hours_long():
    """8 March 2026 is the US spring-forward; a 24-hour span would be wrong."""
    source = _source({"summary": {"steps": 100}})

    got = list(source.fetch("steps", _utc(2026, 3, 8, 12), _utc(2026, 3, 8, 13)))

    assert len(got) == 1
    assert got[0].end_ts - got[0].ts == timedelta(hours=23)


def test_an_unknown_profile_timezone_is_a_permanent_error():
    """Carrying on with UTC would misplace every timestamp silently."""
    source = _source({"summary": {"steps": 1}}, tz="Mars/Olympus_Mons")
    with pytest.raises(base.PermanentError):
        list(source.fetch("steps", _utc(2026, 1, 14), _utc(2026, 1, 15)))


# -- Windowing --------------------------------------------------------------

def test_a_per_day_metric_costs_one_request_per_local_day():
    calls = []
    source = _source({"summary": {"steps": 1}}, calls=calls)

    list(source.fetch("steps", _utc(2026, 1, 14, 12), _utc(2026, 1, 17, 12)))

    assert len(calls) == 4  # the 14th through the 17th inclusive
    assert "/2026-01-14.json" in calls[0]


def test_a_range_metric_is_chunked_to_the_endpoints_limit():
    calls = []
    source = _source({"spo2": []}, calls=calls)

    # 70 days against a 30-day limit is three requests.
    list(source.fetch("spo2_pct", _utc(2026, 1, 1), _utc(2026, 3, 11)))

    assert len(calls) == 3


def test_chunks_never_exceed_the_limit():
    from datetime import date
    days = [date(2026, 1, 1) + timedelta(days=n) for n in range(70)]
    for start, end in _chunks(days, 30):
        assert (end - start).days < 30


def test_a_backwards_window_asks_for_nothing():
    calls = []
    source = _source({"summary": {}}, calls=calls)
    assert list(source.fetch("steps", _utc(2026, 1, 15),
                             _utc(2026, 1, 14))) == []
    assert calls == []


def test_results_are_not_trimmed_to_the_window():
    """A daily total starts at local midnight, before a midday `since`.

    Trimming would drop the only record of that day; the store dedupes
    overlapping writes for free, so returning it is strictly better.
    """
    source = _source({"summary": {"steps": 8412}})
    got = list(source.fetch("steps", _utc(2026, 1, 14, 18),
                            _utc(2026, 1, 14, 19)))
    assert len(got) == 1
    assert got[0].ts < _utc(2026, 1, 14, 18)


# -- Failure modes the scheduler reacts to ----------------------------------

def test_rate_limiting_is_raised_with_the_servers_own_reset():
    source = _source({"errors": [{"message": "too many"}]}, status=429,
                     headers={"Retry-After": "412"})

    with pytest.raises(base.RateLimited) as excinfo:
        list(source.fetch("steps", _utc(2026, 1, 14), _utc(2026, 1, 15)))

    assert excinfo.value.retry_after == 412


def test_fitbits_own_rate_limit_header_is_honoured():
    source = _source({}, status=429,
                     headers={"Fitbit-Rate-Limit-Reset": "900"})
    with pytest.raises(base.RateLimited) as excinfo:
        list(source.fetch("steps", _utc(2026, 1, 14), _utc(2026, 1, 15)))
    assert excinfo.value.retry_after == 900


def test_a_rate_limit_without_a_hint_waits_out_the_hour():
    source = _source({}, status=429)
    with pytest.raises(base.RateLimited) as excinfo:
        list(source.fetch("steps", _utc(2026, 1, 14), _utc(2026, 1, 15)))
    assert excinfo.value.retry_after == 3600


def test_rate_limiting_never_sleeps():
    """Section 4.1: the connector raises, the scheduler decides."""
    import time as time_module
    source = _source({}, status=429, headers={"Retry-After": "5"})
    started = time_module.monotonic()
    with pytest.raises(base.RateLimited):
        list(source.fetch("steps", _utc(2026, 1, 14), _utc(2026, 1, 15)))
    assert time_module.monotonic() - started < 1.0


def test_insufficient_scope_is_permanent_not_an_expired_token():
    """Intraday HR needs Fitbit to approve the app; re-auth cannot fix it."""
    source = _source({"errors": [{
        "errorType": "insufficient_scope",
        "message": "This application does not have permission to "
                   "access intraday data"}]}, status=403)

    with pytest.raises(base.PermanentError) as excinfo:
        list(source.fetch("heart_rate_bpm", _utc(2026, 1, 14),
                          _utc(2026, 1, 15)))
    assert "approve" in str(excinfo.value)


def test_a_rejected_token_is_auth_expired():
    source = _source({"errors": [{"message": "expired"}]}, status=401)
    with pytest.raises(base.AuthExpired):
        list(source.fetch("steps", _utc(2026, 1, 14), _utc(2026, 1, 15)))


def test_a_server_error_is_transient():
    source = _source({"errors": [{"message": "oops"}]}, status=503)
    with pytest.raises(base.TransientError):
        list(source.fetch("steps", _utc(2026, 1, 14), _utc(2026, 1, 15)))


def test_health_never_raises_without_credentials():
    health = FitbitSource(auth_ref=None).health()
    assert health.ok is False
    assert health.state == "auth_missing"


# -- Token refresh ----------------------------------------------------------

def test_an_expired_access_token_is_refreshed_before_the_request():
    stale = oauth.TokenSet(
        access_token="old", refresh_token="rt-0",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    persisted = []

    def token_transport(url, data, headers):
        return _response({"access_token": "new", "refresh_token": "rt-1",
                          "expires_in": 3600})

    seen = {}

    def transport(url, request_headers):
        seen["auth"] = request_headers["Authorization"]
        return _response({"summary": {"steps": 5}})

    source = FitbitSource(tokens=stale, profile_tz=NY, transport=transport,
                          token_transport=token_transport,
                          base_url="https://fitbit.test")
    source._persist = persisted.append

    list(source.fetch("steps", _utc(2026, 1, 14), _utc(2026, 1, 15)))

    assert seen["auth"] == "Bearer new"
    assert persisted and persisted[0].refresh_token == "rt-1"


def test_a_live_token_is_not_refreshed():
    live = oauth.TokenSet(
        access_token="still-good", refresh_token="rt",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2))

    def token_transport(url, data, headers):
        raise AssertionError("should not have refreshed")

    source = FitbitSource(
        tokens=live, profile_tz=NY,
        transport=lambda url, headers: _response({"summary": {"steps": 1}}),
        token_transport=token_transport, base_url="https://fitbit.test")

    list(source.fetch("steps", _utc(2026, 1, 14), _utc(2026, 1, 15)))


def test_fetching_without_credentials_asks_for_authorization():
    source = FitbitSource(auth_ref=None, profile_tz=NY)
    with pytest.raises(base.AuthExpired):
        list(source.fetch("steps", _utc(2026, 1, 14), _utc(2026, 1, 15)))


# -- Parsing details --------------------------------------------------------

def test_sleep_stages_become_categorical_spans():
    source = _source({"sleep": [{"logId": 7, "levels": {"data": [
        {"dateTime": "2026-01-14T23:41:30.000", "level": "deep",
         "seconds": 600},
        {"dateTime": "2026-01-14T23:51:30.000", "level": "rem",
         "seconds": 300}]}}]})

    got = list(source.fetch("sleep_stage", _utc(2026, 1, 14),
                            _utc(2026, 1, 16)))

    assert [ob.text_value for ob in got] == ["deep", "rem"]
    assert got[0].end_ts - got[0].ts == timedelta(seconds=600)
    assert all(ob.session_key == "7" for ob in got)


def test_an_unknown_sleep_stage_is_skipped_not_guessed():
    source = _source({"sleep": [{"logId": 7, "levels": {"data": [
        {"dateTime": "2026-01-14T23:41:30.000", "level": "quantum",
         "seconds": 600}]}}]})
    assert list(source.fetch("sleep_stage", _utc(2026, 1, 14),
                             _utc(2026, 1, 16))) == []


def test_the_classic_sleep_vocabulary_is_mapped_too():
    source = _source({"sleep": [{"logId": 7, "levels": {"data": [
        {"dateTime": "2026-01-14T23:41:30.000", "level": "restless",
         "seconds": 60}]}}]})
    got = list(source.fetch("sleep_stage", _utc(2026, 1, 14),
                            _utc(2026, 1, 16)))
    assert got[0].text_value == "awake"


def test_stage_segments_of_one_night_keep_distinct_external_ids():
    source = _source({"sleep": [{"logId": 7, "levels": {"data": [
        {"dateTime": "2026-01-14T23:41:30.000", "level": "deep", "seconds": 60},
        {"dateTime": "2026-01-14T23:42:30.000", "level": "deep",
         "seconds": 60}]}}]})
    got = list(source.fetch("sleep_stage", _utc(2026, 1, 14),
                            _utc(2026, 1, 16)))
    assert got[0].external_id != got[1].external_id


def test_a_sleep_log_is_offered_as_a_session():
    sessions = []
    source = _source({"sleep": [{
        "logId": 99, "startTime": "2026-01-14T23:41:30.000",
        "endTime": "2026-01-15T07:12:00.000", "minutesAsleep": 430}]},
        on_session=sessions.append)

    list(source.fetch("sleep_duration_s", _utc(2026, 1, 14),
                      _utc(2026, 1, 16)))

    assert len(sessions) == 1
    assert sessions[0].kind == "sleep"
    assert sessions[0].key == "99"


def test_spo2_arrives_as_a_bare_array():
    """The SpO2 range endpoint answers with a list, unlike its neighbours."""
    source = _source([{"dateTime": "2026-01-14",
                       "value": {"avg": 96.4, "min": 92.0}}])
    got = list(source.fetch("spo2_pct", _utc(2026, 1, 14), _utc(2026, 1, 15)))
    assert len(got) == 1
    assert got[0].value == pytest.approx(96.4)


def test_breathing_rate_is_read_from_its_wrapper():
    source = _source({"br": [{"dateTime": "2026-01-14",
                              "value": {"breathingRate": 14.2}}]})
    got = list(source.fetch("respiratory_rate_bpm", _utc(2026, 1, 14),
                            _utc(2026, 1, 15)))
    assert got[0].value == pytest.approx(14.2)


def test_skin_temperature_is_the_nightly_relative_delta():
    source = _source({"tempSkin": [{"dateTime": "2026-01-14",
                                    "value": {"nightlyRelative": -0.3}}]})
    got = list(source.fetch("skin_temp_delta_c", _utc(2026, 1, 14),
                            _utc(2026, 1, 15)))
    assert got[0].value == pytest.approx(-0.3)


def test_a_weight_log_without_body_fat_is_not_a_zero_reading():
    source = _source({"weight": [{"logId": 4, "date": "2026-01-14",
                                  "time": "07:30:00", "weight": 74.1}]})

    weights = list(source.fetch("weight_kg", _utc(2026, 1, 14),
                                _utc(2026, 1, 15)))
    fats = list(source.fetch("body_fat_pct", _utc(2026, 1, 14),
                             _utc(2026, 1, 15)))

    assert len(weights) == 1 and weights[0].value == pytest.approx(74.1)
    assert fats == []


def test_a_day_with_no_reading_yields_nothing():
    assert list(_source({"summary": {}}).fetch(
        "steps", _utc(2026, 1, 14), _utc(2026, 1, 15))) == []


def test_a_malformed_entry_is_skipped_rather_than_raising():
    source = _source({"activities-heart-intraday": {"dataset": [
        {"time": "not-a-time", "value": 61},
        {"time": "07:03:00", "value": None},
        {"time": "07:04:00", "value": 62}]}}, only_date="2026-01-14")
    got = list(source.fetch("heart_rate_bpm", _utc(2026, 1, 14),
                            _utc(2026, 1, 15)))
    assert [ob.value for ob in got] == [62.0]


def test_non_json_is_a_permanent_error():
    def transport(url, headers):
        return (200, {}, b"<html>maintenance</html>")

    source = FitbitSource(tokens=oauth.TokenSet(access_token="at"),
                          profile_tz=NY, transport=transport,
                          base_url="https://fitbit.test")
    with pytest.raises(base.PermanentError):
        list(source.fetch("steps", _utc(2026, 1, 14), _utc(2026, 1, 15)))


def test_the_entry_extractor_tolerates_both_response_shapes():
    assert _entries([{"a": 1}], "br") == [{"a": 1}]
    assert _entries({"br": [{"a": 1}]}, "br") == [{"a": 1}]
    assert _entries({"other": []}, "br") == []
    assert _entries(None, "br") == []


def test_raw_payloads_are_offered_to_the_caller():
    seen = []
    source = _source({"summary": {"steps": 1}}, on_payload=(
        lambda url, since, until, body: seen.append(url)))
    list(source.fetch("steps", _utc(2026, 1, 14), _utc(2026, 1, 15)))
    # One per local day the UTC window touches.
    assert len(seen) == 2


def test_the_error_describer_truncates():
    assert len(FitbitSource._describe(
        json.dumps({"errors": [{"message": "x" * 900}]}).encode())) <= 200


def test_observations_carry_metric_names_the_schema_knows():
    """A metric the seed data has never heard of would fail at write time."""
    import pathlib
    import re
    seed = pathlib.Path("ticker/db/migrations/0002_multi_source.sql").read_text()
    seeded = set(re.findall(r"\('([a-z0-9_]+)'\s*,", seed))
    missing = FitbitSource().capabilities() - seeded
    assert not missing, "not in the metrics seed: {}".format(sorted(missing))


# -- The authorization flow -------------------------------------------------

def test_begin_authorization_returns_a_receiver_that_is_already_serving():
    """The regression this exists for.

    The constructor binds the socket, so the port is known and the browser
    connects happily -- but until the accept loop is running nothing ever
    answers, and the flow hangs on a listening socket until the five-minute
    timeout. A test that only checks the URL would not notice.
    """
    import threading
    import urllib.request

    from ticker.sources.fitbit import begin_authorization

    receiver, url, state, verifier = begin_authorization("cid")
    try:
        assert receiver.redirect_uri in urllib.parse.unquote(url)

        answered = {}

        def wait():
            answered["code"] = receiver.wait(state, timeout=10)

        waiter = threading.Thread(target=wait)
        waiter.start()
        with urllib.request.urlopen(
                "{}?code=abc&state={}".format(receiver.redirect_uri, state),
                timeout=10) as response:
            assert response.status == 200
        waiter.join(timeout=10)

        assert answered.get("code") == "abc", "the redirect was never answered"
    finally:
        receiver.close()


def test_the_authorization_url_asks_for_the_scopes_the_endpoints_need():
    from ticker.sources.fitbit import SCOPES, begin_authorization

    receiver, url, _state, _verifier = begin_authorization("cid")
    receiver.close()
    for scope in SCOPES.split():
        assert scope in urllib.parse.unquote(url)


def test_starting_a_receiver_twice_is_harmless():
    from ticker.auth.oauth import LoopbackReceiver

    receiver = LoopbackReceiver().start()
    try:
        port = receiver.port
        receiver.start()
        assert receiver.port == port
    finally:
        receiver.close()
