"""
Unit tests for the Heart Rate Measurement characteristic parser and the
streamer's start/stop lifecycle. Both are hardware-free: the parser is pure
bit-twiddling per the Bluetooth SIG spec, and the lifecycle tests never get
as far as touching a BLE adapter.
"""

import asyncio
import queue

import pytest

import ble_source
from ble_source import BLEHRSource, parse_hr_measurement


def test_8bit_heart_rate_no_extra_flags():
    hr, energy, rr = parse_hr_measurement(bytearray([0x00, 72]))
    assert hr == 72
    assert energy is None
    assert rr == []


def test_16bit_heart_rate_with_single_rr_interval():
    # flags: bit0=1 (16-bit HR format), bit4=1 (RR-interval present)
    # hr=180 (0x00B4 little-endian), rr=1024 units (-> 1000.0 ms)
    data = bytearray([0x11, 0xB4, 0x00, 0x00, 0x04])
    hr, energy, rr = parse_hr_measurement(data)
    assert hr == 180
    assert energy is None
    assert rr == [1000.0]


def test_multiple_rr_intervals():
    # flags: bit4=1 (RR present), 8-bit HR
    # two RR values: 512 units (500.0 ms) and 1024 units (1000.0 ms)
    data = bytearray([0x10, 70, 0x00, 0x02, 0x00, 0x04])
    hr, energy, rr = parse_hr_measurement(data)
    assert hr == 70
    assert rr == [500.0, 1000.0]


def test_energy_expended_flag():
    # flags: bit3=1 (energy expended present), 8-bit HR, no RR
    data = bytearray([0x08, 65, 0x0A, 0x00])  # energy = 10
    hr, energy, rr = parse_hr_measurement(data)
    assert hr == 65
    assert energy == 10
    assert rr == []


def test_energy_and_rr_together():
    # flags: bit3=1 (energy), bit4=1 (RR), 8-bit HR
    data = bytearray([0x18, 90, 0x64, 0x00, 0x00, 0x02])  # energy=100, rr=512 (500ms)
    hr, energy, rr = parse_hr_measurement(data)
    assert hr == 90
    assert energy == 100
    assert rr == [500.0]


# Truncated packets must raise rather than slice past the end and return a
# plausible-looking but wrong bpm.

@pytest.mark.parametrize("data", [
    bytearray([]),                    # nothing at all
    bytearray([0x00]),                # flags but no heart rate
    bytearray([0x01, 0xB4]),          # 16-bit HR flagged, only one byte of it
    bytearray([0x08, 65, 0x0A]),      # energy flagged, only one byte of it
])
def test_truncated_packets_raise(data):
    with pytest.raises(ValueError):
        parse_hr_measurement(data)


def test_rr_parsing_ignores_a_trailing_odd_byte():
    # One complete RR value (512 units -> 500ms) plus a stray byte: the
    # complete value is kept and the partial one dropped, not misread.
    data = bytearray([0x10, 70, 0x00, 0x02, 0xFF])
    hr, _energy, rr = parse_hr_measurement(data)
    assert hr == 70
    assert rr == [500.0]


# -- streamer lifecycle ------------------------------------------------

def test_stop_without_start_is_a_no_op():
    BLEHRSource(queue.Queue()).stop()  # must not raise or block


def test_stop_immediately_after_start_shuts_down(monkeypatch):
    """stop() called before the background thread has published its loop and
    stop event must still shut it down -- this is the launch-then-close path,
    and it used to signal nothing and just block on the join.
    """
    streamer = BLEHRSource(queue.Queue(), scan_timeout=0.01, reconnect_delay=0.01)

    async def never_finds_anything():
        return None

    monkeypatch.setattr(streamer, "_find_device", never_finds_anything)

    streamer.start()
    streamer.stop()

    assert not streamer._thread.is_alive()


def test_stop_is_idempotent(monkeypatch):
    streamer = BLEHRSource(queue.Queue(), scan_timeout=0.01, reconnect_delay=0.01)

    async def never_finds_anything():
        return None

    monkeypatch.setattr(streamer, "_find_device", never_finds_anything)

    streamer.start()
    streamer.stop()
    streamer.stop()  # loop is closed by now -- must not raise


