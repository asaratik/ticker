"""
End-to-end tests for the runtime: one process, a real socket, the real page.

A Runtime is started on port 0 against a temporary database, with fake cloud
connectors, a fake live source and an in-memory keyring, and then driven the
way the page drives it -- over HTTP, with JSON bodies. This is the test that
says the pieces are actually linked: an account connected through /ui gets
synced, "Sync now" on the read API reaches the scheduler, a session started
on the page lands in the database, and /mcp answers from the same file.
"""

import json
import time
import urllib.error
import urllib.request
from datetime import timedelta
from types import SimpleNamespace

import pytest

from ticker import config as tconfig
from ticker.app.runtime import Runtime
from ticker.auth import secrets, setup
from ticker.db import store
from ticker.model import iso_utc, now_utc


class FakePull:
    vendor = "oura"

    def __init__(self, **wiring):
        self.fetches = []

    def capabilities(self):
        return frozenset({"heart_rate_bpm"})

    def fetch(self, metric, since, until):
        self.fetches.append((metric, since, until))
        return []


class FakeSource:
    def __init__(self, out_queue):
        self.out = out_queue
        self.clock = now_utc()

    def start(self):
        pass

    def stop(self):
        pass

    def connect(self):
        self.out.put({"type": "status", "status": "connected",
                      "device_name": "Fake strap", "device_address": "AA:BB",
                      "message": None})

    def sample(self, hr):
        self.clock += timedelta(seconds=1)
        self.out.put({"type": "sample", "timestamp": iso_utc(self.clock),
                      "hr": hr, "rr_intervals_ms": []})


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def call(url, method="GET", body=None, headers=None, content_type="application/json"):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    if method == "POST":
        request.add_header("Content-Type", content_type)
        request.data = data if data is not None else b"{}"
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


@pytest.fixture
def keyring(monkeypatch):
    kept = {}
    monkeypatch.setattr(secrets, "available", lambda: True)
    monkeypatch.setattr(secrets, "get_secret",
                        lambda ref, service=secrets.SERVICE: kept.get(ref))
    monkeypatch.setattr(secrets, "set_secret",
                        lambda ref, value, service=secrets.SERVICE:
                        kept.__setitem__(ref, value))
    monkeypatch.setattr(secrets, "delete_secret",
                        lambda ref, service=secrets.SERVICE:
                        kept.pop(ref, None) is not None)
    return kept


@pytest.fixture
def start(tmp_path, monkeypatch, keyring):
    monkeypatch.delenv("HRM_SOURCE", raising=False)
    running = []

    def begin(token=None):
        made, sources = [], []

        def build(**wiring):
            connector = FakePull(**wiring)
            made.append(connector)
            return connector

        def make_source(kind, out_queue, address):
            source = FakeSource(out_queue)
            sources.append(source)
            return source

        path = tmp_path / "app{}.sqlite3".format(len(running))
        runtime = Runtime(path, host="127.0.0.1", port=0, token=token,
                          allow_remote=False, builders={"oura": build},
                          pull_interval=3600, make_source=make_source)
        runtime.start()
        running.append(runtime)
        return SimpleNamespace(runtime=runtime, url=runtime.url, made=made,
                               sources=sources, kept=keyring, path=path)

    yield begin
    for runtime in running:
        runtime.stop()


@pytest.fixture
def app(start, monkeypatch):
    def fake_add_oauth(conn, vendor, name, open_browser=None,
                       client_id=None, client_secret=None):
        assert vendor == "oura"
        open_browser("https://cloud.ouraring.com/oauth/authorize?state=x")
        ref = secrets.auth_ref(vendor, name)
        secrets.set_secret(ref, json.dumps({"access_token": "access",
                                            "refresh_token": "refresh",
                                            "oauth_client_id": client_id,
                                            "oauth_client_secret": client_secret}))
        return store.ensure_source(conn, "pull", vendor, name, auth_ref=ref)
    monkeypatch.setattr(setup, "add_oauth", fake_add_oauth)
    return start()


