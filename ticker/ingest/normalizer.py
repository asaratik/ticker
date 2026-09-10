"""
Between connector and store.

Takes whatever a connector yields and turns it into batches the writer can
take: sanity-checked, with derived metrics filled in, grouped so the writer
does one executemany instead of a thousand.

Metric-name and session-key resolution happen in the store, which is where
the id caches and the writer connection already live -- doing them here
would mean this module holding a second connection for no gain.

Rejections are logged and counted, never silently dropped. An out-of-range
value is a parsing bug somewhere upstream, and a bug that shows up as a
counter climbing is one that gets fixed; a bug that shows up as slightly
wrong dashboards is one that doesn't.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ticker import config as tconfig
from ticker.ingest.derive import RmssdWindow
from ticker.model import Observation

log = logging.getLogger(__name__)

# Plausible ranges per metric. Not clinical thresholds -- these exist to
# catch a misparsed packet or a unit mix-up, so they are deliberately wide
# enough that no real measurement is ever rejected by them.
SANITY_RANGES: Dict[str, Tuple[float, float]] = {
    "heart_rate_bpm": (20.0, 250.0),
    "rr_interval_ms": (200.0, 3000.0),
    "hrv_rmssd_ms": (0.0, 500.0),
    "spo2_pct": (50.0, 100.0),
    "respiratory_rate_bpm": (2.0, 60.0),
    "skin_temp_delta_c": (-15.0, 15.0),
    "steps": (0.0, 200000.0),
    "active_energy_kcal": (0.0, 20000.0),
    "sleep_duration_s": (0.0, 24 * 3600.0),
    "weight_kg": (10.0, 500.0),
    "body_fat_pct": (1.0, 75.0),
}


class Normalizer:
    """Feeds one source's observations into the store.

    Stateful: it holds the RMSSD window for this source's RR stream and the
    partial batch, so there is one per (source, run), not one per process.
    """

    def __init__(self, store, source_id: int,
                 batch_size: Optional[int] = None,
                 derive_hrv: bool = True,
                 on_reject: Optional[Callable[[Observation, str], None]] = None):
        self.store = store
        self.source_id = source_id
        self.batch_size = tconfig.NORMALIZE_BATCH if batch_size is None else batch_size
        self.on_reject = on_reject
        self._hrv = RmssdWindow() if derive_hrv else None
        self._batch: List[Observation] = []
        self.accepted = 0
        self.rejected = 0
        self.rejected_by_metric: Dict[str, int] = {}

    def feed(self, observations: Iterable[Observation]) -> int:
        """Push observations through. Returns how many were accepted.

        Whole batches are handed to the writer as they fill; whatever is left
        over waits for the next feed() or a flush().
        """
        accepted = 0
        for obs in observations:
            if not self._check(obs):
                continue
            self._batch.append(obs)
            accepted += 1
            derived = self._derive(obs)
            if derived is not None and self._check(derived):
                self._batch.append(derived)
                accepted += 1
            if len(self._batch) >= self.batch_size:
                self._send()
        self.accepted += accepted
        return accepted

    def flush(self) -> None:
        """Hand the partial batch to the writer. Does not wait for the commit
        -- that is store.flush()."""
        self._send()

    def _send(self) -> None:
        if self._batch:
            self.store.insert_observations(self.source_id, self._batch)
            self._batch = []

    def _derive(self, obs: Observation) -> Optional[Observation]:
        if self._hrv is None or obs.metric != "rr_interval_ms":
            return None
        return self._hrv.feed(obs.ts, obs.value)

    def _check(self, obs: Observation) -> bool:
        low, high = SANITY_RANGES.get(obs.metric, (float("-inf"), float("inf")))
        if low <= obs.value <= high:
            return True
        self._reject(obs, "value {} outside [{}, {}]".format(obs.value, low, high))
        return False

    def _reject(self, obs: Observation, reason: str) -> None:
        self.rejected += 1
        self.rejected_by_metric[obs.metric] = (
            self.rejected_by_metric.get(obs.metric, 0) + 1)
        log.warning("rejected %s from source %s at %s: %s",
                    obs.metric, self.source_id, obs.ts.isoformat(), reason)
        if self.on_reject is not None:
            self.on_reject(obs, reason)