def test_stop_interrupts_a_long_scan(monkeypatch):
    """A scan in flight has to be cancellable. Otherwise closing the window
    blocks the UI thread for the full scan timeout, which is the normal case
    whenever no strap is connected.
    """
    import asyncio
    import time

    scan_cancelled = []

    async def slow_scan():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            scan_cancelled.append(True)
            raise
        return None

    streamer = BLEHRSource(queue.Queue(), scan_timeout=30, reconnect_delay=30)
    monkeypatch.setattr(streamer, "_find_device", slow_scan)

    streamer.start()
    started = time.monotonic()
    streamer.stop()
    elapsed = time.monotonic() - started

    assert not streamer._thread.is_alive()
    assert scan_cancelled == [True]
    assert elapsed < 5, f"stop() took {elapsed:.1f}s -- the scan wasn't cancelled"


# -- device selection --------------------------------------------------

class _FakeAdv:
    def __init__(self, service_uuids):
        self.service_uuids = service_uuids


class _FakeDevice:
    def __init__(self, name, address):
        self.name = name
        self.address = address


def _patch_scanner(monkeypatch, *, by_address, discovered):
    """Replace both BleakScanner lookups. `discovered` is a list of devices
    returned by a full scan, all advertising the Heart Rate Service.
    """
    async def find_device_by_address(address, timeout=None):
        return by_address

    async def discover(timeout=None, return_adv=False):
        adv = _FakeAdv([HR_UUID])
        return {d.address: (d, adv) for d in discovered}

    monkeypatch.setattr(ble_source.BleakScanner, "find_device_by_address",
                        find_device_by_address)
    monkeypatch.setattr(ble_source.BleakScanner, "discover", discover)


HR_UUID = "0000180d-0000-1000-8000-00805f9b34fb"


def test_pinned_address_does_not_fall_back_to_another_device(monkeypatch):
    """A pinned address means that device or nothing.

    Falling through to a general scan would connect to whatever other strap
    happens to be in range -- the gym's, the neighbour's -- which is the one
    thing pinning an address exists to prevent.
    """
    someone_elses = _FakeDevice("Someone Else's Strap", "FF:FF:FF:FF:FF:FF")
    _patch_scanner(monkeypatch, by_address=None, discovered=[someone_elses])

    out = queue.Queue()
    source = BLEHRSource(out, address="AA:BB:CC:DD:EE:FF", scan_timeout=0.01)

    assert asyncio.run(source._find_device()) is None

    errors = [m for m in _drain(out) if m["type"] == "error"]
    assert errors, "a pinned device that isn't there should say so"
    assert "AA:BB:CC:DD:EE:FF" in errors[0]["message"]


def test_remembered_address_still_falls_back_to_scanning(monkeypatch):
    """The address remembered from a previous discovery is only a hint.

    _main() overwrites self.address to make reconnects cheap; that must not
    silently turn into a pin, or a strap that changes address (or is swapped
    out) would never be found again.
    """
    other = _FakeDevice("Some Strap", "11:22:33:44:55:66")
    _patch_scanner(monkeypatch, by_address=None, discovered=[other])

    source = BLEHRSource(queue.Queue(), scan_timeout=0.01)  # no pin
    source.address = "AA:BB:CC:DD:EE:FF"  # as _main does after a discovery

    assert asyncio.run(source._find_device()) is other


def test_pinned_address_is_used_when_present(monkeypatch):
    pinned = _FakeDevice("My Strap", "AA:BB:CC:DD:EE:FF")

    def discover_must_not_run(*args, **kwargs):
        raise AssertionError("scanned even though the pinned device was found")

    async def find_device_by_address(address, timeout=None):
        return pinned

    monkeypatch.setattr(ble_source.BleakScanner, "find_device_by_address",
                        find_device_by_address)
    monkeypatch.setattr(ble_source.BleakScanner, "discover", discover_must_not_run)

    source = BLEHRSource(queue.Queue(), address="AA:BB:CC:DD:EE:FF", scan_timeout=0.01)
    assert asyncio.run(source._find_device()) is pinned


def _drain(q: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out
