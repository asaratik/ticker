"""
Tests for the MCP transports: stdio and HTTP (/mcp).

The stdio loop is tested over in-memory streams, and then once for real: a
`python -m ticker.mcp.server` subprocess on real pipes, which is exactly
how Claude Code or Codex launches it. That one is worth the second it
costs, because the thing most likely to break a stdio server is something
other than the protocol writing to stdout.

The HTTP half binds a real socket on port 0, like test_api_server.py, and
tests only what the transport owns: status codes, the token, the Origin
check, and that a notification gets a bodiless 202.
"""

import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ticker.api import server as apiserver
from ticker.api.routes import Api
from ticker.db import queries, store
from ticker.mcp import protocol
from ticker.mcp.server import build, serve
from ticker.model import iso_utc, now_utc

ROOT = Path(__file__).resolve().parents[1]

INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "pytest", "version": "0"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def call(tool, arguments=None, id=3):
    return {"jsonrpc": "2.0", "id": id, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}}}


def lines(*messages):
    return b"".join(protocol.encode(m) + b"\n" for m in messages)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "mcp.sqlite3"
    conn = store.connect(path)
    source = store.ensure_source(conn, "stream", "ble", "BLE strap")
    conn.execute(
        "INSERT INTO observations (source_id, metric_id, ts, value, "
        "external_id, ingested_at) VALUES (?,?,?,?,?,?)",
        (source, queries.metric_id(conn, "heart_rate_bpm"), iso_utc(now_utc()),
         72.0, "", iso_utc(now_utc())))
    conn.close()
    return path


# -- the stdio loop, in memory ---------------------------------------------------

def run_serve(db_path, data):
    server, readonly = build(db_path)
    out = io.BytesIO()
    try:
        serve(server, io.BytesIO(data), out)
    finally:
        readonly.close()
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_each_request_gets_exactly_one_line_back(db):
    replies = run_serve(db, lines(INITIALIZE, INITIALIZED,
                                  {"jsonrpc": "2.0", "id": 2,
                                   "method": "tools/list"}))
    assert [r["id"] for r in replies] == [1, 2]
    assert len(replies[1]["result"]["tools"]) == 7


def test_bad_json_gets_a_parse_error_and_the_stream_carries_on(db):
    replies = run_serve(db, b"{not json\n" + lines(
        {"jsonrpc": "2.0", "id": 5, "method": "ping"}))
    assert replies[0]["error"]["code"] == protocol.PARSE_ERROR
    assert replies[1] == {"jsonrpc": "2.0", "id": 5, "result": {}}


def test_blank_lines_and_crlf_are_tolerated(db):
    data = b"\r\n\n" + lines({"jsonrpc": "2.0", "id": 9,
                              "method": "ping"}).replace(b"\n", b"\r\n")
    assert run_serve(db, data) == [{"jsonrpc": "2.0", "id": 9, "result": {}}]


# -- the stdio server as a real process ----------------------------------------------

def run_process(db_path, data):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run(
        [sys.executable, "-m", "ticker.mcp.server", "--db", str(db_path)],
        input=data, capture_output=True, timeout=60, cwd=str(ROOT), env=env)


def test_ticker_mcp_over_real_stdio(db):
    done = run_process(db, lines(INITIALIZE, INITIALIZED,
                                 {"jsonrpc": "2.0", "id": 2,
                                  "method": "tools/list"},
                                 call("get_overview")))
    assert done.returncode == 0, done.stderr.decode(errors="replace")
    # Every line on stdout must be protocol; logging belongs on stderr.
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[0]["result"]["serverInfo"]["name"] == "ticker"
    overview = json.loads(replies[2]["result"]["content"][0]["text"])
    assert overview["metrics"][0]["metric"] == "heart_rate_bpm"
    assert b"read-only" in done.stderr


