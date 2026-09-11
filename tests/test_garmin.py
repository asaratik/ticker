"""
Tests for the Garmin Connect source, against a stand-in for garminconnect.

The real library needs Python 3.12 and an account, and neither belongs in a
test suite; the stand-in has the same surface -- Garmin(email, password,
prompt_mfa), login(tokenstore), get_*(date) -- and writes its session into
the token directory the way the library documents. What is pinned here is
Ticker's side: the password never stored, the session kept in the keyring
and written back when it refreshes, responses parsed and cached per day,
and the library's failures turned into the scheduler's.
"""

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ticker.auth import secrets, setup
from ticker.sources import garmin
from ticker.sources.base import AuthExpired, PermanentError, RateLimited

UTC = timezone.utc
DAY = datetime(2026, 5, 2, tzinfo=UTC)
MS = int(DAY.timestamp() * 1000)


class AuthenticationError(Exception):
    pass


AuthenticationError.__name__ = "GarminConnectAuthenticationError"


class TooManyRequests(Exception):
    pass


TooManyRequests.__name__ = "GarminConnectTooManyRequestsError"


def fake_library(responses=None, needs_mfa=False, refresh=False):
    lib = types.SimpleNamespace(calls=[], needs_mfa=needs_mfa, refresh=refresh,
                                responses=responses or {}, raise_on=None)

    class Garmin:
        def __init__(self, email=None, password=None, prompt_mfa=None):
            self.email, self.password, self.prompt_mfa = email, password, prompt_mfa

        def login(self, tokenstore):
            tokens = Path(tokenstore) / "garmin_tokens.json"
            if self.email:
                if lib.needs_mfa and self.prompt_mfa() != "123456":
                    raise AuthenticationError("wrong code")
                # Big enough that Windows' credential store couldn't hold it
                # in one entry.
                tokens.write_text(json.dumps({"oauth2": "t" * 3000}))
                return
            if not tokens.exists():
                raise AuthenticationError("no session")
            if lib.refresh:
                tokens.write_text(json.dumps({"oauth2": "refreshed"}))

        def __getattr__(self, method):
            if not method.startswith("get_"):
                raise AttributeError(method)

            def endpoint(day):
                lib.calls.append((method, day))
                if lib.raise_on:
                    raise lib.raise_on
                return lib.responses.get(method, {})
            return endpoint

    lib.Garmin = Garmin
    return lib


@pytest.fixture
def keyring(monkeypatch):
    kept = {}
    monkeypatch.setattr(secrets, "get_secret",
                        lambda ref, service=secrets.SERVICE: kept.get(ref))
    monkeypatch.setattr(secrets, "set_secret",
                        lambda ref, value, service=secrets.SERVICE:
                        kept.__setitem__(ref, value))
    monkeypatch.setattr(secrets, "delete_secret",
                        lambda ref, service=secrets.SERVICE:
                        kept.pop(ref, None) is not None)
    return kept


def signed_in(keyring, lib, ref="garmin:Garmin"):
    garmin.sign_in("me@example.com", "hunter2", ref, prompt_mfa=lambda: "123456",
                   library=lib)
    return ref


def source(lib, ref="garmin:Garmin", **kwargs):
    return garmin.GarminSource(auth_ref=ref, library=lib, zone=UTC, **kwargs)


# -- availability and sign-in ------------------------------------------------------

def test_without_the_library_it_says_what_is_needed():
    why = garmin.available()
    if sys.version_info < (3, 12):
        assert "3.12" in why
    else:
        assert why is None or "pip install garminconnect" in why


def test_sign_in_keeps_the_session_and_never_the_password(keyring):
    signed_in(keyring, fake_library())
    assert keyring["garmin:Garmin"].startswith("chunks:")     # too big for one entry
    stored = json.loads(secrets.get_large_secret("garmin:Garmin"))
    assert stored["garmin_tokens.json"]
    assert not any("hunter2" in value for value in keyring.values())


def test_sign_in_asks_for_the_code_when_garmin_does(keyring):
    asked = []
    garmin.sign_in("me@example.com", "pw", "garmin:G",
                   prompt_mfa=lambda: asked.append(True) or "123456",
                   library=fake_library(needs_mfa=True))
    assert asked == [True]
    with pytest.raises(AuthExpired):
        garmin.sign_in("me@example.com", "pw", "garmin:G2",
                       prompt_mfa=lambda: "000000",
                       library=fake_library(needs_mfa=True))


def test_setup_signs_in_and_registers_the_source(keyring, tmp_path):
    from ticker.db import store
    conn = store.connect(tmp_path / "g.sqlite3")
    try:
        source_id = setup.add_password(conn, "garmin", "Garmin", "me@example.com",
                                       "pw", lambda: "123456", library=fake_library())
        row = conn.execute("SELECT kind, vendor, auth_ref FROM sources WHERE id = ?",
                           (source_id,)).fetchone()
        assert row == ("pull", "garmin", "garmin:Garmin")
        # Removing it forgets every chunk of the session.
        assert setup.remove(conn, "garmin", "Garmin")
        assert not keyring
    finally:
        conn.close()


# -- fetching ------------------------------------------------------------------------

def test_not_signed_in_is_auth_expired(keyring):
    with pytest.raises(AuthExpired, match="sign in"):
        source(fake_library()).fetch("steps", DAY, DAY + timedelta(days=1))


