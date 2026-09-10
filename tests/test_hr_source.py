"""
Tests for the source abstraction itself: the factory that turns
config.HR_SOURCE into an object, and the shared message helpers every
source emits through.

Constructing a source doesn't start it, so nothing here touches a
Bluetooth adapter or binds a port.
"""

import queue

import pytest

import ble_source
import config
import hr_source
import http_source


def test_factory_builds_a_ble_source():
    source = hr_source.create_source(queue.Queue(), "ble")
    assert isinstance(source, ble_source.BLEHRSource)


def test_factory_builds_an_http_source_configured_from_config(monkeypatch):
    monkeypatch.setattr(config, "HTTP_PORT", 9999)
    monkeypatch.setattr(config, "HTTP_TOKEN", "abc")

    source = hr_source.create_source(queue.Queue(), "http")

    assert isinstance(source, http_source.HTTPHRSource)
    assert source.port == 9999
    assert source.token == "abc"


def test_factory_falls_back_to_the_configured_source(monkeypatch):
    monkeypatch.setattr(config, "HR_SOURCE", "http")
    assert isinstance(hr_source.create_source(queue.Queue()),
                      http_source.HTTPHRSource)


def test_source_names_are_case_and_space_insensitive():
    assert isinstance(hr_source.create_source(queue.Queue(), "  HTTP "),
                      http_source.HTTPHRSource)


def test_an_unknown_source_says_what_the_valid_ones_are():
    """A typo in HRM_SOURCE is a config mistake, and the message is the only
    place the user finds out what they should have typed.
    """
    with pytest.raises(ValueError) as excinfo:
        hr_source.create_source(queue.Queue(), "carrier-pigeon")

    message = str(excinfo.value)
    assert "carrier-pigeon" in message
    for name in hr_source.SOURCE_NAMES:
        assert name in message


# -- the message protocol ----------------------------------------------

class _Source(hr_source.HRSource):
    def start(self):
        pass

    def stop(self):
        pass


def test_emitted_messages_have_the_shape_the_app_expects():
    out = queue.Queue()
    source = _Source(out)

    source.emit_status("connected", device_name="Strap", device_address="AA:BB")
    source.emit_sample(142, [812.5])
    source.emit_error("boom")

    status, sample, error = (out.get_nowait() for _ in range(3))
    assert status == {"type": "status", "status": "connected",
                      "device_name": "Strap", "device_address": "AA:BB",
                      "message": None}
    assert sample["type"] == "sample"
    assert sample["hr"] == 142
    assert sample["rr_intervals_ms"] == [812.5]
    assert error == {"type": "error", "message": "boom"}


def test_a_sample_without_rr_data_carries_an_empty_list():
    out = queue.Queue()
    _Source(out).emit_sample(70)
    assert out.get_nowait()["rr_intervals_ms"] == []


def test_timestamps_resolve_to_milliseconds():
    """RR intervals are sub-second: at second resolution two readings from a
    fast-notifying strap land on the same timestamp and can't be ordered.
    """
    stamp = hr_source.now_iso()
    assert stamp.endswith("+00:00")
    seconds_and_fraction = stamp.split("T")[1].split("+")[0]
    assert len(seconds_and_fraction.split(".")[1]) == 3