def query(path, sql):
    conn = store.connect(path, migrate_first=False)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# -- the page ------------------------------------------------------------------

def test_the_page_and_its_state_are_served(app):
    status, page = call(app.url + "/")
    assert status == 200 and b"Ticker" in page
    status, state = call(app.url + "/ui/state")
    assert status == 200
    assert state["app"]["url"] == app.url
    assert "ticker mcp" in state["agents"]["claude_code"]
    assert state["agents"]["http"].endswith(app.url + "/mcp")


# -- cloud accounts ------------------------------------------------------------

def connect_oura(app):
    status, reply = call(app.url + "/ui/connect/oura", "POST", {
        "client_id": "client", "client_secret": "s3cret"})
    assert status == 200, reply
    wait_for(lambda: query(app.path, "SELECT id FROM sources WHERE vendor='oura'"))
    return query(app.path, "SELECT id FROM sources WHERE vendor='oura'")[0][0]


def test_connecting_oura_on_the_page_stores_the_token_and_syncs(app):
    source_id = connect_oura(app)
    assert set(app.kept) == {"oura:Ring"}
    assert "s3cret" in app.kept["oura:Ring"]
    wait_for(lambda: app.made and app.made[0].fetches)
    _, state = call(app.url + "/ui/state")
    ring = next(s for s in state["sources"] if s["id"] == source_id)
    assert ring["vendor"] == "oura" and "syncing" in ring


def test_sync_now_on_the_read_api_reaches_the_scheduler(app):
    # Before the runtime, this answered 503: nothing in the server process
    # could make a sync happen.
    source_id = connect_oura(app)
    wait_for(lambda: not app.runtime.pulls.status()[source_id]["syncing"])
    before = len(app.made[0].fetches)
    status, _ = call(app.url + "/api/sync/{}".format(source_id), "POST", {})
    assert status == 202
    wait_for(lambda: len(app.made[0].fetches) > before)


def test_disconnecting_forgets_the_token_and_stops_syncing(app):
    source_id = connect_oura(app)
    wait_for(lambda: source_id in app.runtime.pulls.running())
    status, _ = call(app.url + "/ui/disconnect/{}".format(source_id), "POST", {})
    assert status == 200
    assert app.kept == {}
    wait_for(lambda: source_id not in app.runtime.pulls.running())


def test_fitbit_without_a_client_id_says_what_to_do(app, monkeypatch):
    monkeypatch.setattr(tconfig, "FITBIT_CLIENT_ID", "")
    status, reply = call(app.url + "/ui/connect/fitbit", "POST", {})
    assert status == 400 and "TICKER_FITBIT_CLIENT_ID" in reply["error"]


def test_fitbit_sign_in_hands_the_page_a_url_and_finishes_in_the_background(
        app, monkeypatch):
    monkeypatch.setattr(tconfig, "FITBIT_CLIENT_ID", "client-id")

    def fake_add_oauth(conn, vendor, name, open_browser=None):
        open_browser("https://www.fitbit.com/oauth2/authorize?state=x")
        return store.ensure_source(conn, "pull", vendor, name)

    monkeypatch.setattr(setup, "add_oauth", fake_add_oauth)
    status, reply = call(app.url + "/ui/connect/fitbit", "POST", {})
    assert status == 200
    assert reply["result"]["url"].startswith("https://www.fitbit.com/")

    def job_done():
        _, state = call(app.url + "/ui/state")
        return any(j["kind"] == "fitbit" and j["status"] == "done"
                   for j in state["jobs"])
    wait_for(job_done)


# -- the live source -------------------------------------------------------------

