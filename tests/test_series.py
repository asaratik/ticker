"""
Tests for server-side bucketing and the source listing.

Bucketing is the part of the read API that has to be right for the
dashboard to be honest: a chart that quietly drops the last bucket, or
shifts every point by half a window when you pan, is worse than one that
fails.
"""

from datetime import datetime, timedelta, timezone

import pytest

from ticker.db import queries, store
from ticker.model import iso_utc

UTC = timezone.utc
START = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "s.sqlite3")
    yield connection
    connection.close()


@pytest.fixture
def source_id(conn):
    return store.ensure_source(conn, "stream", "ble", "Strap")


def write(conn, source_id, metric, samples):
    metric_id = queries.metric_id(conn, metric)
    conn.executemany(
        "INSERT INTO observations (source_id, metric_id, ts, value, external_id, "
        "ingested_at) VALUES (?,?,?,?,?,?)",
        [(source_id, metric_id, iso_utc(ts), float(v), str(i), iso_utc(ts))
         for i, (ts, v) in enumerate(samples)])
    return metric_id


def every_second(count, value=lambda i: 60 + i, start=START):
    return [(start + timedelta(seconds=i), value(i)) for i in range(count)]


# -- parse_bucket --------------------------------------------------------

@pytest.mark.parametrize("text,seconds", [
    ("30s", 30), ("5m", 300), ("1h", 3600), ("2d", 172800),
    ("m", 60),                       # a bare unit means one of them
    ("  15M  ", 900),                # whitespace and case are forgiven
])
def test_bucket_sizes_are_parsed(text, seconds):
    assert queries.parse_bucket(text) == seconds


@pytest.mark.parametrize("text", [None, "", "raw", "   "])
def test_no_bucket_means_raw_points(text):
    assert queries.parse_bucket(text) is None


@pytest.mark.parametrize("text", ["5", "5x", "abc", "0s", "-5m"])
def test_a_bad_bucket_raises_rather_than_defaulting(text):
    # A typo in a dashboard URL must not silently draw a different chart
    # from the one that was asked for.
    with pytest.raises(ValueError):
        queries.parse_bucket(text)


# -- raw ------------------------------------------------------------------

def test_raw_points_come_back_in_order(conn, source_id):
    write(conn, source_id, "heart_rate_bpm", every_second(5))
    points = queries.series(conn, "heart_rate_bpm", START,
                            START + timedelta(minutes=1))
    assert [p["value"] for p in points] == [60, 61, 62, 63, 64]
    assert [p["ts"] for p in points] == sorted(p["ts"] for p in points)


def test_the_window_is_half_open(conn, source_id):
    write(conn, source_id, "heart_rate_bpm", every_second(3))
    # [START, START+2s) is the first two samples: the one exactly on the
    # upper bound belongs to the next window, or panning double-counts it.
    points = queries.series(conn, "heart_rate_bpm", START,
                            START + timedelta(seconds=2))
    assert [p["value"] for p in points] == [60, 61]


def test_raw_points_can_be_filtered_to_one_source(conn, source_id):
    other = store.ensure_source(conn, "stream", "http", "Watch")
    write(conn, source_id, "heart_rate_bpm", every_second(3))
    write(conn, other, "heart_rate_bpm", every_second(3, lambda i: 100 + i))
    points = queries.series(conn, "heart_rate_bpm", START,
                            START + timedelta(minutes=1), source_id=other)
    assert [p["value"] for p in points] == [100, 101, 102]


# -- bucketed -------------------------------------------------------------

def test_bucketing_aggregates_each_window(conn, source_id):
    write(conn, source_id, "heart_rate_bpm", every_second(120))
    points = queries.series(conn, "heart_rate_bpm", START,
                            START + timedelta(minutes=5), bucket="1m")
    assert len(points) == 2
    assert [p["n"] for p in points] == [60, 60]
    assert points[0]["min"] == 60 and points[0]["max"] == 119
    assert points[1]["min"] == 120 and points[1]["max"] == 179
    assert points[0]["value"] == pytest.approx(89.5)


