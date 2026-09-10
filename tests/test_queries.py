"""
Tests for the read helpers and rollup computation.

Every test that touches days pins an explicit zone rather than using the
machine's, both for determinism in CI and because the interesting cases --
a local day that isn't 24 hours long -- only exist in a zone with DST.
"""

from datetime import datetime, timedelta, timezone

import pytest

from ticker.db import queries, store
from ticker.model import iso_utc

UTC = timezone.utc

try:
    from zoneinfo import ZoneInfo
    NEW_YORK = ZoneInfo("America/New_York")
except Exception:                                  # pragma: no cover
    NEW_YORK = None


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "q.sqlite3")
    yield connection
    connection.close()


@pytest.fixture
def source_id(conn):
    return store.ensure_source(conn, "stream", "ble", "Strap")


def write(conn, source_id, metric, samples, zone=UTC):
    """samples: [(datetime, value)] -> rows, returning the days touched."""
    metric_id = queries.metric_id(conn, metric)
    conn.executemany(
        "INSERT INTO observations (source_id, metric_id, ts, value, external_id, "
        "ingested_at) VALUES (?,?,?,?,?,?)",
        [(source_id, metric_id, iso_utc(ts), float(v), str(i), iso_utc(ts))
         for i, (ts, v) in enumerate(samples)])
    return metric_id


# -- registry and series -------------------------------------------------

def test_seeded_metrics_are_listed(conn):
    names = {m["name"] for m in queries.list_metrics(conn)}
    assert "heart_rate_bpm" in names
    assert len(names) == 12


def test_metric_id_raises_for_an_unknown_name(conn):
    with pytest.raises(KeyError):
        queries.metric_id(conn, "nope")


def test_observations_are_half_open_and_ordered(conn, source_id):
    base = datetime(2026, 3, 1, 12, tzinfo=UTC)
    write(conn, source_id, "heart_rate_bpm",
          [(base + timedelta(minutes=i), 70 + i) for i in range(4)])

    got = queries.observations(conn, "heart_rate_bpm", base,
                               base + timedelta(minutes=3))
    # [since, until): three minutes in, the fourth excluded.
    assert [v for _, v, _ in got] == [70.0, 71.0, 72.0]


def test_observations_can_be_filtered_by_source(conn, source_id):
    other = store.ensure_source(conn, "pull", "oura", "Ring")
    base = datetime(2026, 3, 1, 12, tzinfo=UTC)
    write(conn, source_id, "heart_rate_bpm", [(base, 70)])
    write(conn, other, "heart_rate_bpm", [(base + timedelta(minutes=1), 55)])

    got = queries.observations(conn, "heart_rate_bpm", base,
                               base + timedelta(hours=1), source_id=other)
    assert [v for _, v, _ in got] == [55.0]


def test_list_sessions_includes_a_still_open_session(conn, source_id):
    conn.execute(
        "INSERT INTO sessions (source_id, start_ts, kind, label) VALUES (?,?,?,?)",
        (source_id, iso_utc(datetime(2026, 3, 1, 12, tzinfo=UTC)), "manual", "open"))
    found = queries.list_sessions(
        conn, since=datetime(2026, 3, 1, 18, tzinfo=UTC),
        until=datetime(2026, 3, 2, tzinfo=UTC))
    assert [s["label"] for s in found] == ["open"]


def test_list_sessions_counts_observations(conn, source_id):
    conn.execute(
        "INSERT INTO sessions (source_id, start_ts, end_ts, kind, label) "
        "VALUES (?,?,?,?,?)",
        (source_id, iso_utc(datetime(2026, 3, 1, 12, tzinfo=UTC)),
         iso_utc(datetime(2026, 3, 1, 13, tzinfo=UTC)), "manual", "ride"))
    metric_id = queries.metric_id(conn, "heart_rate_bpm")
    conn.execute(
        "INSERT INTO observations (source_id, metric_id, session_id, ts, value, "
        "external_id, ingested_at) VALUES (?,?,1,?,70,'','x')",
        (source_id, metric_id, iso_utc(datetime(2026, 3, 1, 12, tzinfo=UTC))))
    assert queries.list_sessions(conn)[0]["n_observations"] == 1


# -- rollups -------------------------------------------------------------

def test_rebuild_days_computes_the_aggregates(conn, source_id):
    base = datetime(2026, 3, 1, 12, tzinfo=UTC)
    metric_id = write(conn, source_id, "heart_rate_bpm",
                      [(base + timedelta(minutes=i), v)
                       for i, v in enumerate([60, 70, 80, 90])])

    assert queries.rebuild_days(conn, [(metric_id, "2026-03-01")], zone=UTC) == 1
    day = queries.daily(conn, "heart_rate_bpm", "2026-03-01", "2026-03-01")[0]
    assert day["n"] == 4
    assert day["sum"] == 300.0
    assert day["min"] == 60.0
    assert day["max"] == 90.0
    assert day["avg"] == 75.0
    assert day["p50"] == 70.0        # lower median of an even count


def test_rebuild_days_only_touches_the_days_it_is_given(conn, source_id):
    metric_id = write(conn, source_id, "heart_rate_bpm", [
        (datetime(2026, 3, 1, 12, tzinfo=UTC), 70),
        (datetime(2026, 3, 2, 12, tzinfo=UTC), 80),
    ])
    queries.rebuild_days(conn, [(metric_id, "2026-03-01")], zone=UTC)
    assert [d["day"] for d in
            queries.daily(conn, "heart_rate_bpm", "2026-01-01", "2026-12-31")] == \
        ["2026-03-01"]


