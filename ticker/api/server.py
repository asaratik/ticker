"""
The read API's HTTP transport.

    python -m ticker.api.server                 # loopback, no token
    python -m ticker.api.server --host 0.0.0.0 --allow-remote

Stdlib `http.server`, not FastAPI. Section 11 pencils FastAPI in, and for a
service with a large surface it would be the right answer -- but this one is
seven endpoints whose logic all lives in `routes.py`, and the cost is real:
FastAPI plus uvicorn plus pydantic is roughly thirty megabytes in a
PyInstaller bundle this project deliberately keeps small and
boring, on top of a dependency tree that a local-first health logger then
has to keep patched. `http_source.py` already runs a threaded stdlib server
for the watch endpoint and it has not been the source of a single bug.

The routing table is framework-independent by construction, so this stays a
decision rather than a commitment: swapping in FastAPI is a new module of
about this size and no change to `routes.py` at all.

MCP
---
/mcp serves the same read-only tools as `ticker mcp`, over MCP's Streamable
HTTP transport, for an agent that can't launch a process on this machine --
the homelab deployment, where the database lives on another box. Replies
are plain JSON responses to POSTs; there is no event stream to GET, which
the transport allows. The token check is the same as everywhere else, and
also accepts `Authorization: Bearer`, which is what MCP clients know how to
send.

The page
--------
When the app runs this server it also serves its page: / and /static/* (the
page itself, public) and /ui/* (its data and actions, behind the token like
everything else). See ticker.app.web.

Browsers
--------
A server on 127.0.0.1 with a page that stores tokens and can stop the app is
worth attacking from any web page its user happens to visit, so every route
but /health gets two checks. On a loopback-bound server with no token, the
Host header must be a loopback name (or one listed in
TICKER_API_ALLOWED_HOSTS, such as host.docker.internal for a container): a
page that rebinds its own hostname to 127.0.0.1 still sends its own name,
and this is what defeats DNS rebinding, which the browser would otherwise
treat as same-origin. With a token the check is unnecessary -- the rebound
page doesn't have it -- and is skipped. And a request carrying a
foreign Origin is refused -- what the MCP spec asks of /mcp, and what stops
cross-site form posts everywhere else. Agents, curl and Grafana send no
Origin and a loopback Host, so neither check touches them.

Binding
-------
Loopback by default. Section 9.3 requires binding anywhere else to need an
explicit flag *and* a token, and this refuses to start rather than warning:
this endpoint writes to the database, so the failure mode of getting it
wrong is not a leak but an open ingest port.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from ticker import config as tconfig
from ticker.api.routes import Api
from ticker.db import store
from ticker.mcp import protocol as mcp_protocol
from ticker.mcp.server import build as build_mcp

log = logging.getLogger("ticker.api")

TOKEN_HEADER = "X-Ticker-Token"

MCP_PATH = "/mcp"
MCP_VERSION_HEADER = "MCP-Protocol-Version"

# Largest request body accepted. An ingest batch of the default 5000
# observations is well under a megabyte; this is the cap that stops an
# unauthenticated caller from making us read an arbitrary amount of it.
MAX_BODY_BYTES = 16 * 1024 * 1024

# Most of an unread body that gets drained before answering an error --
# same reasoning as http_source: answering without draining resets the
# connection and turns a clean 401 into an unexplained transport error.
MAX_DRAIN_BYTES = 64 * 1024

# How often the serving loop checks whether it has been asked to stop.
SHUTDOWN_POLL_SEC = 0.05

# Addresses that need no token because the OS is already the access control.
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


class RemoteBindRefused(RuntimeError):
    """Off-loopback bind without the flag or without a token."""


def check_bind(host: str, token: Optional[str], allow_remote: bool) -> None:
    """Section 9.3's rule, in one place so both the CLI and the server obey
    the same one. Raises rather than warning: a warning on a service that
    then starts anyway is a service that runs open for months."""
    if host in LOOPBACK:
        return
    if not allow_remote:
        raise RemoteBindRefused(
            "refusing to bind {} without --allow-remote (or "
            "TICKER_API_ALLOW_REMOTE=1): /api/ingest writes to the "
            "database".format(host))
    if not token:
        raise RemoteBindRefused(
            "refusing to bind {} without a token: set TICKER_API_TOKEN "
            "or pass --token".format(host))


class _Handler(BaseHTTPRequestHandler):
    """`self.server.api` is the Api; `self.server.token` the shared secret."""

    server_version = "Ticker"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # The default writes a line per request to stderr, which in a
        # packaged build is the log file. A dashboard polling every five
        # seconds would be the only thing in it.
        log.debug("%s - %s", self.address_string(), fmt % args)

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")

    def do_HEAD(self):
        # Answered so a health check that uses HEAD gets a status rather
        # than a 501 from the base class.
        self._serve("GET", head_only=True)

    def _serve(self, method: str, head_only: bool = False):
        self._body_read = False
        parsed = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        # /health answers before the token check so an unauthenticated
        # liveness probe -- a container orchestrator, a watch checking
        # reachability -- can still tell the server is up. It reveals
        # nothing but that.
        if parsed.path.rstrip("/") in ("/health", "/api/health"):
            self._send(200, {"ok": True, "app": "Ticker"}, head_only)
            return

        route = parsed.path.rstrip("/") or "/"
        refusal = self._refusal()
        if refusal:
            self._send(403, {"ok": False, "error": refusal}, head_only)
            return

        ui = getattr(self.server, "ui", None)
        if ui is not None and ui.serves_page(route):
            # The page itself skips the token: a remote page has to load
            # before it can be told the token, and it is nothing but code.
            # Everything it then asks for goes through the check below.
            self._send_raw(*ui.handle(method, parsed.path), head_only=head_only)
            return

        if not self._authorized(params):
            return

        body = b""
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send(400, {"ok": False, "error": "bad Content-Length"})
                return
            if length > MAX_BODY_BYTES:
                self._send(413, {"ok": False, "error": "body too large"})
                return
            body = self._read_body(length)

        if route == MCP_PATH:
            self._serve_mcp(method, body, head_only, params)
            return
        if ui is not None and route.startswith("/ui/"):
            self._send_raw(*ui.handle(method, parsed.path, body,
                                      self.headers.get("Content-Type") or ""),
                           head_only=head_only)
            return

        status, payload = self.server.api.handle(method, parsed.path, params, body)
        self._send(status, payload, head_only)

    def _serve_mcp(self, method: str, body: bytes, head_only: bool,
                   params: Optional[dict] = None):
        # ?profile=compact: the smaller tool set, for local models.
        profile = (params or {}).get("profile") or "full"
        if profile not in ("full", "compact"):
            self._send(400, {"ok": False, "error": "profile is full or compact"},
                       head_only)
            return
        mcp = getattr(self.server,
                      "mcp_compact" if profile == "compact" else "mcp", None)
        if mcp is None:
            self._send(404, {"ok": False, "error": "MCP is not enabled here"},
                       head_only)
            return
        if method != "POST":
            self._send(405, {"ok": False,
                             "error": "POST JSON-RPC messages to /mcp"},
                       head_only, headers={"Allow": "POST"})
            return
        version = self.headers.get(MCP_VERSION_HEADER)
        if version and version not in mcp_protocol.PROTOCOL_VERSIONS:
            self._send(400, {"ok": False, "error": "unsupported {}: {}".format(
                MCP_VERSION_HEADER, version)})
            return
        try:
            message = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            self._send(400, mcp_protocol.parse_error(str(exc)))
            return
        reply = mcp.handle(message)
        if reply is None:
            # Notifications and responses: accepted, nothing to say back.
            self._send(202, None)
        else:
            self._send(200, reply)

    def _refusal(self) -> Optional[str]:
        """Why a browser-borne request can't be served, or None. See
        'Browsers' in the module docstring."""
        host = self.headers.get("Host") or ""
        # Only without a token: with one, a rebinding page is stopped by not
        # having it, and the Host check would only turn away honest clients
        # that reach this machine by another name.
        if (host and getattr(self.server, "loopback_only", False)
                and not self.server.token
                and _hostname(host) not in getattr(self.server, "allowed_hosts",
                                                   LOOPBACK)):
            return ("this server only answers to 127.0.0.1 or localhost; "
                    "add other names to TICKER_API_ALLOWED_HOSTS")
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            # 'null' -- a sandboxed frame, a file:// page -- parses to no
            # hostname and no netloc, so it is refused along with the rest.
            if (parsed.hostname or "") not in LOOPBACK and (
                    not parsed.netloc or parsed.netloc != host):
                return "cross-origin requests are refused"
        return None

    def _authorized(self, params: dict) -> bool:
        expected = self.server.token
        if not expected:
            return True
        supplied = (self.headers.get(TOKEN_HEADER)
                    or _bearer(self.headers.get("Authorization"))
                    or params.get("token") or "")
        if hmac.compare_digest(supplied, expected):
            return True
        self._send(401, {"ok": False, "error": "bad or missing token"})
        return False

    def _read_body(self, length: int) -> bytes:
        self._body_read = True
        if length <= 0:
            return b""
        # Read in a loop: a single read() can come up short on a large body
        # split across packets, and a truncated JSON body would be reported
        # as the client's malformed request rather than our short read.
        chunks, remaining = [], length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _drain_request_body(self):
        if getattr(self, "_body_read", False):
            return
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        remaining = min(length, MAX_DRAIN_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 8192))
            if not chunk:
                return
            remaining -= len(chunk)

    def _send(self, code: int, payload: Optional[dict], head_only: bool = False,
              headers: Optional[dict] = None):
        """Answer with `payload` as JSON, or with no body when it is None."""
        body = b"" if payload is None else json.dumps(payload, default=str).encode("utf-8")
        sent = dict(headers or {})
        if payload is not None:
            sent.setdefault("Content-Type", "application/json")
        self._send_raw(code, sent, body, head_only)

    def _send_raw(self, code: int, headers: dict, body: bytes,
                  head_only: bool = False):
        self._drain_request_body()
        try:
            self.send_response(code)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body and not head_only:
                self.wfile.write(body)
        except OSError:
            # The client hung up mid-answer. Normal for a dashboard whose
            # tab was closed; not worth a traceback in the log.
            pass


class ApiServer:
    """The read API on a background thread.

    Owned by whoever starts it -- the app's runtime, or this module's own
    main() for an API with nothing else behind it -- which is why start()
    and stop() are separate from main().
    """

    def __init__(self, api: Api, host: Optional[str] = None,
                 port: Optional[int] = None, token: Optional[str] = None,
                 allow_remote: Optional[bool] = None, mcp=None, ui=None,
                 allowed_hosts=None, mcp_compact=None):
        self.api = api
        # Names besides loopback a token-less server answers to; see
        # TICKER_API_ALLOWED_HOSTS.
        self.allowed_hosts = frozenset(
            name.lower() for name in (tconfig.API_ALLOWED_HOSTS
                                      if allowed_hosts is None else allowed_hosts))
        # An McpServer to answer /mcp, and the app's page (ticker.app.web.Ui)
        # to answer /, /static and /ui -- each None to leave it unrouted.
        self.mcp = mcp
        # The same tools in their compact profile, at /mcp?profile=compact.
        self.mcp_compact = mcp_compact
        self.ui = ui
        self.host = tconfig.API_HOST if host is None else host
        self.port = tconfig.API_PORT if port is None else port
        self.token = tconfig.API_TOKEN if token is None else token
        self.allow_remote = (tconfig.API_ALLOW_REMOTE if allow_remote is None
                             else allow_remote)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> int:
        """Bind and serve. Returns the port actually bound, which is what
        port 0 is for in tests."""
        check_bind(self.host, self.token, self.allow_remote)
        httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        # daemon_threads: a request in flight must not keep the process
        # alive at shutdown. The writer is flushed separately, so nothing
        # durable is lost by cutting a connection.
        httpd.daemon_threads = True
        httpd.api = self.api
        httpd.mcp = self.mcp
        httpd.mcp_compact = self.mcp_compact
        httpd.ui = self.ui
        httpd.token = self.token
        httpd.loopback_only = self.host in LOOPBACK
        httpd.allowed_hosts = LOOPBACK | self.allowed_hosts
        self._httpd = httpd
        self.port = httpd.server_address[1]
        # serve_forever polls for the shutdown flag, and stop() blocks until
        # it notices. The 0.5s default is half a second of dead time on every
        # Ctrl+C -- and, multiplied by a test suite that starts a server per
        # test, most of that suite's runtime.
        self._thread = threading.Thread(
            target=lambda: httpd.serve_forever(poll_interval=SHUTDOWN_POLL_SEC),
            name="ticker-api", daemon=True)
        self._thread.start()
        return self.port

    def stop(self, timeout: float = 5.0) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return "http://{}:{}".format(host, self.port)


def thread_local_reader(db_path: Path):
    """A connection factory that keeps one connection per serving thread.

    sqlite3 connections are not safe to share across threads, and a threaded
    server answers several requests at once. Opening one per request would
    also work, but a dashboard polling four panels every five seconds would
    then be opening a quarter of a million connections a day; a WAL reader
    per thread is cheap and long-lived.
    """
    local = threading.local()

    def reader():
        conn = getattr(local, "conn", None)
        if conn is None:
            # Already migrated by main() before serving started; a reader
            # must never be the one to run migrations.
            conn = store.connect(db_path, migrate_first=False)
            local.conn = conn
        return conn

    return reader


def build(db_path: Optional[Path] = None, writer=None, on_sync=None,
          mcp=None, ui=None, mcp_compact=None, **kwargs) -> ApiServer:
    """An ApiServer over a database, with its own writer unless given one.

    The single-machine deployment passes the app's existing AsyncStore so
    there is one writer thread for the process, which is what keeps the
    live stream and the API from interleaving commits.

    /mcp gets the same read-only tools as `ticker mcp` unless `mcp` says
    otherwise. They open their own read-only connections rather than
    borrowing the API's readers, which can write.
    """
    path = Path(db_path) if db_path else tconfig.DB_PATH
    store.connect(path).close()          # migrate once, before any reader
    if writer is None:
        writer = store.AsyncStore(path, migrate_first=False)
    if mcp is None:
        mcp, _db = build_mcp(path)
    if mcp_compact is None:
        mcp_compact, _db = build_mcp(path, profile="compact")
    api = Api(thread_local_reader(path), writer, on_sync=on_sync)
    return ApiServer(api, mcp=mcp, mcp_compact=mcp_compact, ui=ui, **kwargs)


def _hostname(host_header: str) -> str:
    """The name in a Host header, without its port: '127.0.0.1:8477' and
    '[::1]:8477' become '127.0.0.1' and '::1'."""
    return urlparse("//" + host_header).hostname or ""


def _bearer(header: Optional[str]) -> Optional[str]:
    if header and header[:7].lower() == "bearer ":
        return header[7:].strip()
    return None


def install_stop_handlers() -> threading.Event:
    """An Event that gets set when the OS asks this process to stop.

    SIGTERM matters as much as Ctrl+C here: the server's natural home is a
    box where it runs under systemd or docker, and both stop a service by
    sending SIGTERM. Python's default for it is to die immediately, which
    would skip the flush and the rollup rebuild on every restart.

    SIGBREAK is the Windows equivalent -- Ctrl+Break, and what a console
    process gets on shutdown -- and defaults to terminating too.

    Handlers can only be installed from the main thread; anywhere else this
    returns an Event nobody sets, and the caller's KeyboardInterrupt path
    still works.
    """
    import signal

    stopping = threading.Event()

    def handler(signum, frame):
        stopping.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, handler)
        except (ValueError, OSError):
            pass                   # not the main thread, or not supported
    return stopping


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default=tconfig.API_HOST)
    parser.add_argument("--port", type=int, default=tconfig.API_PORT)
    parser.add_argument("--token", default=tconfig.API_TOKEN,
                        help="shared secret; required to bind off-loopback")
    parser.add_argument("--allow-remote", action="store_true",
                        default=tconfig.API_ALLOW_REMOTE,
                        help="permit binding somewhere other than loopback")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    db_path = args.db or tconfig.DB_PATH
    try:
        check_bind(args.host, args.token, args.allow_remote)
    except RemoteBindRefused as exc:
        print(exc, file=sys.stderr)
        return 2

    server = build(db_path, host=args.host, port=args.port, token=args.token,
                   allow_remote=args.allow_remote)
    try:
        server.start()
    except OSError as exc:
        print("cannot bind {}:{} - {}".format(args.host, args.port, exc),
              file=sys.stderr)
        return 1

    log.info("serving %s over %s", db_path, server.url)
    log.info("MCP for agents at %s%s", server.url, MCP_PATH)
    if not args.token and args.host not in LOOPBACK:
        log.warning("no token set")
    idle = install_stop_handlers()
    try:
        # The server runs on its own thread; this one waits to be told to
        # stop. Waiting with a timeout in a loop rather than joining the
        # serving thread or blocking forever: an untimed wait on the main
        # thread is where Ctrl+C goes to be ignored on Windows, and this
        # way the signal is noticed between two short waits everywhere.
        while not idle.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass                       # no handler could be installed
    finally:
        print("stopping")
        server.stop()
        writer = server.api.writer
        # Commit whatever is still coalescing, then bring the rollups up to
        # date over the days this run changed. Reaching this on the way out
        # is the entire reason the signals are handled rather than left to
        # terminate the process.
        writer.rebuild_rollups()
        writer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
