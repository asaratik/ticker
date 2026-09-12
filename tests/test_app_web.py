"""
Tests for the app's page and its endpoints, with no socket.

Ui takes a parsed request and returns (status, headers, body), the same
shape of seam as the read API's routes -- so routing, argument handling
and the way failures come back are tested against a stand-in runtime here,
and the real one gets its end-to-end test in test_app_runtime.py.
"""

import json
import re
from html.parser import HTMLParser

import pytest

from ticker.app.live import LiveError
from ticker.app.web import STATIC_DIR, AppError, Ui


class FakeLive:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"source": "off", "status": "off", "bpm": None}

    def set_source(self, kind):
        self.calls.append(("source", kind))
        return {"source": kind}

    def start_session(self, label):
        if label == "busy":
            raise LiveError("a session is already running")
        self.calls.append(("start", label))
        return {"session": {"label": label}}

    def stop_session(self):
        self.calls.append(("stop",))
        return {"session": None}


class FakeRuntime:
    def __init__(self):
        self.live = FakeLive()
        self.calls = []
        self.stopped = False

    def state(self):
        return {"sources": [], "metrics": []}

    def sync_now(self, source_id):
        self.calls.append(("sync", source_id))
        return {"queued": True}

    def connect_oura(self, client_id, client_secret, name):
        if not client_id or not client_secret:
            raise AppError(400, "enter the client id and client secret")
        self.calls.append(("oura", client_id, client_secret, name))
        return {"job_id": "7", "url": "https://cloud.ouraring.com/oauth/authorize"}

    def connect_fitbit(self, name):
        raise RuntimeError("database is on fire")

    def start_import(self, path, name):
        self.calls.append(("import", path, name))
        return {"job_id": "1"}

    def pick_import(self):
        return {"path": "C:/export.zip"}

    def create_backup(self):
        return {"file": "C:/ticker-backup.sqlite3"}

    def diagnostics(self):
        return {"ticker_version": "test", "integrity": "ok"}

    def check_update(self):
        return {"current": "1.0.0", "latest": "1.0.1", "available": True}

    def disconnect(self, source_id):
        self.calls.append(("disconnect", source_id))
        return {"source_id": source_id}

    def request_stop(self):
        self.stopped = True

    def ask_config(self):
        return {"url": "http://127.0.0.1:11434", "model": "tiny",
                "models": ["tiny"], "reachable": True}

    def set_ask_config(self, url, model):
        self.calls.append(("ask_config", url, model))
        return self.ask_config()

    def ask(self, question, history):
        self.calls.append(("ask", question, history))
        return {"id": "1"}

    def ask_status(self, ask_id):
        if ask_id != "1":
            raise AppError(404, "no question {}".format(ask_id))
        return {"id": "1", "status": "done", "answer": "fine"}

    def connect_garmin(self, email, password, name):
        self.calls.append(("garmin", email, name))
        return {"job_id": "3", "status": "needs_code"}

    def garmin_code(self, job_id, code):
        self.calls.append(("code", job_id, code))
        return {"job_id": job_id}


@pytest.fixture
def runtime():
    return FakeRuntime()


@pytest.fixture
def ui(runtime):
    return Ui(runtime)


def post(ui, route, payload=None, content_type="application/json"):
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    status, headers, raw = ui.handle("POST", route, body, content_type)
    return status, json.loads(raw)


def get(ui, route):
    status, headers, raw = ui.handle("GET", route)
    return status, headers, raw


# -- the page ------------------------------------------------------------------

