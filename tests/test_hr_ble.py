"""
Unit tests for the Heart Rate Measurement characteristic parser and the
streamer's start/stop lifecycle. Both are hardware-free: the parser is pure
bit-twiddling per the Bluetooth SIG spec, and the lifecycle tests never get
as far as touching a BLE adapter.
"""

import queue

import pytest

from hr_ble import HRStreamer, parse_hr_measurement


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
    HRStreamer(queue.Queue()).stop()  # must not raise or block


def test_stop_immediately_after_start_shuts_down(monkeypatch):
    """stop() called before the background thread has published its loop and
    stop event must still shut it down -- this is the launch-then-close path,
    and it used to signal nothing and just block on the join.
    """
    streamer = HRStreamer(queue.Queue(), scan_timeout=0.01, reconnect_delay=0.01)

    async def never_finds_anything():
        return None

    monkeypatch.setattr(streamer, "_find_device", never_finds_anything)

    streamer.start()
    streamer.stop()

    assert not streamer._thread.is_alive()


def test_stop_is_idempotent(monkeypatch):
    streamer = HRStreamer(queue.Queue(), scan_timeout=0.01, reconnect_delay=0.01)

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

    streamer = HRStreamer(queue.Queue(), scan_timeout=30, reconnect_delay=30)
    monkeypatch.setattr(streamer, "_find_device", slow_scan)

    streamer.start()
    started = time.monotonic()
    streamer.stop()
    elapsed = time.monotonic() - started

    assert not streamer._thread.is_alive()
    assert scan_cancelled == [True]
    assert elapsed < 5, f"stop() took {elapsed:.1f}s -- the scan wasn't cancelled"
