"""
Tests for the HTTP heart rate source. These start a real server on
127.0.0.1 with port 0 (the OS picks a free port) and make real requests to
it, because the thing worth testing is the wire contract a watch will
actually hit -- routing, parsing, auth, status codes -- not a mock of it.

No hardware, no watch, no fixed port, so they run anywhere including CI.
"""

import json
import queue
import time
import urllib.error
import urllib.request

import pytest

import http_source
from http_source import HTTPHRSource, _coerce_hr, _coerce_rr


def _drain(q: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def _samples(q: queue.Queue) -> list:
    return [m for m in _drain(q) if m["type"] == "sample"]


def _call(url, data=None, headers=None, timeout=5):
    """Returns (status_code, parsed_json_body) for 2xx and error responses
    alike -- the error bodies are part of what's being tested.
    """
    body = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(
        url, data=body, headers=headers or {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture
def source():
    """A started source on a free loopback port, stopped after the test."""
    out = queue.Queue()
    src = HTTPHRSource(out, host="127.0.0.1", port=0, sample_timeout=30)
    src.start()
    try:
        yield src, out
    finally:
        src.stop()


# -- payload parsing ---------------------------------------------------

def test_coerce_hr_accepts_ints_floats_and_strings():
    assert _coerce_hr(142) == 142
    assert _coerce_hr("142") == 142
    assert _coerce_hr(141.6) == 142  # a watch sending a smoothed average


@pytest.mark.parametrize("bad", [None, "", "abc", 0, 301, -5])
def test_coerce_hr_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        _coerce_hr(bad)


def test_coerce_rr_accepts_lists_and_comma_separated_strings():
    assert _coerce_rr([812.5, 800]) == [812.5, 800.0]
    assert _coerce_rr("812.5,800") == [812.5, 800.0]
    assert _coerce_rr("") == []
    assert _coerce_rr(None) == []
    # A trailing comma shouldn't reject an otherwise good reading.
    assert _coerce_rr("812.5,") == [812.5]


def test_coerce_rr_rejects_non_numeric_entries():
    with pytest.raises(ValueError):
        _coerce_rr("812.5,banana")


# -- the wire contract -------------------------------------------------

def test_post_json_becomes_a_sample(source):
    src, out = source
    _drain(out)  # discard the "searching" status from start()

    status, body = _call(src.url, {"hr": 142, "rr": [812.5], "device": "Forerunner 965"})

    assert (status, body) == (200, {"ok": True})
    samples = _samples(out)
    assert len(samples) == 1
    assert samples[0]["hr"] == 142
    assert samples[0]["rr_intervals_ms"] == [812.5]


def test_get_with_query_string_becomes_a_sample(source):
    """The GET form exists because it's the simplest thing to emit from a
    Connect IQ app -- no body, no content type to get wrong.
    """
    src, out = source
    _drain(out)

    status, _ = _call(f"{src.url}?hr=88&rr=680.0,690.0&device=Fenix")

    assert status == 200
    samples = _samples(out)
    assert samples[0]["hr"] == 88
    assert samples[0]["rr_intervals_ms"] == [680.0, 690.0]


def test_first_reading_reports_the_device_as_connected(source):
    src, out = source
    _drain(out)

    _call(src.url, {"hr": 100, "device": "Forerunner 965"})

    statuses = [m for m in _drain(out) if m["type"] == "status"]
    assert [s["status"] for s in statuses] == ["connected"]
    assert statuses[0]["device_name"] == "Forerunner 965"
    assert statuses[0]["device_address"] == "127.0.0.1"


def test_connected_is_reported_once_not_per_reading(source):
    src, out = source
    _drain(out)

    for hr in (100, 101, 102):
        _call(src.url, {"hr": hr, "device": "Forerunner 965"})

    messages = _drain(out)
    assert len([m for m in messages if m["type"] == "status"]) == 1
    assert len([m for m in messages if m["type"] == "sample"]) == 3


def test_a_bad_reading_is_rejected_and_reported(source):
    src, out = source
    _drain(out)

    status, body = _call(src.url, {"hr": 900})

    assert status == 400
    assert body["ok"] is False
    messages = _drain(out)
    assert not [m for m in messages if m["type"] == "sample"]
    # Reported to the UI too: a watch sending garbage should look different
    # from a watch sending nothing.
    assert [m for m in messages if m["type"] == "error"]


def test_unknown_paths_404(source):
    src, _ = source
    base = src.url.rsplit("/", 1)[0]
    status, _ = _call(f"{base}/nope")
    assert status == 404


def test_health_endpoint_answers_without_a_token(source):
    """The watch (and a browser, when someone's debugging a firewall) needs
    a way to check reachability that doesn't fake a heart rate to do it.
    """
    src, out = source
    base = src.url.rsplit("/", 1)[0]

    status, body = _call(f"{base}/health")

    assert status == 200
    assert body["ok"] is True
    assert not _samples(out)


def test_url_points_at_the_bound_port(source):
    src, _ = source
    assert src.url == f"http://127.0.0.1:{src.port}/hr"
    assert src.port != 0  # resolved from the OS's choice


# -- auth ---------------------------------------------------------------

@pytest.fixture
def token_source():
    out = queue.Queue()
    src = HTTPHRSource(out, host="127.0.0.1", port=0, token="s3cret", sample_timeout=30)
    src.start()
    try:
        yield src, out
    finally:
        src.stop()


def test_token_is_required_when_configured(token_source):
    src, out = token_source
    _drain(out)

    status, _ = _call(src.url, {"hr": 120})

    assert status == 401
    assert not _samples(out)


def test_token_accepted_in_a_header_or_the_query_string(token_source):
    src, out = token_source
    _drain(out)

    header_status, _ = _call(src.url, {"hr": 120}, headers={"X-Ticker-Token": "s3cret"})
    query_status, _ = _call(f"{src.url}?token=s3cret&hr=121")

    assert (header_status, query_status) == (200, 200)
    assert [s["hr"] for s in _samples(out)] == [120, 121]


def test_a_wrong_token_is_rejected(token_source):
    src, out = token_source
    _drain(out)

    status, _ = _call(f"{src.url}?token=wrong&hr=120")

    assert status == 401
    assert not _samples(out)


# -- lifecycle ----------------------------------------------------------

def test_start_announces_where_to_point_the_watch():
    out = queue.Queue()
    src = HTTPHRSource(out, host="127.0.0.1", port=0)
    src.start()
    try:
        status = _drain(out)[0]
        assert status["status"] == "searching"
        assert src.url in status["message"]
    finally:
        src.stop()


def test_stop_without_start_is_a_no_op():
    HTTPHRSource(queue.Queue(), host="127.0.0.1", port=0).stop()  # must not raise


def test_stop_is_idempotent_and_releases_the_port():
    out = queue.Queue()
    src = HTTPHRSource(out, host="127.0.0.1", port=0)
    src.start()
    port = src.port
    src.stop()
    src.stop()  # must not raise

    # The port is genuinely free again: binding it is how another run of the
    # app (or a restart after a crash) has to be able to come back up.
    again = HTTPHRSource(queue.Queue(), host="127.0.0.1", port=port)
    again.start()
    try:
        assert again.port == port
    finally:
        again.stop()


def test_a_bind_failure_is_reported_not_raised():
    """An unusable host/port shouldn't take the app down -- the window stays
    up and the status line says why nothing is arriving.
    """
    out = queue.Queue()
    # TEST-NET-1: guaranteed not to be an address this machine holds.
    src = HTTPHRSource(out, host="192.0.2.1", port=8787)
    src.start()
    try:
        errors = [m for m in _drain(out) if m["type"] == "error"]
        assert errors
        assert "192.0.2.1" in errors[0]["message"]
    finally:
        src.stop()


def test_a_reserved_windows_port_explains_itself(monkeypatch):
    """WinError 10013 on a port Windows has reserved for Hyper-V/WSL reads
    like a firewall problem and isn't one -- the message has to say so,
    because "permission denied" sends people down the wrong path for hours.
    """
    def refuse(*args, **kwargs):
        error = OSError("An attempt was made to access a socket in a way "
                        "forbidden by its access permissions")
        error.winerror = 10013
        raise error

    monkeypatch.setattr(http_source, "_BoundedServer", refuse)

    out = queue.Queue()
    HTTPHRSource(out, host="127.0.0.1", port=8787).start()

    message = [m for m in _drain(out) if m["type"] == "error"][0]["message"]
    assert "8787" in message
    assert "excludedportrange" in message
    assert "HRM_HTTP_PORT" in message


def test_silence_flips_back_to_reconnecting():
    """There's no disconnect event over HTTP: a watch that goes out of range
    or has its app closed just stops sending, so a gap is the only signal.
    """
    out = queue.Queue()
    src = HTTPHRSource(out, host="127.0.0.1", port=0, sample_timeout=0.2)
    src.start()
    try:
        _call(src.url, {"hr": 130, "device": "Forerunner 965"})

        deadline = time.monotonic() + 5
        statuses = []
        while time.monotonic() < deadline:
            statuses += [m["status"] for m in _drain(out) if m["type"] == "status"]
            if "reconnecting" in statuses:
                break
            time.sleep(0.05)

        assert "reconnecting" in statuses, f"never went stale (saw {statuses})"
    finally:
        src.stop()


def test_readings_after_a_gap_reconnect():
    gap = 0.2
    out = queue.Queue()
    src = HTTPHRSource(out, host="127.0.0.1", port=0, sample_timeout=gap)
    src.start()
    try:
        _call(src.url, {"hr": 130, "device": "Forerunner 965"})
        time.sleep(gap * 4)
        _drain(out)

        _call(src.url, {"hr": 131, "device": "Forerunner 965"})

        statuses = [m["status"] for m in _drain(out) if m["type"] == "status"]
        assert "connected" in statuses
    finally:
        src.stop()


def test_a_rejected_post_still_gets_its_status_not_a_dropped_connection(
        token_source):
    """Answering a POST without reading its body closes the socket while the
    client is still writing, and the client sees a connection reset instead
    of the status. A watch with a bad token has to be told it is
    unauthorised, not left guessing at a network error.
    """
    src, out = token_source
    _drain(out)

    # A body large enough that it cannot all sit in the socket buffer, which
    # is what makes the race reliable rather than occasional.
    padding = {"hr": 120, "note": "x" * 200_000}
    status, body = _call(src.url, padding)

    assert status == 401
    assert body["ok"] is False
    assert not _samples(out)


def test_a_post_to_an_unknown_path_also_answers_cleanly(source):
    src, out = source
    status, body = _call(f"{src.url.rsplit('/', 1)[0]}/nope", {"hr": 1, "pad": "y" * 100_000})
    assert status == 404
    assert body["ok"] is False
