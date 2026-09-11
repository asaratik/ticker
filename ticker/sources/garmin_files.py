"""
Garmin's files as an import: a .fit file, a zip of them, or the archive that
Garmin's "Export Your Data" produces.

    ticker import activity.fit
    ticker import garmin-export.zip          (or Connect -> Import on the page)

From FIT files -- the watch's own format, from its GARMIN folder over USB or
"Export Original" in Garmin Connect:
    workouts      sessions, with heart rate and beat-to-beat intervals (HRV
                  is then derived from those, exactly as for a strap)
    monitoring    all-day heart rate
    sleep         stage segments: awake, light, deep, REM

From the export archive, besides the FIT file of every uploaded activity:
    DI-Connect-Wellness/*sleepData.json    each night's window and time asleep
    DI-Connect-Aggregator/UDSFile*.json    daily steps, active energy, resting
                                           heart rate and average stress

Garmin publishes no schema for the archive's JSON. The sleep fields are the
ones community tools read (sleepStartTimestampGMT, deepSleepSeconds, ...);
the daily-summary fields are Garmin Connect's user-summary names, which the
archive is believed to share. Anything absent is skipped, and the report
names every file that was found but yielded nothing -- so a mismatch shows
up as a loud line in the import summary, not a quietly empty import.
"""

from __future__ import annotations

import hashlib
import json
import struct
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, List, Optional, Tuple

from ticker.model import Observation, SessionRecord, iso_utc
from ticker.sources import fit
from ticker.sources.base import PermanentError, SourceHealth

CAPABILITIES = frozenset({
    "heart_rate_bpm", "rr_interval_ms", "sleep_stage", "sleep_duration_s",
    "steps", "active_energy_kcal", "resting_heart_rate_bpm", "stress_level"})

# FIT sport enum -> a session label. Cosmetic: anything else is a Workout.
SPORTS = {0: "Workout", 1: "Run", 2: "Ride", 4: "Gym", 5: "Swim", 10: "Training",
          11: "Walk", 12: "Cross-country ski", 13: "Ski", 14: "Snowboard",
          15: "Row", 17: "Hike", 19: "Paddle", 21: "E-bike"}

# FIT sleep_level enum -> the stage names every connector uses. 0 is
# 'unmeasurable', which is not a stage and is skipped.
STAGES = {1: "awake", 2: "light", 3: "deep", 4: "rem"}

# FIT field numbers (fit-csharp-sdk, Profile/Mesgs).
_SESSION_START, _SESSION_SPORT, _SESSION_ELAPSED = 2, 5, 7     # elapsed: ms
_RECORD_HEART_RATE = 3
_HRV_TIME = 0                                                  # s, scale 1000
_MONITORING_TIMESTAMP_16, _MONITORING_HEART_RATE = 26, 27
_SLEEP_LEVEL = 0
_FILE_SERIAL, _FILE_CREATED = 3, 4

# The export's daily summaries: key -> metric.
_DAILY = (("totalSteps", "steps"),
          ("activeKilocalories", "active_energy_kcal"),
          ("restingHeartRate", "resting_heart_rate_bpm"),
          ("averageStressLevel", "stress_level"))


def garmin_time(value: Any) -> Optional[datetime]:
    """A Garmin timestamp as an aware UTC datetime.

    The export writes GMT as '2019-08-04T20:28:00.0' -- no offset, and a
    one-digit fraction that fromisoformat can't read before Python 3.11 --
    and the Connect API sends epoch milliseconds. Both mean UTC.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace(" ", "T").split(".")[0].rstrip("Z")
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def entries(payload: Any) -> List[dict]:
    """The records in one of Garmin's JSON files, whether it is a list of
    them, one of them, or an object holding a list of them."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if "calendarDate" in payload:
            return [payload]
        for value in payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return [item for item in value if isinstance(item, dict)]
    return []


