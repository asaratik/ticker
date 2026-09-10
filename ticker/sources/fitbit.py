"""
Fitbit, as a PullSource.

The third connector, and the first that needs the full OAuth2 flow: section
7 lists Fitbit as OAuth2 + PKCE, rate limited per user per hour, with
intraday heart rate behind an extra approval. All three of those shape this
module more than the parsing does.

Two things here are genuinely different from Oura, and both are the kind of
thing that is silently wrong rather than loudly broken:

**Fitbit reports in the profile's local time, without an offset.** An
intraday heart rate point arrives as `"time": "07:03:00"` next to the date
that was asked for, and a sleep log as `"2026-01-14T23:41:30.000"`. None of
those carry a zone. Section 2.4 is explicit that naive timestamps are never
stored and every connector converts at the edge, so this one is constructed
with the profile's IANA zone and attaches it before anything leaves. Reading
those as UTC would look completely reasonable and put every night's sleep
several hours from where it happened.

**Rate limits are low.** 150 requests per hour per user, and the per-day
endpoints below cost one request per day of the window, so a year of
backfill is not a thing that fits in an afternoon. `RateLimited` carries the
server's own reset when it sends one; the scheduler decides what to do about
it, and nothing here ever sleeps.

Obeys the connector rules: never touches SQLite, never sleeps
for rate limiting, never decides what "now" means.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 and older
    ZoneInfo = None

from ticker.auth import oauth
from ticker.model import Observation, SessionRecord
from ticker.sources.base import (AuthExpired, PermanentError, RateLimited,
                                 SourceHealth, TransientError)

log = logging.getLogger(__name__)

BASE_URL = "https://api.fitbit.com"
AUTHORIZE_URL = "https://www.fitbit.com/oauth2/authorize"
TOKEN_URL = "https://api.fitbit.com/oauth2/token"
USER_AGENT = "ticker/2 (+https://github.com/ticker)"
TIMEOUT_SEC = 30.0

# Fitbit's documented ceiling is 150 requests per hour per user. Nothing here
# enforces it -- the scheduler owns pacing -- but it is why the per-day
# endpoints are marked as such: each costs one request per day of the window.
REQUESTS_PER_HOUR = 150

# Fitbit codes sleep stages as words already; the only mapping needed is
# from its two vocabularies onto the seed metric's values. The 'classic'
# vocabulary appears for older devices and short naps.
SLEEP_STAGES = {
    "deep": "deep", "light": "light", "rem": "rem", "wake": "awake",
    "asleep": "light", "restless": "awake", "awake": "awake",
}

# metric -> (path template, window style, max days per request)
#
# 'range' endpoints take a start and end date and answer in one request.
# 'per-day' endpoints answer for a single date, so a window costs one
# request per day -- which is the whole rate-limit story above.
ENDPOINTS: Dict[str, Tuple[str, str, int]] = {
    "heart_rate_bpm": ("/1/user/-/activities/heart/date/{date}/1d/1min.json",
                       "per-day", 1),
    "steps": ("/1/user/-/activities/date/{date}.json", "per-day", 1),
    "active_energy_kcal": ("/1/user/-/activities/date/{date}.json",
                           "per-day", 1),
    "sleep_stage": ("/1.2/user/-/sleep/date/{start}/{end}.json", "range", 100),
    "sleep_duration_s": ("/1.2/user/-/sleep/date/{start}/{end}.json",
                         "range", 100),
    "spo2_pct": ("/1/user/-/spo2/date/{start}/{end}.json", "range", 30),
    "respiratory_rate_bpm": ("/1/user/-/br/date/{start}/{end}.json",
                             "range", 30),
    "skin_temp_delta_c": ("/1/user/-/temp/skin/date/{start}/{end}.json",
                          "range", 30),
    "weight_kg": ("/1/user/-/body/log/weight/date/{start}/{end}.json",
                  "range", 31),
    "body_fat_pct": ("/1/user/-/body/log/weight/date/{start}/{end}.json",
                     "range", 31),
}

CAPABILITIES = frozenset(ENDPOINTS)

# The scopes those endpoints need. 'heartrate' covers the summary; the
# intraday *series* additionally needs Fitbit to approve the application,
# which is an out-of-band request and not something re-authorizing fixes.
SCOPES = "activity heartrate sleep oxygen_saturation respiratory_rate temperature weight"


def _urllib_transport(url: str, headers: Dict[str, str]
                      ) -> Tuple[int, Dict[str, str], bytes]:
    """Default transport: stdlib only, no new dependency for one GET."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except urllib.error.URLError as exc:
        raise TransientError("could not reach Fitbit: {}".format(exc.reason))
    except OSError as exc:
        raise TransientError("could not reach Fitbit: {}".format(exc))


