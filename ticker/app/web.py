"""
The app's page, and the JSON behind it.

Like routes.py for the read API: every endpoint is a function from a parsed
request to a response, with no socket in sight -- ticker.api.server routes
/, /static/* and /ui/* here. The page is plain HTML, CSS and JavaScript
shipped as package data: no build step, no framework, nothing to install.

    GET  /                      the page
    GET  /static/<file>         its stylesheet and script
    GET  /ui/live               the live source: status, bpm, the last few
                                minutes, the running session (polled each second)
    GET  /ui/state              everything else: sources, metrics, jobs and
                                agent setup (polled every few seconds)
    POST /ui/live/source        {"source": "off" | "ble" | "http"}
    POST /ui/session/start      {"label": "..."}
    POST /ui/session/stop
    POST /ui/sync/{id}          sync a cloud account now
    POST /ui/connect/oura       {"token": "...", "name": "..."}
    POST /ui/connect/fitbit     -> {"url": ...} for the page to open
    POST /ui/connect/garmin     {"email", "password", "name"}; may answer
                                status 'needs_code' ...
    POST /ui/connect/garmin/code  {"job_id", "code"} ... which this supplies
    POST /ui/import             {"path": ".../export.zip"}, an Apple Health export
    POST /ui/disconnect/{id}    forget a cloud account's credentials; data stays
    POST /ui/quit               stop the app
    GET  /ui/ask/config         the Ask box's model server, and the models it has
    POST /ui/ask/config         {"url": "...", "model": "..."}
    POST /ui/ask                {"question": "...", "history": [[q, a], ...]}
                                -> {"id": ...}; answered in the background
    GET  /ui/ask/{id}           that question's steps so far, and its answer

Every POST must carry Content-Type: application/json. A browser won't send
that to another origin without a CORS preflight, which this server never
approves -- one of three defences, with server.py's Host and Origin checks,
against some other web page driving the app.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ticker.app.live import LiveError

log = logging.getLogger("ticker.ui")

STATIC_DIR = Path(__file__).parent / "static"

Response = Tuple[int, Dict[str, str], bytes]

_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

# One path segment of letters, digits, dots, dashes and underscores: no
# directories, so no way out of STATIC_DIR.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# The CSP allows this server's own files and nothing else -- no inline
# script, no third party -- so even a value that somehow reached the page as
# markup could not run. frame-ancestors keeps the page out of other sites'
# frames, where its buttons could be clicked for you.
PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "style-src 'self'; script-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


class AppError(Exception):
    """A request the app can't carry out: the status, and what to say."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Ui:
    """The page and its endpoints, bound to a running Runtime."""

    def __init__(self, runtime):
        self.runtime = runtime

    @staticmethod
    def serves_page(route: str) -> bool:
        """The routes that are the page itself, served without the token:
        a remote page has to load before it can be told the token, and it
        holds nothing but code."""
        return route == "/" or route.startswith("/static/")

    def handle(self, method: str, path: str, body: bytes = b"",
               content_type: str = "") -> Response:
        """Route one request. Never raises; every failure is a status."""
        route = path.rstrip("/") or "/"
        try:
            if self.serves_page(route):
                if method != "GET":
                    return _json(405, {"ok": False, "error": "method not allowed"})
                return _static("index.html" if route == "/"
                               else route[len("/static/"):])
            if method == "GET":
                return self._get(route)
            if method != "POST":
                return _json(405, {"ok": False, "error": "method not allowed"})
            if content_type.split(";")[0].strip().lower() != "application/json":
                return _json(415, {"ok": False,
                                   "error": "send JSON, with Content-Type: "
                                            "application/json"})
            return self._post(route, _body(body))
        except AppError as exc:
            return _json(exc.status, {"ok": False, "error": exc.message})
        except LiveError as exc:
            return _json(409, {"ok": False, "error": str(exc)})
        except Exception as exc:
            log.exception("unhandled error serving %s %s", method, path)
            return _json(500, {"ok": False,
                               "error": "{}: {}".format(type(exc).__name__, exc)})

    def _get(self, route: str) -> Response:
        if route == "/ui/live":
            return _json(200, self.runtime.live.snapshot())
        if route == "/ui/state":
            return _json(200, self.runtime.state())
        if route == "/ui/ask/config":
            return _json(200, self.runtime.ask_config())
        if route.startswith("/ui/ask/") and route[len("/ui/ask/"):].isdigit():
            return _json(200, self.runtime.ask_status(route[len("/ui/ask/"):]))
        return _json(404, {"ok": False, "error": "not found"})

    def _post(self, route: str, payload: Dict[str, Any]) -> Response:
        rt = self.runtime
        if route == "/ui/live/source":
            return _ok(rt.live.set_source(_text(payload, "source")))
        if route == "/ui/session/start":
            return _ok(rt.live.start_session(_text(payload, "label")))
        if route == "/ui/session/stop":
            return _ok(rt.live.stop_session())
        if route == "/ui/connect/oura":
            return _ok(rt.connect_token("oura", _text(payload, "token"),
                                        _text(payload, "name")))
        if route == "/ui/connect/fitbit":
            return _ok(rt.connect_fitbit(_text(payload, "name")))
        if route == "/ui/connect/garmin":
            return _ok(rt.connect_garmin(_text(payload, "email"),
                                         _text(payload, "password"),
                                         _text(payload, "name")))
        if route == "/ui/connect/garmin/code":
            return _ok(rt.garmin_code(_text(payload, "job_id"),
                                      _text(payload, "code")))
        if route == "/ui/import":
            return _ok(rt.start_import(_text(payload, "path"),
                                       _text(payload, "name")))
        if route == "/ui/quit":
            rt.request_stop()
            return _ok({"stopping": True})
        if route == "/ui/ask/config":
            return _ok(rt.set_ask_config(_text(payload, "url"),
                                         _text(payload, "model")))
        if route == "/ui/ask":
            return _ok(rt.ask(_text(payload, "question"),
                              _history(payload.get("history"))))
        for prefix, action in (("/ui/sync/", rt.sync_now),
                               ("/ui/disconnect/", rt.disconnect)):
            if route.startswith(prefix):
                return _ok(action(_source_id(route[len(prefix):])))
        return _json(404, {"ok": False, "error": "not found"})