def number(value: Any) -> Optional[float]:
    """A usable measurement: a real number, not a flag. Garmin writes -1 and
    -2 for 'not measured' in several fields."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


class GarminFiles:
    """Parses Garmin files into observations; workouts become sessions."""

    vendor = "garmin"

    def __init__(self, on_progress: Optional[Callable[[int], None]] = None,
                 on_session: Optional[Callable[[SessionRecord], None]] = None,
                 progress_every: int = 50):
        self.on_progress = on_progress
        self.on_session = on_session
        self.progress_every = progress_every
        self.seen = 0                   # files read
        self.emitted = 0
        self.found: Counter = Counter()      # what was recognised, by kind
        self.skipped: Counter = Counter()    # what couldn't be read, by kind
        self.empty: List[str] = []           # recognised files that gave nothing
        self._last_error: Optional[str] = None

    # -- Source ----------------------------------------------------------

    def capabilities(self) -> "frozenset[str]":
        return CAPABILITIES

    def health(self) -> SourceHealth:
        return SourceHealth(
            ok=self._last_error is None,
            state="idle" if self._last_error is None else "error",
            detail=self._last_error, last_error=self._last_error)

    # -- ImportSource ----------------------------------------------------

    def parse(self, path: Path) -> Iterator[Observation]:
        path = Path(path)
        if not path.exists():
            raise PermanentError("no such file: {}".format(path))
        for observation in self._walk(path):
            self.emitted += 1
            yield observation

    def report(self) -> str:
        parts = ["{} file{} read".format(self.seen, "" if self.seen == 1 else "s"),
                 "{} observations".format(self.emitted)]
        parts += ["{} {}".format(count, what) for what, count in self.found.items()]
        parts += ["{} {}".format(count, what) for what, count in self.skipped.items()]
        if self.empty:
            names = sorted(set(self.empty))
            parts.append("nothing recognised in {}{}".format(
                ", ".join(names[:3]), " and {} more".format(len(names) - 3)
                if len(names) > 3 else ""))
        return "; ".join(parts)

    # -- files -------------------------------------------------------------

    def _walk(self, path: Path) -> Iterator[Observation]:
        if path.suffix.lower() == ".fit":
            yield from self._fit(path.read_bytes(), path.name)
        elif zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                yield from self._archive(archive, depth=0)
        else:
            self._last_error = "not a .fit file or a zip"
            raise PermanentError("{} is neither a .fit file nor a zip".format(path.name))

    def _archive(self, archive: zipfile.ZipFile, depth: int) -> Iterator[Observation]:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name, lower = info.filename, info.filename.lower()
            if lower.endswith(".fit"):
                yield from self._fit(archive.read(info), name)
            elif lower.endswith(".zip") and depth < 2:
                # The export nests every uploaded activity in zips of its
                # own. Opened in place: they can run to gigabytes.
                with archive.open(info) as stream, zipfile.ZipFile(stream) as inner:
                    yield from self._archive(inner, depth + 1)
            elif lower.endswith("sleepdata.json"):
                yield from self._counted(name, self._nights(self._json(archive, info)))
            elif "udsfile" in lower and lower.endswith(".json"):
                yield from self._counted(name, self._days(self._json(archive, info)))

    def _tick(self) -> None:
        self.seen += 1
        if self.on_progress is not None and self.seen % self.progress_every == 0:
            self.on_progress(self.seen)

    def _json(self, archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> Any:
        self._tick()
        try:
            return json.loads(archive.read(info).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.skipped["unreadable JSON files"] += 1
            return None

    def _counted(self, name: str, observations: Iterable[Observation]
                 ) -> Iterator[Observation]:
        count = 0
        for observation in observations:
            count += 1
            yield observation
        if not count:
            self.empty.append(name.rsplit("/", 1)[-1])

    # -- FIT ---------------------------------------------------------------

    def _fit(self, data: bytes, name: str) -> Iterator[Observation]:
        self._tick()
        try:
            messages = list(fit.read_messages(data))
        except (fit.FitError, struct.error, IndexError):
            self.skipped["unreadable FIT files"] += 1
            return
        key = self._file_key(messages, data)
        sessions = self._sessions(messages, key)
        for record, _start, _end in sessions:
            if self.on_session is not None:
                # Before the readings that belong to it: the writer takes
                # work in order, and they refer to it.
                self.on_session(record)
        count = 0
        for observation in self._fit_observations(messages, key, sessions):
            count += 1
            yield observation
        if not count and not sessions:
            self.empty.append(name.rsplit("/", 1)[-1])

    @staticmethod
    def _file_key(messages: List[fit.Message], data: bytes) -> str:
        """A stable id for the file, so importing it twice changes nothing:
        the device serial and creation time, or failing that its hash."""
        for message in messages:
            if message.num == fit.FILE_ID:
                serial = message.fields.get(_FILE_SERIAL)
                created = message.fields.get(_FILE_CREATED)
                if serial and created:
                    return "{}-{}".format(serial, created)
                break
        return hashlib.sha1(data).hexdigest()[:16]

    def _sessions(self, messages: List[fit.Message], key: str
                  ) -> List[Tuple[SessionRecord, datetime, Optional[datetime]]]:
        out = []
        for message in messages:
            if message.num != fit.SESSION:
                continue
            start = message.fields.get(_SESSION_START)
            if not isinstance(start, int):
                continue
            started = fit.fit_time(start)
            elapsed = message.fields.get(_SESSION_ELAPSED)
            ended = (started + timedelta(milliseconds=elapsed)
                     if isinstance(elapsed, int) else None)
            session_key = "fit:{}:{}".format(key, len(out))
            out.append((SessionRecord(
                key=session_key, start_ts=started, end_ts=ended, kind="workout",
                label=SPORTS.get(message.fields.get(_SESSION_SPORT), "Workout"),
                external_id=session_key), started, ended))
        if out:
            self.found["workouts"] += len(out)
        return out

    def _fit_observations(self, messages: List[fit.Message], key: str,
                          sessions: List[Tuple[SessionRecord, datetime,
                                               Optional[datetime]]]
                          ) -> Iterator[Observation]:
        external = "fit:" + key

        def session_for(ts: datetime) -> Optional[str]:
            for record, started, ended in sessions:
                if started <= ts and (ended is None or ts <= ended):
                    return record.key
            return None

        last_full: Optional[int] = None
        beat: Optional[datetime] = sessions[0][1] if sessions else None
        beats = 0
        monitored = 0
        levels: List[Tuple[int, int]] = []

        for message in messages:
            stamp = message.fields.get(fit.TIMESTAMP)
            if isinstance(stamp, int):
                last_full = stamp

            if message.num == fit.RECORD:
                hr = message.fields.get(_RECORD_HEART_RATE)
                if isinstance(hr, int) and hr > 0 and isinstance(stamp, int):
                    ts = fit.fit_time(stamp)
                    beat = beat or ts
                    yield Observation("heart_rate_bpm", ts, float(hr),
                                      external_id=external,
                                      session_key=session_for(ts))

            elif message.num == fit.HRV and beat is not None:
                # Beat-to-beat intervals in seconds at a scale of 1000 --
                # which is to say milliseconds. They carry no timestamps, so
                # beats are placed by accumulating from the workout's start.
                times = message.fields.get(_HRV_TIME)
                for value in times if isinstance(times, list) else [times]:
                    if not isinstance(value, int) or value <= 0:
                        continue
                    beat += timedelta(milliseconds=value)
                    yield Observation("rr_interval_ms", beat, float(value),
                                      external_id="{}:rr{}".format(external, beats),
                                      session_key=session_for(beat))
                    beats += 1

            elif message.num == fit.MONITORING:
                hr = message.fields.get(_MONITORING_HEART_RATE)
                if not isinstance(hr, int) or hr <= 0:
                    continue
                if not isinstance(stamp, int):
                    # A 16-bit timestamp counts on from the last full one,
                    # wrapping every 18 hours.
                    low = message.fields.get(_MONITORING_TIMESTAMP_16)
                    if not isinstance(low, int) or last_full is None:
                        continue
                    last_full += (low - last_full) & 0xFFFF
                    stamp = last_full
                monitored += 1
                yield Observation("heart_rate_bpm", fit.fit_time(stamp), float(hr),
                                  external_id=external)

            elif message.num == fit.SLEEP_LEVEL:
                level = message.fields.get(_SLEEP_LEVEL)
                if isinstance(stamp, int) and isinstance(level, int):
                    levels.append((stamp, level))

        if monitored:
            self.found["days of all-day heart rate"] += 1
        # Each level lasts until the next one; the last has no known end, and
        # is dropped rather than given an invented one.
        segments = 0
        for index, ((start, level), (end, _next)) in enumerate(zip(levels, levels[1:])):
            stage = STAGES.get(level)
            if stage is None or end <= start:
                continue
            segments += 1
            yield Observation("sleep_stage", fit.fit_time(start), float(end - start),
                              end_ts=fit.fit_time(end), text_value=stage,
                              external_id="{}:sleep{}".format(external, index))
        if segments:
            self.found["nights of sleep stages"] += 1

    # -- the export's JSON ---------------------------------------------------

    def _nights(self, payload: Any) -> Iterator[Observation]:
        for entry in entries(payload):
            start = garmin_time(entry.get("sleepStartTimestampGMT"))
            end = garmin_time(entry.get("sleepEndTimestampGMT"))
            parts = [number(entry.get(key)) for key in
                     ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds")]
            if start is None or end is None or end <= start:
                continue
            asleep = sum(part for part in parts if part is not None)
            if asleep <= 0:
                continue
            self.found["nights"] += 1
            yield Observation("sleep_duration_s", start, asleep, end_ts=end,
                              external_id="export:sleep:{}".format(
                                  entry.get("calendarDate") or iso_utc(start)))

    def _days(self, payload: Any) -> Iterator[Observation]:
        for entry in entries(payload):
            day = entry.get("calendarDate")
            if isinstance(day, dict):
                day = day.get("date")
            try:
                start = datetime.strptime(str(day)[:10], "%Y-%m-%d").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                continue
            # Garmin's day is the wearer's local day, in a zone it doesn't
            # name; like Oura's daily figures, it lands as a day-long span.
            end = start + timedelta(days=1)
            found = False
            for field, metric in _DAILY:
                value = number(entry.get(field))
                if value is not None:
                    found = True
                    yield Observation(metric, start, value, end_ts=end,
                                      external_id="export:day:{}".format(str(day)[:10]))
            if found:
                self.found["daily summaries"] += 1
