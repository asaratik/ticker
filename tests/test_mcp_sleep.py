"""
Tests for rebuilding nights from sleep-stage segments.

Pure functions over tuples: no database. The cases worth pinning are the
ones that would quietly misreport a night -- a nap merged into it, two
sources merged together, overlapping segments counted twice.
"""

from datetime import datetime, timedelta, timezone

from ticker.mcp.sleep import SLEEP_GAP, attach_reported, group_segments

UTC = timezone.utc
NIGHT = datetime(2026, 5, 1, 23, 0, tzinfo=UTC)


def seg(source, start_min, length_min, stage):
    start = NIGHT + timedelta(minutes=start_min)
    return (source, start, start + timedelta(minutes=length_min), stage)


def test_contiguous_segments_make_one_period():
    period, = group_segments([seg(1, 0, 30, "light"), seg(1, 30, 60, "deep"),
                              seg(1, 90, 10, "awake")])
    assert period.start == NIGHT
    assert period.end == NIGHT + timedelta(minutes=100)
    assert period.stages == {"light": 1800, "deep": 3600, "awake": 600}
    assert period.asleep_s == 5400          # awake is in bed, not asleep
    assert period.in_bed_s == 6000


def test_a_gap_longer_than_the_threshold_starts_a_new_period():
    gap = int(SLEEP_GAP.total_seconds() // 60)
    periods = group_segments([seg(1, 0, 30, "light"),
                              seg(1, 30 + gap + 1, 30, "light")])
    assert len(periods) == 2


def test_a_shorter_gap_does_not():
    gap = int(SLEEP_GAP.total_seconds() // 60)
    assert len(group_segments([seg(1, 0, 30, "light"),
                               seg(1, 30 + gap - 1, 30, "light")])) == 1


def test_sources_never_share_a_period():
    periods = group_segments([seg(1, 0, 60, "light"), seg(2, 0, 60, "light")])
    assert [p.source_id for p in periods] == [1, 2]


def test_overlapping_segments_count_once():
    # Two devices writing the same hour into one Apple Health export.
    period, = group_segments([seg(1, 0, 60, "light"), seg(1, 30, 60, "deep")])
    assert period.stages == {"light": 3600, "deep": 1800}
    assert period.asleep_s == period.in_bed_s == 5400


def test_a_segment_inside_another_adds_nothing():
    period, = group_segments([seg(1, 0, 60, "light"), seg(1, 10, 20, "deep")])
    assert period.stages == {"light": 3600}


def test_a_vendor_total_attaches_to_the_period_it_overlaps():
    periods = attach_reported(group_segments([seg(1, 0, 60, "light")]),
                              [(1, NIGHT, NIGHT + timedelta(minutes=60), 3300.0)])
    assert len(periods) == 1
    assert periods[0].reported_s == 3300.0
    # Measured stages still decide time asleep; the vendor's figure rides
    # along for comparison.
    assert periods[0].asleep_s == 3600


def test_a_total_from_another_source_does_not_attach():
    periods = attach_reported(group_segments([seg(1, 0, 60, "light")]),
                              [(2, NIGHT, NIGHT + timedelta(minutes=60), 3300.0)])
    assert len(periods) == 2


def test_a_total_with_no_stages_becomes_a_period_of_its_own():
    period, = attach_reported([], [(3, NIGHT, NIGHT + timedelta(hours=7),
                                    6 * 3600.0)])
    assert not period.staged
    assert period.asleep_s == 6 * 3600.0
