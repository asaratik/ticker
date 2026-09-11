"""
Garmin Connect as a cloud source, through the community `garminconnect`
library.

Garmin offers individuals no API -- its Health API is for approved business
partners -- so this signs in the way Garmin's own app does, through a
library that follows Garmin's changes. That makes it the one connector that
can stop working for reasons that have nothing to do with Ticker: Garmin
changed its login in March 2026 and broke every client like this for a
while. When that happens, Garmin's data export imports into the same
database (garmin_files.py) and nothing already recorded is lost.

    pip install garminconnect curl_cffi        # needs Python 3.12 or later

Sign-in happens once, on the page or with `python -m ticker.auth.setup add
garmin`. The password is used for that one login and never stored. The
session tokens the library keeps go to the OS keyring -- in chunks, since
they outgrow what Windows' credential store takes in one entry -- and are
handed back to the library in a private temporary directory, the only form
it accepts them in. Refreshed tokens are written back after every fetch.

Garmin publishes no schema for these responses; the field names below are
the ones the library's users rely on. Every response is also kept verbatim
(raw_payloads, 90 days by default), so a field read wrongly here is a parser
fix away from being right, not a re-sync away. Everything is fetched a day
at a time, which is how Garmin's endpoints are shaped, and each day's
response is cached for a few minutes: ten metrics come from seven endpoints,
and should cost seven requests, not ten.
"""

from __future__ import annotations

import atexit
import importlib
import importlib.util
import json
import logging
import shutil
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from ticker import config as tconfig
from ticker.auth import secrets
from ticker.model import Observation
from ticker.sources.base import (AuthExpired, PermanentError, RateLimited,
                                 SourceHealth, TransientError)
from ticker.sources.garmin_files import garmin_time, number

log = logging.getLogger(__name__)

# A day's response is reused for this long, across the metrics that share it.
CACHE_SEC = 600.0

# How long to back off when Garmin says slow down and doesn't say how long.
RATE_LIMIT_SEC = 900.0

# metric -> the library method whose day response it comes from.
ENDPOINTS: Dict[str, str] = {
    "heart_rate_bpm": "get_heart_rates",
    "resting_heart_rate_bpm": "get_heart_rates",
    "steps": "get_stats",
    "active_energy_kcal": "get_stats",
    "stress_level": "get_stress_data",
    "body_battery": "get_stress_data",
    "sleep_duration_s": "get_sleep_data",
    "hrv_rmssd_ms": "get_hrv_data",
    "spo2_pct": "get_spo2_data",
    "respiratory_rate_bpm": "get_respiration_data",
}

CAPABILITIES = frozenset(ENDPOINTS)


def available(library=None) -> Optional[str]:
    """None when Garmin Connect sign-in can work here; otherwise why not, in
    words the page can show."""
    if library is not None:
        return None
    if sys.version_info < (3, 12):
        return ("Garmin Connect sign-in needs Python 3.12 or later (this is "
                "{}.{}). Garmin's data export imports on any version.".format(
                    *sys.version_info[:2]))
    if importlib.util.find_spec("garminconnect") is None:
        return ("Garmin Connect sign-in needs the garminconnect library: "
                "pip install garminconnect curl_cffi")
    return None


def _library(library=None):
    why = available(library)
    if why:
        raise PermanentError(why)
    return library if library is not None else importlib.import_module("garminconnect")


def _translate(exc: Exception) -> Exception:
    """The library's exceptions, as the scheduler's. Matched by name so this
    module imports without the library installed."""
    name = type(exc).__name__
    if "TooManyRequests" in name or "429" in str(exc):
        return RateLimited(RATE_LIMIT_SEC, "Garmin is rate limiting: {}".format(exc))
    if "Authentication" in name or "401" in str(exc):
        return AuthExpired("Garmin wants you to sign in again ({})".format(exc))
    if isinstance(exc, (PermanentError, RateLimited, AuthExpired, TransientError)):
        return exc
    return TransientError("Garmin Connect: {}".format(exc))


