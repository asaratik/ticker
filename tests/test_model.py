"""
Tests for the core value types and the canonical timestamp format.

The format matters more than it looks: ts is TEXT and SQLite compares it as
a string, so lexicographic order has to equal chronological order. That only
holds while every timestamp is spelled identically, which is what iso_utc
guarantees and what these tests pin down.
"""

from datetime import datetime, timedelta, timezone

import pytest

from ticker.model import (TS_LEN, Observation, SessionRecord, iso_utc, now_iso,
                          parse_iso)

UTC = timezone.utc


def test_iso_utc_is_fixed_width_and_always_utc():
    assert iso_utc(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00.000+00:00"
    assert len(iso_utc(datetime(2026, 1, 1, tzinfo=UTC))) == TS_LEN
    assert len(now_iso()) == TS_LEN


def test_iso_utc_converts_other_offsets():
    ist = timezone(timedelta(hours=5, minutes=30))
    assert iso_utc(datetime(2026, 1, 1, 5, 30, tzinfo=ist)) == \
        "2026-01-01T00:00:00.000+00:00"


def test_iso_utc_rejects_naive_datetimes():
    # "Every connector converts at the edge" only holds if the edge refuses
    # to accept anything else.
    with pytest.raises(ValueError):
        iso_utc(datetime(2026, 1, 1))


def test_string_order_matches_time_order_including_subsecond():
    moments = [
        datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 0, 0, 500000, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC),
        datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    ]
    encoded = [iso_utc(m) for m in moments]
    assert encoded == sorted(encoded)


def test_millisecond_resolution_keeps_rr_intervals_distinct():
    # Second resolution is insufficient because RR intervals are
    # sub-second by definition, and at second resolution these two beats
    # would share a timestamp, collide on the natural-key unique index and
    # overwrite each other.
    base = datetime(2026, 1, 1, tzinfo=UTC)
    assert iso_utc(base) != iso_utc(base + timedelta(milliseconds=750))


def test_parse_iso_round_trips_and_accepts_v1_precision():
    ts = datetime(2026, 1, 1, 12, 30, 15, 250000, tzinfo=UTC)
    assert parse_iso(iso_utc(ts)) == ts
    # v1 wrote session times at second resolution and sample times at
    # millisecond resolution; both have to read back.
    assert parse_iso("2026-01-01T00:00:00+00:00") == datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_iso("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=UTC)


def test_parse_iso_rejects_naive_storage():
    with pytest.raises(ValueError):
        parse_iso("2026-01-01T00:00:00")


def test_observation_requires_aware_timestamps():
    with pytest.raises(ValueError):
        Observation("heart_rate_bpm", datetime(2026, 1, 1), 70.0)
    with pytest.raises(ValueError):
        Observation("sleep_stage", datetime(2026, 1, 1, tzinfo=UTC), 1.0,
                    end_ts=datetime(2026, 1, 1, 1))


def test_observation_rejects_end_before_start():
    ts = datetime(2026, 1, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        Observation("sleep_stage", ts, 1.0, end_ts=ts - timedelta(minutes=1))


def test_observation_defaults_make_a_point_sample():
    obs = Observation("heart_rate_bpm", datetime(2026, 1, 1, tzinfo=UTC), 70.0)
    assert obs.end_ts is None
    assert obs.external_id == ""      # '' when the source has no stable id
    assert obs.session_key is None


def test_session_record_requires_aware_timestamps():
    with pytest.raises(ValueError):
        SessionRecord(key="k", start_ts=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        SessionRecord(key="k", start_ts=datetime(2026, 1, 1, tzinfo=UTC),
                      end_ts=datetime(2026, 1, 1, 1))


def test_session_record_rejects_end_before_start():
    started = datetime(2026, 1, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        SessionRecord(key="k", start_ts=started,
                      end_ts=started - timedelta(minutes=1))
