"""
Tests for the compact tool profile: the MCP surface sized for small local
models, at /mcp?profile=compact and `ticker mcp --compact`.

The point of the profile is what it costs a model before a question is
asked, so the size of the definitions is asserted directly, alongside the
smaller defaults and the absence of query_sql.
"""

import json
import socket
from datetime import datetime, timedelta, timezone

import pytest

from ticker.api import server as apiserver
from ticker.app import main as cli
from ticker.db import queries, store
from ticker.mcp import protocol
from ticker.mcp.readonly import ReadOnlyDatabase
from ticker.mcp.server import build
from ticker.mcp.tools import COMPACT_INSTRUCTIONS, COMPACT_TOOLS, ToolError, Tools
from ticker.model import iso_utc

UTC = timezone.utc
NOW = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "compact.sqlite3"
    store.connect(path).close()
    return path


@pytest.fixture
def pair(db):
    full_db, compact_db = ReadOnlyDatabase(db), ReadOnlyDatabase(db)
    yield (Tools(full_db, zone=UTC, now=lambda: NOW),
           Tools(compact_db, zone=UTC, now=lambda: NOW, profile="compact"))
    full_db.close()
    compact_db.close()


def test_compact_shows_the_everyday_tools_and_not_sql(pair):
    full, compact = pair
    names = [d["name"] for d in compact.definitions()]
    assert set(names) == set(COMPACT_TOOLS)
    assert "query_sql" not in names and "query_sql" not in compact


def test_compact_definitions_cost_a_fraction_of_the_full_ones(pair):
    full, compact = pair
    full_size = len(json.dumps(full.definitions()))
    compact_size = len(json.dumps(compact.definitions()))
    assert compact_size < 0.4 * full_size
    for definition in compact.definitions():
        assert len(definition["description"]) < 160


def test_compact_keeps_the_schema_a_model_needs(pair):
    full, compact = pair
    by_name = {d["name"]: d for d in compact.definitions()}
    summary = by_name["get_daily_summary"]["inputSchema"]
    assert summary["required"] == ["metrics"]
    assert summary["properties"]["group_by"]["enum"] == ["auto", "day", "week", "month"]
    assert summary["additionalProperties"] is False


def test_compact_calls_default_to_smaller_answers(db, pair):
    full, compact = pair
    conn = store.connect(db, migrate_first=False)
    strap = store.ensure_source(conn, "stream", "ble", "BLE strap")
    for i in range(12):
        start = NOW - timedelta(days=i, hours=2)
        conn.execute("INSERT INTO sessions (source_id, start_ts, end_ts, kind) "
                     "VALUES (?,?,?,?)", (strap, iso_utc(start),
                                          iso_utc(start + timedelta(hours=1)), "manual"))
    conn.close()
    assert len(compact.call("list_sessions", {})["rows"]) == 10
    assert len(full.call("list_sessions", {})["rows"]) == 12


def test_compact_overview_leaves_out_the_prose(db, pair):
    full, compact = pair
    conn = store.connect(db, migrate_first=False)
    source = store.ensure_source(conn, "stream", "ble", "BLE strap")
    conn.execute("INSERT INTO observations (source_id, metric_id, ts, value, "
                 "external_id, ingested_at) VALUES (?,?,?,?,?,?)",
                 (source, queries.metric_id(conn, "heart_rate_bpm"),
                  iso_utc(NOW - timedelta(hours=1)), 70.0, "", iso_utc(NOW)))
    conn.close()
    lean, rich = compact.call("get_overview", {}), full.call("get_overview", {})
    assert "description" not in lean["metrics"][0]
    assert "metrics_without_data" not in lean and "metrics_without_data" in rich


def test_an_unknown_profile_is_refused(db):
    with pytest.raises(ValueError):
        Tools(ReadOnlyDatabase(db), profile="tiny")


def test_compact_sql_is_an_unknown_tool(pair):
    with pytest.raises(ToolError, match="unknown tool"):
        pair[1].call("query_sql", {"sql": "SELECT 1"})


def test_the_compact_server_has_its_own_instructions(db):
    server, readonly = build(db, profile="compact")
    try:
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {}})
        assert reply["result"]["instructions"] == COMPACT_INSTRUCTIONS
    finally:
        readonly.close()


# -- over HTTP, and through the bridge ------------------------------------------

def post(url, message):
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, data=protocol.encode(message),
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None


def test_the_profile_is_chosen_by_query_parameter(tmp_path):
    server = apiserver.build(tmp_path / "http.sqlite3", host="127.0.0.1", port=0)
    server.start()
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    try:
        _, full = post(server.url + "/mcp", listing)
        _, compact = post(server.url + "/mcp?profile=compact", listing)
        assert len(full["result"]["tools"]) == 7
        assert len(compact["result"]["tools"]) == len(COMPACT_TOOLS)
        assert post(server.url + "/mcp?profile=huge", listing)[0] == 400
    finally:
        server.stop()
        server.api.writer.close()


def test_the_bridge_falls_back_to_compact_tools_too(db):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    bridge = cli.Bridge("http://127.0.0.1:{}/mcp?profile=compact".format(port),
                        db_path=db, profile="compact")
    try:
        tools = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert "query_sql" not in [t["name"] for t in tools["result"]["tools"]]
    finally:
        bridge.close()
