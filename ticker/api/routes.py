"""
The read API's behaviour, with no HTTP in it.

Every endpoint here is a function from parsed request to
`(status, payload)`. Nothing in this module opens a socket, reads a header
or knows what a status line looks like -- `ticker.api.server` does all of
that. Two reasons:

* The interesting failures are argument parsing, window arithmetic and the
  ingest path, and none of them should need a listening port to test.
* Section 11 pencils in FastAPI. Keeping the routing table this side of the
  boundary means swapping the transport is a new `server.py` and nothing
  else -- the endpoints, their parameters and their answers are already
  independent of it.

Errors are returned, not raised. A read API that 500s because someone typed
`from=yesterday` is a read API that gets blamed for the dashboard being
broken, so every bad argument comes back as a 400 saying which one.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from ticker import config as tconfig
from ticker.api import wire
from ticker.db import queries, store
from ticker.model import now_utc, parse_iso

log = logging.getLogger(__name__)

Response = Tuple[int, Dict[str, Any]]

# Default window for /api/observations when neither bound is given. A day is
# what a dashboard opening cold usually wants, and it is small enough that
# an unbucketed request for it is not a mistake worth making expensive.
DEFAULT_WINDOW = timedelta(hours=24)

# How far back /api/sessions looks when no window is given.
DEFAULT_SESSION_WINDOW = timedelta(days=30)

# Relative windows a dashboard can ask for without doing date arithmetic:
# from=-7d. Grafana sends absolute times, but a curl at the command line
# shouldn't have to.
_RELATIVE = re.compile(r"^-(\d+(?:\.\d+)?)([smhd])$")
_RELATIVE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Above this, a bare integer timestamp is milliseconds rather than seconds:
# 1e11 seconds is the year 5138, and 1e11 milliseconds is 1973.
_EPOCH_MS_THRESHOLD = 10 ** 11


class ApiError(Exception):
    """A bad request, with the status and message to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def parse_time(text: Optional[str], default: datetime) -> datetime:
    """An absolute ISO timestamp, a relative '-6h', or the default."""
    if text is None or not text.strip():
        return default
    text = text.strip()
    match = _RELATIVE.match(text)
    if match:
        seconds = float(match.group(1)) * _RELATIVE_UNITS[match.group(2)]
        return now_utc() - timedelta(seconds=seconds)
    # Grafana and most clients send epoch milliseconds when they aren't
    # sending ISO; accepting both costs one branch.
    if text.isdigit():
        value = int(text)
        if value > _EPOCH_MS_THRESHOLD:
            value = value / 1000.0
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise ApiError(400, "bad timestamp {!r}: {}".format(text, exc))
    try:
        return parse_iso(text)
    except ValueError as exc:
        raise ApiError(400, "bad timestamp {!r}: {}".format(text, exc))