def _json(status: int, payload: Any) -> Response:
    return status, {"Content-Type": "application/json",
                    "Cache-Control": "no-store"}, json.dumps(
        payload, default=str).encode("utf-8")


def _ok(result: Any) -> Response:
    return _json(200, {"ok": True, "result": result})


def _static(name: str) -> Response:
    path = STATIC_DIR / name
    if not _SAFE_NAME.match(name) or not path.is_file():
        return _json(404, {"ok": False, "error": "not found"})
    headers = dict(PAGE_HEADERS)
    headers["Content-Type"] = _TYPES.get(path.suffix.lower(),
                                         "application/octet-stream")
    return 200, headers, path.read_bytes()


def _body(body: bytes) -> Dict[str, Any]:
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AppError(400, "bad JSON: {}".format(exc))
    if not isinstance(payload, dict):
        raise AppError(400, "the body must be a JSON object")
    return payload


def _text(payload: Dict[str, Any], name: str) -> Optional[str]:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AppError(400, "{} must be a string".format(name))
    return value


def _history(value: Any) -> list:
    """The page's earlier questions and answers: [[question, answer], ...]."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(
            isinstance(pair, list) and len(pair) == 2
            and all(isinstance(text, str) for text in pair) for pair in value):
        raise AppError(400, "history is a list of [question, answer] pairs")
    return [(asked[:1000], answered[:4000]) for asked, answered in value[-3:]]


def _source_id(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise AppError(400, "source id must be a number")
