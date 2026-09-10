"""
Tests for the normalizer and derived metrics.

The store is stubbed here on purpose: the normalizer's job is to decide what
reaches the writer, so these assert on the batches it hands over rather than
on what ends up in SQLite.
"""

import math
from datetime import datetime, timedelta, timezone

import pytest

from ticker.ingest.derive import RmssdWindow, rmssd
from ticker.ingest.normalizer import SANITY_RANGES, Normalizer
from ticker.model import Observation

UTC = timezone.utc
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class FakeStore:
    def __init__(self):
        self.batches = []

    def insert_observations(self, source_id, batch):
        self.batches.append((source_id, list(batch)))

    @property
    def written(self):
        return [obs for _, batch in self.batches for obs in batch]


def hr(seconds, value):
    return Observation("heart_rate_bpm", T0 + timedelta(seconds=seconds), float(value))


# -- sanity ranges -------------------------------------------------------

def test_in_range_values_pass_through():
    store = FakeStore()
    norm = Normalizer(store, source_id=1, derive_hrv=False)
    assert norm.feed([hr(0, 70), hr(1, 71)]) == 2
    norm.flush()
    assert [o.value for o in store.written] == [70.0, 71.0]


@pytest.mark.parametrize("value", [0, 19, 251, 10000])
def test_out_of_range_heart_rate_is_rejected(value):
    # "HR outside 20-250 is a parsing bug, not data."
    store = FakeStore()
    norm = Normalizer(store, source_id=1, derive_hrv=False)
    assert norm.feed([hr(0, value)]) == 0
    norm.flush()
    assert store.written == []


def test_rejections_are_counted_per_metric_not_silently_dropped():
    store = FakeStore()
    norm = Normalizer(store, source_id=1, derive_hrv=False)
    norm.feed([hr(0, 500), hr(1, 70), hr(2, 1)])
    assert norm.rejected == 2
    assert norm.rejected_by_metric == {"heart_rate_bpm": 2}
    assert norm.accepted == 1


def test_a_rejection_callback_sees_the_reason():
    seen = []
    store = FakeStore()
    norm = Normalizer(store, source_id=1, derive_hrv=False,
                      on_reject=lambda obs, why: seen.append((obs.metric, why)))
    norm.feed([hr(0, 400)])
    assert seen[0][0] == "heart_rate_bpm"
    assert "400" in seen[0][1]


def test_an_unranged_metric_is_not_second_guessed():
    store = FakeStore()
    norm = Normalizer(store, source_id=1, derive_hrv=False)
    # sleep_stage is categorical: its numeric value carries no range.
    norm.feed([Observation("sleep_stage", T0, 42.0, end_ts=T0 + timedelta(minutes=5),
                           text_value="deep")])
    norm.flush()
    assert len(store.written) == 1


def test_boundaries_are_inclusive():
    low, high = SANITY_RANGES["heart_rate_bpm"]
    store = FakeStore()
    norm = Normalizer(store, source_id=1, derive_hrv=False)
    assert norm.feed([hr(0, low), hr(1, high)]) == 2


# -- batching ------------------------------------------------------------

def test_full_batches_go_out_without_waiting_for_a_flush():
    store = FakeStore()
    norm = Normalizer(store, source_id=7, batch_size=10, derive_hrv=False)
    norm.feed([hr(i, 70) for i in range(25)])
    # Two full batches sent, five still held back.
    assert [len(b) for _, b in store.batches] == [10, 10]
    norm.flush()
    assert [len(b) for _, b in store.batches] == [10, 10, 5]
    assert all(source_id == 7 for source_id, _ in store.batches)


def test_flushing_nothing_sends_nothing():
    store = FakeStore()
    Normalizer(store, source_id=1).flush()
    assert store.batches == []


# -- derived metrics -----------------------------------------------------

def test_rmssd_matches_the_definition():
    # RMSSD is the root mean square of successive differences.
    intervals = [800.0, 810.0, 790.0, 805.0]
    diffs = [10.0, -20.0, 15.0]
    expected = math.sqrt(sum(d * d for d in diffs) / len(diffs))
    assert rmssd(intervals) == pytest.approx(expected)


def test_rmssd_needs_at_least_two_intervals():
    assert rmssd([]) is None
    assert rmssd([800.0]) is None


def test_hrv_is_derived_from_rr_so_every_source_gets_it_free():
    store = FakeStore()
    norm = Normalizer(store, source_id=1, batch_size=10000)
    ts = T0
    for i in range(40):
        rr = 800.0 + (10.0 if i % 2 else -10.0)
        norm.feed([Observation("rr_interval_ms", ts, rr)])
        ts += timedelta(milliseconds=rr)
    norm.flush()

    derived = [o for o in store.written if o.metric == "hrv_rmssd_ms"]
    assert derived, "RR intervals should have produced an HRV observation"
    # Alternating +/-10 ms gives successive differences of 20 ms.
    assert derived[0].value == pytest.approx(20.0, abs=0.5)
    # It describes a window, so it carries both ends.
    assert derived[0].end_ts is not None
    assert derived[0].end_ts > derived[0].ts


def test_hrv_can_be_switched_off():
    store = FakeStore()
    norm = Normalizer(store, source_id=1, derive_hrv=False)
    ts = T0
    for _ in range(60):
        norm.feed([Observation("rr_interval_ms", ts, 800.0)])
        ts += timedelta(milliseconds=800)
    norm.flush()
    assert not [o for o in store.written if o.metric == "hrv_rmssd_ms"]


def test_ectopic_beats_are_filtered_out_of_hrv():
    """RMSSD is a sum of squared successive differences, so one dropped beat
    swamps the statistic. It has to be rejected, and counted."""
    window = RmssdWindow(window_sec=120, emit_every_sec=0, min_intervals=5)
    ts = T0
    for _ in range(20):
        window.feed(ts, 800.0)
        ts += timedelta(milliseconds=800)
    clean = window.value()

    window.feed(ts, 1600.0)          # a missed beat: two intervals merged
    assert window.rejected == 1
    assert window.value() == pytest.approx(clean)


def test_implausible_rr_values_never_enter_the_window():
    window = RmssdWindow(min_intervals=2)
    assert window.feed(T0, 50.0) is None        # 1200 bpm
    assert window.feed(T0, 5000.0) is None      # 12 bpm
    assert window.rejected == 2
    assert window.value() is None


def test_hrv_is_not_emitted_before_there_is_enough_data():
    window = RmssdWindow(min_intervals=20, emit_every_sec=0)
    ts = T0
    for _ in range(5):
        assert window.feed(ts, 800.0) is None
        ts += timedelta(milliseconds=800)


def test_hrv_emits_at_most_once_per_interval():
    window = RmssdWindow(window_sec=60, emit_every_sec=30, min_intervals=5)
    ts = T0
    emitted = []
    for _ in range(200):
        out = window.feed(ts, 800.0 + (5.0 if len(emitted) % 2 else -5.0))
        if out:
            emitted.append(out)
        ts += timedelta(milliseconds=800)
    # 200 beats at 800 ms is 160 seconds; at one every 30 s that's ~5.
    assert 4 <= len(emitted) <= 6


def test_the_window_forgets_old_beats():
    window = RmssdWindow(window_sec=10, emit_every_sec=0, min_intervals=2)
    ts = T0
    for _ in range(20):
        window.feed(ts, 800.0)
        ts += timedelta(milliseconds=800)
    # A 10 s window at 800 ms per beat holds about 12 beats, not all 20.
    assert len(window._beats) <= 13