def test_ticker_mcp_does_not_create_a_missing_database(tmp_path):
    missing = tmp_path / "nothing-here.sqlite3"
    done = run_process(missing, lines(INITIALIZE, call("get_overview", id=2)))
    assert done.returncode == 0
    reply = json.loads(done.stdout.splitlines()[-1])
    assert reply["result"]["isError"] is True
    assert "No Ticker database" in reply["result"]["content"][0]["text"]
    assert not missing.exists()


# -- /mcp over HTTP ---------------------------------------------------------

class NoWriter:
    pass


@pytest.fixture
def serving(db):
    started = []

    def start(token=None, mcp=True):
        api = Api(apiserver.thread_local_reader(db), NoWriter())
        server = apiserver.ApiServer(api, host="127.0.0.1", port=0, token=token,
                                     mcp=build(db)[0] if mcp else None)
        server.start()
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


def post(url, message, headers=None, method="POST"):
    """(status, headers, raw body) instead of raising on 4xx."""
    data = None if message is None else (
        message if isinstance(message, bytes) else protocol.encode(message))
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json, text/event-stream")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_initialize_and_a_tool_call_over_http(serving):
    server = serving()
    status, headers, body = post(server.url + "/mcp", INITIALIZE)
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body)["result"]["protocolVersion"] == "2025-06-18"

    status, _, body = post(server.url + "/mcp", call("get_overview"),
                           headers={"MCP-Protocol-Version": "2025-06-18"})
    assert status == 200
    assert json.loads(body)["result"]["isError"] is False


def test_a_notification_is_accepted_with_no_body(serving):
    status, _, body = post(serving().url + "/mcp", INITIALIZED)
    assert status == 202 and body == b""


def test_get_is_405_because_there_is_no_event_stream(serving):
    status, headers, _ = post(serving().url + "/mcp", None, method="GET")
    assert status == 405
    assert headers["Allow"] == "POST"


def test_a_foreign_origin_is_refused(serving):
    # DNS rebinding: a page on evil.example resolving its own name to
    # 127.0.0.1 would otherwise be same-origin with this server.
    server = serving()
    status, _, _ = post(server.url + "/mcp", INITIALIZE,
                        headers={"Origin": "http://evil.example"})
    assert status == 403
    status, _, _ = post(server.url + "/mcp", INITIALIZE,
                        headers={"Origin": "http://localhost:3000"})
    assert status == 200


def test_the_token_applies_and_bearer_is_accepted(serving):
    server = serving(token="secret")
    assert post(server.url + "/mcp", INITIALIZE)[0] == 401
    assert post(server.url + "/mcp", INITIALIZE,
                headers={"Authorization": "Bearer secret"})[0] == 200
    assert post(server.url + "/mcp", INITIALIZE,
                headers={"Authorization": "Bearer guess"})[0] == 401
    # The existing header still works, on /mcp and everywhere else.
    assert post(server.url + "/mcp", INITIALIZE,
                headers={apiserver.TOKEN_HEADER: "secret"})[0] == 200


def test_bearer_works_for_the_read_api_too(serving):
    server = serving(token="secret")
    status, _, _ = post(server.url + "/api/metrics", None, method="GET",
                        headers={"Authorization": "Bearer secret"})
    assert status == 200


def test_an_unsupported_protocol_version_header_is_400(serving):
    status, _, _ = post(serving().url + "/mcp", INITIALIZE,
                        headers={"MCP-Protocol-Version": "1999-01-01"})
    assert status == 400


def test_bad_json_is_a_parse_error_on_the_wire(serving):
    status, _, body = post(serving().url + "/mcp", b"{nope")
    assert status == 400
    assert json.loads(body)["error"]["code"] == protocol.PARSE_ERROR


def test_mcp_is_not_routed_when_not_configured(serving):
    assert post(serving(mcp=False).url + "/mcp", INITIALIZE)[0] == 404


def test_build_serves_mcp_by_default(tmp_path):
    server = apiserver.build(tmp_path / "built.sqlite3", host="127.0.0.1", port=0)
    server.start()
    try:
        assert post(server.url + "/mcp", INITIALIZE)[0] == 200
    finally:
        server.stop()
        server.api.writer.close()