def test_the_page_is_served_with_its_security_headers(ui):
    status, headers, body = get(ui, "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert b"<title>Ticker</title>" in body


@pytest.mark.parametrize("name, kind", [("app.js", "text/javascript"),
                                        ("app.css", "text/css")])
def test_the_pages_files_are_served(ui, name, kind):
    status, headers, _ = get(ui, "/static/" + name)
    assert status == 200 and headers["Content-Type"].startswith(kind)


@pytest.mark.parametrize("path", ["/static/../web.py", "/static/..%2fweb.py",
                                  "/static/.hidden", "/static/nope.js",
                                  "/static/sub/app.js"])
def test_nothing_outside_the_static_folder_is_served(ui, path):
    assert get(ui, path)[0] == 404


def test_the_page_only_posts_to_itself(ui):
    assert ui.handle("POST", "/", b"{}", "application/json")[0] == 405


class _PageAudit(HTMLParser):
    """What the CSP would block: inline script, handlers, style attributes."""

    def __init__(self):
        super().__init__()
        self.problems = []
        self.assets = []
        self._in_inline_script = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            if "src" not in attrs:
                self._in_inline_script = True
                self.problems.append("inline <script>")
            else:
                self.assets.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.assets.append(attrs["href"])
        for name in attrs:
            if name.startswith("on") or name == "style":
                self.problems.append("{} on <{}>".format(name, tag))


def test_the_page_needs_nothing_the_csp_forbids():
    # 'script-src self' and 'style-src self' would silently break an inline
    # handler or style -- the page would render and then not work.
    audit = _PageAudit()
    audit.feed((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
    assert audit.problems == []
    for asset in audit.assets:
        assert (STATIC_DIR / asset.rsplit("/", 1)[-1]).is_file(), asset


def test_the_script_never_writes_markup():
    # Server strings reach the DOM as text only.
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"\.innerHTML\s*=|insertAdjacentHTML|document\.write", script)


# -- reads -------------------------------------------------------------------

def test_live_and_state_are_json(ui):
    status, headers, body = get(ui, "/ui/live")
    assert status == 200 and json.loads(body)["source"] == "off"
    assert headers["Cache-Control"] == "no-store"
    assert json.loads(get(ui, "/ui/state")[2]) == {"sources": [], "metrics": []}


def test_an_unknown_read_is_404(ui):
    assert get(ui, "/ui/nope")[0] == 404


# -- actions -----------------------------------------------------------------

def test_an_action_must_be_json(ui):
    # A cross-site form can send text/plain without a preflight; it can't
    # send application/json.
    status, payload = post(ui, "/ui/quit", {}, content_type="text/plain")
    assert status == 415


def test_a_bad_body_is_400(ui):
    assert ui.handle("POST", "/ui/quit", b"{nope", "application/json")[0] == 400
    assert ui.handle("POST", "/ui/quit", b"[1]", "application/json")[0] == 400


def test_a_session_starts_with_its_label(ui, runtime):
    status, payload = post(ui, "/ui/session/start", {"label": "ride"})
    assert status == 200 and payload["result"]["session"]["label"] == "ride"
    assert runtime.live.calls == [("start", "ride")]


def test_a_live_refusal_is_409_with_the_reason(ui):
    status, payload = post(ui, "/ui/session/start", {"label": "busy"})
    assert status == 409 and "already running" in payload["error"]


def test_the_live_source_can_be_switched(ui, runtime):
    assert post(ui, "/ui/live/source", {"source": "ble"})[0] == 200
    assert runtime.live.calls == [("source", "ble")]


def test_connecting_oura_passes_oauth_credentials_and_name(ui, runtime):
    status, payload = post(ui, "/ui/connect/oura", {
        "client_id": "client", "client_secret": "secret", "name": "Mine"})
    assert status == 200 and payload["result"]["job_id"] == "7"
    assert runtime.calls == [("oura", "client", "secret", "Mine")]


def test_an_app_error_keeps_its_status_and_message(ui):
    status, payload = post(ui, "/ui/connect/oura", {})
    assert status == 400 and "client id" in payload["error"]


def test_an_unexpected_failure_is_500_not_a_crash(ui):
    status, payload = post(ui, "/ui/connect/fitbit", {})
    assert status == 500 and "on fire" in payload["error"]


def test_a_non_string_field_is_400(ui):
    assert post(ui, "/ui/import", {"path": 12})[0] == 400


@pytest.mark.parametrize("route, call", [("/ui/sync/3", ("sync", 3)),
                                         ("/ui/disconnect/4", ("disconnect", 4))])
def test_source_actions_take_the_id_from_the_path(ui, runtime, route, call):
    assert post(ui, route)[0] == 200
    assert runtime.calls == [call]


def test_a_non_numeric_source_id_is_400(ui):
    assert post(ui, "/ui/sync/oura")[0] == 400


def test_quit_asks_the_runtime_to_stop(ui, runtime):
    assert post(ui, "/ui/quit")[0] == 200
    assert runtime.stopped


def test_an_unknown_action_is_404_and_other_methods_405(ui):
    assert post(ui, "/ui/launch-rockets")[0] == 404
    assert ui.handle("PUT", "/ui/quit", b"{}", "application/json")[0] == 405


# -- the Ask box ------------------------------------------------------------------

def test_the_ask_config_is_read_and_saved(ui, runtime):
    status, headers, body = get(ui, "/ui/ask/config")
    assert status == 200 and json.loads(body)["model"] == "tiny"
    assert post(ui, "/ui/ask/config", {"url": "http://box:11434", "model": "big"})[0] == 200
    assert runtime.calls == [("ask_config", "http://box:11434", "big")]


def test_a_question_carries_the_last_three_answers(ui, runtime):
    history = [["q{}".format(i), "a{}".format(i)] for i in range(5)]
    status, payload = post(ui, "/ui/ask", {"question": "and?", "history": history})
    assert status == 200 and payload["result"] == {"id": "1"}
    assert runtime.calls == [("ask", "and?", [("q2", "a2"), ("q3", "a3"), ("q4", "a4")])]


@pytest.mark.parametrize("history", ["q and a", [["only a question"]], [[1, 2]]])
def test_a_malformed_history_is_400(ui, history):
    assert post(ui, "/ui/ask", {"question": "q", "history": history})[0] == 400


def test_an_answer_is_fetched_by_id(ui):
    status, _, body = get(ui, "/ui/ask/1")
    assert status == 200 and json.loads(body)["answer"] == "fine"
    assert get(ui, "/ui/ask/7")[0] == 404
    assert get(ui, "/ui/ask/../state")[0] == 404


# -- Garmin sign-in ----------------------------------------------------------------

def test_garmin_sign_in_and_its_code(ui, runtime):
    status, payload = post(ui, "/ui/connect/garmin",
                           {"email": "me@example.com", "password": "pw"})
    assert status == 200 and payload["result"]["status"] == "needs_code"
    assert post(ui, "/ui/connect/garmin/code", {"job_id": "3", "code": "123456"})[0] == 200
    assert runtime.calls == [("garmin", "me@example.com", None), ("code", "3", "123456")]
