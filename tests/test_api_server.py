"""
Tests for the read API's HTTP transport.

These do bind a real socket, on port 0 so nothing collides in CI. What is
tested here is only what the transport owns -- binding rules, the token,
body handling, status codes on the wire -- because the endpoints themselves
are covered without a socket in test_api_routes.py.
"""

import json
import urllib.error
import urllib.request

import pytest

from ticker.api import server as apiserver
from ticker.api.routes import Api
from ticker.db import store
from ticker.model import iso_utc, now_utc


class FakeWriter:
    def __init__(self):
        self.batches = []

    def insert_observations(self, source_id, batch):
        self.batches.append((source_id, list(batch)))

    def begin_session(self, *args, **kwargs):
        pass

    def end_session(self, *args, **kwargs):
        pass


def get(url, token=None, method="GET", data=None):
    """Request a URL, returning (status, parsed body) instead of raising."""
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header(apiserver.TOKEN_HEADER, token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "srv.sqlite3"
    store.connect(path).close()               # migrate once, before serving
    return path


@pytest.fixture
def writer():
    return FakeWriter()


@pytest.fixture
def serving(db, writer):
    """A server on an ephemeral port, stopped however the test ends.

    Readers come from thread_local_reader, not one shared connection: a
    threaded server answers on several threads and sqlite3 refuses to be
    used across them -- which is what the real wiring does, so the fixture
    should not quietly differ from it.
    """
    started = []

    def start(token=None, **kwargs):
        api = Api(apiserver.thread_local_reader(db), writer)
        server = apiserver.ApiServer(api, host="127.0.0.1", port=0,
                                     token=token, **kwargs)
        server.start()
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


# -- binding rules -------------------------------------------------------

def test_loopback_needs_no_token():
    apiserver.check_bind("127.0.0.1", None, allow_remote=False)


def test_binding_everywhere_needs_the_explicit_flag():
    # /api/ingest writes to the database, so getting this wrong is not a
    # leak but an open ingest port. It refuses rather than warning.
    with pytest.raises(apiserver.RemoteBindRefused) as raised:
        apiserver.check_bind("0.0.0.0", "secret", allow_remote=False)
    assert "allow-remote" in str(raised.value)


def test_binding_everywhere_also_needs_a_token():
    with pytest.raises(apiserver.RemoteBindRefused) as raised:
        apiserver.check_bind("0.0.0.0", None, allow_remote=True)
    assert "token" in str(raised.value)


def test_the_flag_and_a_token_together_are_enough():
    apiserver.check_bind("0.0.0.0", "secret", allow_remote=True)


def test_starting_a_refused_bind_never_reaches_the_socket(db, writer):
    api = Api(apiserver.thread_local_reader(db), writer)
    server = apiserver.ApiServer(api, host="0.0.0.0", port=0, token=None,
                                 allow_remote=True)
    with pytest.raises(apiserver.RemoteBindRefused):
        server.start()


def test_the_cli_refuses_rather_than_binding_open(capsys, tmp_path):
    code = apiserver.main(["--host", "0.0.0.0", "--allow-remote",
                           "--db", str(tmp_path / "x.sqlite3")])
    assert code == 2
    assert "token" in capsys.readouterr().err


# -- the token ------------------------------------------------------------

def test_a_request_without_a_token_is_401(serving):
    server = serving(token="secret")
    status, payload = get(server.url + "/api/metrics")
    assert status == 401
    assert "token" in payload["error"]


def test_a_wrong_token_is_401(serving):
    server = serving(token="secret")
    assert get(server.url + "/api/metrics", token="guess")[0] == 401


def test_the_right_token_gets_through(serving):
    server = serving(token="secret")
    assert get(server.url + "/api/metrics", token="secret")[0] == 200


def test_the_token_can_come_as_a_query_parameter(serving):
    # Grafana's SQLite/JSON datasources can add a query parameter far more
    # easily than a header.
    server = serving(token="secret")
    assert get(server.url + "/api/metrics?token=secret")[0] == 200


def test_health_answers_without_a_token(serving):
    # A liveness probe should not need the secret, and it reveals nothing
    # beyond the fact that the process is up.
    server = serving(token="secret")
    status, payload = get(server.url + "/health")
    assert status == 200 and payload["ok"] is True


def test_no_token_configured_means_no_check(serving):
    server = serving(token=None)
    assert get(server.url + "/api/metrics")[0] == 200


# -- requests on the wire -------------------------------------------------

def test_a_get_reaches_its_endpoint(serving):
    server = serving()
    status, payload = get(server.url + "/api/sources")
    assert status == 200 and "sources" in payload


def test_a_post_body_reaches_the_endpoint(serving, writer):
    server = serving()
    body = json.dumps({
        "source": {"vendor": "ble", "display_name": "Strap"},
        "observations": [{"metric": "heart_rate_bpm", "ts": iso_utc(now_utc()),
                          "value": 142}],
    }).encode("utf-8")
    status, payload = get(server.url + "/api/ingest", method="POST", data=body)
    assert status == 200 and payload["accepted"] == 1
    assert writer.batches[0][1][0].value == 142


def test_a_large_body_is_read_whole(serving, writer):
    # A single read() can come up short on a body split across packets; a
    # truncated one would be blamed on the client as malformed JSON.
    server = serving()
    rows = [{"metric": "heart_rate_bpm", "ts": iso_utc(now_utc()),
             "value": 60, "external_id": str(i)} for i in range(4000)]
    body = json.dumps({"source": {"vendor": "ble", "display_name": "Strap"},
                       "observations": rows}).encode("utf-8")
    assert len(body) > 64 * 1024
    status, payload = get(server.url + "/api/ingest", method="POST", data=body)
    assert status == 200 and payload["accepted"] == 4000


def test_an_unknown_path_is_404_on_the_wire(serving):
    server = serving()
    assert get(server.url + "/api/nope")[0] == 404


def test_a_bad_request_still_answers_json(serving):
    server = serving()
    status, payload = get(server.url + "/api/ingest", method="POST",
                          data=b"not json")
    assert status == 400 and "error" in payload


def test_a_rejected_post_answers_rather_than_resetting(serving):
    # Answering without draining the body closes the socket while the
    # client is still writing, which on Windows surfaces as a connection
    # error instead of the 401 we actually sent.
    server = serving(token="secret")
    body = json.dumps({"observations": [{}] * 500}).encode("utf-8")
    status, _ = get(server.url + "/api/ingest", method="POST", data=body)
    assert status == 401


def test_head_gets_a_status_not_a_501(serving):
    server = serving()
    request = urllib.request.Request(server.url + "/health", method="HEAD")
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200


# -- lifecycle ------------------------------------------------------------

def test_the_bound_port_is_reported_back(serving):
    server = serving()
    assert server.port > 0
    assert str(server.port) in server.url


def test_stopping_twice_is_harmless(serving):
    server = serving()
    server.stop()
    server.stop()


def test_a_stop_signal_sets_the_event_the_server_waits_on():
    # SIGTERM matters as much as Ctrl+C: the server's natural home is a box
    # where systemd or docker stops it that way, and Python's default is to
    # die immediately -- skipping the flush and the rollup rebuild.
    import signal
    saved = {name: signal.getsignal(getattr(signal, name))
             for name in ("SIGINT", "SIGTERM", "SIGBREAK")
             if hasattr(signal, name)}
    try:
        stopping = apiserver.install_stop_handlers()
        assert not stopping.is_set()
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
        assert stopping.is_set()
    finally:
        for name, previous in saved.items():
            signal.signal(getattr(signal, name), previous)


def test_stop_handlers_off_the_main_thread_do_not_raise():
    # signal.signal is main-thread only. Anywhere else this has to return an
    # Event nobody sets rather than take the caller down.
    import threading
    result = {}

    def run():
        try:
            result["event"] = apiserver.install_stop_handlers()
        except Exception as exc:                       # pragma: no cover
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=5)
    assert "error" not in result
    assert not result["event"].is_set()


def test_build_wires_a_database_and_its_own_writer(tmp_path):
    path = tmp_path / "built.sqlite3"
    server = apiserver.build(path, host="127.0.0.1", port=0)
    server.start()
    try:
        status, payload = get(server.url + "/api/metrics")
        assert status == 200 and payload["metrics"]
    finally:
        server.stop()
        server.api.writer.close()


def test_readers_are_per_thread(tmp_path):
    # sqlite3 connections are not safe across threads, and a threaded
    # server answers several requests at once.
    store.connect(tmp_path / "r.sqlite3").close()
    reader = apiserver.thread_local_reader(tmp_path / "r.sqlite3")
    seen = []
    import threading
    threads = [threading.Thread(target=lambda: seen.append(id(reader())))
               for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(seen)) == 3
    assert id(reader()) == id(reader())         # and reused within a thread
