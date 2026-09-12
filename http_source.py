"""
Heart rate over the network, for a Garmin watch running the Connect IQ app
in connectiq/ (or anything else that can make an HTTP request).

This is the one path that needs no Bluetooth or ANT+ hardware on this
machine at all: the watch pushes readings to a small HTTP server we run
here. The watch reaches us over its own Wi-Fi or via the phone it's paired
with -- either way, nothing plugs into or pairs with the PC.

Wire format, both accepted so the watch can use whichever is easier:

    POST /hr        {"hr": 142, "rr": [812.5, 800.0], "device": "Forerunner"}
    GET  /hr?hr=142&rr=812.5,800.0&device=Forerunner
    GET  /health    -> {"ok": true}, for checking reachability from the watch

Set config.HTTP_TOKEN and send it as a `token` query parameter or an
X-Ticker-Token header. When listening beyond loopback, Ticker generates a
strong pairing token if none was configured and shows it in the status.
Use a trusted LAN or put the endpoint behind TLS; the token authenticates
the watch but HTTP does not encrypt the reading or token in transit.

Unlike BLE there is no connection to observe, so "connected" here means "a
reading arrived recently": the first push flips the UI to connected, and a
gap longer than sample_timeout flips it back.
"""

from __future__ import annotations

import hmac
import json
import queue
import secrets as pysecrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

from hr_source import HRSource

# Heart rates outside this range are a parsing accident, not a person.
MIN_HR = 1
MAX_HR = 300

# Most a request body will be drained before answering an error. A watch
# posts a few hundred bytes; anything larger is not something to read just
# to be polite about closing the connection.
MAX_DRAIN_BYTES = 64 * 1024
MAX_BODY_BYTES = 64 * 1024
REQUEST_TIMEOUT_SEC = 5.0
MAX_REQUEST_THREADS = 16

TOKEN_HEADER = "X-Ticker-Token"


def _coerce_hr(value) -> int:
    """Parse and range-check a heart rate from JSON or a query string."""
    if value is None or value == "":
        raise ValueError("missing 'hr'")
    try:
        hr = int(round(float(value)))
    except (TypeError, ValueError):
        raise ValueError(f"'hr' is not a number: {value!r}")
    if not MIN_HR <= hr <= MAX_HR:
        raise ValueError(f"'hr' out of range: {hr}")
    return hr


def _coerce_rr(value) -> List[float]:
    """Parse RR intervals from a JSON list or a comma-separated string.

    Empty segments are skipped -- a trailing comma from the watch shouldn't
    reject an otherwise good reading -- but anything non-numeric is an error
    rather than silently dropped RR data.
    """
    if value is None or value == "":
        return []
    parts = value.split(",") if isinstance(value, str) else value
    if not isinstance(parts, (list, tuple)):
        raise ValueError(f"'rr' is not a list: {value!r}")
    out = []
    for part in parts:
        if isinstance(part, str):
            part = part.strip()
            if not part:
                continue
        try:
            out.append(round(float(part), 1))
        except (TypeError, ValueError):
            raise ValueError(f"'rr' contains a non-number: {part!r}")
    return out


def _bind_hint(exc: OSError) -> str:
    """Extra explanation for bind failures that don't explain themselves.

    Windows reserves blocks of ports for Hyper-V/WSL -- often the whole
    8600-9100 region on a machine with either installed -- and binding
    inside one fails with a permission error rather than "address in use",
    which reads like a firewall problem and isn't one.
    """
    if getattr(exc, "winerror", None) == 10013:
        return (" — Windows has reserved this port. Run "
                "`netsh interface ipv4 show excludedportrange protocol=tcp` "
                "and set HRM_HTTP_PORT to something outside those ranges.")
    return ""


