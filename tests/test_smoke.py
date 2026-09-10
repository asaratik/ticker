"""
Import-only smoke tests -- catch syntax/import breakage across every module
without needing real hardware, a network peer, or an actual Tk window
(which would need a display and isn't created at import time).
"""


def test_hr_source_imports():
    import hr_source
    assert hasattr(hr_source, "HRSource")
    assert hasattr(hr_source, "create_source")


def test_ble_source_imports():
    import ble_source
    assert hasattr(ble_source, "BLEHRSource")
    assert hasattr(ble_source, "parse_hr_measurement")


def test_http_source_imports():
    import http_source
    assert hasattr(http_source, "HTTPHRSource")


def test_storage_imports():
    import storage
    assert hasattr(storage, "AsyncSessionStore")


def test_ui_app_imports_without_creating_a_window():
    from ticker.ui import app
    assert hasattr(app, "HRApp")
    assert hasattr(app, "main")