def test_buckets_are_aligned_to_the_epoch_not_the_window(conn, source_id):
    # Panning a chart must not reshuffle which samples land together. Two
    # requests with different starts have to agree on the bucket a given
    # sample falls in, which is only true if alignment ignores the window.
    write(conn, source_id, "heart_rate_bpm", every_second(300))
    first = queries.series(conn, "heart_rate_bpm", START,
                           START + timedelta(minutes=10), bucket="1m")
    shifted = queries.series(conn, "heart_rate_bpm", START - timedelta(seconds=17),
                             START + timedelta(minutes=10), bucket="1m")
    assert [p["ts"] for p in first] == [p["ts"] for p in shifted]
    assert [p["value"] for p in first] == [p["value"] for p in shifted]


def test_a_bucket_timestamp_is_the_start_of_its_window(conn, source_id):
    write(conn, source_id, "heart_rate_bpm", every_second(60))
    points = queries.series(conn, "heart_rate_bpm", START,
                            START + timedelta(minutes=5), bucket="1m")
    assert points[0]["ts"] == iso_utc(START)


def test_empty_buckets_are_omitted_rather_than_sent_as_nulls(conn, source_id):
    # A night of no data should not cost 28,800 null points on the wire;
    # a break in the line is what a gap looks like either way.
    write(conn, source_id, "heart_rate_bpm", [(START, 60)])
    write(conn, source_id, "heart_rate_bpm", [(START + timedelta(hours=3), 61)])
    points = queries.series(conn, "heart_rate_bpm", START,
                            START + timedelta(hours=4), bucket="1m")
    assert len(points) == 2


def test_bucketing_respects_the_source_filter(conn, source_id):
    other = store.ensure_source(conn, "stream", "http", "Watch")
    write(conn, source_id, "heart_rate_bpm", every_second(60))
    write(conn, other, "heart_rate_bpm", every_second(60, lambda i: 200))
    points = queries.series(conn, "heart_rate_bpm", START,
                            START + timedelta(minutes=5), bucket="1m",
                            source_id=other)
    assert points[0]["n"] == 60
    assert points[0]["value"] == 200


def test_day_bucketing_respects_the_source_filter(conn, source_id):
    other = store.ensure_source(conn, "stream", "http", "Watch")
    write(conn, source_id, "heart_rate_bpm", every_second(3))
    write(conn, other, "heart_rate_bpm", every_second(3, lambda i: 200))
    metric_id = queries.metric_id(conn, "heart_rate_bpm")
    queries.rebuild_days(conn, queries.all_days(conn, metric_id, UTC), UTC)

    points = queries.series(conn, "heart_rate_bpm", START,
                            START + timedelta(hours=1), bucket="1d",
                            source_id=other)
    assert points == [{"ts": "2026-03-01", "n": 3, "value": 200.0,
                       "min": 200.0, "max": 200.0, "sum": 600.0}]


def test_an_unknown_metric_raises(conn):
    with pytest.raises(KeyError):
        queries.series(conn, "nope", START, START + timedelta(hours=1))


# -- day buckets read the rollups ----------------------------------------

def test_a_day_bucket_is_served_from_the_rollups(conn, source_id):
    # Not recomputed from the observations: reading the rollup is the whole
    # reason rollups_daily exists.
    metric_id = write(conn, source_id, "heart_rate_bpm", every_second(10))
    day = queries.all_days(conn, metric_id, UTC)
    queries.rebuild_days(conn, day, UTC)
    conn.execute("DELETE FROM observations")

    points = queries.series(conn, "heart_rate_bpm", START,
                            START + timedelta(hours=1), bucket="1d")
    assert len(points) == 1
    assert points[0]["n"] == 10