class TokenDir:
    """The library's session files, in the private directory it insists on."""

    _live: "set" = set()

    def __init__(self, files: Optional[Dict[str, str]] = None):
        self.path = Path(tempfile.mkdtemp(prefix="ticker-garmin-"))
        self.path.chmod(0o700)
        for name, text in (files or {}).items():
            if Path(name).name == name:            # a bare file name, nothing else
                (self.path / name).write_text(text, encoding="utf-8")
        self._saved = self.read()
        TokenDir._live.add(self)

    def read(self) -> Dict[str, str]:
        if not self.path.exists():
            return {}
        return {item.name: item.read_text(encoding="utf-8")
                for item in self.path.iterdir() if item.is_file()}

    def save_if_changed(self, ref: str) -> None:
        current = self.read()
        if current and current != self._saved:
            secrets.set_large_secret(ref, json.dumps(current))
            self._saved = current

    def close(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
        TokenDir._live.discard(self)


@atexit.register
def _remove_token_dirs() -> None:
    for tokens in list(TokenDir._live):
        tokens.close()


def sign_in(email: str, password: str, auth_ref: str,
            prompt_mfa: Callable[[], str], library=None) -> None:
    """Log in once and keep the session in the keyring. `prompt_mfa` is
    called if Garmin asks for a code, and returns it."""
    module = _library(library)
    tokens = TokenDir()
    try:
        client = module.Garmin(email, password, prompt_mfa=prompt_mfa)
        try:
            client.login(str(tokens.path))
        except Exception as exc:
            raise _translate(exc)
        files = tokens.read()
        if not files:
            raise PermanentError("Garmin signed in but left no session to keep")
        secrets.set_large_secret(auth_ref, json.dumps(files))
    finally:
        tokens.close()


class GarminSource:
    """Pulls Garmin Connect data for one account."""

    vendor = "garmin"

    def __init__(self, auth_ref: Optional[str] = None,
                 on_payload: Optional[Callable] = None,
                 on_session: Optional[Callable] = None,
                 library=None, zone=None, clock: Callable[[], float] = time.monotonic):
        self.auth_ref = auth_ref
        self._on_payload = on_payload
        self._library = library
        self._zone = zone
        self._clock = clock
        self._client = None
        self._tokens: Optional[TokenDir] = None
        self._cache: Dict[Tuple[str, str], Tuple[float, Any]] = {}
        self._last_error: Optional[str] = None
        self._last_success: Optional[datetime] = None

    # -- Source ----------------------------------------------------------

    def capabilities(self) -> "frozenset[str]":
        return CAPABILITIES

    def health(self) -> SourceHealth:
        why = available(self._library)
        if why:
            return SourceHealth(ok=False, state="unavailable", detail=why)
        return SourceHealth(ok=self._last_error is None,
                            state="idle" if self._last_error is None else "error",
                            last_success=self._last_success,
                            last_error=self._last_error)

    # -- PullSource ------------------------------------------------------

    def fetch(self, metric: str, since: datetime, until: datetime
              ) -> List[Observation]:
        if metric not in ENDPOINTS:
            raise PermanentError("Garmin has no {}".format(metric))
        try:
            client = self._connect()
            out: List[Observation] = []
            for day in self._days(since, until):
                payload = self._day(client, ENDPOINTS[metric], day)
                out.extend(obs for obs in PARSERS[metric](payload, day)
                           if obs.ts < until and (obs.end_ts or obs.ts) >= since)
            if self._tokens is not None:
                self._tokens.save_if_changed(self.auth_ref)
        except Exception as exc:
            error = _translate(exc)
            self._last_error = str(error)
            if isinstance(error, AuthExpired):
                self._client = None          # sign in afresh next time
            raise error
        self._last_error = None
        self._last_success = datetime.now(timezone.utc)
        return out

    # -- plumbing --------------------------------------------------------

    def _connect(self):
        if self._client is not None:
            return self._client
        module = _library(self._library)
        stored = secrets.get_large_secret(self.auth_ref) if self.auth_ref else None
        if not stored:
            raise AuthExpired("not signed in to Garmin Connect; sign in on "
                              "Ticker's page")
        try:
            files = json.loads(stored)
        except ValueError:
            raise AuthExpired("the stored Garmin session is unreadable; sign in again")
        if self._tokens is not None:
            self._tokens.close()
        self._tokens = TokenDir(files)
        client = module.Garmin()
        client.login(str(self._tokens.path))
        self._client = client
        return client

    def _days(self, since: datetime, until: datetime) -> Iterator[date]:
        """The account's local days that [since, until) touches."""
        zone = self._zone or tconfig.local_zone()
        day = since.astimezone(zone).date()
        last = (until - timedelta(microseconds=1)).astimezone(zone).date()
        while day <= last:
            yield day
            day += timedelta(days=1)

    def _day(self, client, method: str, day: date) -> Any:
        key = (method, day.isoformat())
        cached = self._cache.get(key)
        if cached is not None and self._clock() - cached[0] < CACHE_SEC:
            return cached[1]
        payload = getattr(client, method)(day.isoformat())
        if self._on_payload is not None:
            start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
            self._on_payload(method, start, start + timedelta(days=1),
                             json.dumps(payload, default=str).encode("utf-8"))
        self._cache[key] = (self._clock(), payload)
        if len(self._cache) > 256:
            self._cache.pop(next(iter(self._cache)))
        return payload


# -- parsers: one day's response -> observations --------------------------------

def _day_span(day: date) -> Tuple[datetime, datetime]:
    # Garmin's day is the account's local day, in a zone the responses don't
    # name; like Oura's daily figures it lands as a day-long span.
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _daily(metric: str, key: str) -> Callable[[Any, date], List[Observation]]:
    def parse(payload: Any, day: date) -> List[Observation]:
        value = number((payload or {}).get(key)) if isinstance(payload, dict) else None
        if value is None:
            return []
        start, end = _day_span(day)
        return [Observation(metric, start, value, end_ts=end,
                            external_id="day:" + day.isoformat())]
    return parse


def _rows(payload: Any, key: str) -> List[list]:
    rows = (payload or {}).get(key) if isinstance(payload, dict) else None
    return [row for row in rows or [] if isinstance(row, (list, tuple)) and len(row) >= 2]


def _series(metric: str, key: str) -> Callable[[Any, date], List[Observation]]:
    """Rows of [epoch ms, value]."""
    def parse(payload: Any, day: date) -> List[Observation]:
        out = []
        for row in _rows(payload, key):
            ts, value = garmin_time(row[0]), number(row[1])
            if ts is not None and value is not None:
                out.append(Observation(metric, ts, value))
        return out
    return parse


def _battery(payload: Any, day: date) -> List[Observation]:
    """Body Battery comes as [epoch ms, level] or as [epoch ms, status,
    level, version]: after a status string, the level is the next field --
    not the last number in the row, which is the version."""
    out = []
    for row in _rows(payload, "bodyBatteryValuesArray"):
        raw = row[2] if isinstance(row[1], str) and len(row) > 2 else row[1]
        ts, value = garmin_time(row[0]), number(raw)
        if ts is not None and value is not None:
            out.append(Observation("body_battery", ts, value))
    return out


def _sleep(payload: Any, day: date) -> List[Observation]:
    dto = (payload or {}).get("dailySleepDTO") if isinstance(payload, dict) else None
    if not isinstance(dto, dict):
        return []
    start = garmin_time(dto.get("sleepStartTimestampGMT"))
    end = garmin_time(dto.get("sleepEndTimestampGMT"))
    asleep = number(dto.get("sleepTimeSeconds"))
    if asleep is None:
        parts = [number(dto.get(key)) for key in
                 ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds")]
        asleep = sum(p for p in parts if p is not None) or None
    if start is None or end is None or end <= start or not asleep:
        return []
    return [Observation("sleep_duration_s", start, asleep, end_ts=end,
                        external_id="sleep:{}".format(dto.get("id") or day.isoformat()))]


def _hrv(payload: Any, day: date) -> List[Observation]:
    summary = (payload or {}).get("hrvSummary") if isinstance(payload, dict) else None
    value = number(summary.get("lastNightAvg")) if isinstance(summary, dict) else None
    if value is None:
        return []
    start, end = _day_span(day)
    return [Observation("hrv_rmssd_ms", start, value, end_ts=end,
                        external_id="night:" + day.isoformat())]


PARSERS: Dict[str, Callable[[Any, date], List[Observation]]] = {
    "heart_rate_bpm": _series("heart_rate_bpm", "heartRateValues"),
    "resting_heart_rate_bpm": _daily("resting_heart_rate_bpm", "restingHeartRate"),
    "steps": _daily("steps", "totalSteps"),
    "active_energy_kcal": _daily("active_energy_kcal", "activeKilocalories"),
    "stress_level": _series("stress_level", "stressValuesArray"),
    "body_battery": _battery,
    "sleep_duration_s": _sleep,
    "hrv_rmssd_ms": _hrv,
    "spo2_pct": _daily("spo2_pct", "averageSpO2"),
    "respiratory_rate_bpm": _daily("respiratory_rate_bpm", "avgSleepRespirationValue"),
}