def test_heart_rate_readings_in_the_window(keyring):
    lib = fake_library({"get_heart_rates": {"heartRateValues": [
        [MS, 58], [MS + 60000, None], [MS + 120000, 61]], "restingHeartRate": 50}})
    ref = signed_in(keyring, lib)
    observations = source(lib, ref).fetch("heart_rate_bpm", DAY, DAY + timedelta(hours=1))
    assert [(o.ts, o.value) for o in observations] == [
        (DAY, 58), (DAY + timedelta(minutes=2), 61)]


def test_daily_figures_share_one_request_per_day(keyring):
    lib = fake_library({"get_stats": {"totalSteps": 9000, "activeKilocalories": 480}})
    ref = signed_in(keyring, lib)
    garmin_source = source(lib, ref)
    steps = garmin_source.fetch("steps", DAY, DAY + timedelta(days=1))
    energy = garmin_source.fetch("active_energy_kcal", DAY, DAY + timedelta(days=1))
    assert [o.value for o in steps] == [9000] and [o.value for o in energy] == [480]
    assert steps[0].end_ts == DAY + timedelta(days=1)
    assert lib.calls == [("get_stats", "2026-05-02")]


def test_stress_skips_unmeasured_and_body_battery_reads_either_row_shape(keyring):
    lib = fake_library({"get_stress_data": {
        "stressValuesArray": [[MS, 25], [MS + 180000, -1]],
        "bodyBatteryValuesArray": [[MS, "MEASURED", 55, 2.0], [MS + 180000, 60]]}})
    ref = signed_in(keyring, lib)
    garmin_source = source(lib, ref)
    stress = garmin_source.fetch("stress_level", DAY, DAY + timedelta(days=1))
    battery = garmin_source.fetch("body_battery", DAY, DAY + timedelta(days=1))
    assert [o.value for o in stress] == [25]
    assert [o.value for o in battery] == [55, 60]


def test_sleep_hrv_spo2_and_breathing(keyring):
    start = MS - 3 * 3600 * 1000
    lib = fake_library({
        "get_sleep_data": {"dailySleepDTO": {
            "id": 99, "sleepStartTimestampGMT": start,
            "sleepEndTimestampGMT": MS + 4 * 3600 * 1000, "sleepTimeSeconds": 24000}},
        "get_hrv_data": {"hrvSummary": {"lastNightAvg": 48}},
        "get_spo2_data": {"averageSpO2": 96.0},
        "get_respiration_data": {"avgSleepRespirationValue": 14.5}})
    ref = signed_in(keyring, lib)
    garmin_source = source(lib, ref)
    window = (DAY, DAY + timedelta(days=1))
    night, = garmin_source.fetch("sleep_duration_s", *window)
    assert night.value == 24000 and night.end_ts == DAY + timedelta(hours=4)
    assert [o.value for o in garmin_source.fetch("hrv_rmssd_ms", *window)] == [48]
    assert [o.value for o in garmin_source.fetch("spo2_pct", *window)] == [96.0]
    assert [o.value for o in garmin_source.fetch("respiratory_rate_bpm", *window)] == [14.5]


def test_missing_fields_are_no_data_not_an_error(keyring):
    lib = fake_library({"get_stats": {"somethingNew": 1}})
    ref = signed_in(keyring, lib)
    assert source(lib, ref).fetch("steps", DAY, DAY + timedelta(days=1)) == []


def test_every_response_is_kept_verbatim(keyring):
    kept = []
    lib = fake_library({"get_stats": {"totalSteps": 1}})
    ref = signed_in(keyring, lib)
    source(lib, ref, on_payload=lambda *args: kept.append(args)).fetch(
        "steps", DAY, DAY + timedelta(days=1))
    endpoint, since, until, body = kept[0]
    assert endpoint == "get_stats" and since == DAY and b"totalSteps" in body


def test_a_refreshed_session_is_written_back(keyring):
    lib = fake_library({"get_stats": {"totalSteps": 1}}, refresh=True)
    ref = signed_in(keyring, lib)
    source(lib, ref).fetch("steps", DAY, DAY + timedelta(days=1))
    assert json.loads(secrets.get_large_secret(ref)) == {
        "garmin_tokens.json": json.dumps({"oauth2": "refreshed"})}


def test_rate_limits_and_expired_sessions_reach_the_scheduler_as_its_own(keyring):
    lib = fake_library()
    ref = signed_in(keyring, lib)
    garmin_source = source(lib, ref)
    lib.raise_on = TooManyRequests("slow down")
    with pytest.raises(RateLimited) as limited:
        garmin_source.fetch("steps", DAY, DAY + timedelta(days=1))
    assert limited.value.retry_after == garmin.RATE_LIMIT_SEC
    lib.raise_on = AuthenticationError("expired")
    with pytest.raises(AuthExpired):
        garmin_source.fetch("hrv_rmssd_ms", DAY, DAY + timedelta(days=1))
    assert garmin_source._client is None          # signs in afresh next time


def test_an_unknown_metric_is_permanent(keyring):
    with pytest.raises(PermanentError):
        source(fake_library()).fetch("weight_kg", DAY, DAY + timedelta(days=1))
