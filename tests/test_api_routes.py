"""
Tests for the read API's endpoints.

None of these open a socket. The routing table takes a parsed request and
returns a status and a payload, which is the whole reason it is separate
from the transport -- argument parsing and the ingest path are where the
bugs are, and neither needs a listening port to exercise.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from ticker.api.routes import Api, ApiError, parse_time
from ticker.db import queries, store
from ticker.model import iso_utc, now_utc

UTC = timezone.utc
START = datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC)


class FakeWriter:
    """Records what would have been written, without a writer thread."""

    def __init__(self):
        self.batches = []
        self.sessions = []
        self.ended = []

    def insert_observations(self, source_id, batch):
        self.batches.append((source_id, list(batch)))

    def begin_session(self, source_id, record, device_id=None):
        self.sessions.append((source_id, record, device_id))

    def end_session(self, source_id, key, end_ts=None):
        self.ended.append((source_id, key))


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "api.sqlite3"
    conn = store.connect(path)
    yield path, conn
    conn.close()


@pytest.fixture
def writer():
    return FakeWriter()


@pytest.fixture
def api(db, writer):
    _, conn = db
    return Api(lambda: conn, writer)


def seed(conn, source_id, metric, count, start=START):
    metric_id = queries.metric_id(conn, metric)
    conn.executemany(
        "INSERT INTO observations (source_id, metric_id, ts, value, external_id, "
        "ingested_at) VALUES (?,?,?,?,?,?)",
        [(source_id, metric_id, iso_utc(start + timedelta(seconds=i)),
          60.0 + i, str(i), iso_utc(start)) for i in range(count)])


def body(**payload):
    return json.dumps(payload).encode("utf-8")


# -- parse_time -----------------------------------------------------------

def test_an_absolute_timestamp_is_parsed():
    assert parse_time("2026-05-01T09:00:00Z", None) == START


def test_a_missing_time_falls_back_to_the_default():
    assert parse_time(None, START) == START
    assert parse_time("  ", START) == START


def test_a_relative_time_is_measured_back_from_now():
    parsed = parse_time("-6h", None)
    assert abs((now_utc() - parsed).total_seconds() - 6 * 3600) < 5


def test_epoch_seconds_and_milliseconds_are_both_understood():
    seconds = parse_time(str(int(START.timestamp())), None)
    millis = parse_time(str(int(START.timestamp() * 1000)), None)
    assert seconds == millis == START


def test_an_unparseable_time_is_a_400_not_a_crash():
    with pytest.raises(ApiError) as raised:
        parse_time("yesterday", None)
    assert raised.value.status == 400


# -- routing --------------------------------------------------------------

def test_health_needs_nothing(api):
    status, payload = api.handle("GET", "/health", {})
    assert status == 200 and payload["ok"] is True


def test_an_unknown_path_is_404(api):
    assert api.handle("GET", "/api/nope", {})[0] == 404
    assert api.handle("POST", "/api/nope", {}, b"{}")[0] == 404


def test_an_unsupported_method_is_405(api):
    assert api.handle("DELETE", "/api/metrics", {})[0] == 405


def test_a_trailing_slash_reaches_the_same_endpoint(api):
    assert api.handle("GET", "/api/metrics/", {})[0] == 200


def test_an_unexpected_failure_is_a_500_not_an_exception(db, writer):
    def broken():
        raise RuntimeError("database is on fire")

    status, payload = Api(broken, writer).handle("GET", "/api/metrics", {})
    assert status == 500
    # A dashboard polling every five seconds must not be able to take the
    # server down by asking at a bad moment.
    assert "on fire" in payload["error"]


# -- GET /api/metrics -----------------------------------------------------

def test_metrics_returns_the_registry(api):
    status, payload = api.handle("GET", "/api/metrics", {})
    assert status == 200
    assert any(m["name"] == "heart_rate_bpm" for m in payload["metrics"])


# -- GET /api/observations ------------------------------------------------

def test_observations_needs_a_metric(api):
    status, payload = api.handle("GET", "/api/observations", {})
    assert status == 400 and "metric" in payload["error"]


def test_observations_returns_points_in_the_window(api, db):
    _, conn = db
    source_id = store.ensure_source(conn, "stream", "ble", "Strap")
    seed(conn, source_id, "heart_rate_bpm", 5)
    status, payload = api.handle("GET", "/api/observations", {
        "metric": "heart_rate_bpm",
        "from": iso_utc(START),
        "to": iso_utc(START + timedelta(minutes=1)),
    })
    assert status == 200
    assert payload["n"] == 5
    assert payload["bucket"] == "raw"


def test_observations_buckets_server_side(api, db):
    _, conn = db
    source_id = store.ensure_source(conn, "stream", "ble", "Strap")
    seed(conn, source_id, "heart_rate_bpm", 120)
    status, payload = api.handle("GET", "/api/observations", {
        "metric": "heart_rate_bpm", "from": iso_utc(START),
        "to": iso_utc(START + timedelta(minutes=5)), "bucket": "1m",
    })
    assert status == 200
    assert payload["n"] == 2


def test_an_unknown_metric_is_404(api):
    status, payload = api.handle("GET", "/api/observations", {"metric": "nope"})
    assert status == 404


def test_a_bad_bucket_is_400(api, db):
    _, conn = db
    status, payload = api.handle("GET", "/api/observations",
                                 {"metric": "heart_rate_bpm", "bucket": "5x"})
    assert status == 400
    assert "bucket" in payload["error"]


def test_a_backwards_window_is_400(api):
    status, payload = api.handle("GET", "/api/observations", {
        "metric": "heart_rate_bpm", "from": iso_utc(START),
        "to": iso_utc(START - timedelta(hours=1))})
    assert status == 400


def test_a_backwards_session_window_is_400(api):
    status, _ = api.handle("GET", "/api/sessions", {
        "from": iso_utc(START),
        "to": iso_utc(START - timedelta(hours=1))})
    assert status == 400


def test_a_bad_source_id_is_400(api):
    status, _ = api.handle("GET", "/api/observations",
                           {"metric": "heart_rate_bpm", "source_id": "abc"})
    assert status == 400


def test_omitting_the_window_defaults_to_the_last_day(api):
    status, payload = api.handle("GET", "/api/observations",
                                 {"metric": "heart_rate_bpm"})
    assert status == 200
    span = (datetime.fromisoformat(payload["to"])
            - datetime.fromisoformat(payload["from"]))
    assert span == timedelta(hours=24)


# -- GET /api/sessions and /api/sources -----------------------------------

def test_sessions_lists_what_overlaps_the_window(api, db):
    _, conn = db
    source_id = store.ensure_source(conn, "stream", "ble", "Strap")
    conn.execute("INSERT INTO sessions (source_id, start_ts, kind) VALUES (?,?,?)",
                 (source_id, iso_utc(START), "manual"))
    status, payload = api.handle("GET", "/api/sessions",
                                 {"from": iso_utc(START - timedelta(days=1))})
    assert status == 200
    assert len(payload["sessions"]) == 1


def test_sources_reports_configuration_and_health(api):
    status, payload = api.handle("GET", "/api/sources", {})
    assert status == 200
    # The v1 migration seeds the BLE strap, so this is never empty.
    assert any(s["vendor"] == "ble" for s in payload["sources"])


def test_sources_omits_row_counts_by_default(api):
    _, payload = api.handle("GET", "/api/sources", {})
    assert "n_observations" not in payload["sources"][0]


def test_sources_counts_rows_when_asked_to(api):
    _, payload = api.handle("GET", "/api/sources", {"counts": "1"})
    assert payload["sources"][0]["n_observations"] == 0


def test_a_bare_flag_counts_as_on(api):
    # ?counts with no value, the way a command-line flag reads.
    _, payload = api.handle("GET", "/api/sources", {"counts": ""})
    assert "n_observations" in payload["sources"][0]


# -- POST /api/ingest -----------------------------------------------------

def ingest_body(observations=None, **extra):
    payload = {"source": {"vendor": "ble", "display_name": "Strap"},
               "observations": observations if observations is not None else
               [{"metric": "heart_rate_bpm", "ts": iso_utc(START), "value": 142}]}
    payload.update(extra)
    return json.dumps(payload).encode("utf-8")


def test_ingest_writes_the_batch(api, writer):
    status, payload = api.handle("POST", "/api/ingest", {}, ingest_body())
    assert status == 200
    assert payload["accepted"] == 1
    (source_id, batch), = writer.batches
    assert batch[0].metric == "heart_rate_bpm"
    assert batch[0].value == 142


def test_ingest_resolves_the_source_by_vendor_and_name(api, db):
    _, conn = db
    api.handle("POST", "/api/ingest", {}, ingest_body())
    api.handle("POST", "/api/ingest", {}, ingest_body())
    # Twice, but one source row: the agent doesn't know its source_id and
    # must not be able to create a new one by posting again.
    rows = conn.execute("SELECT COUNT(*) FROM sources WHERE display_name = 'Strap'")
    assert rows.fetchone()[0] == 1


def test_ingest_defaults_the_display_name_to_the_vendors(api, db):
    _, conn = db
    api.handle("POST", "/api/ingest", {},
               body(source={"vendor": "ble"}, observations=[]))
    assert conn.execute(
        "SELECT COUNT(*) FROM sources WHERE display_name = 'BLE strap'"
    ).fetchone()[0] == 1


def test_ingest_needs_a_source(api):
    status, payload = api.handle("POST", "/api/ingest", {},
                                 body(observations=[]))
    assert status == 400 and "vendor" in payload["error"]


def test_ingest_rejects_an_unknown_source_kind(api):
    status, _ = api.handle("POST", "/api/ingest", {},
                           body(source={"vendor": "ble", "kind": "magic"},
                                observations=[]))
    assert status == 400


def test_ingest_rejects_a_malformed_observation(api, writer):
    status, payload = api.handle("POST", "/api/ingest", {},
                                 ingest_body([{"metric": "heart_rate_bpm"}]))
    assert status == 400
    assert "ts" in payload["error"]
    # Nothing partially written: a batch is taken whole or not at all.
    assert writer.batches == []


@pytest.mark.parametrize("value", [True, "NaN", "Infinity"])
def test_ingest_rejects_non_finite_or_boolean_values(api, writer, value):
    status, payload = api.handle("POST", "/api/ingest", {}, ingest_body([
        {"metric": "heart_rate_bpm", "ts": iso_utc(START), "value": value}]))
    assert status == 400
    assert "finite number" in payload["error"]
    assert writer.batches == []


def test_ingest_rejects_non_string_metadata(api, writer):
    status, payload = api.handle("POST", "/api/ingest", {}, ingest_body([
        {"metric": "heart_rate_bpm", "ts": iso_utc(START), "value": 60,
         "external_id": {"not": "bindable"}}]))
    assert status == 400
    assert "external_id" in payload["error"]
    assert writer.batches == []


def test_ingest_rejects_unknown_metrics_before_acknowledging(api, writer):
    status, payload = api.handle("POST", "/api/ingest", {}, ingest_body([
        {"metric": "future_metric", "ts": iso_utc(START), "value": 1}]))
    assert status == 422
    assert "future_metric" in payload["error"]
    assert writer.batches == []


def test_ingest_rejects_a_batch_over_the_limit(db, writer):
    _, conn = db
    api = Api(lambda: conn, writer, max_batch=2)
    rows = [{"metric": "heart_rate_bpm", "ts": iso_utc(START), "value": 60,
             "external_id": str(i)} for i in range(3)]
    status, _ = api.handle("POST", "/api/ingest", {}, ingest_body(rows))
    assert status == 413
    assert writer.batches == []


def test_ingest_rejects_a_body_that_is_not_json(api):
    assert api.handle("POST", "/api/ingest", {}, b"not json")[0] == 400


def test_ingest_rejects_an_empty_body(api):
    assert api.handle("POST", "/api/ingest", {}, b"")[0] == 400


def test_ingest_rejects_a_json_array(api):
    assert api.handle("POST", "/api/ingest", {}, b"[1, 2]")[0] == 400


def test_ingest_opens_sessions_before_writing_observations(api, writer):
    api.handle("POST", "/api/ingest", {}, ingest_body(
        sessions=[{"key": "1", "start_ts": iso_utc(START), "label": "run"}]))
    # The session is the foreign key the observations reference, so it has
    # to reach the writer first.
    assert writer.sessions and writer.batches
    assert writer.sessions[0][1].label == "run"


def test_ingest_closes_the_sessions_it_is_told_about(api, writer):
    api.handle("POST", "/api/ingest", {}, ingest_body(closed_sessions=["1"]))
    assert writer.ended == [(writer.batches[0][0], "1")]


def test_a_malformed_session_rejects_the_whole_batch(api, writer):
    status, _ = api.handle("POST", "/api/ingest", {},
                           ingest_body(sessions=[{"start_ts": iso_utc(START)}]))
    assert status == 400
    assert writer.sessions == [] and writer.batches == []


def test_ingest_registers_the_device_it_is_told_about(api, db, writer):
    _, conn = db
    api.handle("POST", "/api/ingest", {}, ingest_body(
        device={"address": "AA:BB:CC", "name": "H10"},
        sessions=[{"key": "1", "start_ts": iso_utc(START)}]))
    row = conn.execute("SELECT name, address FROM devices").fetchone()
    assert row == ("H10", "AA:BB:CC")
    assert writer.sessions[0][2] is not None


def test_a_device_without_an_address_is_not_a_row(api, db):
    _, conn = db
    api.handle("POST", "/api/ingest", {}, ingest_body(device={"name": "H10"}))
    assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0


def test_an_empty_batch_is_accepted(api):
    # An agent posting only a session end has no observations to send, and
    # that is not an error.
    status, payload = api.handle("POST", "/api/ingest", {}, ingest_body([]))
    assert status == 200 and payload["accepted"] == 0


# -- POST /api/sync/{id} --------------------------------------------------

@pytest.fixture
def pull_source(db):
    _, conn = db
    return store.ensure_source(conn, "pull", "oura", "Ring")


def test_sync_queues_the_source(db, writer, pull_source):
    asked = []
    api = Api(lambda: db[1], writer, on_sync=lambda sid: asked.append(sid) or True)
    status, payload = api.handle("POST", "/api/sync/{}".format(pull_source), {})
    assert status == 202
    assert asked == [pull_source]


def test_sync_says_so_when_nothing_can_act_on_it(api, pull_source):
    # A 200 that meant nothing is worse than an honest 503: the caller is
    # polling for fresh data that is never going to arrive.
    status, payload = api.handle("POST", "/api/sync/{}".format(pull_source), {})
    assert status == 503
    assert "ticker-sync" in payload["error"]


def test_sync_rejects_a_source_that_is_already_running(db, writer, pull_source):
    api = Api(lambda: db[1], writer, on_sync=lambda sid: False)
    assert api.handle("POST", "/api/sync/{}".format(pull_source), {})[0] == 409


def test_sync_of_an_unknown_source_is_404(api):
    assert api.handle("POST", "/api/sync/999", {})[0] == 404


def test_sync_of_a_stream_source_is_refused(api, db):
    _, conn = db
    source_id = store.ensure_source(conn, "stream", "ble", "Strap")
    status, payload = api.handle("POST", "/api/sync/{}".format(source_id), {})
    assert status == 400
    assert "pull" in payload["error"]


def test_sync_of_a_disabled_source_is_refused(api, db, pull_source):
    _, conn = db
    conn.execute("UPDATE sources SET enabled = 0 WHERE id = ?", (pull_source,))
    assert api.handle("POST", "/api/sync/{}".format(pull_source), {})[0] == 409


def test_a_non_numeric_source_id_is_400(api):
    assert api.handle("POST", "/api/sync/oura", {})[0] == 400


# -- connection handling --------------------------------------------------

def test_a_connection_the_factory_owns_is_not_closed(db, writer):
    _, conn = db
    api = Api(lambda: conn, writer)
    api.handle("GET", "/api/metrics", {})
    # Still usable: a per-thread connection must survive being borrowed.
    assert api.handle("GET", "/api/metrics", {})[0] == 200


def test_a_connection_opened_for_one_request_is_closed_again(db, writer):
    path, _ = db
    opened = []

    def factory():
        conn = store.connect(path, migrate_first=False)
        opened.append(conn)
        return conn, True                      # (connection, owned)

    Api(factory, writer).handle("GET", "/api/metrics", {})
    assert len(opened) == 1
    with pytest.raises(Exception):
        opened[0].execute("SELECT 1")
