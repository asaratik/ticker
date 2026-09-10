"""
Oura Ring, as a PullSource.

Personal access tokens are simpler than OAuth2 for this project: one user,
one ring, and a token the user pastes once. The token lives in the OS
keyring; `auth_ref` names the
entry, and nothing here ever holds it longer than a request.

This is the connector that decides whether the abstraction is real. Where
the BLE source is a stream of one instant metric, Oura is a paginated HTTP
API returning four different value kinds:

    heart_rate_bpm     instant      point samples, minutes apart
    spo2_pct           instant      one daily average, spanning the day
    steps              cumulative   a daily total over an interval
    active_energy_kcal cumulative   likewise
    sleep_duration_s   interval     one figure per sleep period
    sleep_stage        categorical  a five-minute bucket per character

It obeys the connector rules: it never touches SQLite, it never
sleeps for rate limiting (it raises RateLimited and lets the scheduler
decide), and it never decides what "now" means -- windows arrive as
arguments.

Raw responses and vendor sleep periods leave through callbacks rather than
being written here, which keeps the no-SQLite rule intact while still
letting the scheduler persist both.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from ticker.auth import secrets
from ticker.model import Observation, SessionRecord, parse_iso
from ticker.sources.base import (AuthExpired, PermanentError, RateLimited,
                                 SourceHealth, TransientError)

log = logging.getLogger(__name__)

BASE_URL = "https://api.ouraring.com/v2/usercollection"
USER_AGENT = "ticker/2 (+https://github.com/ticker)"
TIMEOUT_SEC = 30.0

# Oura codes each five-minute block of a sleep period as a digit.
SLEEP_PHASES = {"1": "deep", "2": "light", "3": "rem", "4": "awake"}
SLEEP_PHASE_SECONDS = 300

# metric -> (endpoint, window style). 'datetime' endpoints take an instant
# range; 'date' endpoints take whole days and ignore the time of day.
ENDPOINTS: Dict[str, Tuple[str, str]] = {
    "heart_rate_bpm": ("heartrate", "datetime"),
    "spo2_pct": ("daily_spo2", "date"),
    "steps": ("daily_activity", "date"),
    "active_energy_kcal": ("daily_activity", "date"),
    "sleep_duration_s": ("sleep", "date"),
    "sleep_stage": ("sleep", "date"),
}

CAPABILITIES = frozenset(ENDPOINTS)


def _urllib_transport(url: str, headers: Dict[str, str]
                      ) -> Tuple[int, Dict[str, str], bytes]:
    """Default transport: stdlib only, no new dependency for one GET."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        # An HTTP error is still a response; the status is what matters, and
        # the body usually explains the problem.
        return exc.code, dict(exc.headers or {}), exc.read()
    except urllib.error.URLError as exc:
        raise TransientError("could not reach Oura: {}".format(exc.reason))
    except OSError as exc:
        raise TransientError("could not reach Oura: {}".format(exc))


