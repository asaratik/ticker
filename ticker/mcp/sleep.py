"""
Nights, rebuilt from sleep-stage segments.

Sleep arrives three ways and only two of them come with sessions: Oura and
Fitbit hand over a sleep period with its bounds, while an Apple Health export
is nothing but stage segments. So a night is rebuilt from the segments
themselves, which works the same for all three -- per source, in time order,
split wherever the gap between one segment and the next exceeds SLEEP_GAP.

No SQL in here. The caller reads the rows; this decides what they mean,
which is the part worth testing on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

# Longer than any mid-night waking a tracker records as a gap rather than as
# 'awake', shorter than the time between a late nap and going to bed.
SLEEP_GAP = timedelta(minutes=60)

# The one stage that is not sleep. Every connector maps its vendor's
# vocabulary onto deep / light / rem / awake / asleep before storing.
AWAKE = "awake"


@dataclass
class Period:
    """One continuous stretch of sleep from one source."""

    source_id: int
    start: datetime
    end: datetime
    stages: Dict[str, float] = field(default_factory=dict)   # stage -> seconds
    reported_s: Optional[float] = None     # the vendor's own total, if given

    @property
    def staged(self) -> bool:
        return bool(self.stages)

    @property
    def in_bed_s(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def asleep_s(self) -> float:
        if self.stages:
            return sum(s for stage, s in self.stages.items() if stage != AWAKE)
        return self.reported_s or 0.0


def group_segments(segments: Iterable[Tuple[int, datetime, datetime, str]],
                   gap: timedelta = SLEEP_GAP) -> List[Period]:
    """(source_id, start, end, stage) rows, sorted by source then start,
    into periods.

    Overlapping segments count once. Two devices writing the same night into
    one Apple Health export is ordinary, and summing both would report a
    night longer than the time spent in bed.
    """
    periods: List[Period] = []
    current: Optional[Period] = None
    for source_id, start, end, stage in segments:
        if (current is None or current.source_id != source_id
                or start - current.end > gap):
            current = Period(source_id, start, start)
            periods.append(current)
        counted_from = max(start, current.end)
        if end > counted_from:
            current.stages[stage] = (current.stages.get(stage, 0.0)
                                     + (end - counted_from).total_seconds())
        if end > current.end:
            current.end = end
    return periods


def attach_reported(periods: List[Period],
                    reported: Iterable[Tuple[int, datetime, datetime, float]]
                    ) -> List[Period]:
    """Pair each vendor-reported total (source_id, start, end, seconds) with
    the staged period it overlaps.

    A total with no staged period to belong to -- a sleep log that came
    without stage detail -- becomes a period of its own, so a night isn't
    missing just because the tracker didn't break it down.
    """
    out = list(periods)
    for source_id, start, end, seconds in reported:
        match = next((p for p in out if p.source_id == source_id
                      and p.start < end and start < p.end), None)
        if match is None:
            out.append(Period(source_id, start, end, reported_s=seconds))
        else:
            match.reported_s = (match.reported_s or 0.0) + seconds
    out.sort(key=lambda p: (p.start, p.source_id))
    return out
