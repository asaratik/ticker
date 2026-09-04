"""
config.py resolves DB_PATH/FONT_FAMILY from the environment and sys.platform
at import time, so these tests reload the module under a patched
environment rather than relying on the one import done at collection time.
An autouse fixture reloads config fresh after every test in this file so a
patched sys.platform/env var never leaks into a test that runs after it.
"""

import importlib
from pathlib import Path

import pytest

import config


@pytest.fixture(autouse=True)
def _restore_real_config():
    yield
    # monkeypatch has already undone its patches by the time this runs, so
    # this reload reflects the real OS/environment again.
    importlib.reload(config)


def test_db_path_defaults_to_a_per_user_app_data_folder(monkeypatch):
    monkeypatch.delenv("HRM_DB_PATH", raising=False)
    reloaded = importlib.reload(config)
    assert reloaded.DB_PATH.name == "hrm_data.sqlite3"
    assert "Ticker" in str(reloaded.DB_PATH)


def test_data_dir_follows_each_os_convention(monkeypatch):
    # _default_data_dir() reads sys.platform at call time (no reload
    # needed), so this exercises all three branches regardless of which OS
    # is actually running the test suite.
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    win_dir = config._default_data_dir()
    assert win_dir == Path(r"C:\Users\someone\AppData\Local") / "Ticker"

    monkeypatch.setattr(config.sys, "platform", "darwin")
    mac_dir = config._default_data_dir()
    assert mac_dir == Path.home() / "Library" / "Application Support" / "Ticker"

    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    linux_dir = config._default_data_dir()
    assert linux_dir == Path.home() / ".local" / "share" / "Ticker"

    monkeypatch.setenv("XDG_DATA_HOME", "/custom/xdg")
    linux_dir_xdg = config._default_data_dir()
    assert linux_dir_xdg == Path("/custom/xdg") / "Ticker"


def test_db_path_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom.sqlite3"
    monkeypatch.setenv("HRM_DB_PATH", str(custom))
    reloaded = importlib.reload(config)
    assert reloaded.DB_PATH == custom


def test_device_address_defaults_to_none(monkeypatch):
    monkeypatch.delenv("HRM_DEVICE_ADDRESS", raising=False)
    reloaded = importlib.reload(config)
    assert reloaded.DEVICE_ADDRESS is None


def test_font_family_differs_by_platform(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "win32")
    reloaded = importlib.reload(config)
    assert reloaded.FONT_FAMILY == "Segoe UI"

    monkeypatch.setattr(config.sys, "platform", "darwin")
    reloaded = importlib.reload(config)
    assert reloaded.FONT_FAMILY == "Helvetica Neue"

    monkeypatch.setattr(config.sys, "platform", "linux")
    reloaded = importlib.reload(config)
    assert reloaded.FONT_FAMILY == "DejaVu Sans"