class Api:
    """The endpoints, bound to a database and a writer.

    `reader` is a callable returning a connection rather than a connection
    itself: a threaded server answers several requests at once, and a
    sqlite3 connection is not safe to share across threads. The writer is
    shared on purpose -- it is a queue with one thread behind it, which is
    exactly what is safe to hand to everyone.
    """

    def __init__(self, reader: Callable[[], sqlite3.Connection],
                 writer, on_sync: Optional[Callable[[int], bool]] = None,
                 max_batch: Optional[int] = None):
        self.reader = reader
        self.writer = writer
        self.on_sync = on_sync
        self.max_batch = (tconfig.API_MAX_BATCH if max_batch is None
                          else max_batch)
        self.ingested = 0

    # -- dispatch --------------------------------------------------------

    def handle(self, method: str, path: str, params: Dict[str, str],
               body: bytes = b"") -> Response:
        """Route one request. Never raises; every failure is a status."""
        path = path.rstrip("/") or "/"
        try:
            if method == "GET":
                return self._get(path, params)
            if method == "POST":
                return self._post(path, params, body)
            return 405, {"ok": False, "error": "method not allowed"}
        except ApiError as exc:
            return exc.status, {"ok": False, "error": exc.message}
        except KeyError as exc:
            # queries.metric_id raises this for an unregistered metric, which
            # is the caller's typo far more often than a missing seed row.
            return 404, {"ok": False,
                         "error": str(exc.args[0] if exc.args else exc)}
        except Exception as exc:
            # A read API is something a dashboard polls every few seconds.
            # One unexpected failure must not take the server down with it.
            log.exception("unhandled error serving %s %s", method, path)
            return 500, {"ok": False,
                         "error": "{}: {}".format(type(exc).__name__, exc)}

    def _get(self, path: str, params: Dict[str, str]) -> Response:
        if path in ("/health", "/api/health"):
            return 200, {"ok": True, "app": "Ticker"}
        if path == "/api/metrics":
            return self.metrics()
        if path == "/api/observations":
            return self.observations(params)
        if path == "/api/sessions":
            return self.sessions(params)
        if path == "/api/sources":
            return self.sources(params)
        return 404, {"ok": False, "error": "not found"}

    def _post(self, path: str, params: Dict[str, str], body: bytes) -> Response:
        if path == "/api/ingest":
            return self.ingest(body)
        if path.startswith("/api/sync/"):
            return self.sync(path[len("/api/sync/"):])
        return 404, {"ok": False, "error": "not found"}

    # -- reads -----------------------------------------------------------

    def metrics(self) -> Response:
        with self._read() as conn:
            return 200, {"metrics": queries.list_metrics(conn)}

    def observations(self, params: Dict[str, str]) -> Response:
        metric = params.get("metric")
        if not metric:
            raise ApiError(400, "metric is required")
        until = parse_time(params.get("to"), now_utc())
        since = parse_time(params.get("from"), until - DEFAULT_WINDOW)
        if since > until:
            raise ApiError(400, "from is after to")
        source_id = _int_param(params, "source_id")
        bucket = params.get("bucket")
        with self._read() as conn:
            try:
                points = queries.series(conn, metric, since, until, bucket,
                                        source_id)
            except ValueError as exc:
                raise ApiError(400, str(exc))
        return 200, {"metric": metric, "from": since.isoformat(),
                     "to": until.isoformat(), "bucket": bucket or "raw",
                     "n": len(points), "points": points}

    def sessions(self, params: Dict[str, str]) -> Response:
        until = parse_time(params.get("to"), now_utc())
        since = parse_time(params.get("from"), until - DEFAULT_SESSION_WINDOW)
        if since > until:
            raise ApiError(400, "from is after to")
        with self._read() as conn:
            return 200, {"sessions": queries.list_sessions(conn, since, until)}

    def sources(self, params: Optional[Dict[str, str]] = None) -> Response:
        # counts=1 adds a row count per source, which is a scan per source.
        # Off by default so this endpoint stays cheap enough to poll.
        counts = _flag(params or {}, "counts")
        with self._read() as conn:
            return 200, {"sources": queries.list_sources(conn, counts=counts)}

    # -- writes ----------------------------------------------------------

    def ingest(self, body: bytes) -> Response:
        """Accept a batch of observations from an agent.

        Idempotent by construction: the rows go through the same upsert
        every other writer uses, so a spool replayed after a crash writes
        the same data twice and changes nothing the second time. That is
        what lets the agent delete a batch only once it is acknowledged,
        and re-send anything it isn't sure about.
        """
        payload = _json_object(body)
        try:
            batch = wire.observations_from_json(payload.get("observations", []))
        except wire.BadObservation as exc:
            return 400, {"ok": False, "error": str(exc)}
        if len(batch) > self.max_batch:
            return 413, {"ok": False,
                         "error": "batch of {} exceeds the {} row limit".format(
                             len(batch), self.max_batch)}

        source = payload.get("source")
        if not isinstance(source, dict) or not source.get("vendor"):
            return 400, {"ok": False, "error": "source.vendor is required"}
        vendor = str(source["vendor"])
        display_name = str(source.get("display_name")
                           or tconfig.source_display_name(vendor))
        kind = str(source.get("kind") or "stream")
        if kind not in ("stream", "pull", "import"):
            return 400, {"ok": False,
                         "error": "source.kind must be stream, pull or import"}

        try:
            sessions = [wire.session_from_json(item)
                        for item in payload.get("sessions") or []]
        except wire.BadObservation as exc:
            return 400, {"ok": False, "error": str(exc)}

        with self._read() as conn:
            known_metrics = store.metric_ids(conn)
            unknown_metrics = sorted({obs.metric for obs in batch}
                                     - known_metrics.keys())
            if unknown_metrics:
                return 422, {"ok": False,
                             "error": "unknown metric(s): {}".format(
                                 ", ".join(unknown_metrics))}
            source_id = store.ensure_source(conn, kind, vendor, display_name)
            device_id = None
            device = wire.device_from_json(payload.get("device"))
            if device:
                device_id = store.ensure_device(conn, source_id, **device)

        # Sessions first: they are the foreign key the observations in this
        # same batch reference, and the writer resolves session_key against
        # what it has already opened.
        for record in sessions:
            self.writer.begin_session(source_id, record, device_id)
        self.writer.insert_observations(source_id, batch)
        for key in payload.get("closed_sessions") or []:
            self.writer.end_session(source_id, str(key))

        self.ingested += len(batch)
        return 200, {"ok": True, "accepted": len(batch), "source_id": source_id}

    def sync(self, source_ref: str) -> Response:
        """Ask for a pull source to be synced now.

        Answers 503 rather than pretending when nothing is wired up to act
        on it -- a server running without the scheduler in-process cannot
        make a sync happen, and a caller polling for fresh Oura data
        deserves to be told so rather than left waiting on a 200 that meant
        nothing.
        """
        try:
            source_id = int(source_ref)
        except ValueError:
            raise ApiError(400, "source id must be a number")
        with self._read() as conn:
            row = conn.execute(
                "SELECT kind, enabled FROM sources WHERE id = ?",
                (source_id,)).fetchone()
        if row is None:
            return 404, {"ok": False, "error": "no source {}".format(source_id)}
        if row[0] != "pull":
            return 400, {"ok": False,
                         "error": "source {} is a {} source; only pull sources "
                                  "are synced on demand".format(source_id, row[0])}
        if not row[1]:
            return 409, {"ok": False,
                         "error": "source {} is disabled".format(source_id)}
        if self.on_sync is None:
            return 503, {"ok": False,
                         "error": "no scheduler in this process: start "
                                  "Ticker itself (`ticker`), which syncs "
                                  "cloud accounts, or run `ticker sync --once`"}
        if not self.on_sync(source_id):
            return 409, {"ok": False,
                         "error": "source {} is already syncing".format(source_id)}
        return 202, {"ok": True, "source_id": source_id, "status": "queued"}

    # -- plumbing --------------------------------------------------------

    def _read(self):
        return _Reader(self.reader)


