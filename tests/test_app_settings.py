"""
Tests for the settings file the app keeps beside the database.

The file is a convenience, so every way it can be missing or mangled has
to come out as the defaults rather than a failure to start -- and the
environment has to beat it, so a deployment can pin what the page may not
change.
"""

import json

import pytest

from ticker.app.settings import Settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    monkeypatch.delenv("HRM_SOURCE", raising=False)
    monkeypatch.delenv("HRM_DEVICE_ADDRESS", raising=False)


def test_no_file_means_the_defaults(tmp_path):
    settings = Settings(tmp_path / "settings.json")
    assert settings.live_source == "off"
    assert settings.ble_address is None
    assert not settings.live_source_pinned


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"live_source": "ant"}'])
def test_a_mangled_file_means_the_defaults(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")
    assert Settings(path).live_source == "off"


def test_an_update_survives_a_restart(tmp_path):
    path = tmp_path / "nested" / "settings.json"
    Settings(path).update(live_source="ble", ble_address="AA:BB")
    again = Settings(path)
    assert (again.live_source, again.ble_address) == ("ble", "AA:BB")
    assert not (tmp_path / "nested" / "settings.json.tmp").exists()


def test_unknown_keys_on_disk_are_ignored_and_unknown_updates_refused(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"live_source": "http", "future": 1}), encoding="utf-8")
    settings = Settings(path)
    assert settings.live_source == "http"
    with pytest.raises(KeyError):
        settings.update(future=2)


def test_the_environment_wins(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    Settings(path).update(live_source="http", ble_address="AA:BB")
    monkeypatch.setenv("HRM_SOURCE", "BLE")
    monkeypatch.setenv("HRM_DEVICE_ADDRESS", "CC:DD")
    settings = Settings(path)
    assert settings.live_source == "ble" and settings.live_source_pinned
    assert settings.ble_address == "CC:DD"


def test_the_ask_box_defaults_to_ollama_here_with_no_model(tmp_path, monkeypatch):
    monkeypatch.delenv("TICKER_LLM_URL", raising=False)
    monkeypatch.delenv("TICKER_LLM_MODEL", raising=False)
    settings = Settings(tmp_path / "settings.json")
    assert settings.llm_url == "http://127.0.0.1:11434"
    assert settings.llm_model == "" and not settings.llm_pinned
    settings.update(llm_url="http://127.0.0.1:1234/v1", llm_model="qwen3:14b")
    again = Settings(tmp_path / "settings.json")
    assert (again.llm_url, again.llm_model) == ("http://127.0.0.1:1234/v1", "qwen3:14b")


def test_the_environment_pins_the_ask_model(tmp_path, monkeypatch):
    monkeypatch.setenv("TICKER_LLM_MODEL", "llama3.1:8b")
    settings = Settings(tmp_path / "settings.json")
    assert settings.llm_model == "llama3.1:8b" and settings.llm_pinned