def test_rebuild_days_is_idempotent_and_updates_in_place(conn, source_id):
    metric_id = write(conn, source_id, "heart_rate_bpm",
                      [(datetime(2026, 3, 1, 12, tzinfo=UTC), 70)])
    queries.rebuild_days(conn, [(metric_id, "2026-03-01")], zone=UTC)
    queries.rebuild_days(conn, [(metric_id, "2026-03-01")], zone=UTC)
    assert conn.execute("SELECT COUNT(*) FROM rollups_daily").fetchone() == (1,)

    write(conn, source_id, "heart_rate_bpm",
          [(datetime(2026, 3, 1, 13, tzinfo=UTC), 90)])
    queries.rebuild_days(conn, [(metric_id, "2026-03-01")], zone=UTC)
    day = queries.daily(conn, "heart_rate_bpm", "2026-03-01", "2026-03-01")[0]
    assert (day["n"], day["max"]) == (2, 90.0)


def test_a_day_whose_data_is_gone_loses_its_rollup(conn, source_id):
    metric_id = write(conn, source_id, "heart_rate_bpm",
                      [(datetime(2026, 3, 1, 12, tzinfo=UTC), 70)])
    queries.rebuild_days(conn, [(metric_id, "2026-03-01")], zone=UTC)
    conn.execute("DELETE FROM observations")
    queries.rebuild_days(conn, [(metric_id, "2026-03-01")], zone=UTC)
    # A stale rollup is worse than a missing one.
    assert conn.execute("SELECT COUNT(*) FROM rollups_daily").fetchone() == (0,)


@pytest.mark.skipif(NEW_YORK is None, reason="no IANA tz database available")
def test_local_days_bucket_by_local_midnight_not_utc(conn, source_id):
    # 03:00 UTC on 2 March is 22:00 on 1 March in New York.
    metric_id = write(conn, source_id, "heart_rate_bpm",
                      [(datetime(2026, 3, 2, 3, tzinfo=UTC), 70)])
    queries.rebuild_days(conn, [(metric_id, "2026-03-01")], zone=NEW_YORK)
    day = queries.daily(conn, "heart_rate_bpm", "2026-03-01", "2026-03-01")[0]
    assert day["n"] == 1


@pytest.mark.skipif(NEW_YORK is None, reason="no IANA tz database available")
def test_a_dst_day_is_not_assumed_to_be_24_hours(conn):
    # 8 March 2026, the US spring-forward day: 23 hours long.
    start, end = queries.day_bounds("2026-03-08", zone=NEW_YORK)
    span = (datetime.fromisoformat(end) - datetime.fromisoformat(start))
    assert span == timedelta(hours=23)
    # And the autumn day is 25.
    start, end = queries.day_bounds("2026-11-01", zone=NEW_YORK)
    assert datetime.fromisoformat(end) - datetime.fromisoformat(start) == \
        timedelta(hours=25)


def test_all_days_finds_every_day_with_data(conn, source_id):
    metric_id = write(conn, source_id, "heart_rate_bpm", [
        (datetime(2026, 3, 1, 12, tzinfo=UTC), 70),
        (datetime(2026, 3, 1, 13, tzinfo=UTC), 71),
        (datetime(2026, 3, 3, 12, tzinfo=UTC), 72),
    ])
    assert queries.all_days(conn, zone=UTC) == {
        (metric_id, "2026-03-01"), (metric_id, "2026-03-03")}


# -- retention -----------------------------------------------------------

def test_sweep_raw_payloads_deletes_only_what_is_past_the_window(conn, source_id):
    now = datetime(2026, 6, 1, tzinfo=UTC)
    conn.executemany(
        "INSERT INTO raw_payloads (source_id, endpoint, fetched_at, body) "
        "VALUES (?,?,?,?)",
        [(source_id, "/sleep", iso_utc(now - timedelta(days=days)), b"{}")
         for days in (1, 89, 91, 400)])

    assert queries.sweep_raw_payloads(conn, keep_days=90, now=now) == 2
    assert conn.execute("SELECT COUNT(*) FROM raw_payloads").fetchone() == (2,)


def test_retention_of_zero_keeps_everything(conn, source_id):
    conn.execute(
        "INSERT INTO raw_payloads (source_id, endpoint, fetched_at, body) "
        "VALUES (?,?,?,?)",
        (source_id, "/sleep", iso_utc(datetime(2020, 1, 1, tzinfo=UTC)), b"{}"))
    assert queries.sweep_raw_payloads(conn, keep_days=0) == 0
    assert conn.execute("SELECT COUNT(*) FROM raw_payloads").fetchone() == (1,)


def test_sync_state_is_keyed_by_metric_name(conn, source_id):
    metric_id = queries.metric_id(conn, "spo2_pct")
    conn.execute(
        "INSERT INTO sync_state (source_id, metric_id, watermark_ts) VALUES (?,?,?)",
        (source_id, metric_id, "2026-03-01T00:00:00.000+00:00"))
    state = queries.sync_state(conn, source_id)
    assert state["spo2_pct"]["watermark_ts"] == "2026-03-01T00:00:00.000+00:00"