def test_a_session_started_on_the_page_is_saved_and_agents_see_it(app):
    call(app.url + "/ui/live/source", "POST", {"source": "ble"})
    strap = app.sources[-1]
    strap.connect()
    wait_for(lambda: call(app.url + "/ui/live")[1]["status"] == "connected")

    status, _ = call(app.url + "/ui/session/start", "POST", {"label": "e2e ride"})
    assert status == 200
    strap.sample(120)
    strap.sample(140)
    wait_for(lambda: call(app.url + "/ui/live")[1]["session"]["n"] == 2)
    assert call(app.url + "/ui/session/stop", "POST", {})[0] == 200
    assert app.runtime.writer.flush()

    rows = query(app.path, "SELECT s.label, COUNT(o.id) FROM sessions s "
                           "JOIN observations o ON o.session_id = s.id "
                           "GROUP BY s.id")
    assert rows == [("e2e ride", 2)]

    status, reply = call(app.url + "/mcp", "POST", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_sessions", "arguments": {}}})
    assert status == 200
    assert "e2e ride" in reply["result"]["content"][0]["text"]


# -- imports ---------------------------------------------------------------------

EXPORT = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Watch" unit="count/min"
  value="61" startDate="2026-05-01 07:00:00 +0000" endDate="2026-05-01 07:00:00 +0000"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Watch" unit="count/min"
  value="64" startDate="2026-05-01 07:05:00 +0000" endDate="2026-05-01 07:05:00 +0000"/>
</HealthData>
"""


def test_an_apple_health_export_imports_in_the_background(app, tmp_path):
    export = tmp_path / "export.xml"
    export.write_text(EXPORT, encoding="utf-8")
    status, reply = call(app.url + "/ui/import", "POST", {"path": str(export)})
    assert status == 200

    def finished():
        _, state = call(app.url + "/ui/state")
        return [j for j in state["jobs"] if j["kind"] == "import"
                and j["status"] != "running"]
    job, = wait_for(finished) and finished()
    assert job["status"] == "done", job
    assert app.runtime.writer.flush()
    assert query(app.path, "SELECT COUNT(*) FROM observations o JOIN sources s "
                           "ON s.id = o.source_id WHERE s.vendor = 'apple_health'"
                 ) == [(2,)]


def test_importing_a_file_that_is_not_there_is_refused(app, tmp_path):
    status, reply = call(app.url + "/ui/import", "POST",
                         {"path": str(tmp_path / "nope.zip")})
    assert status == 400 and "no file" in reply["error"]


# -- stopping, and who may ask ---------------------------------------------------

def test_quit_on_the_page_asks_the_runtime_to_stop(app):
    assert call(app.url + "/ui/quit", "POST", {})[0] == 200
    assert app.runtime.stop_requested


def test_another_site_cannot_stop_the_app(app):
    status, _ = call(app.url + "/ui/quit", "POST", {},
                     headers={"Origin": "http://evil.example"})
    assert status == 403
    assert not app.runtime.stop_requested


def test_a_form_post_cannot_stop_the_app(app):
    # text/plain is what a cross-site form can send without a preflight.
    status, _ = call(app.url + "/ui/quit", "POST", {}, content_type="text/plain")
    assert status == 415
    assert not app.runtime.stop_requested


def test_a_rebound_hostname_gets_nothing(app):
    port = app.url.rsplit(":", 1)[1]
    status, _ = call(app.url + "/ui/state",
                     headers={"Host": "evil.example:" + port})
    assert status == 403


def test_with_a_token_the_page_loads_but_its_data_needs_the_token(start):
    app = start(token="tok")
    assert call(app.url + "/")[0] == 200
    assert call(app.url + "/ui/state")[0] == 401
    assert call(app.url + "/ui/state", headers={"X-Ticker-Token": "tok"})[0] == 200


def test_stopping_twice_is_harmless(app):
    app.runtime.stop()
    app.runtime.stop()


# -- the Ask box ------------------------------------------------------------------

@pytest.fixture
def no_pinned_model(monkeypatch):
    monkeypatch.delenv("TICKER_LLM_URL", raising=False)
    monkeypatch.delenv("TICKER_LLM_MODEL", raising=False)


def test_a_question_is_answered_by_a_local_model(app, no_pinned_model):
    from llmstub import StubModelServer, ollama
    stub = StubModelServer([ollama("", [("get_overview", {})]),
                            ollama("Nothing recorded yet.")]).start()
    try:
        status, reply = call(app.url + "/ui/ask/config", "POST",
                             {"url": stub.url, "model": "tiny"})
        assert status == 200
        assert reply["result"]["reachable"] and reply["result"]["models"] == ["tiny"]
        assert reply["result"]["local"] is True

        status, reply = call(app.url + "/ui/ask", "POST", {"question": "How am I?"})
        assert status == 200
        ask = "/ui/ask/" + reply["result"]["id"]
        wait_for(lambda: call(app.url + ask)[1]["status"] != "running")
        job = call(app.url + ask)[1]
        assert (job["status"], job["answer"]) == ("done", "Nothing recorded yet.")
        assert [step["tool"] for step in job["steps"]] == ["get_overview"]

        path, body = stub.requests[0]
        assert path == "/api/chat"
        assert body["options"]["num_ctx"] == tconfig.LLM_CONTEXT
        assert "query_sql" not in [t["function"]["name"] for t in body["tools"]]
    finally:
        stub.stop()


def test_a_question_waits_for_a_model_to_be_chosen(app, no_pinned_model):
    status, reply = call(app.url + "/ui/ask", "POST", {"question": "hi"})
    assert status == 400 and "pick a model" in reply["error"]


def test_a_model_server_that_is_down_is_reported_not_raised(app, no_pinned_model):
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    status, reply = call(app.url + "/ui/ask/config", "POST",
                         {"url": "http://127.0.0.1:{}".format(port)})
    assert status == 200
    assert reply["result"]["reachable"] is False
    assert "can't reach" in reply["result"]["error"]


def test_the_model_server_must_be_a_url(app, no_pinned_model):
    status, _ = call(app.url + "/ui/ask/config", "POST", {"url": "ollama please"})
    assert status == 400


# -- Garmin sign-in ------------------------------------------------------------------

def garmin_library():
    import types
    from pathlib import Path

    class Garmin:
        def __init__(self, email=None, password=None, prompt_mfa=None):
            self.email, self.prompt_mfa = email, prompt_mfa

        def login(self, tokenstore):
            if self.email and self.prompt_mfa() != "123456":
                raise RuntimeError("wrong code")
            (Path(tokenstore) / "garmin_tokens.json").write_text('{"oauth2": "t"}')

    return types.SimpleNamespace(Garmin=Garmin)


def test_garmin_sign_in_waits_for_the_code_then_connects(app):
    app.runtime._garmin_library = garmin_library()
    status, reply = call(app.url + "/ui/connect/garmin", "POST",
                         {"email": "me@example.com", "password": "hunter2"})
    assert status == 200 and reply["result"]["status"] == "needs_code"

    status, _ = call(app.url + "/ui/connect/garmin/code", "POST",
                     {"job_id": reply["result"]["job_id"], "code": "123456"})
    assert status == 200

    def connected():
        _, state = call(app.url + "/ui/state")
        return any(j["kind"] == "garmin" and j["status"] == "done" for j in state["jobs"])
    wait_for(connected)
    assert "garmin:Garmin" in app.kept
    assert not any("hunter2" in value for value in app.kept.values())


def test_garmin_sign_in_explains_itself_where_it_cannot_run(app):
    import sys
    if sys.version_info >= (3, 12):
        pytest.skip("this interpreter could run the real library")
    status, reply = call(app.url + "/ui/connect/garmin", "POST",
                         {"email": "me@example.com", "password": "pw"})
    assert status == 501 and "3.12" in reply["error"]
