"""
Import-only smoke tests -- catch syntax/import breakage across every module
without needing real BLE hardware or creating an actual Tk window (which
would need a display and isn't exercised at import time).
"""


def test_hr_ble_imports():
    import hr_ble
    assert hasattr(hr_ble, "HRStreamer")
    assert hasattr(hr_ble, "parse_hr_measurement")


def test_storage_imports():
    import storage
    assert hasattr(storage, "AsyncSessionStore")


def test_hrm_app_imports_without_creating_a_window():
    import hrm_app
    assert hasattr(hrm_app, "HRApp")
    assert hasattr(hrm_app, "main")
