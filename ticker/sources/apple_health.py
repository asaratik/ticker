"""
Apple Health, as an ImportSource.

There is no server API. The user exports from the Health app and gets a zip
containing `export.xml`, which is routinely
over a gigabyte -- a few years of a watch writing a heart rate sample every
few seconds adds up. So this parses with `iterparse` and clears as it goes;
`parse()` is a generator and never holds more than one record. Loading the
tree would need memory proportional to the whole export, which on a normal
laptop means it does not finish.

The zip is read in place rather than extracted first, for the same reason:
unpacking a gigabyte to a temp directory to read it once is a lot of disk
for no benefit.

Two decisions here are deliberate omissions rather than gaps:

**SDNN is not RMSSD.** Apple records
`HKQuantityTypeIdentifierHeartRateVariabilitySDNN`, and the only HRV metric
in the seed set is `hrv_rmssd_ms`. They are both time-domain HRV measures
and they are not the same number. Writing SDNN into the RMSSD metric would
put it in the same series as the BLE strap's real RMSSD and quietly corrupt
every HRV query. It is skipped until there is a metric that means SDNN.

**Wrist temperature is not a delta.** `AppleSleepingWristTemperature` is an
absolute reading; `skin_temp_delta_c` is a deviation from baseline. Same
reasoning.

Units are checked rather than assumed. An export's units follow the phone's
locale, so weight arrives in kg, lb, or stone depending on who exported it,
and an unrecognised unit is skipped and counted rather than being taken at
face value -- a pounds figure written into a kilograms metric is exactly the
kind of mix-up the normalizer's sanity ranges exist to catch, and it is
better not to make it in the first place.
"""

from __future__ import annotations

import logging
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterator, Optional, Tuple
from xml.etree import ElementTree

from ticker.model import Observation
from ticker.sources.base import PermanentError, SourceHealth

log = logging.getLogger(__name__)

# The name Apple gives the export inside the zip. Older exports used the
# same name at the archive root; newer ones nest it under a folder.
EXPORT_NAMES = ("export.xml", "apple_health_export/export.xml")

# Apple's timestamps look like "2026-01-14 07:03:00 -0500": a space instead
# of the ISO 'T', and an offset without a colon. fromisoformat cannot read
# that on any Python this project supports, so it is parsed explicitly.
STAMP_FORMAT = "%Y-%m-%d %H:%M:%S %z"

# HealthKit type -> (metric, {unit: multiplier}).
#
# The unit table is the point: a value is only accepted if its unit is one
# this mapping knows how to turn into the metric's unit.
QUANTITY_TYPES: Dict[str, Tuple[str, Dict[str, float]]] = {
    "HKQuantityTypeIdentifierHeartRate": (
        "heart_rate_bpm", {"count/min": 1.0}),
    "HKQuantityTypeIdentifierRespiratoryRate": (
        "respiratory_rate_bpm", {"count/min": 1.0}),
    "HKQuantityTypeIdentifierOxygenSaturation": (
        # HealthKit's native unit is a fraction; the export writes percent.
        "spo2_pct", {"%": 1.0}),
    "HKQuantityTypeIdentifierStepCount": (
        "steps", {"count": 1.0}),
    "HKQuantityTypeIdentifierActiveEnergyBurned": (
        # 'Cal' is Apple's spelling of the kilocalorie, not the calorie.
        "active_energy_kcal", {"kcal": 1.0, "Cal": 1.0, "kJ": 0.2390057}),
    "HKQuantityTypeIdentifierBodyMass": (
        "weight_kg", {"kg": 1.0, "lb": 0.45359237, "st": 6.35029318}),
    "HKQuantityTypeIdentifierBodyFatPercentage": (
        "body_fat_pct", {"%": 1.0}),
}

# Apple's sleep vocabulary, old and new, onto the seed metric's values.
# 'InBed' is deliberately absent: lying down is not a sleep stage, and
# counting it as one would inflate every night.
SLEEP_STAGES = {
    "HKCategoryValueSleepAnalysisAsleepDeep": "deep",
    "HKCategoryValueSleepAnalysisAsleepCore": "light",
    "HKCategoryValueSleepAnalysisAsleepREM": "rem",
    "HKCategoryValueSleepAnalysisAwake": "awake",
    # Pre-watchOS 9 exports have only a single "asleep" value with no stage
    # breakdown. Recording it as light would invent a precision the export
    # does not have, so it keeps its own name.
    "HKCategoryValueSleepAnalysisAsleepUnspecified": "asleep",
    "HKCategoryValueSleepAnalysisAsleep": "asleep",
}

SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"

CAPABILITIES = frozenset(
    [metric for metric, _units in QUANTITY_TYPES.values()] + ["sleep_stage"])


def parse_stamp(raw: Optional[str]) -> Optional[datetime]:
    """Parse one of Apple's offset-bearing timestamps.

    These do carry an offset, unlike Fitbit's, so there is nothing to guess
    -- but the format is not ISO and the offset has no colon.
    """
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), STAMP_FORMAT)
    except ValueError:
        return None


