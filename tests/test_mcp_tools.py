"""
Tests for the MCP tools, against real temporary databases.

Each test seeds a freshly migrated file through an ordinary writable
connection and asks the tools through a read-only one, the way the real
server does -- so the guarantee that matters most, that nothing an agent
sends can change the data, is tested against the actual connection rather
than taken on trust.

The zone is pinned to UTC and "now" to a fixed instant, so local days and
relative windows come out the same on every machine.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from ticker.db import queries, store
from ticker.mcp.readonly import ReadOnlyDatabase
from ticker.mcp.tools import ToolError, Tools
from ticker.model import iso_utc

UTC = timezone.utc
NOW = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)

try:
    from zoneinfo import ZoneInfo
    NEW_YORK = ZoneInfo("America/New_York")
except Exception:                                  # pragma: no cover
    NEW_YORK = None


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "mcp.sqlite3"
    conn = store.connect(path)
    yield path, conn
    conn.close()


@pytest.fixture
def make_tools(db):
    opened = []

    def make(zone=UTC, **kwargs):
        readonly = ReadOnlyDatabase(db[0])
        opened.append(readonly)
        return Tools(readonly, zone=zone, now=lambda: NOW, **kwargs)

    yield make
    for readonly in opened:
        readonly.close()


@pytest.fixture
def tools(make_tools):
    return make_tools(sql_budget_sec=1.0)


@pytest.fixture
def strap(db):
    # The v2 migration always creates this row, so it is source 1.
    return store.ensure_source(db[1], "stream", "ble", "BLE strap")


@pytest.fixture
def ring(db):
    return store.ensure_source(db[1], "pull", "oura", "Ring")


def add(conn, source_id, metric, ts, value, end=None, text=None, key=None,
        session_id=None):
    conn.execute(
        "INSERT INTO observations (source_id, metric_id, session_id, ts, "
        "end_ts, value, text_value, external_id, ingested_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (source_id, queries.metric_id(conn, metric), session_id, iso_utc(ts),
         iso_utc(end) if end else None, float(value), text,
         key if key is not None else iso_utc(ts), iso_utc(NOW)))


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def session(conn, source_id, start, end, kind, label=None):
    cur = conn.execute(
        "INSERT INTO sessions (source_id, start_ts, end_ts, kind, label) "
        "VALUES (?,?,?,?,?)",
        (source_id, iso_utc(start), iso_utc(end) if end else None, kind, label))
    return cur.lastrowid


def build_rollups(conn):
    queries.rebuild_days(conn, queries.all_days(conn, zone=UTC), zone=UTC)


# -- availability -----------------------------------------------------------

def test_a_missing_database_is_reported_not_created(tmp_path):
    path = tmp_path / "not-yet.sqlite3"
    tools = Tools(ReadOnlyDatabase(path), zone=UTC, now=lambda: NOW)
    with pytest.raises(ToolError, match="No Ticker database"):
        tools.call("get_overview", {})
    assert not path.exists()


def test_the_database_is_picked_up_once_it_exists(tmp_path):
    # The agent may start before anything has ever been recorded; the
    # server should start working the moment there is something to read.
    path = tmp_path / "later.sqlite3"
    tools = Tools(ReadOnlyDatabase(path), zone=UTC, now=lambda: NOW)
    with pytest.raises(ToolError):
        tools.call("get_overview", {})
    store.connect(path).close()
    assert "sources" in tools.call("get_overview", {})


def test_an_old_schema_asks_for_an_upgrade_rather_than_migrating(tmp_path):
    path = tmp_path / "v1.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.executescript("CREATE TABLE sessions (id INTEGER);"
                       "CREATE TABLE samples (id INTEGER);")
    conn.close()
    tools = Tools(ReadOnlyDatabase(path), zone=UTC, now=lambda: NOW)
    with pytest.raises(ToolError, match="schema version 1"):
        tools.call("get_overview", {})
    conn = sqlite3.connect(str(path))
    tables = {name for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert tables == {"sessions", "samples"}          # untouched


# -- query_sql and the read-only guarantee --------------------------------------

@pytest.mark.parametrize("sql", [
    "DELETE FROM observations",
    "UPDATE sources SET enabled = 0",
    "INSERT INTO metrics (name, unit, value_kind) VALUES ('x', 'x', 'instant')",
    "DROP TABLE observations",
    "CREATE TABLE evil (x)",
    "CREATE TEMP TABLE scratch (x)",
    "ATTACH DATABASE ':memory:' AS other",
    "PRAGMA query_only = OFF",
    "PRAGMA journal_mode = DELETE",
])
def test_nothing_can_write_through_query_sql(tools, db, strap, sql):
    add(db[1], strap, "heart_rate_bpm", NOW, 60)
    with pytest.raises(ToolError, match="read-only"):
        tools.call("query_sql", {"sql": sql})
    conn = db[1]
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone() == (1,)
    assert conn.execute("SELECT COUNT(*) FROM sources WHERE enabled = 0"
                        ).fetchone() == (0,)


def test_vacuum_into_cannot_copy_the_database_elsewhere(tools, tmp_path):
    target = tmp_path / "copy.sqlite3"
    with pytest.raises(ToolError):
        tools.call("query_sql", {"sql": "VACUUM INTO '{}'".format(target.as_posix())})
    assert not target.exists()


def test_a_select_returns_columns_and_rows(tools, db, strap):
    add(db[1], strap, "heart_rate_bpm", NOW, 61)
    result = tools.call("query_sql", {
        "sql": "SELECT m.name, o.value FROM observations o "
               "JOIN metrics m ON m.id = o.metric_id"})
    assert result["columns"] == ["name", "value"]
    assert result["rows"] == [["heart_rate_bpm", 61.0]]
    assert result["truncated"] is False


def test_rows_past_max_rows_are_cut_and_the_answer_says_so(tools, db, strap):
    for i in range(5):
        add(db[1], strap, "heart_rate_bpm", NOW - timedelta(seconds=i), 60)
    result = tools.call("query_sql", {"sql": "SELECT ts FROM observations",
                                      "max_rows": 3})
    assert len(result["rows"]) == 3
    assert result["truncated"] is True and "LIMIT" in result["note"]


def test_a_runaway_query_is_stopped_and_the_next_one_still_works(tools):
    with pytest.raises(ToolError, match="stopped after"):
        tools.call("query_sql", {
            "sql": "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL "
                   "SELECT x + 1 FROM c) SELECT COUNT(*) FROM c"})
    assert tools.call("query_sql", {"sql": "SELECT 1"})["rows"] == [[1]]


def test_one_statement_at_a_time(tools):
    with pytest.raises(ToolError, match="one statement"):
        tools.call("query_sql", {"sql": "SELECT 1; SELECT 2"})


def test_blobs_are_described_not_dumped(tools):
    result = tools.call("query_sql", {"sql": "SELECT x'00ff10'"})
    assert result["rows"] == [["<3 bytes>"]]


def test_schema_pragmas_are_allowed(tools):
    result = tools.call("query_sql", {"sql": "PRAGMA table_info(observations)"})
    assert "ts" in [row[1] for row in result["rows"]]


def test_a_syntax_error_is_reported_as_such(tools):
    with pytest.raises(ToolError, match="SQL error"):
        tools.call("query_sql", {"sql": "SELEC 1"})


# -- arguments --------------------------------------------------------------

def test_an_unknown_argument_is_named_with_the_right_ones(tools):
    with pytest.raises(ToolError) as raised:
        tools.call("get_timeseries", {"metric": "heart_rate_bpm", "strat": "-1h"})
    assert "strat" in str(raised.value) and "start" in str(raised.value)


def test_a_missing_required_argument_is_named(tools):
    with pytest.raises(ToolError, match="needs metric"):
        tools.call("get_timeseries", {})


def test_an_unknown_metric_lists_the_known_ones(tools):
    with pytest.raises(ToolError) as raised:
        tools.call("get_timeseries", {"metric": "heartrate"})
    assert "heart_rate_bpm" in str(raised.value)


def test_an_unreadable_time_lists_the_accepted_forms(tools):
    with pytest.raises(ToolError, match="relative time"):
        tools.call("get_timeseries", {"metric": "heart_rate_bpm",
                                      "start": "last tuesday"})


def test_relative_and_named_times(tools):
    assert tools._instant({"t": "-6h"}, "t", None) == NOW - timedelta(hours=6)
    assert tools._instant({"t": "-2w"}, "t", None) == NOW - timedelta(days=14)
    assert tools._instant({"t": "now"}, "t", None) == NOW
    assert tools._instant({"t": "yesterday"}, "t", None) == at("2026-05-09T00:00")
    assert tools._instant({"t": "2026-05-01T06:30:00Z"}, "t", None) == \
        at("2026-05-01T06:30")


@pytest.mark.skipif(NEW_YORK is None, reason="no IANA tz database available")
def test_a_date_or_time_without_an_offset_is_local(make_tools):
    tools = make_tools(zone=NEW_YORK)
    # Midnight in New York on 1 May is 04:00 UTC (EDT).
    assert tools._instant({"t": "2026-05-01"}, "t", None) == at("2026-05-01T04:00")
    assert tools._instant({"t": "2026-05-01T06:30"}, "t", None) == \
        at("2026-05-01T10:30")


# -- get_overview -----------------------------------------------------------

def test_overview_reports_sources_metrics_and_sessions(tools, db, strap, ring):
    conn = db[1]
    add(conn, strap, "heart_rate_bpm", NOW - timedelta(hours=1), 70)
    add(conn, ring, "steps", at("2026-05-09T00:00"), 9000,
        end=at("2026-05-10T00:00"))
    conn.execute(
        "INSERT INTO sync_state (source_id, metric_id, last_attempt, last_error) "
        "VALUES (?,?,?,?)", (ring, queries.metric_id(conn, "steps"),
                             iso_utc(NOW), "401 token expired"))
    session(conn, strap, NOW - timedelta(hours=2), NOW - timedelta(hours=1),
            "manual", "ride")

    result = tools.call("get_overview", {})
    metrics = {m["metric"]: m for m in result["metrics"]}
    assert set(metrics) == {"heart_rate_bpm", "steps"}
    assert metrics["heart_rate_bpm"]["unit"] == "bpm"
    assert metrics["heart_rate_bpm"]["sources"] == ["BLE strap"]
    assert metrics["steps"]["kind"] == "cumulative"
    assert "weight_kg" in result["metrics_without_data"]
    assert result["sessions"]["manual"]["count"] == 1

    sources = {s["name"]: s for s in result["sources"]}
    assert sources["Ring"]["type"] == "cloud sync"
    assert sources["Ring"]["sync_errors"] == {"steps": "401 token expired"}
    assert any("401 token expired" in note for note in result["notes"])
    # The keyring entry name and connector config are nobody's business.
    assert not {"auth_ref", "config_json"} & set(sources["Ring"])


def test_overview_on_an_empty_database_says_how_to_connect(tools):
    result = tools.call("get_overview", {})
    assert result["metrics"] == []
    assert any("Connect" in note for note in result["notes"])


def test_overview_flags_a_cloud_source_that_stopped_delivering(tools, db, ring):
    add(db[1], ring, "heart_rate_bpm", NOW - timedelta(days=5), 55)
    result = tools.call("get_overview", {})
    assert any("delivered nothing since" in note for note in result["notes"])


# -- get_daily_summary ------------------------------------------------------

def test_point_metrics_average_per_day(tools, db, strap):
    conn = db[1]
    for ts, value in [("2026-05-05T08:00", 60), ("2026-05-05T20:00", 70),
                      ("2026-05-06T08:00", 80), ("2026-05-06T20:00", 90)]:
        add(conn, strap, "heart_rate_bpm", at(ts), value)
    build_rollups(conn)

    result = tools.call("get_daily_summary", {
        "metrics": ["heart_rate_bpm"], "start": "2026-05-05", "end": "2026-05-06"})
    metric, = result["results"]
    assert metric["daily_value"] == "average"
    series, = metric["series"]
    assert series["columns"] == ["day", "avg", "min", "max", "n"]
    assert series["rows"] == [["2026-05-05", 65, 60, 70, 2],
                              ["2026-05-06", 85, 80, 90, 2]]
    assert series["summary"]["mean_per_day"] == 75
    assert series["summary"]["highest"] == {"day": "2026-05-06", "value": 85}


def test_today_is_computed_live_even_before_its_rollup(tools, db, strap):
    add(db[1], strap, "heart_rate_bpm", NOW - timedelta(hours=1), 70)
    result = tools.call("get_daily_summary", {"metrics": ["heart_rate_bpm"],
                                              "start": "today"})
    assert result["results"][0]["series"][0]["rows"] == [["2026-05-10", 70, 70, 70, 1]]


def test_unrolled_history_is_computed_from_raw_with_a_note(tools, db, strap):
    add(db[1], strap, "heart_rate_bpm", at("2026-05-03T12:00"), 50)
    metric, = tools.call("get_daily_summary", {
        "metrics": ["heart_rate_bpm"], "start": "2026-05-01",
        "end": "2026-05-05"})["results"]
    assert metric["series"][0]["rows"] == [["2026-05-03", 50, 50, 50, 1]]
    assert any("ticker rollup" in note for note in metric["notes"])


def test_two_sources_counting_steps_are_not_added_together(tools, db, strap, ring):
    conn = db[1]
    day, next_day = at("2026-05-09T00:00"), at("2026-05-10T00:00")
    add(conn, strap, "steps", day, 1000, end=next_day)
    add(conn, ring, "steps", day, 1200, end=next_day)
    metric, = tools.call("get_daily_summary", {
        "metrics": ["steps"], "start": "2026-05-09", "end": "2026-05-09"})["results"]
    totals = {s["source"]: s["rows"][0][1] for s in metric["series"]}
    assert totals == {"BLE strap": 1000, "Ring": 1200}
    assert any("twice" in note for note in metric["notes"])


def test_a_source_filter_shows_just_that_source(tools, db, strap, ring):
    conn = db[1]
    add(conn, strap, "steps", at("2026-05-09T00:00"), 1000)
    add(conn, ring, "steps", at("2026-05-09T00:00"), 1200)
    metric, = tools.call("get_daily_summary", {
        "metrics": ["steps"], "start": "2026-05-09", "end": "2026-05-09",
        "source_id": ring})["results"]
    assert [s["source"] for s in metric["series"]] == ["Ring"]
    with pytest.raises(ToolError, match="no source 999"):
        tools.call("get_daily_summary", {"metrics": ["steps"], "source_id": 999})


def test_grouping_by_week_gives_per_day_and_total(tools, db, ring):
    conn = db[1]
    monday = at("2026-04-27T00:00")
    for i in range(14):
        day = monday + timedelta(days=i)
        add(conn, ring, "steps", day, 1000 if i < 7 else 2000,
            end=day + timedelta(days=1))
    build_rollups(conn)
    metric, = tools.call("get_daily_summary", {
        "metrics": ["steps"], "start": "2026-04-27", "end": "2026-05-10",
        "group_by": "week"})["results"]
    series, = metric["series"]
    assert series["columns"] == ["week_of", "per_day", "total", "days"]
    assert series["rows"] == [["2026-04-27", 1000, 7000, 7],
                              ["2026-05-04", 2000, 14000, 7]]


def test_long_ranges_group_by_week_on_their_own(tools):
    result = tools.call("get_daily_summary", {"metrics": ["steps"],
                                              "start": "-200d"})
    assert result["group_by"] == "week"


def test_a_day_by_day_listing_that_is_too_long_is_refused(tools):
    with pytest.raises(ToolError, match="week"):
        tools.call("get_daily_summary", {"metrics": ["steps"], "start": "-500d",
                                         "group_by": "day"})


def test_sleep_stages_are_pointed_at_get_sleep(tools):
    metric, = tools.call("get_daily_summary", {"metrics": ["sleep_stage"]})["results"]
    assert "get_sleep" in metric["note"]


def test_a_backwards_range_is_refused(tools):
    with pytest.raises(ToolError, match="after"):
        tools.call("get_daily_summary", {"metrics": ["steps"],
                                         "start": "2026-05-09", "end": "2026-05-01"})


def test_metrics_given_as_one_comma_separated_string_are_accepted(tools):
    result = tools.call("get_daily_summary", {"metrics": "steps,heart_rate_bpm"})
    assert [r["metric"] for r in result["results"]] == ["steps", "heart_rate_bpm"]


# -- get_timeseries ---------------------------------------------------------

def hour_of_heart_rate(conn, source_id):
    """3600 readings, one a second, over the hour before NOW. Mean 64.5."""
    metric = queries.metric_id(conn, "heart_rate_bpm")
    conn.executemany(
        "INSERT INTO observations (source_id, metric_id, ts, value, "
        "external_id, ingested_at) VALUES (?,?,?,?,?,?)",
        [(source_id, metric, iso_utc(NOW - timedelta(seconds=3600 - i)),
          60.0 + i % 10, str(i), iso_utc(NOW)) for i in range(3600)])


def test_a_small_window_comes_back_raw_in_local_time(tools, db, strap):
    for i in range(5):
        add(db[1], strap, "heart_rate_bpm", NOW - timedelta(minutes=10 - i), 70 + i)
    result = tools.call("get_timeseries", {"metric": "heart_rate_bpm",
                                           "start": "-1h"})
    assert result["bucket"] == "raw"
    assert result["columns"] == ["time", "value"]
    assert result["rows"][0] == ["2026-05-10T11:50:00+00:00", 70]
    assert result["stats"] == {"n": 5, "mean": 72, "min": 70, "max": 74}


def test_a_large_window_is_bucketed_to_fit(tools, db, strap):
    hour_of_heart_rate(db[1], strap)
    result = tools.call("get_timeseries", {"metric": "heart_rate_bpm",
                                           "start": "-1h", "max_points": 50})
    assert result["bucket"] == "2m"
    assert 0 < len(result["rows"]) <= 50
    assert result["stats"]["n"] == 3600
    assert result["stats"]["mean"] == 64.5


def test_raw_that_would_not_fit_is_refused(tools, db, strap):
    hour_of_heart_rate(db[1], strap)
    with pytest.raises(ToolError, match="3600 readings"):
        tools.call("get_timeseries", {"metric": "heart_rate_bpm", "start": "-1h",
                                      "bucket": "raw", "max_points": 100})


def test_too_fine_a_bucket_is_refused_with_advice(tools, db, strap):
    hour_of_heart_rate(db[1], strap)
    with pytest.raises(ToolError, match="wider bucket"):
        tools.call("get_timeseries", {"metric": "heart_rate_bpm", "start": "-1h",
                                      "bucket": "1s", "max_points": 100})


def test_a_bad_bucket_is_refused(tools, db, strap):
    add(db[1], strap, "heart_rate_bpm", NOW - timedelta(minutes=5), 70)
    with pytest.raises(ToolError, match="30s"):
        tools.call("get_timeseries", {"metric": "heart_rate_bpm", "start": "-1h",
                                      "bucket": "5x"})


def test_accumulating_buckets_hold_totals(tools, db, strap):
    for hours, value in [(3, 100), (2, 200), (1, 300)]:
        add(db[1], strap, "steps", NOW - timedelta(hours=hours, minutes=-5), value)
    result = tools.call("get_timeseries", {"metric": "steps", "start": "-4h",
                                           "bucket": "1h"})
    assert result["columns"] == ["time", "total", "n"]
    assert [row[1] for row in result["rows"]] == [100, 200, 300]
    assert result["stats"]["total"] == 600


def test_an_empty_window_is_an_answer_not_an_error(tools):
    result = tools.call("get_timeseries", {"metric": "spo2_pct"})
    assert result["stats"] == {"n": 0} and result["rows"] == []


@pytest.mark.skipif(NEW_YORK is None, reason="no IANA tz database available")
def test_times_come_back_in_the_configured_zone(make_tools, db, strap):
    add(db[1], strap, "heart_rate_bpm", at("2026-05-10T11:00"), 70)
    result = make_tools(zone=NEW_YORK).call(
        "get_timeseries", {"metric": "heart_rate_bpm", "start": "-6h"})
    assert result["rows"][0][0] == "2026-05-10T07:00:00-04:00"


# -- sessions ---------------------------------------------------------------

def test_list_sessions_newest_first_with_filters(tools, db, strap, ring):
    conn = db[1]
    session(conn, ring, at("2026-05-08T23:00"), at("2026-05-09T07:00"), "sleep")
    session(conn, strap, at("2026-05-09T10:00"), at("2026-05-09T11:00"),
            "manual", "ride")
    session(conn, strap, at("2026-05-10T11:30"), None, "manual", "run")

    result = tools.call("list_sessions", {})
    assert result["total"] == 3
    assert [row[2] for row in result["rows"]] == ["run", "ride", None]
    running = result["rows"][0]
    assert running[6] is None and running[7] == 30         # in progress, 30 min
    assert [row[1] for row in tools.call("list_sessions", {"kind": "sleep"})["rows"]] \
        == ["sleep"]
    assert "newest 1 of 3" in tools.call("list_sessions", {"limit": 1})["note"]


def test_a_session_counts_only_its_own_sources_readings(tools, db, strap, ring):
    conn = db[1]
    sid = session(conn, strap, at("2026-05-09T10:00"), at("2026-05-09T11:00"),
                  "manual", "ride")
    add(conn, strap, "heart_rate_bpm", at("2026-05-09T10:15"), 120)
    add(conn, strap, "heart_rate_bpm", at("2026-05-09T10:30"), 140)
    add(conn, ring, "heart_rate_bpm", at("2026-05-09T10:20"), 60)   # the ring
    add(conn, strap, "heart_rate_bpm", at("2026-05-09T11:30"), 90)  # after

    result = tools.call("get_session", {"session_id": sid})
    heart, = result["metrics"]
    assert (heart["n"], heart["mean"], heart["max"]) == (2, 130, 140)
    assert result["session"]["duration_min"] == 60
    assert len(result["heart_rate"]["rows"]) == 2


def test_a_sleep_session_reports_minutes_per_stage(tools, db, ring):
    conn = db[1]
    sid = session(conn, ring, at("2026-05-08T23:00"), at("2026-05-09T07:00"), "sleep")
    add(conn, ring, "sleep_stage", at("2026-05-08T23:00"), 1,
        end=at("2026-05-09T01:00"), text="deep")
    add(conn, ring, "sleep_stage", at("2026-05-09T01:00"), 2,
        end=at("2026-05-09T07:00"), text="light")
    result = tools.call("get_session", {"session_id": sid})
    assert result["sleep_stages_min"] == {"deep": 120, "light": 360}


def test_an_unknown_session_points_at_list_sessions(tools):
    with pytest.raises(ToolError, match="list_sessions"):
        tools.call("get_session", {"session_id": 42})


# -- get_sleep --------------------------------------------------------------

def a_night(conn, source_id, heart=True):
    """23:00 on 8 May to 07:00 on 9 May: 480 in bed, 450 asleep."""
    for start, end, stage in [("2026-05-08T23:00", "2026-05-09T01:00", "light"),
                              ("2026-05-09T01:00", "2026-05-09T02:30", "deep"),
                              ("2026-05-09T02:30", "2026-05-09T04:00", "rem"),
                              ("2026-05-09T04:00", "2026-05-09T04:30", "awake"),
                              ("2026-05-09T04:30", "2026-05-09T07:00", "light")]:
        add(conn, source_id, "sleep_stage", at(start), 0, end=at(end), text=stage)
    if heart:
        add(conn, source_id, "heart_rate_bpm", at("2026-05-09T02:00"), 50)
        add(conn, source_id, "heart_rate_bpm", at("2026-05-09T05:00"), 60)


def test_a_night_is_rebuilt_and_labelled_by_the_day_of_waking(tools, db, ring):
    a_night(db[1], ring)
    result = tools.call("get_sleep", {"start": "2026-05-09", "end": "2026-05-09"})
    row, = result["rows"]
    assert dict(zip(result["columns"], row)) == {
        "night": "2026-05-09", "source": "Ring", "bedtime": "23:00",
        "wake": "07:00", "in_bed_min": 480, "asleep_min": 450,
        "efficiency_pct": 93.8, "deep_min": 90, "light_min": 270, "rem_min": 90,
        "awake_min": 30, "unstaged_min": 0, "vendor_sleep_min": None,
        "avg_hr": 55, "min_hr": 50, "avg_hrv_ms": None, "main": True}
    assert result["summary"][0]["nights"] == 1


def test_a_vendor_total_without_stages_still_makes_a_night(tools, db, ring):
    add(db[1], ring, "sleep_duration_s", at("2026-05-08T22:30"), 7 * 3600,
        end=at("2026-05-09T06:30"))
    result = tools.call("get_sleep", {})
    row = dict(zip(result["columns"], result["rows"][0]))
    assert row["asleep_min"] == row["vendor_sleep_min"] == 420
    # Not measured is not zero.
    assert row["deep_min"] is None and row["efficiency_pct"] is None


def test_a_nap_is_listed_but_is_not_the_main_sleep(tools, db, ring):
    conn = db[1]
    a_night(conn, ring, heart=False)
    add(conn, ring, "sleep_stage", at("2026-05-09T14:00"), 0,
        end=at("2026-05-09T14:40"), text="light")
    result = tools.call("get_sleep", {"start": "2026-05-09", "end": "2026-05-09"})
    assert [(row[2], row[-1]) for row in result["rows"]] == \
        [("23:00", True), ("14:00", False)]
    assert result["summary"][0]["nights"] == 1


def test_two_sources_are_reported_side_by_side(tools, db, ring):
    conn = db[1]
    watch = store.ensure_source(conn, "pull", "fitbit", "Watch")
    a_night(conn, ring, heart=False)
    a_night(conn, watch, heart=False)
    result = tools.call("get_sleep", {"start": "2026-05-09", "end": "2026-05-09"})
    assert sorted(row[1] for row in result["rows"]) == ["Ring", "Watch"]
    assert any("once per source" in note for note in result["notes"])


def test_no_sleep_says_so(tools):
    result = tools.call("get_sleep", {})
    assert result["rows"] == []
    assert any("No sleep recorded" in note for note in result["notes"])
