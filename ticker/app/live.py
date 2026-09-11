"""
The live heart-rate source, inside the app.

What the Tk window used to do, without the window: run the strap or the
watch, keep the current reading and the last few minutes for the page to
draw, and log to the database while a session runs. Sessions mean what they
always did -- a decision you make, separate from the connection, surviving
a dropout -- so the page's Start and Stop behave exactly as the old buttons
did.

One thread owns all of it. The heart-rate source posts its messages onto a
queue, and so do the page's requests (start, stop, switch source); this
module's thread takes them off in order. That is what lets SessionLogger
keep its single sqlite3 connection, which only the thread that opened it may
use, and what stops a Stop from racing the sample that preceded it.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Deque, Dict, Optional, Tuple

import config
from ticker.ingest.session_logger import SessionLogger
from ticker.model import now_iso

log = logging.getLogger("ticker.live")

KINDS = ("off", "ble", "http")

# How much history the page's chart shows.
WINDOW_SEC = config.GRAPH_WINDOW_SEC

# How long a request from the page waits for this thread to act on it.
COMMAND_TIMEOUT_SEC = 10.0


class LiveError(Exception):
    """A request the live source can't carry out, with the reason."""


def default_source(kind: str, out_queue, address: Optional[str]):
    """The v1 HRSource for `kind`. Imported late, as hr_source does, so a
    machine with no Bluetooth stack can still run the watch source."""
    if kind == "ble":
        import ble_source
        return ble_source.BLEHRSource(
            out_queue, address=address,
            scan_timeout=config.SCAN_TIMEOUT_SEC,
            reconnect_delay=config.RECONNECT_DELAY_SEC)
    import hr_source
    return hr_source.create_source(out_queue, kind)


class _Tagged:
    """Stamps each message a source posts with the source's generation.

    Stopping a source is not instantaneous -- a BLE thread can still post a
    last 'stopped' status after the switch to the watch -- and without the
    stamp that straggler would be applied to the new source's state.
    """

    def __init__(self, target: "queue.Queue", generation: int):
        self._target = target
        self._generation = generation

    def put(self, message: dict) -> None:
        self._target.put(dict(message, _gen=self._generation))