class AppleHealthSource:
    """Parses an Apple Health export into observations.

    Stateless between calls apart from the counters, which exist so an
    import can report what it skipped: a silent import that drops half the
    file because the phone was set to pounds is worse than a loud one.
    """

    vendor = "apple_health"

    def __init__(self, on_progress: Optional[Callable[[int], None]] = None,
                 progress_every: int = 100000):
        self.on_progress = on_progress
        self.progress_every = progress_every
        self.seen = 0
        self.emitted = 0
        self.skipped_unknown_type = 0
        self.skipped_unknown_unit = 0
        self.skipped_malformed = 0
        self._unknown_units: Dict[str, int] = {}
        self._last_error: Optional[str] = None

    # -- Source ----------------------------------------------------------

    def capabilities(self) -> "frozenset[str]":
        return CAPABILITIES

    def health(self) -> SourceHealth:
        """Never raises. A file importer has no connection to be down."""
        return SourceHealth(
            ok=self._last_error is None,
            state="idle" if self._last_error is None else "error",
            detail=self._last_error, last_error=self._last_error)

    # -- ImportSource ----------------------------------------------------

    def parse(self, path: Path) -> Iterator[Observation]:
        """Stream observations out of an export.

        Accepts either the `export.xml` itself or the zip the Health app
        produces. A generator on purpose: the caller can feed it straight
        into the normalizer and never hold the file in memory.
        """
        path = Path(path)
        if not path.exists():
            raise PermanentError("no such export: {}".format(path))

        try:
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as archive:
                    name = self._export_member(archive)
                    with archive.open(name) as stream:
                        yield from self._records(stream)
            else:
                with open(path, "rb") as stream:
                    yield from self._records(stream)
        except ElementTree.ParseError as exc:
            self._last_error = str(exc)
            raise PermanentError(
                "{} is not a readable Health export: {}".format(path, exc))

    @staticmethod
    def _export_member(archive: zipfile.ZipFile) -> str:
        """Find export.xml inside the archive."""
        names = set(archive.namelist())
        for candidate in EXPORT_NAMES:
            if candidate in names:
                return candidate
        for name in names:
            if name.endswith("/export.xml") or name == "export.xml":
                return name
        raise PermanentError(
            "no export.xml in the archive (found {} entries)".format(len(names)))

    def _records(self, stream) -> Iterator[Observation]:
        """Walk <Record> elements, clearing as we go.

        `root.clear()` after each record is what keeps this constant-memory.
        Clearing the element alone is not enough: the parser keeps appending
        finished children to the root, so without this the tree still grows
        to the size of the file and the whole point is lost.
        """
        context = ElementTree.iterparse(stream, events=("start", "end"))
        _event, root = next(context)

        for event, element in context:
            if event != "end" or element.tag != "Record":
                continue
            self.seen += 1
            try:
                for observation in self._record(element):
                    self.emitted += 1
                    yield observation
            finally:
                root.clear()
            if (self.on_progress is not None
                    and self.seen % self.progress_every == 0):
                self.on_progress(self.seen)

    def _record(self, element) -> Iterator[Observation]:
        kind = element.get("type") or ""
        start = parse_stamp(element.get("startDate"))
        if start is None:
            self.skipped_malformed += 1
            return
        end = parse_stamp(element.get("endDate"))

        if kind == SLEEP_TYPE:
            yield from self._sleep(element, start, end)
            return

        mapping = QUANTITY_TYPES.get(kind)
        if mapping is None:
            self.skipped_unknown_type += 1
            return
        metric, units = mapping

        unit = (element.get("unit") or "").strip()
        factor = units.get(unit)
        if factor is None:
            self.skipped_unknown_unit += 1
            self._unknown_units[unit] = self._unknown_units.get(unit, 0) + 1
            return

        try:
            value = float(element.get("value"))
        except (TypeError, ValueError):
            self.skipped_malformed += 1
            return

        # A point sample has start == end in the export; storing a zero-width
        # interval would make it look like a span that happens to be short.
        span_end = end if end is not None and end > start else None
        yield Observation(metric=metric, ts=start, end_ts=span_end,
                          value=value * factor,
                          external_id=self._external_id(element))

    def _sleep(self, element, start: datetime,
               end: Optional[datetime]) -> Iterator[Observation]:
        stage = SLEEP_STAGES.get(element.get("value") or "")
        if stage is None:
            # 'InBed', or a value from a watchOS newer than this table.
            # Skipped rather than guessed.
            self.skipped_unknown_type += 1
            return
        if end is None or end <= start:
            self.skipped_malformed += 1
            return
        seconds = (end - start).total_seconds()
        yield Observation(metric="sleep_stage", ts=start, end_ts=end,
                          value=seconds, text_value=stage,
                          external_id=self._external_id(element))

    @staticmethod
    def _external_id(element) -> str:
        """A stable-enough id for one record.

        The export carries no per-record identifier, so this is the natural
        key the store would use anyway plus the writing device -- which is
        what separates a phone's step count from a watch's for the same
        minute, rather than letting one overwrite the other.
        """
        return "{}|{}".format(element.get("sourceName") or "",
                              element.get("startDate") or "")

    def report(self) -> str:
        """A one-line summary of what an import did and did not take."""
        parts = ["{} records read".format(self.seen),
                 "{} observations".format(self.emitted)]
        if self.skipped_unknown_type:
            parts.append("{} of types not mapped".format(
                self.skipped_unknown_type))
        if self.skipped_unknown_unit:
            units = ", ".join(sorted(self._unknown_units))
            parts.append("{} in unhandled units ({})".format(
                self.skipped_unknown_unit, units))
        if self.skipped_malformed:
            parts.append("{} malformed".format(self.skipped_malformed))
        return "; ".join(parts)
