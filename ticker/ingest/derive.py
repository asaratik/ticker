"""
Derived metrics.

Computed in the pipeline rather than in connectors, so
every source that supplies RR intervals gets HRV for free and there is only
one implementation to get right.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Deque, List, Optional, Sequence, Tuple
from collections import deque

from ticker.model import Observation

# Physiologically possible beat-to-beat intervals. 300 ms is 200 bpm, 2000 ms
# is 30 bpm; anything outside that from a chest strap is a dropped or
# doubled beat, not a heart doing something interesting.
RR_MIN_MS = 300.0
RR_MAX_MS = 2000.0

# An ectopic beat shows up as one interval wildly unlike its neighbour, and
# RMSSD -- a sum of squared *successive differences* -- is exactly the
# statistic that ruins. The usual filter is to drop an interval differing
# from the previous accepted one by more than 20%.
MAX_SUCCESSIVE_CHANGE = 0.20


def expand_rr(sample_ts: datetime, intervals: Sequence[float]
              ) -> List[Tuple[datetime, float]]:
    """Reconstruct a per-beat timestamp for each RR interval.

    RR values arrive newest-last within one notification, and the packet
    carries no timestamps of its own -- only the durations. So the beats are
    placed by summing backwards from the moment the notification arrived:
    the newest interval lands on the sample time, the one before it a full
    interval earlier, and so on.

    Both the live BLE stream and the v1 migration place beats this way, and
    they must place them identically or the same strap's data would be
    timestamped one way before the migration and another way after.
    """
    out, offset_ms = [], 0.0
    for rr in reversed(list(intervals)):
        out.append((sample_ts - timedelta(milliseconds=offset_ms), rr))
        offset_ms += rr
    return list(reversed(out))


def expand_rr_csv(sample_ts: datetime, rr_csv: str) -> List[Tuple[datetime, float]]:
    """expand_rr over v1's comma-joined storage format."""
    if not rr_csv:
        return []
    return expand_rr(sample_ts, [float(v) for v in rr_csv.split(",") if v])


class RmssdWindow:
    """Rolling RMSSD over RR intervals.

    Feed intervals in time order; it emits an hrv_rmssd_ms observation at
    most once every `emit_every` seconds, computed over the trailing
    `window` seconds. Not thread-safe -- one per stream, on the thread
    draining that stream.
    """

    def __init__(self, window_sec: float = 60.0, emit_every_sec: float = 30.0,
                 min_intervals: int = 20):
        self.window = timedelta(seconds=window_sec)
        self.emit_every = timedelta(seconds=emit_every_sec)
        self.min_intervals = min_intervals
        self._beats: Deque[Tuple[datetime, float]] = deque()
        self._last_emit: Optional[datetime] = None
        self._last_accepted: Optional[float] = None
        self.rejected = 0

    def feed(self, ts: datetime, rr_ms: float) -> Optional[Observation]:
        """Add one interval. Returns an observation when one is due."""
        if not self._accept(rr_ms):
            self.rejected += 1
            return None
        self._last_accepted = rr_ms
        self._beats.append((ts, rr_ms))

        cutoff = ts - self.window
        while self._beats and self._beats[0][0] < cutoff:
            self._beats.popleft()

        if self._last_emit is not None and ts - self._last_emit < self.emit_every:
            return None
        value = self.value()
        if value is None:
            return None
        self._last_emit = ts
        # The window ends at ts and the value describes the interval before
        # it, so it is an interval observation, not a point sample.
        return Observation(metric="hrv_rmssd_ms", ts=ts - self.window,
                           end_ts=ts, value=value)

    def value(self) -> Optional[float]:
        """RMSSD over the current window, or None if there isn't enough data."""
        if len(self._beats) < self.min_intervals:
            return None
        rr = [v for _, v in self._beats]
        diffs = [rr[i + 1] - rr[i] for i in range(len(rr) - 1)]
        if not diffs:
            return None
        return math.sqrt(sum(d * d for d in diffs) / len(diffs))

    def _accept(self, rr_ms: float) -> bool:
        if not (RR_MIN_MS <= rr_ms <= RR_MAX_MS):
            return False
        if self._last_accepted is not None:
            change = abs(rr_ms - self._last_accepted) / self._last_accepted
            if change > MAX_SUCCESSIVE_CHANGE:
                return False
        return True


def rmssd(intervals: List[float]) -> Optional[float]:
    """One-shot RMSSD over a list of RR intervals, unfiltered.

    For batch/backfill use where the caller has already cleaned the data.
    """
    if len(intervals) < 2:
        return None
    diffs = [intervals[i + 1] - intervals[i] for i in range(len(intervals) - 1)]
    return math.sqrt(sum(d * d for d in diffs) / len(diffs))