class _Reader:
    """Borrow a connection for the length of one handler.

    A context manager rather than a bare call so that whatever the reader
    hands back -- a per-thread connection it keeps, or a fresh one it opened
    for us -- is closed if and only if it was opened for us. A factory says
    which by returning (connection, owned) instead of a bare connection.
    """

    def __init__(self, factory):
        self.factory = factory
        self.conn = None
        self._owned = False

    def __enter__(self):
        borrowed = self.factory()
        if isinstance(borrowed, tuple):
            self.conn, self._owned = borrowed
        else:
            self.conn, self._owned = borrowed, False
        return self.conn

    def __exit__(self, *exc):
        if self._owned and self.conn is not None:
            self.conn.close()
        return False


def _json_object(body: bytes) -> Dict[str, Any]:
    if not body:
        raise ApiError(400, "empty body")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError(400, "bad JSON: {}".format(exc))
    if not isinstance(payload, dict):
        raise ApiError(400, "body must be a JSON object")
    return payload


def _flag(params: Dict[str, str], name: str) -> bool:
    """A query parameter used as a switch. Bare `?counts` counts as on, the
    way a command-line flag would."""
    raw = params.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in ("", "1", "true", "yes", "on")


def _int_param(params: Dict[str, str], name: str) -> Optional[int]:
    raw = params.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise ApiError(400, "{} must be a number".format(name))