class FitbitSource:
    """Pulls Fitbit data for one account.

    `profile_tz` is the IANA zone the Fitbit account reports in. It is not
    optional in any meaningful sense: every timestamp this API returns is
    local and unlabelled, so without it there is nothing to convert at the
    edge and the naive-timestamp rule cannot be kept. It defaults to UTC only
    so that a misconfigured source fails loudly on inspection rather than at
    import.
    """

    vendor = "fitbit"

    def __init__(self, auth_ref: Optional[str] = None,
                 client_id: str = "",
                 profile_tz: str = "UTC",
                 tokens: Optional[oauth.TokenSet] = None,
                 base_url: str = BASE_URL,
                 token_url: str = TOKEN_URL,
                 transport: Callable = _urllib_transport,
                 token_transport: Optional[Callable] = None,
                 on_payload: Optional[Callable[[str, datetime, datetime, bytes], None]] = None,
                 on_session: Optional[Callable[[SessionRecord], None]] = None):
        self.auth_ref = auth_ref
        self.client_id = client_id
        self.profile_tz = profile_tz
        self._tokens = tokens
        self.base_url = base_url.rstrip("/")
        self.token_url = token_url
        self._transport = transport
        self._token_transport = token_transport
        self._on_payload = on_payload
        self._on_session = on_session

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
            has_tokens = self._stored_tokens() is not None
        except Exception:
            has_tokens = False
        if not has_tokens:
            return SourceHealth(
                ok=False, state="auth_missing",
                detail="No Fitbit tokens in the keyring (entry {!r})".format(
                    self.auth_ref),
                last_success=self._last_success, last_error=self._last_error)
        return SourceHealth(ok=self._state in ("ok", "idle"), state=self._state,
                            detail=self._detail,
                            last_success=self._last_success,
                            last_error=self._last_error)

    # -- Auth ------------------------------------------------------------

    def _stored_tokens(self) -> Optional[oauth.TokenSet]:
        """Tokens from the keyring unless a set was passed in.

        Read per use rather than cached, so revoking or replacing them takes
        effect without a restart -- the same reasoning as the Oura token.
        """
        if self._tokens is not None:
            return self._tokens
        if self.auth_ref is None:
            return None
        return oauth.load_tokens(self.auth_ref)

    def _persist(self, tokens: oauth.TokenSet) -> None:
        """Write refreshed tokens back to wherever they came from."""
        if self.auth_ref is not None:
            oauth.save_tokens(self.auth_ref, tokens)
        if self._tokens is not None:
            self._tokens = tokens

    def access_token(self) -> str:
        """A live access token, refreshing first if this one is spent.

        The refresh persists before returning (see oauth.refresh_tokens):
        Fitbit rotates refresh tokens, so the one on disk is dead the moment
        the response arrives.
        """
        tokens = self._stored_tokens()
        if tokens is None:
            self._state = "auth_missing"
            raise AuthExpired(
                "no Fitbit tokens stored for {!r}".format(self.auth_ref))
        if tokens.expired():
            tokens = oauth.refresh_tokens(
                self.token_url, self.client_id, tokens, persist=self._persist,
                transport=self._token_transport)
        return tokens.access_token

    # -- PullSource ------------------------------------------------------

    def fetch(self, metric: str, since: datetime, until: datetime
              ) -> Iterable[Observation]:
        """Everything in [since, until) for one metric.

        Windows arrive in UTC and are converted to local dates before being
        asked for, because Fitbit's endpoints are addressed by the profile's
        calendar day, not by instant.
        """
        if metric not in ENDPOINTS:
            raise PermanentError("Fitbit cannot supply {!r}".format(metric))
        path, style, max_days = ENDPOINTS[metric]
        parse = self._parser_for(metric)

        out: List[Observation] = []
        for payload, day in self._payloads(path, style, max_days, since, until):
            out.extend(parse(metric, payload, day))

        self._state = "ok"
        self._last_success = until
        self._detail = None
        # Deliberately not trimmed to [since, until). Requests round out to
        # whole local days, so a window starting at midday still gets that
        # day's total -- an observation that starts before `since` and is the
        # only record of the day. Dropping it to respect the window would
        # lose data the store would otherwise dedupe for free.
        return out

    def _parser_for(self, metric: str) -> Callable:
        return {
            "heart_rate_bpm": self._parse_heart_rate,
            "steps": self._parse_activity_summary,
            "active_energy_kcal": self._parse_activity_summary,
            "sleep_stage": self._parse_sleep_stages,
            "sleep_duration_s": self._parse_sleep_duration,
            "spo2_pct": self._parse_spo2,
            "respiratory_rate_bpm": self._parse_breathing_rate,
            "skin_temp_delta_c": self._parse_skin_temp,
            "weight_kg": self._parse_body_log,
            "body_fat_pct": self._parse_body_log,
        }[metric]

    # -- Time ------------------------------------------------------------

    def _zone(self):
        """The profile's zone.

        A bad zone name is a configuration error worth surfacing as a
        permanent one: every timestamp from this source depends on it, so
        carrying on with UTC would quietly misplace all of them.
        """
        if ZoneInfo is None:
            raise PermanentError(
                "zoneinfo is unavailable, so Fitbit's local timestamps "
                "cannot be converted")
        try:
            return ZoneInfo(self.profile_tz)
        except Exception as exc:
            raise PermanentError(
                "unknown Fitbit profile timezone {!r}: {}".format(
                    self.profile_tz, exc))

    def _local_to_utc(self, stamp: datetime) -> datetime:
        """Attach the profile zone to a naive local stamp, then normalise."""
        if stamp.tzinfo is not None:
            return stamp.astimezone(timezone.utc)
        return stamp.replace(tzinfo=self._zone()).astimezone(timezone.utc)

    def _local_days(self, since: datetime, until: datetime) -> List[date]:
        """The local calendar days a UTC window touches.

        The end is inclusive of the day containing `until` -- over-fetching a
        day is free and missing one is not -- so a window that ends at
        00:30 local still asks for that day.
        """
        zone = self._zone()
        first = since.astimezone(zone).date()
        last = until.astimezone(zone).date()
        if last < first:
            return []
        return [first + timedelta(days=offset)
                for offset in range((last - first).days + 1)]

    # -- HTTP ------------------------------------------------------------

    def _payloads(self, path: str, style: str, max_days: int,
                  since: datetime, until: datetime):
        """Yield (payload, day) for each request the window needs.

        `day` is the local date a per-day response belongs to, which the
        intraday and summary parsers need because the response body itself
        does not repeat it. It is None for range responses.
        """
        days = self._local_days(since, until)
        if not days:
            return

        if style == "per-day":
            for day in days:
                url = self.base_url + path.format(date=day.isoformat())
                yield self._get(url, since, until), day
            return

        for start, end in _chunks(days, max_days):
            url = self.base_url + path.format(start=start.isoformat(),
                                              end=end.isoformat())
            yield self._get(url, since, until), None

    def _get(self, url: str, since: datetime, until: datetime):
        token = self.access_token()
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            # Fitbit localises some responses off this header; asking for the
            # profile's own units keeps weight in kg rather than stone.
            "Accept-Language": "en_US",
            "User-Agent": USER_AGENT,
        }
        status, response_headers, body = self._transport(url, headers)
        self._check(status, response_headers, body)
        if self._on_payload is not None:
            self._on_payload(url, since, until, body)
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PermanentError("Fitbit returned non-JSON: {}".format(exc))

    def _check(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        if 200 <= status < 300:
            return
        detail = self._describe(body)
        lowered = {k.lower(): v for k, v in headers.items()}

        if status == 429:
            # Fitbit sends its own reset alongside the standard header; it is
            # seconds until the hourly window rolls over.
            raw = (lowered.get("retry-after")
                   or lowered.get("fitbit-rate-limit-reset"))
            try:
                seconds = float(raw)
            except (TypeError, ValueError):
                # No usable hint: wait out the rest of the hour rather than
                # hammering a limit that resets on a fixed schedule.
                seconds = 3600.0
            self._state = "rate_limited"
            raise RateLimited(seconds, "Fitbit rate limited: " + detail)

        if status == 403 and self._is_scope_failure(body, detail):
            # Insufficient scope is not an expired token. Intraday heart rate
            # in particular needs Fitbit to approve the application, so
            # re-authorizing would loop forever without ever fixing it.
            self._state = "error"
            self._last_error = detail
            raise PermanentError(
                "Fitbit refused for scope reasons -- intraday heart rate "
                "needs Fitbit to approve the application: " + detail)

        if status in (401, 403):
            self._state = "auth_expired"
            self._last_error = detail
            raise AuthExpired("Fitbit rejected the token: " + detail)

        if status >= 500:
            self._state = "error"
            self._last_error = detail
            raise TransientError("Fitbit returned {}: {}".format(status, detail))

        self._state = "error"
        self._last_error = detail
        raise PermanentError("Fitbit returned {}: {}".format(status, detail))

    @staticmethod
    def _is_scope_failure(body: bytes, detail: str) -> bool:
        """Whether a 403 is about permission rather than a dead token.

        Read from `errorType` rather than the human message: the message for
        an intraday refusal says "does not have permission to access
        intraday data" and never uses the word scope, so matching on prose
        would classify the one case this exists for as an expired token and
        send the scheduler into a re-authorization loop that cannot succeed.
        """
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:
            return "scope" in detail.lower()
        types = set()
        if isinstance(parsed, dict):
            for error in parsed.get("errors") or []:
                if isinstance(error, dict) and error.get("errorType"):
                    types.add(str(error["errorType"]).lower())
        return bool(types & {"insufficient_scope", "insufficient_permissions"})

    @staticmethod
    def _describe(body: bytes) -> str:
        """A short, safe description of an error body.

        Section 8: payload bodies stay out of the logs, so this is truncated
        and pulls only the documented error fields.
        """
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:
            return "(unreadable response)"
        if isinstance(parsed, dict):
            errors = parsed.get("errors")
            if isinstance(errors, list) and errors:
                first = errors[0]
                if isinstance(first, dict):
                    return str(first.get("message")
                               or first.get("errorType") or "")[:200]
            for key in ("message", "error_description", "error"):
                if parsed.get(key):
                    return str(parsed[key])[:200]
        return "(no detail)"

    # -- parsers ---------------------------------------------------------

    def _parse_heart_rate(self, metric: str, payload: dict,
                          day: Optional[date]) -> List[Observation]:
        """Intraday heart rate: one point per minute, in local time."""
        intraday = payload.get("activities-heart-intraday") or {}
        dataset = intraday.get("dataset") or []
        if day is None:
            return []
        out = []
        for point in dataset:
            value, stamp = point.get("value"), point.get("time")
            if value is None or not stamp:
                continue
            try:
                parsed = time.fromisoformat(stamp)
            except ValueError:
                continue
            out.append(Observation(
                metric="heart_rate_bpm",
                ts=self._local_to_utc(datetime.combine(day, parsed)),
                value=float(value)))
        return out

    def _parse_activity_summary(self, metric: str, payload: dict,
                                day: Optional[date]) -> List[Observation]:
        """Daily totals, spanning the local day they belong to."""
        summary = payload.get("summary") or {}
        field = {"steps": "steps",
                 "active_energy_kcal": "activityCalories"}[metric]
        value = summary.get(field)
        if value is None or day is None:
            return []
        start, end = self._day_bounds(day)
        return [Observation(metric=metric, ts=start, end_ts=end,
                            value=float(value),
                            external_id=day.isoformat())]

    def _parse_sleep_stages(self, metric: str, payload: dict,
                            day: Optional[date]) -> List[Observation]:
        """Each stage segment, as a categorical observation over its span."""
        out = []
        for entry in payload.get("sleep") or []:
            levels = entry.get("levels") or {}
            log_id = str(entry.get("logId") or "")
            for segment in levels.get("data") or []:
                stage = SLEEP_STAGES.get(str(segment.get("level", "")).lower())
                seconds = segment.get("seconds")
                stamp = segment.get("dateTime")
                if stage is None or seconds is None or not stamp:
                    # An unrecognised stage code is skipped rather than
                    # guessed -- a wrong stage is worse than a missing one.
                    continue
                start = self._parse_local(stamp)
                if start is None:
                    continue
                out.append(Observation(
                    metric="sleep_stage", ts=start,
                    end_ts=start + timedelta(seconds=float(seconds)),
                    value=float(seconds), text_value=stage,
                    external_id="{}:{}".format(log_id, stamp),
                    session_key=log_id or None))
        return out

    def _parse_sleep_duration(self, metric: str, payload: dict,
                              day: Optional[date]) -> List[Observation]:
        """One figure per sleep log, spanning that sleep period."""
        out = []
        for entry in payload.get("sleep") or []:
            minutes = entry.get("minutesAsleep")
            stamp = entry.get("startTime")
            if minutes is None or not stamp:
                continue
            start = self._parse_local(stamp)
            if start is None:
                continue
            log_id = str(entry.get("logId") or "")
            end = self._parse_local(entry.get("endTime")) or (
                start + timedelta(minutes=float(minutes)))
            out.append(Observation(
                metric="sleep_duration_s", ts=start, end_ts=end,
                value=float(minutes) * 60.0, external_id=log_id,
                session_key=log_id or None))
            if self._on_session is not None and log_id:
                self._on_session(SessionRecord(
                    key=log_id, start_ts=start, end_ts=end, kind="sleep",
                    external_id=log_id))
        return out

    def _parse_spo2(self, metric: str, payload, day) -> List[Observation]:
        return self._daily_values(_entries(payload, "spo2"), "spo2_pct",
                                  lambda value: value.get("avg"))

    def _parse_breathing_rate(self, metric: str, payload,
                              day) -> List[Observation]:
        return self._daily_values(_entries(payload, "br"),
                                  "respiratory_rate_bpm",
                                  lambda value: value.get("breathingRate"))

    def _parse_skin_temp(self, metric: str, payload, day) -> List[Observation]:
        return self._daily_values(_entries(payload, "tempSkin"),
                                  "skin_temp_delta_c",
                                  lambda value: value.get("nightlyRelative"))

    def _daily_values(self, entries: List[dict], metric: str,
                      pick: Callable[[dict], Optional[float]]
                      ) -> List[Observation]:
        """Shared shape: {dateTime, value: {...}} entries covering whole days.

        Three Fitbit endpoints answer in exactly this form and differ only in
        which field inside `value` is wanted.
        """
        out = []
        for entry in entries:
            value = entry.get("value")
            raw = pick(value) if isinstance(value, dict) else value
            stamp = entry.get("dateTime")
            if raw is None or not stamp:
                continue
            try:
                entry_day = date.fromisoformat(str(stamp)[:10])
            except ValueError:
                continue
            start, end = self._day_bounds(entry_day)
            out.append(Observation(metric=metric, ts=start, end_ts=end,
                                   value=float(raw),
                                   external_id=entry_day.isoformat()))
        return out

    def _parse_body_log(self, metric: str, payload: dict,
                        day: Optional[date]) -> List[Observation]:
        """Weight logs carry both mass and body fat on one entry."""
        field = {"weight_kg": "weight", "body_fat_pct": "fat"}[metric]
        out = []
        for entry in payload.get("weight") or []:
            value = entry.get(field)
            if value is None:
                # Body fat is only present when the scale measured it; a
                # weight-only log is not a zero-percent body fat reading.
                continue
            stamp = "{}T{}".format(entry.get("date", ""),
                                   entry.get("time", "00:00:00"))
            parsed = self._parse_local(stamp)
            if parsed is None:
                continue
            out.append(Observation(metric=metric, ts=parsed, value=float(value),
                                   external_id=str(entry.get("logId") or "")))
        return out

    # -- helpers ---------------------------------------------------------

    def _parse_local(self, stamp: Optional[str]) -> Optional[datetime]:
        """Parse one of Fitbit's unlabelled local timestamps.

        They arrive with milliseconds and without an offset, which
        fromisoformat handles on 3.11+ but not on 3.9, so the fraction is
        trimmed rather than relied upon.
        """
        if not stamp:
            return None
        text = stamp.strip()
        if "." in text:
            text = text.split(".", 1)[0]
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return self._local_to_utc(parsed)

    def _day_bounds(self, day: date) -> Tuple[datetime, datetime]:
        """The UTC instants bounding one local calendar day."""
        zone = self._zone()
        start = datetime.combine(day, time.min).replace(tzinfo=zone)
        end = datetime.combine(day + timedelta(days=1),
                               time.min).replace(tzinfo=zone)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _entries(payload, key: str) -> List[dict]:
    """The list of daily entries in a response.

    The SpO2 range endpoint answers with a bare JSON array while the
    breathing-rate and skin-temperature ones wrap theirs in an object, so
    both shapes are accepted rather than special-cased at each call site.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        found = payload.get(key)
        if isinstance(found, list):
            return [item for item in found if isinstance(item, dict)]
    return []


def _chunks(days: List[date], max_days: int):
    """Split a run of days into (start, end) pairs no longer than max_days."""
    for index in range(0, len(days), max_days):
        window = days[index:index + max_days]
        yield window[0], window[-1]


def begin_authorization(client_id: str, scopes: str = SCOPES):
    """Start the browser half of the OAuth flow.

    Returns everything the caller needs to finish it. Split from
    `complete_authorization` so the UI or CLI owns actually opening a
    browser and blocking on the user -- this module stays testable and
    headless.
    """
    verifier = oauth.generate_verifier()
    state = oauth.generate_state()
    # Constructed, then started: the constructor binds the socket so the
    # ephemeral port is known in time to go into the URL below, and start()
    # begins answering. Without the start the browser connects to a listening
    # socket that never replies, and the flow hangs until the timeout.
    receiver = oauth.LoopbackReceiver().start()
    url = oauth.build_authorization_url(
        AUTHORIZE_URL, client_id=client_id,
        redirect_uri=receiver.redirect_uri, scope=scopes, state=state,
        code_challenge=oauth.challenge_for(verifier))
    return receiver, url, state, verifier


def complete_authorization(receiver, state: str, verifier: str,
                           client_id: str, auth_ref: str,
                           client_secret: Optional[str] = None,
                           token_url: str = TOKEN_URL,
                           transport: Optional[Callable] = None
                           ) -> oauth.TokenSet:
    """Wait for the redirect, exchange the code, and store the tokens."""
    code = receiver.wait(state)
    tokens = oauth.exchange_code(
        token_url, client_id=client_id, code=code, code_verifier=verifier,
        redirect_uri=receiver.redirect_uri, client_secret=client_secret,
        transport=transport)
    oauth.save_tokens(auth_ref, tokens)
    return tokens