class OuraSource:
    """Pulls Oura v2 data for one ring."""

    vendor = "oura"

    def __init__(self, token: Optional[str] = None,
                 auth_ref: Optional[str] = None,
                 base_url: str = BASE_URL,
                 transport: Callable = _urllib_transport,
                 on_payload: Optional[Callable[[str, datetime, datetime, bytes], None]] = None,
                 on_session: Optional[Callable[[SessionRecord], None]] = None,
                 page_limit: int = 100):
        self._token = token
        self.auth_ref = auth_ref
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._on_payload = on_payload
        self._on_session = on_session
        self._page_limit = page_limit

        self._state = "idle"
        self._detail: Optional[str] = None
        self._last_success: Optional[datetime] = None
        self._last_error: Optional[str] = None

    # -- Source ----------------------------------------------------------

    def capabilities(self) -> "frozenset[str]":
        return CAPABILITIES

    def health(self) -> SourceHealth:
        """Never raises -- this renders a row in a source list."""
        try:
            has_token = self.token() is not None
        except Exception:
            has_token = False
        if not has_token:
            return SourceHealth(
                ok=False, state="auth_missing",
                detail="No Oura token in the keyring (entry {!r})".format(
                    self.auth_ref),
                last_success=self._last_success, last_error=self._last_error)
        return SourceHealth(ok=self._state in ("ok", "idle"), state=self._state,
                            detail=self._detail, last_success=self._last_success,
                            last_error=self._last_error)

    def token(self) -> Optional[str]:
        """The personal access token, from the keyring unless one was passed.

        Fetched per use rather than cached at construction, so revoking or
        replacing it in the keyring takes effect without a restart.
        """
        if self._token is not None:
            return self._token
        if self.auth_ref is None:
            return None
        return secrets.get_secret(self.auth_ref)

    # -- PullSource ------------------------------------------------------

    def fetch(self, metric: str, since: datetime, until: datetime
              ) -> Iterable[Observation]:
        """Everything in [since, until) for one metric.

        Overlapping windows are expected and cheap -- the store's unique
        index makes a re-fetch a no-op -- so nothing here tries to be clever
        about window edges.
        """
        if metric not in ENDPOINTS:
            raise PermanentError("Oura cannot supply {!r}".format(metric))
        endpoint, window = ENDPOINTS[metric]
        parse = getattr(self, "_parse_" + endpoint)

        out: List[Observation] = []
        for payload in self._pages(endpoint, window, since, until):
            for item in payload.get("data", []):
                out.extend(parse(metric, item))
        self._state = "ok"
        self._last_success = until
        self._detail = None
        return out

    # -- HTTP ------------------------------------------------------------

    def _pages(self, endpoint: str, window: str, since: datetime,
               until: datetime) -> Iterator[dict]:
        token = self.token()
        if token is None:
            self._state = "auth_missing"
            raise AuthExpired(
                "no Oura token in the keyring (entry {!r})".format(self.auth_ref))

        params = self._window_params(window, since, until)
        params["limit"] = str(self._page_limit)
        next_token = None
        seen = 0
        while True:
            if next_token:
                params["next_token"] = next_token
            url = "{}/{}?{}".format(self.base_url, endpoint,
                                    urllib.parse.urlencode(params))
            status, headers, body = self._transport(url, {
                "Authorization": "Bearer " + token,
                "User-Agent": USER_AGENT,
            })
            self._check(status, headers, body)
            if self._on_payload is not None:
                # Verbatim, before parsing -- the point of raw_payloads is to
                # let the normalizer be rewritten without re-fetching.
                self._on_payload(endpoint, since, until, body)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise TransientError("Oura returned unparseable JSON: {}".format(exc))
            yield payload

            next_token = payload.get("next_token")
            seen += 1
            if not next_token:
                return
            if seen > 1000:
                # A vendor that keeps handing back the same cursor would
                # otherwise loop until the process is killed.
                raise PermanentError("Oura pagination did not terminate")

    def _check(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        if 200 <= status < 300:
            return
        detail = self._describe(body)
        if status == 429:
            retry_after = headers.get("Retry-After") or headers.get("retry-after")
            try:
                seconds = float(retry_after)
            except (TypeError, ValueError):
                seconds = 60.0
            self._state = "rate_limited"
            # Raised, never slept on: the scheduler owns the retry policy.
            raise RateLimited(seconds, "Oura rate limited: " + detail)
        if status in (401, 403):
            self._state = "auth_expired"
            self._last_error = detail
            raise AuthExpired("Oura rejected the token: " + detail)
        if status >= 500:
            self._state = "error"
            self._last_error = detail
            raise TransientError("Oura returned {}: {}".format(status, detail))
        self._state = "error"
        self._last_error = detail
        raise PermanentError("Oura returned {}: {}".format(status, detail))

    @staticmethod
    def _describe(body: bytes) -> str:
        """A short, safe description of an error body.

        Truncated and never logged at INFO by the caller so payload bodies
        stay out of the logs.
        """
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:
            return "(unreadable response)"
        if isinstance(parsed, dict):
            for key in ("detail", "message", "error"):
                if key in parsed:
                    return str(parsed[key])[:200]
        return "(no detail)"

    @staticmethod
    def _window_params(window: str, since: datetime, until: datetime
                       ) -> Dict[str, str]:
        if window == "datetime":
            return {"start_datetime": since.astimezone(timezone.utc).isoformat(),
                    "end_datetime": until.astimezone(timezone.utc).isoformat()}
        # Date endpoints work in whole days. end_date is inclusive, so the
        # last day of a half-open window is still asked for -- over-fetching
        # a day is free, missing one is not.
        return {"start_date": since.astimezone(timezone.utc).date().isoformat(),
                "end_date": until.astimezone(timezone.utc).date().isoformat()}

    # -- parsers ---------------------------------------------------------
    #
    # One per endpoint, named _parse_<endpoint> and looked up by fetch().

    @staticmethod
    def _parse_heartrate(metric: str, item: dict) -> List[Observation]:
        bpm, stamp = item.get("bpm"), item.get("timestamp")
        if bpm is None or not stamp:
            return []
        return [Observation(
            metric="heart_rate_bpm", ts=parse_iso(stamp), value=float(bpm),
            # Oura tags each reading with how it was taken ('awake', 'rest',
            # 'workout'), and two can share a timestamp. Without this they
            # would collide on the natural key and one would be lost.
            external_id=str(item.get("source") or ""))]

    def _parse_daily_spo2(self, metric: str, item: dict) -> List[Observation]:
        percentage = item.get("spo2_percentage") or {}
        average = percentage.get("average") if isinstance(percentage, dict) else None
        if average is None:
            # Oura returns the day with a null reading when the ring wasn't
            # worn; that is absence of data, not a zero.
            return []
        start, end = self._day_bounds(item.get("day"))
        if start is None:
            return []
        return [Observation(metric="spo2_pct", ts=start, end_ts=end,
                            value=float(average),
                            external_id=str(item.get("id") or ""))]

    def _parse_daily_activity(self, metric: str, item: dict) -> List[Observation]:
        field = {"steps": "steps", "active_energy_kcal": "active_calories"}[metric]
        value = item.get(field)
        if value is None:
            return []
        start, end = self._day_bounds(item.get("day"))
        if start is None:
            return []
        # A daily total is an interval record, not a point sample: it carries
        # both ends.
        return [Observation(metric=metric, ts=start, end_ts=end,
                            value=float(value),
                            external_id=str(item.get("id") or ""))]

    def _parse_sleep(self, metric: str, item: dict) -> List[Observation]:
        start = item.get("bedtime_start")
        end = item.get("bedtime_end")
        if not start or not end:
            return []
        started, ended = parse_iso(start), parse_iso(end)
        external_id = str(item.get("id") or "")

        if self._on_session is not None:
            # A vendor sleep period is a session. The connector
            # doesn't write it -- it hands it to whoever asked.
            self._on_session(SessionRecord(
                key="oura-sleep:" + external_id, start_ts=started, end_ts=ended,
                kind="sleep", external_id=external_id))

        if metric == "sleep_duration_s":
            total = item.get("total_sleep_duration")
            if total is None:
                return []
            return [Observation(metric="sleep_duration_s", ts=started, end_ts=ended,
                                value=float(total), external_id=external_id,
                                session_key="oura-sleep:" + external_id)]

        return self._parse_sleep_phases(item, started, external_id)

    @staticmethod
    def _parse_sleep_phases(item: dict, started: datetime,
                            external_id: str) -> List[Observation]:
        phases = item.get("sleep_phase_5_min") or ""
        out = []
        for index, code in enumerate(phases):
            name = SLEEP_PHASES.get(code)
            if name is None:
                continue          # a code this client doesn't know yet
            block_start = started + timedelta(seconds=index * SLEEP_PHASE_SECONDS)
            out.append(Observation(
                metric="sleep_stage",
                ts=block_start,
                end_ts=block_start + timedelta(seconds=SLEEP_PHASE_SECONDS),
                value=float(code),
                text_value=name,
                # Every block of one night shares a timestamp space with
                # every other, so the index is what keeps them apart.
                external_id="{}:{}".format(external_id, index),
                session_key="oura-sleep:" + external_id))
        return out

    @staticmethod
    def _day_bounds(day: Optional[str]) -> Tuple[Optional[datetime], Optional[datetime]]:
        """A calendar day as a UTC interval.

        Oura's 'day' is the user's local day and the API doesn't say which
        zone, so this treats it as UTC. That is exact for the value and can
        be off by the user's offset for the boundary, which is why these
        land as day-long intervals rather than instants.
        """
        if not day:
            return None, None
        try:
            start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None, None
        return start, start + timedelta(days=1)
