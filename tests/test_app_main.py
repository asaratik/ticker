"""
Tests for the `ticker` command: dispatch, the MCP bridge, status, import.

The bridge is the part with real behaviour of its own -- forward to the
running app, fall back to reading the database when none answers, and
never make an agent wait on a dead port twice in a row -- so it is tested
against a real server on port 0 and a port nothing listens on.
"""

import json
import socket

import pytest

from ticker.api import server as apiserver
from ticker.api.routes import Api
from ticker.app import main as cli
from ticker.db import queries, store
from ticker.mcp.server import build
from ticker.model import iso_utc, now_utc

INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18"}}
NOTIFY = {"jsonrpc": "2.0", "method": "notifications/initialized"}
TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "cli.sqlite3"
    conn = store.connect(path)
    source = store.ensure_source(conn, "pull", "oura", "Ring")
    conn.execute(
        "INSERT INTO observations (source_id, metric_id, ts, value, external_id, "
        "ingested_at) VALUES (?,?,?,?,?,?)",
        (source, queries.metric_id(conn, "heart_rate_bpm"), iso_utc(now_utc()),
         58.0, "", iso_utc(now_utc())))
    conn.close()
    return path


@pytest.fixture
def running(db):
    started = []

    def start(token=None):
        mcp, readonly = build(db)
        server = apiserver.ApiServer(Api(apiserver.thread_local_reader(db), None),
                                     host="127.0.0.1", port=0, token=token, mcp=mcp)
        server.start()
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


def dead_url():
    """A loopback port with nothing listening on it."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return "http://127.0.0.1:{}/mcp".format(port)


# -- the bridge ------------------------------------------------------------------

def test_the_bridge_forwards_to_the_running_app(running):
    server = running()
    bridge = cli.Bridge(server.url + "/mcp")
    assert bridge.handle(INITIALIZE)["result"]["serverInfo"]["name"] == "ticker"
    assert bridge.handle(NOTIFY) is None
    assert bridge._local is None                  # never needed the database


def test_with_nothing_running_the_bridge_answers_from_the_database(db):
    now = {"t": 100.0}
    bridge = cli.Bridge(dead_url(), db_path=db, clock=lambda: now["t"])
    try:
        tools = bridge.handle(TOOLS)["result"]["tools"]
        assert "get_overview" in [t["name"] for t in tools]
        # And doesn't knock on the dead port again for a while.
        assert bridge._down_until == 100.0 + cli.BRIDGE_RETRY_SEC
    finally:
        bridge.close()


def test_a_named_server_that_is_down_is_an_error_not_a_fallback(db):
    bridge = cli.Bridge(dead_url(), required=True, db_path=db)
    reply = bridge.handle(TOOLS)
    assert "no Ticker answering" in reply["error"]["message"]
    assert bridge.handle(NOTIFY) is None
    assert bridge._local is None


def test_a_refusal_from_the_app_reaches_the_agent(running):
    server = running(token="secret")
    reply = cli.Bridge(server.url + "/mcp").handle(TOOLS)
    assert "401" in reply["error"]["message"]
    reply = cli.Bridge(server.url + "/mcp", token="secret").handle(TOOLS)
    assert "tools" in reply["result"]


def test_errors_for_a_batch_answer_only_its_requests():
    replies = cli._errors_for([TOOLS, NOTIFY], "down")
    assert [r["id"] for r in replies] == [2]
    assert cli._errors_for(NOTIFY, "down") is None


# -- status ------------------------------------------------------------------------

def test_status_says_what_is_connected(db, capsys, monkeypatch):
    monkeypatch.setattr(cli, "already_running", lambda url, timeout=1.5: False)
    assert cli.status_main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "not running" in out
    assert "Ring (oura, cloud sync)" in out
    assert "heart_rate_bpm" in out


def test_status_as_json(db, capsys, monkeypatch):
    monkeypatch.setattr(cli, "already_running", lambda url, timeout=1.5: True)
    assert cli.status_main(["--db", str(db), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["running"] is True
    assert payload["metrics"][0]["metric"] == "heart_rate_bpm"


def test_status_without_a_database_says_so(tmp_path, capsys):
    assert cli.status_main(["--db", str(tmp_path / "none.sqlite3")]) == 1
    assert "No Ticker database" in capsys.readouterr().err


# -- dispatch ----------------------------------------------------------------------

@pytest.fixture
def importer_calls(monkeypatch):
    from ticker.ingest import importer
    calls = []
    monkeypatch.setattr(importer, "main", lambda argv: calls.append(argv) or 0)
    return calls


def test_import_leaves_the_format_to_the_importer(importer_calls):
    assert cli.main(["import", "export.zip"]) == 0
    assert importer_calls == [["export.zip"]]


def test_import_takes_the_format_first_too(importer_calls):
    cli.main(["import", "apple_health", "export.zip", "--name", "Phone"])
    assert importer_calls == [["apple_health", "export.zip", "--name", "Phone"]]


def test_maintenance_commands_reach_their_modules(monkeypatch):
    from ticker.db import rollup
    calls = []
    monkeypatch.setattr(rollup, "main", lambda argv: calls.append(argv) or 0)
    assert cli.main(["rollup", "--all"]) == 0
    assert calls == [["--all"]]


def test_starting_twice_opens_the_running_one(monkeypatch):
    opened = []
    monkeypatch.setattr(cli, "already_running", lambda url, timeout=1.5: True)
    monkeypatch.setattr(cli, "open_page", lambda url, token=None: opened.append(url))
    assert cli.main(["--port", "8123"]) == 0
    assert opened == ["http://127.0.0.1:8123"]
    assert cli.main(["--port", "8123", "--headless"]) == 0
    assert len(opened) == 1


def test_serving_beyond_this_machine_needs_a_token(monkeypatch, capsys):
    monkeypatch.setattr(cli, "already_running", lambda url, timeout=1.5: False)
    assert cli.main(["--host", "0.0.0.0", "--allow-remote", "--token", ""]) == 2
    assert "token" in capsys.readouterr().err


def test_a_wildcard_bind_is_opened_on_loopback():
    assert cli.page_url("0.0.0.0", 8477) == "http://127.0.0.1:8477"
    assert cli.page_url("192.168.1.5", 8477) == "http://192.168.1.5:8477"