class LiveMonitor:
    """The current live source, its readings, and the running session."""

    def __init__(self, settings, writer, db_path=None,
                 make_source: Callable = default_source,
                 make_logger: Optional[Callable] = None,
                 clock: Callable[[], float] = time.time):
        self.settings = settings
        self._make_source = make_source
        self._make_logger = make_logger or (
            lambda vendor, errors: SessionLogger(
                vendor, db_path=db_path, error_queue=errors, store=writer))
        self._clock = clock
        self._queue: "queue.Queue" = queue.Queue()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._generation = 0

        # Written on this module's thread; read by anyone, under the lock.
        self._kind = "off"
        self._source = None
        self._logger = None
        self._status = "off"
        self._message: Optional[str] = None
        self._error: Optional[str] = None
        self._device_name: Optional[str] = None
        self._device_address: Optional[str] = None
        self._bpm: Optional[int] = None
        self._points: Deque[Tuple[float, int]] = deque()
        self._session: Optional[Dict[str, Any]] = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ticker-live",
                                        daemon=True)
        self._thread.start()
        try:
            self._call("switch", kind=self.settings.live_source)
        except LiveError as exc:
            # A strap that won't start is shown on the page, not a reason
            # for the app not to.
            log.warning("live source: %s", exc)

    def close(self, timeout: float = 10.0) -> None:
        """End any session, stop the source. The shared writer is left open
        for its owner to close."""
        if self._thread is None:
            return
        self._queue.put({"type": "_stop"})
        self._thread.join(timeout)
        self._thread = None

    # -- requests, from any thread ---------------------------------------

    def set_source(self, kind: str) -> dict:
        kind = (kind or "").strip().lower()
        if kind not in KINDS:
            raise LiveError("the live source is one of: off, ble, http")
        if self.settings.live_source_pinned and kind != self.settings.live_source:
            raise LiveError("HRM_SOURCE is set in the environment, so the live "
                            "source can't be changed from here")
        self.settings.update(live_source=kind)
        return self._call("switch", kind=kind)

    def start_session(self, label: Optional[str] = None) -> dict:
        return self._call("start", label=(label or "").strip() or None)

    def stop_session(self) -> dict:
        return self._call("stop")

    def snapshot(self) -> Dict[str, Any]:
        now = self._clock()
        with self._lock:
            session = None
            if self._session is not None:
                s = self._session
                session = {
                    "label": s["label"], "started": s["started_iso"],
                    "elapsed_s": max(0, int(now - s["started"])), "n": s["n"],
                    "avg": round(s["sum"] / s["n"]) if s["n"] else None,
                    "min": s["min"], "max": s["max"],
                }
            cutoff = now - WINDOW_SEC
            return {
                "source": self._kind,
                "pinned": self.settings.live_source_pinned,
                "status": self._status,
                "message": self._message,
                "error": self._error,
                "device": self._device_name,
                "bpm": self._bpm if self._status == "connected" else None,
                "session": session,
                "points": [[int(t * 1000), hr] for t, hr in self._points
                           if t >= cutoff],
                "window_s": WINDOW_SEC,
                "logging": bool(self._logger is not None
                                and self._logger.available),
            }

    def _call(self, op: str, **kwargs) -> dict:
        if self._thread is None or not self._thread.is_alive():
            raise LiveError("the live source isn't running")
        future: Future = Future()
        self._queue.put({"type": "_cmd", "op": op, "kwargs": kwargs,
                         "future": future})
        try:
            return future.result(timeout=COMMAND_TIMEOUT_SEC)
        except FutureTimeout:
            raise LiveError("the live source didn't respond")

    # -- this module's thread --------------------------------------------

    def _run(self) -> None:
        while True:
            try:
                message = self._queue.get(timeout=0.5)
            except queue.Empty:
                self._prune()
                continue
            kind = message.get("type")
            if kind == "_stop":
                self._teardown()
                return
            if kind == "_cmd":
                self._command(message)
                continue
            try:
                self._handle(message)
            except Exception:
                # One malformed message must not end live logging for the
                # rest of the run -- the Tk app's _tick rule, kept.
                log.exception("could not handle live message %r", message)

    def _command(self, message: dict) -> None:
        future = message["future"]
        try:
            result = getattr(self, "_op_" + message["op"])(**message["kwargs"])
        except LiveError as exc:
            future.set_exception(exc)
        except Exception as exc:
            log.exception("live %s failed", message["op"])
            future.set_exception(LiveError(str(exc)))
        else:
            future.set_result(result)

    def _op_switch(self, kind: str) -> dict:
        if kind == self._kind and (kind == "off" or self._source is not None):
            return self.snapshot()
        self._teardown()
        with self._lock:
            self._kind = kind
            self._status = "off" if kind == "off" else "starting"
            self._message = self._error = None
            self._device_name = self._device_address = None
            self._bpm = None
            self._points.clear()
        if kind == "off":
            return self.snapshot()
        tagged = _Tagged(self._queue, self._generation)
        try:
            self._source = self._make_source(kind, tagged,
                                             self.settings.ble_address)
        except Exception as exc:
            with self._lock:
                self._status, self._error = "error", str(exc)
            raise LiveError(str(exc))
        # Made here, on this thread, because SessionLogger's connection may
        # only ever be used from the thread that opened it.
        self._logger = self._make_logger(kind, tagged)
        self._source.start()
        return self.snapshot()

    def _op_start(self, label: Optional[str]) -> dict:
        if self._session is not None:
            raise LiveError("a session is already running")
        if self._status != "connected":
            raise LiveError("nothing is connected yet; wait for the strap or "
                            "watch to connect, then start")
        if self._logger is None or not self._logger.available:
            raise LiveError("sessions can't be saved: {}".format(
                getattr(self._logger, "error", None) or "no database"))
        key = self._logger.start_session(label=label,
                                         device_name=self._device_name,
                                         device_address=self._device_address)
        if key is None:
            raise LiveError(self._error or "the session could not be started")
        with self._lock:
            self._session = {"label": label, "started": self._clock(),
                             "started_iso": now_iso(), "n": 0, "sum": 0,
                             "min": None, "max": None}
        return self.snapshot()

    def _op_stop(self) -> dict:
        if self._session is not None:
            if self._logger is not None:
                self._logger.end_session()
            with self._lock:
                self._session = None
        return self.snapshot()

    def _teardown(self) -> None:
        """Stop whatever is running; later messages from it are ignored."""
        self._op_stop()
        if self._source is not None:
            try:
                self._source.stop()
            except Exception:
                log.exception("stopping the live source failed")
            self._source = None
        if self._logger is not None:
            try:
                self._logger.close()
            except Exception:
                log.exception("closing the session logger failed")
            self._logger = None
        self._generation += 1

    def _handle(self, message: dict) -> None:
        if message.get("_gen") != self._generation:
            return                      # from a source that's been replaced
        kind = message.get("type")
        if kind == "status":
            status = message.get("status") or self._status
            with self._lock:
                self._status = status
                self._message = message.get("message")
                if status == "connected":
                    self._device_name = message.get("device_name")
                    self._device_address = message.get("device_address")
                    self._error = None
                else:
                    self._bpm = None
        elif kind == "sample":
            hr = int(message["hr"])
            now = self._clock()
            with self._lock:
                self._bpm = hr
                self._points.append((now, hr))
                self._prune_locked(now)
                s = self._session
                if s is not None:
                    s["n"] += 1
                    s["sum"] += hr
                    s["min"] = hr if s["min"] is None else min(s["min"], hr)
                    s["max"] = hr if s["max"] is None else max(s["max"], hr)
            if self._session is not None and self._logger is not None:
                self._logger.log_sample(message)
        elif kind == "error":
            with self._lock:
                self._error = message.get("message")

    def _prune(self) -> None:
        with self._lock:
            self._prune_locked(self._clock())

    def _prune_locked(self, now: float) -> None:
        cutoff = now - WINDOW_SEC
        while self._points and self._points[0][0] < cutoff:
            self._points.popleft()
