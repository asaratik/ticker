"""
The BLE strap as a StreamSource.

This wraps ble_source.BLEHRSource rather than reimplementing it. That code
handles the things that are genuinely hard about BLE and took real hardware
to get right -- interrupting a multi-second scan on shutdown, refusing to
silently connect to the gym's strap when an address is pinned, surviving a
notification callback that raises inside bleak's own backend -- and all of
it is covered by tests/test_ble_source.py. Proving the source abstraction
against known-good code means keeping the known-good
code, not rewriting it under a new name.

So the adaptation is narrow and lives here:

* v1's message queue is a thread-safe queue.Queue, because the app's live
  monitor drains it from a plain thread. A StreamSource yields into asyncio. The bridge is
  _QueueBridge below -- it looks like the queue.Queue that HRSource expects,
  and hands each message to the consumer's event loop.
* one v1 'sample' message becomes several observations: one heart_rate_bpm,
  plus one rr_interval_ms per beat in the packet.
* v1's status strings become SourceHealth for the UI's source list.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, List, Optional

from ticker.model import Observation, parse_iso
from ticker.sources.hr_messages import observations_from_sample
from ticker.sources.base import SourceHealth

# What a Heart Rate Service device can tell us. Not every strap sends RR --
# it's an optional flag in the packet -- but the ones worth owning do.
CAPABILITIES = frozenset({"heart_rate_bpm", "rr_interval_ms"})

# v1 status strings that mean data is currently arriving.
_LIVE_STATES = frozenset({"connected"})


class _QueueBridge:
    """Looks like the queue.Queue HRSource writes to; feeds an asyncio queue.

    HRSource.emit_* calls .put() from the BLE thread, which is not the
    consumer's event loop thread, so every message crosses over via
    call_soon_threadsafe. Once the loop is closed -- the consumer went away
    first -- put() drops the message instead of raising into a callback the
    BLE code has no way to handle.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self.messages: "asyncio.Queue" = asyncio.Queue()

    def put(self, message: dict) -> None:
        try:
            self._loop.call_soon_threadsafe(self.messages.put_nowait, message)
        except RuntimeError:
            pass    # loop already closed; nothing left to deliver to


class BleStreamSource:
    """Streams heart rate and RR intervals from a Bluetooth strap."""

    vendor = "ble"

    def __init__(self, address: Optional[str] = None,
                 scan_timeout: Optional[float] = None,
                 reconnect_delay: Optional[float] = None,
                 display_name: str = "BLE strap"):
        self.address = address
        self.display_name = display_name
        self._scan_timeout = scan_timeout
        self._reconnect_delay = reconnect_delay
        self._state = "idle"
        self._detail: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_sample = None
        self.device_name: Optional[str] = None
        self.device_address: Optional[str] = None

    # -- Source ----------------------------------------------------------

    def capabilities(self) -> "frozenset[str]":
        return CAPABILITIES

    def health(self) -> SourceHealth:
        """Never raises: this is called to render a UI row, and a source list
        that can crash the window is worse than one that says 'unknown'."""
        return SourceHealth(
            ok=self._state in _LIVE_STATES,
            state=self._state,
            detail=self._detail or self.device_name,
            last_success=self._last_sample,
            last_error=self._last_error,
        )

    # -- StreamSource ----------------------------------------------------

    async def stream(self) -> AsyncIterator[Observation]:
        """Yield observations as they arrive, reconnecting internally.

        Cancellation is the normal stop path: the finally block shuts the BLE
        thread down, and because that join can take a moment it runs in an
        executor rather than blocking the event loop.
        """
        loop = asyncio.get_running_loop()
        bridge = _QueueBridge(loop)
        source = self._make_source(bridge)
        source.start()
        self._state = "searching"
        try:
            while True:
                message = await bridge.messages.get()
                for observation in self._convert(message):
                    yield observation
        finally:
            self._state = "stopped"
            # stop() joins the BLE thread, which can block for as long as a
            # disconnect takes, so it goes to an executor rather than
            # stalling the loop. Shielded because the usual way we get here
            # is the consumer cancelling us: without it the cancellation
            # would tear down the shutdown too and leave the BLE thread
            # holding the adapter.
            try:
                await asyncio.shield(loop.run_in_executor(None, source.stop))
            except asyncio.CancelledError:
                pass

    def _make_source(self, out_queue):
        # Imported here, not at module scope: a machine running only pull
        # sources should not need bleak installed or a Bluetooth adapter
        # present. Same reasoning as hr_source.create_source.
        import config
        import ble_source

        return ble_source.BLEHRSource(
            out_queue,
            address=self.address if self.address is not None else config.DEVICE_ADDRESS,
            scan_timeout=(config.SCAN_TIMEOUT_SEC if self._scan_timeout is None
                          else self._scan_timeout),
            reconnect_delay=(config.RECONNECT_DELAY_SEC if self._reconnect_delay is None
                             else self._reconnect_delay),
        )

    # -- message conversion ----------------------------------------------

    def _convert(self, message: dict) -> List[Observation]:
        kind = message.get("type")
        if kind == "sample":
            return self._observations(message)
        if kind == "status":
            self._state = message.get("status") or self._state
            self._detail = message.get("message")
            if message.get("device_name"):
                self.device_name = message["device_name"]
            if message.get("device_address"):
                self.device_address = message["device_address"]
                # Remember it so a reconnect can skip the scan, exactly as
                # the v1 source does.
                self.address = self.address or message["device_address"]
        elif kind == "error":
            self._last_error = message.get("message")
        return []

    def _observations(self, message: dict) -> List[Observation]:
        # Shared with the app's live monitor, which drains the same protocol
        # from its own queue -- see ticker.sources.hr_messages.
        self._last_sample = parse_iso(message["timestamp"])
        return observations_from_sample(message)