@pytest.mark.parametrize("bucket", ["1d", "d", "day", "daily"])
def test_every_spelling_of_a_day_reads_the_rollups(conn, source_id, bucket):
    metric_id = write(conn, source_id, "heart_rate_bpm", every_second(4))
    queries.rebuild_days(conn, queries.all_days(conn, metric_id, UTC), UTC)
    conn.execute("DELETE FROM observations")
    assert queries.series(conn, "heart_rate_bpm", START,
                          START + timedelta(hours=1), bucket=bucket)


# -- the source listing ---------------------------------------------------

def only(listed, display_name):
    """The one source with this name. Indexing would be wrong: the v1
    migration seeds a 'BLE strap' row, so a fresh database is never empty."""
    matched = [s for s in listed if s["display_name"] == display_name]
    assert len(matched) == 1, [s["display_name"] for s in listed]
    return matched[0]


def test_a_source_reports_when_it_last_delivered(conn, source_id):
    write(conn, source_id, "heart_rate_bpm", every_second(3))
    listed = only(queries.list_sources(conn), "Strap")
    assert listed["vendor"] == "ble"
    assert listed["last_observation"] == iso_utc(START + timedelta(seconds=2))


def test_the_latest_is_taken_across_every_metric(conn, source_id):
    # Per-metric reverse seeks, so the answer has to be the max over them --
    # not whichever metric happened to be looked at last.
    write(conn, source_id, "heart_rate_bpm", every_second(2))
    write(conn, source_id, "rr_interval_ms",
          [(START + timedelta(minutes=5), 800.0)])
    listed = only(queries.list_sources(conn), "Strap")
    assert listed["last_observation"] == iso_utc(START + timedelta(minutes=5))


def test_a_source_with_no_data_still_appears(conn, source_id):
    listed = only(queries.list_sources(conn), "Strap")
    assert listed["last_observation"] is None


def test_row_counts_are_not_computed_unless_asked_for(conn, source_id):
    # A count per source is a scan per source, and this listing exists to be
    # cheap enough for a UI to poll.
    write(conn, source_id, "heart_rate_bpm", every_second(3))
    assert "n_observations" not in only(queries.list_sources(conn), "Strap")
    counted = only(queries.list_sources(conn, counts=True), "Strap")
    assert counted["n_observations"] == 3


def test_last_observation_agrees_with_the_obvious_query(conn, source_id):
    # The fast formulation has to give the same answer as the slow one it
    # replaces, or it is just a faster way of being wrong.
    write(conn, source_id, "heart_rate_bpm", every_second(50))
    write(conn, source_id, "rr_interval_ms", every_second(50))
    scanned = conn.execute("SELECT MAX(ts) FROM observations WHERE source_id = ?",
                           (source_id,)).fetchone()[0]
    assert queries.last_observation(conn, source_id) == scanned


def test_the_seeded_ble_source_is_listed_too(conn):
    # The v1 migration creates it, and the app looks it up by that name --
    # so a strap's history and its new data stay under one source row.
    assert only(queries.list_sources(conn), "BLE strap")["vendor"] == "ble"


def test_a_sync_error_is_surfaced_on_the_source(conn):
    source_id = store.ensure_source(conn, "pull", "oura", "Ring")
    conn.execute(
        "INSERT INTO sync_state (source_id, metric_id, last_attempt, last_error) "
        "VALUES (?,?,?,?)",
        (source_id, queries.metric_id(conn, "sleep_duration_s"),
         iso_utc(START), "token expired"))
    listed = only(queries.list_sources(conn), "Ring")
    assert listed["last_error"] == "token expired"
    assert listed["sync"]["sleep_duration_s"]["last_error"] == "token expired"


def test_the_listing_never_carries_a_secret(conn):
    # auth_ref names a keyring entry; the token itself must never leave the
    # keyring, let alone appear on an HTTP endpoint.
    store.ensure_source(conn, "pull", "oura", "Ring", auth_ref="ticker:oura:Ring")
    listed = only(queries.list_sources(conn), "Ring")
    assert listed["auth_ref"] == "ticker:oura:Ring"
    assert "token" not in {k.lower() for k in listed}
