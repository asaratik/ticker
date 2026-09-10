"""
Pluggable heart rate sources.

A "source" is anything that produces heart rate readings and pushes them
onto a thread-safe queue.Queue: a BLE strap (ble_source), a Garmin watch
POSTing over the network (http_source), an ANT+ dongle one day. The app
drains that queue and never knows or cares which one it got.

Every source speaks the same message protocol (all dicts):

    {"type": "status", "status": "searching" | "connected" | "reconnecting"
                                  | "stopped",
     "device_name": str | None, "device_address": str | None,
     "message": str | None}

    {"type": "sample", "timestamp": <ISO8601 UTC str>, "hr": int,
     "rr_intervals_ms": [float, ...]}

    {"type": "error", "message": str}

"connected" and "reconnecting" mean different mechanics per source -- a live
GATT link for BLE, recent-vs-stale pushes for HTTP -- but they mean the same
thing to the UI: readings are, or aren't, currently arriving.

"message" is optional per-source text ("Waiting for a watch on
http://192.168.1.5:8787/hr") that the UI shows in place of its own generic
wording. Sources that have nothing useful to add leave it None.
"""

from __future__ import annotations

import queue
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import List, Optional

import config

SOURCE_NAMES = ("ble", "http")


def now_iso() -> str:
    """Current UTC time, ISO 8601, millisecond resolution.

    Milliseconds rather than seconds because RR intervals are sub-second:
    at second resolution two readings from a fast-notifying strap land on
    an identical timestamp and become impossible to order after the fact.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class HRSource(ABC):
    """Base class for anything that streams heart rate onto a queue.

    Subclasses do their work on their own thread(s) -- start() must return
    immediately, and stop() must be safe to call more than once, safe to
    call without a preceding start(), and must not block the caller (the UI
    thread, at window close) for more than a moment.
    """

    def __init__(self, out_queue: "queue.Queue"):
        self.out_queue = out_queue

    @abstractmethod
    def start(self) -> None:
        """Begin producing messages. Returns immediately."""

    @abstractmethod
    def stop(self) -> None:
        """Shut down and stop producing. Idempotent, and must not hang."""

    # -- message helpers (used by subclasses) -----------------------------

    def _emit(self, msg: dict) -> None:
        self.out_queue.put(msg)

    def emit_status(self, status: str, device_name: Optional[str] = None,
                    device_address: Optional[str] = None,
                    message: Optional[str] = None) -> None:
        self._emit({
            "type": "status", "status": status,
            "device_name": device_name, "device_address": device_address,
            "message": message,
        })

    def emit_sample(self, hr: int, rr_intervals_ms: Optional[List[float]] = None,
                    timestamp: Optional[str] = None) -> None:
        self._emit({
            "type": "sample",
            "timestamp": timestamp or now_iso(),
            "hr": hr,
            "rr_intervals_ms": rr_intervals_ms or [],
        })

    def emit_error(self, message: str) -> None:
        self._emit({"type": "error", "message": message})


def create_source(out_queue: "queue.Queue", name: Optional[str] = None) -> HRSource:
    """Build the source named by `name`, defaulting to config.HR_SOURCE.

    The per-source imports are deliberately inside the branches: running on
    HTTP shouldn't require bleak to be installed or a Bluetooth adapter to
    exist, and vice versa. (PyInstaller still traces both, so a packaged
    build ships with either one usable.)
    """
    name = (name or config.HR_SOURCE).strip().lower()

    if name == "ble":
        import ble_source
        return ble_source.BLEHRSource(
            out_queue,
            address=config.DEVICE_ADDRESS,
            scan_timeout=config.SCAN_TIMEOUT_SEC,
            reconnect_delay=config.RECONNECT_DELAY_SEC,
        )

    if name == "http":
        import http_source
        return http_source.HTTPHRSource(
            out_queue,
            host=config.HTTP_HOST,
            port=config.HTTP_PORT,
            token=config.HTTP_TOKEN,
            sample_timeout=config.HTTP_SAMPLE_TIMEOUT_SEC,
        )

    raise ValueError(
        f"Unknown heart rate source {name!r}. "
        f"Set HRM_SOURCE to one of: {', '.join(SOURCE_NAMES)}."
    )