def _local_ip() -> str:
    """Best guess at this machine's LAN address, for the "point the watch
    here" hint. Opening a UDP socket toward a public address sends nothing;
    it just makes the OS pick the interface it would route through.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


class _Handler(BaseHTTPRequestHandler):
    """Request handler. `self.server.source` is the owning HTTPHRSource."""

    server_version = "Ticker"

    def setup(self):
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SEC)

    def log_message(self, fmt, *args):
        # Silence the default one-line-per-request stderr logging: at one
        # reading per second that would fill the packaged app's log file
        # (stderr is redirected there) with nothing anyone wants.
        pass

    # -- routing ----------------------------------------------------------

    def do_GET(self):
        self._body_read = False
        parsed = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if parsed.path == "/health":
            self._send_json(200, {"ok": True, "app": "Ticker"})
            return
        if parsed.path != "/hr":
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized(params):
            return
        self._accept(params)

    def do_POST(self):
        self._body_read = False
        parsed = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if parsed.path != "/hr":
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized(params):
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(400, {"ok": False, "error": "bad Content-Length"})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"ok": False, "error": "body too large"})
            return
        try:
            body = self._read_body(length)
        except (TimeoutError, OSError):
            self._send_json(408, {"ok": False, "error": "request body timed out"})
            return

        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                self._send_json(400, {"ok": False, "error": f"bad JSON: {exc}"})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"ok": False, "error": "body must be a JSON object"})
                return
            # Query parameters still apply, so ?token=... works alongside a
            # JSON body; the body wins for any key present in both.
            merged = dict(params)
            merged.update(payload)
            payload = merged
        else:
            payload = params

        self._accept(payload)

    # -- helpers ----------------------------------------------------------

    def _authorized(self, params: dict) -> bool:
        """Check the shared secret, answering 401 itself if it fails."""
        expected = self.server.source.token
        if not expected:
            return True
        supplied = self.headers.get(TOKEN_HEADER) or params.get("token") or ""
        # compare_digest rather than == : overkill for a LAN secret, but it
        # costs nothing and means the comparison never has to be thought
        # about again.
        if hmac.compare_digest(supplied, expected):
            return True
        self._send_json(401, {"ok": False, "error": "bad or missing token"})
        return False

    def _accept(self, payload: dict):
        try:
            hr = _coerce_hr(payload.get("hr"))
            rr = _coerce_rr(payload.get("rr"))
        except ValueError as exc:
            # Surfaced to the UI as well as answered to the watch: a watch
            # quietly sending garbage otherwise just looks like silence.
            self.server.source.emit_error(f"Rejected reading from watch: {exc}")
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        device = payload.get("device") or None
        self.server.source.record_sample(
            hr, rr, device_name=device, device_address=self.client_address[0]
        )
        self._send_json(200, {"ok": True})

    def _read_body(self, length: int) -> bytes:
        self._body_read = True
        return self.rfile.read(length) if length > 0 else b""

    def _drain_request_body(self):
        """Consume an unread request body before answering.

        Answering a POST without reading its body means closing the socket
        while the client is still writing to it, and the client then sees a
        connection reset instead of the status we sent. On Windows that turns
        a clean 401 into ConnectionAbortedError -- so a watch with a bad
        token would report 'connection failed' rather than 'unauthorised',
        which is a much worse thing to have to debug.

        Capped: an unauthenticated caller must not be able to make us read an
        unbounded body just by claiming a large Content-Length.
        """
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

    def _send_json(self, code: int, payload: dict):
        self._drain_request_body()
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # watch hung up mid-response -- nothing to do about it


class _BoundedServer(ThreadingHTTPServer):
    """Threaded server with a hard cap for unauthenticated LAN traffic."""

    daemon_threads = True

    def server_bind(self):
        self._slots = threading.BoundedSemaphore(MAX_REQUEST_THREADS)
        super().server_bind()

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class HTTPHRSource(HRSource):
    """Receives heart rate pushed over HTTP and streams it onto the queue."""

    def __init__(self, out_queue: "queue.Queue", host: str = "0.0.0.0",
                 port: int = 8787, token: Optional[str] = None,
                 sample_timeout: float = 15.0):
        super().__init__(out_queue)
        self.host = host
        self.port = port
        # A LAN listener is paired by default. Loopback remains tokenless for
        # tests and local integrations that cannot be reached from the LAN.
        self.token = token or (pysecrets.token_urlsafe(24)
                               if host not in ("127.0.0.1", "::1", "localhost")
                               else None)
        self.sample_timeout = sample_timeout

        self._server: Optional[ThreadingHTTPServer] = None
        self._serve_thread: Optional[threading.Thread] = None
        self._watch_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Guards the "is data arriving" state, written from server threads
        # and read by the watchdog thread.
        self._lock = threading.Lock()
        self._connected = False
        self._device_name: Optional[str] = None
        self._device_address: Optional[str] = None
        self._last_sample_at: Optional[float] = None

    # -- HRSource ---------------------------------------------------------

    def start(self):
        if self._server is not None or self._stop_event.is_set():
            return  # already started, or already stopped
        try:
            self._server = _BoundedServer((self.host, self.port), _Handler)
        except OSError as exc:
            # Port already taken, a host address this machine doesn't have,
            # or a reserved port. Reported rather than raised on a background
            # thread: the app stays up and the user can see why nothing is
            # arriving.
            self.emit_error(
                f"Could not listen on {self.host}:{self.port} — {exc}{_bind_hint(exc)}"
            )
            return
        self._server.source = self  # read back by _Handler

        # Resolve the bound port: passing port 0 asks the OS to pick one.
        self.port = self._server.server_address[1]

        self._serve_thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.2},
            daemon=True,
        )
        self._serve_thread.start()

        self._watch_thread = threading.Thread(target=self._watch_for_silence, daemon=True)
        self._watch_thread.start()

        pairing = " · token {}".format(self.token) if self.token else ""
        self.emit_status("searching", message=f"Waiting for a watch on {self.url}{pairing}")

    def stop(self):
        if self._stop_event.is_set():
            return  # already stopped
        self._stop_event.set()

        if self._server is not None:
            try:
                self._server.shutdown()      # unblocks serve_forever
                self._server.server_close()  # releases the port
            except Exception:
                pass
        for thread in (self._serve_thread, self._watch_thread):
            if thread is not None:
                thread.join(timeout=5)

        self.emit_status("stopped")

    # -- receiving --------------------------------------------------------

    @property
    def url(self) -> str:
        """The URL to point the watch at.

        0.0.0.0 means "every interface", which is the right thing to listen
        on and a useless thing to type into a watch, so show the LAN
        address instead.
        """
        host = _local_ip() if self.host in ("0.0.0.0", "", "::") else self.host
        return f"http://{host}:{self.port}/hr"

    def record_sample(self, hr: int, rr_intervals_ms: List[float],
                      device_name: Optional[str] = None,
                      device_address: Optional[str] = None):
        """Called from a server thread for each accepted reading."""
        with self._lock:
            became_connected = not self._connected
            renamed = device_name is not None and device_name != self._device_name
            self._connected = True
            self._last_sample_at = time.monotonic()
            if device_name is not None:
                self._device_name = device_name
            if device_address is not None:
                self._device_address = device_address
            name, address = self._device_name, self._device_address

        # Status first, so the UI is already showing "connected" when the
        # first bpm lands rather than one reading later.
        if became_connected or renamed:
            self.emit_status("connected", device_name=name, device_address=address,
                             message=f"Receiving from {name or address or 'watch'}")
        self.emit_sample(hr, rr_intervals_ms)

    def _watch_for_silence(self):
        """Flip back to "reconnecting" when readings stop arriving.

        There is no disconnect event to listen for -- a watch that walks out
        of range, loses its phone, or has the app closed simply stops
        sending -- so silence is the only signal there is.
        """
        interval = max(0.05, min(1.0, self.sample_timeout / 4.0))
        while not self._stop_event.wait(interval):
            with self._lock:
                if not self._connected or self._last_sample_at is None:
                    continue
                if time.monotonic() - self._last_sample_at <= self.sample_timeout:
                    continue
                self._connected = False
                name = self._device_name
            self.emit_status(
                "reconnecting", device_name=name,
                message=(f"No readings for {self.sample_timeout:.0f}s -- "
                         f"waiting for {name or 'the watch'}"),
            )
