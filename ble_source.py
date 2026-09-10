"""
BLE heart rate source for anything speaking the standard Heart Rate Service
(0x180D): chest straps (Garmin HRM-Dual/Pro/Fit/600, Polar, Wahoo...) and
Garmin watches with Broadcast Heart Rate switched on, which advertise the
same service and are read exactly the same way.

Runs its own asyncio event loop on a background thread and pushes messages
onto a thread-safe queue.Queue -- see hr_source for the message protocol --
so a GUI (or anything else) can consume them without touching asyncio.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Optional

from bleak import BleakClient, BleakScanner

from hr_source import HRSource

HEART_RATE_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HEART_RATE_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

RECONNECT_DELAY_SEC = 5
SCAN_TIMEOUT_SEC = 10.0

# Returned by _await_or_stop when shutdown interrupted the awaited work.
_STOPPED = object()


def parse_hr_measurement(data: bytearray):
    """Parse the Heart Rate Measurement characteristic per Bluetooth SIG spec.
    Returns (heart_rate_bpm, energy_expended_or_None, rr_intervals_ms_list).

    Raises ValueError on a packet too short for what its own flags claim --
    slicing past the end would otherwise silently yield a plausible-looking
    but wrong heart rate rather than an error.
    """
    if len(data) < 2:
        raise ValueError(f"packet too short ({len(data)} bytes)")

    flags = data[0]
    hr_format_16bit = flags & 0x1
    energy_expended_present = (flags >> 3) & 0x1
    rr_present = (flags >> 4) & 0x1

    offset = 1
    if hr_format_16bit:
        if len(data) < 3:
            raise ValueError("16-bit heart rate flagged but packet is too short")
        hr = int.from_bytes(data[1:3], byteorder="little")
        offset = 3
    else:
        hr = data[1]
        offset = 2

    energy_expended = None
    if energy_expended_present:
        if len(data) < offset + 2:
            raise ValueError("energy expended flagged but packet is too short")
        energy_expended = int.from_bytes(data[offset:offset + 2], byteorder="little")
        offset += 2

    rr_intervals_ms = []
    if rr_present:
        while offset + 1 < len(data):
            rr_raw = int.from_bytes(data[offset:offset + 2], byteorder="little")
            rr_intervals_ms.append(round(rr_raw / 1024.0 * 1000.0, 1))
            offset += 2

    return hr, energy_expended, rr_intervals_ms


class BLEHRSource(HRSource):
    """Connects to a BLE heart rate device and streams samples to a queue.

    Runs entirely on its own background thread (own asyncio event loop) so it
    can be driven from a synchronous GUI. Call start() once; call stop() to
    shut down cleanly. Automatically reconnects on disconnect.
    """

    def __init__(self, out_queue: "queue.Queue", address: Optional[str] = None,
                 scan_timeout: float = SCAN_TIMEOUT_SEC,
                 reconnect_delay: float = RECONNECT_DELAY_SEC):
        super().__init__(out_queue)
        self.address = address
        # A pinned address means "this device or nothing". self.address is
        # also overwritten with whatever we discovered, to make reconnects
        # cheap -- and that remembered value must stay a hint that falls
        # back to scanning, not a pin. Only the caller can pin.
        self._pinned = address is not None
        self.scan_timeout = scan_timeout
        self.reconnect_delay = reconnect_delay
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        # _loop/_stop_event are assigned on the background thread, so stop()
        # can't just read them -- a stop() racing a just-started thread would
        # see None for both and signal nothing. _ready marks them published.
        self._ready = threading.Event()

    def start(self):
        if self._thread is not None:
            return  # already started
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Signal the background thread to shut down, and wait for it.

        Safe to call more than once, and safe to call immediately after
        start(). Returns once the thread is done -- normally near-instantly,
        because everything it can block on races the stop event.
        """
        if self._thread is None:
            return  # never started
        self._ready.wait(timeout=5)
        loop, stop_event = self._loop, self._stop_event
        if loop is not None and stop_event is not None:
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                pass  # loop already finished and closed -- nothing to stop
        self._thread.join(timeout=10)

    # -- internals --------------------------------------------------------

    def _run(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            # Constructed after set_event_loop: on Python 3.9 asyncio.Event
            # binds to the current event loop at construction time.
            self._stop_event = asyncio.Event()
        finally:
            # Always publish, even if the setup above failed, so a caller
            # blocked in stop() isn't left waiting out the full timeout.
            self._ready.set()
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    async def _await_or_stop(self, coro):
        """Await `coro`, cancelling it as soon as stop() is called.

        Returns the coroutine's result, or the _STOPPED sentinel if the stop
        event won the race. Anything long-running (scanning, connecting,
        waiting on a disconnect) has to go through here, otherwise stop()
        can't interrupt it and the caller -- the UI thread, at window close
        -- is stuck waiting out a full scan timeout.

        The loser of the race is always awaited after being cancelled, so
        asyncio never reports a task destroyed while pending.
        """
        task = asyncio.ensure_future(coro)
        stop_task = asyncio.ensure_future(self._stop_event.wait())
        done, pending = await asyncio.wait(
            {task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if task in done:
            return task.result()
        return _STOPPED

    async def _find_device(self):
        if self.address:
            self.emit_status("searching", message=f"Looking for {self.address}…")
            device = await BleakScanner.find_device_by_address(
                self.address, timeout=self.scan_timeout
            )
            if device:
                return device
            if self._pinned:
                # Falling through to the general scan here would quietly
                # connect to whatever *other* heart rate device is in range
                # -- the gym's, the neighbour's -- which is the one thing
                # pinning an address is meant to prevent.
                self.emit_error(
                    f"Pinned device {self.address} not found. "
                    f"Unset HRM_DEVICE_ADDRESS to use any strap in range."
                )
                return None

        self.emit_status("searching")
        devices_and_adv = await BleakScanner.discover(
            timeout=self.scan_timeout, return_adv=True
        )
        for device, adv in devices_and_adv.values():
            service_uuids = [u.lower() for u in (adv.service_uuids or [])]
            if HEART_RATE_SERVICE_UUID in service_uuids:
                return device
        return None

    async def _sleep_or_stop(self, seconds: float):
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _main(self):
        while not self._stop_event.is_set():
            try:
                device = await self._await_or_stop(self._find_device())
            except Exception as exc:  # adapter missing, BLE turned off, etc.
                self.emit_error(f"Scan failed: {exc}")
                device = None
            if device is _STOPPED:
                break
            if device is None:
                if not self._pinned:  # _find_device already said why if pinned
                    self.emit_error("No heart rate monitor found nearby.")
                await self._sleep_or_stop(self.reconnect_delay)
                continue

            self.address = device.address  # remember for faster reconnects
            try:
                await self._stream_from(device)
            except Exception as exc:  # connection drop, adapter hiccup, etc.
                self.emit_error(f"Connection lost: {exc}")

            if self._stop_event.is_set():
                break
            self.emit_status("reconnecting", device_name=device.name,
                             device_address=device.address)
            await self._sleep_or_stop(self.reconnect_delay)

        self.emit_status("stopped")

    async def _stream_from(self, device):
        disconnected = asyncio.Event()

        def on_disconnect(_client):
            disconnected.set()

        # Deliberately not `async with`: connecting is itself a multi-second
        # operation that stop() has to be able to interrupt, which means the
        # connect has to go through _await_or_stop.
        client = BleakClient(device, disconnected_callback=on_disconnect)
        if await self._await_or_stop(client.connect()) is _STOPPED:
            await self._disconnect_quietly(client)
            return

        try:
            self.emit_status("connected", device_name=device.name,
                             device_address=device.address)

            def handle_notification(_sender, data: bytearray):
                try:
                    hr, _energy, rr_intervals = parse_hr_measurement(data)
                except Exception as exc:
                    # This runs inside bleak's own callback, where a raised
                    # exception is swallowed or merely logged by the backend
                    # and never reaches the app -- so report it ourselves
                    # and keep the stream running.
                    self.emit_error(f"Bad heart rate packet: {exc}")
                    return
                self.emit_sample(hr, rr_intervals)

            await client.start_notify(HEART_RATE_MEASUREMENT_UUID, handle_notification)
            await self._await_or_stop(disconnected.wait())

            if not disconnected.is_set():
                try:
                    await client.stop_notify(HEART_RATE_MEASUREMENT_UUID)
                except Exception:
                    pass
        finally:
            await self._disconnect_quietly(client)

    @staticmethod
    async def _disconnect_quietly(client):
        try:
            await client.disconnect()
        except Exception:
            pass  # already gone, adapter unplugged, etc. -- nothing to do
