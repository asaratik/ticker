"""Native controls, real temporary data, lifecycle and thread-bound commands."""

import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QObject, QProcess, Signal
from PySide6.QtGui import QCloseEvent, QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from ticker.app import main as cli
from ticker.app.desktop import Instance
from ticker.app.desktop_backend import Controller, Worker
from ticker.app.desktop_data import Reader, chart_points, display_value
from ticker.app.desktop_window import Window
from ticker.app.runtime import Runtime
from ticker.app.settings import Settings
from ticker.db import store
from ticker.model import iso_utc, now_utc


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication(["ticker-tests"])
    app.setQuitOnLastWindowClosed(False)
    if sys.platform == "win32" and os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        # The headless plugin has no Windows font discovery. Real desktop
        # launches use the platform plugin and its normal system fonts.
        font = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts/segoeui.ttf"
        font_id = QFontDatabase.addApplicationFont(str(font))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            app.setFont(QFont(families[0], 10))
    return app


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        # Qt's C++ test wait can hold the GIL on Windows. Give the runtime's
        # Python writer/live threads time to run between GUI event pumps.
        time.sleep(0.01)
    raise AssertionError("Native state did not settle")


@pytest.fixture
def data(tmp_path):
    path = tmp_path / "health.sqlite3"
    conn = store.connect(path)
    reader = Reader(path)
    yield path, conn, reader
    reader.close()
    conn.close()


def observation(conn, sid, metric, value, instant=None):
    instant = instant or now_utc()
    mid = conn.execute("SELECT id FROM metrics WHERE name = ?", (metric,)).fetchone()[0]
    conn.execute("INSERT INTO observations (source_id, metric_id, ts, value, ingested_at) "
                 "VALUES (?,?,?,?,?)", (sid, mid, iso_utc(instant), value, iso_utc(now_utc())))


def test_latest_measurements_have_real_values_sources_and_dates(data):
    _, conn, reader = data
    sid = store.ensure_source(conn, "pull", "oura", "My ring")
    observation(conn, sid, "resting_heart_rate_bpm", 56)
    observation(conn, sid, "hrv_rmssd_ms", 0)
    rows = reader.latest()
    assert rows[0]["value"] == 56
    assert rows[0]["source"] == "My ring" and rows[0]["timestamp"]
    assert rows[1]["value"] == 0
    assert rows[2]["value"] is None
    assert display_value(0, "ms") == "0 ms"
    assert display_value(None, "bpm") == "—"
    assert display_value(7 * 3600 + 24 * 60, "s") == "7h 24m"


def test_trends_never_silently_blend_two_sources(data, monkeypatch):
    _, conn, reader = data
    end = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("ticker.app.desktop_data.now_utc", lambda: end)
    one = store.ensure_source(conn, "pull", "oura", "Ring")
    two = store.ensure_source(conn, "pull", "garmin", "Watch")
    # Queries use a half-open window and millisecond timestamps. Samples at
    # wall-clock "now" can fall exactly on the excluded end on fast runners.
    instant = end - timedelta(seconds=1)
    observation(conn, one, "heart_rate_bpm", 60, instant)
    observation(conn, two, "heart_rate_bpm", 100, instant)
    assert len(reader.sources("heart_rate_bpm")) == 2
    result = reader.series("heart_rate_bpm", one, 7)
    assert result["source"] == "Ring" and result["stats"]["mean"] == 60
    assert result["stats"]["n"] == 1
    assert chart_points(result)[0][1] == 60
    other = reader.series("heart_rate_bpm", two, 7)
    assert other["source"] == "Watch" and other["stats"]["mean"] == 100
    assert other["stats"]["n"] == 1


def test_native_reader_cannot_write_to_health_data(data):
    with data[2].db.session() as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM sources")


def test_chart_keeps_time_gaps():
    points = chart_points({"rows": [["2026-10-01T00:00:00Z", 60],
                                    ["2026-10-01T01:00:00Z", 70]]})
    assert points[1][0] - points[0][0] == 3600


def test_default_and_alias_launch_the_native_app(monkeypatch):
    from ticker.app import desktop
    called = []
    monkeypatch.setattr(desktop, "main", lambda argv: called.append(argv) or 0)
    assert cli.main([]) == 0
    assert cli.main(["desktop", "--port", "8123"]) == 0
    assert called == [[], ["--port", "8123"]]


class FakeController(QObject):
    ready = Signal()
    snapshot = Signal(dict)
    live = Signal(dict)
    result = Signal(str, object)
    error = Signal(str, str)
    stopped = Signal()

    def __init__(self):
        super().__init__()
        self.calls = []
        self.closing = False
        self.thread = type("ThreadState", (), {"isRunning": lambda self: False})()

    def submit(self, name, **args):
        self.calls.append((name, args))

    def shutdown(self):
        self.closing = True


@pytest.fixture
def window(qt_app, monkeypatch):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: False)
    controller = FakeController()
    win = Window(controller)
    win.show()
    controller.ready.emit()
    yield win, controller
    win.poll.stop()
    win.tray.hide()
    win._closing = True
    win.close()
    win.deleteLater()
    qt_app.processEvents()


def test_first_launch_has_one_window_and_honest_empty_state(window):
    win, controller = window
    controller.snapshot.emit({"metrics": [], "sources": [], "latest": []})
    assert win.welcome.isVisible()
    assert not win.summaries.isVisible()
    assert win.nav.count() == 6 and win.pages.count() == 6
    assert not win.record_button.isEnabled()
    assert win.grab().isNull() is False


def test_toolbar_navigates_without_opening_a_browser(window, monkeypatch):
    from ticker.app import main
    monkeypatch.setattr(main.webbrowser, "open", lambda *args: pytest.fail("Unexpected browser"))
    win, controller = window
    for index in range(6):
        win.nav.setCurrentIndex(index)
        assert win.pages.currentIndex() == index
    assert ("sessions", {}) in controller.calls
    assert ("ask_config", {}) in controller.calls


def test_connection_rows_offer_native_sync_and_disconnect_actions(window):
    win, controller = window
    win.render_accounts({"sources": [{"id": 3, "name": "Ring", "vendor": "oura",
                                      "type": "cloud sync", "enabled": True}]})
    actions = win.accounts.cellWidget(0, 4).findChildren(__import__(
        "PySide6.QtWidgets", fromlist=["QPushButton"]).QPushButton)
    assert [action.text() for action in actions] == ["Sync", "Disconnect"]
    actions[0].click()
    assert controller.calls[-1] == ("sync", {"source_id": 3})


def test_late_trend_response_does_not_replace_current_selection(window):
    win, _ = window
    win.metric_combo.blockSignals(True)
    win.metric_combo.addItem("Heart rate", "heart_rate_bpm")
    win.metric_combo.blockSignals(False)
    win.source.addItem("Ring", 1)
    win.trend.set_points([(1, 60)])
    win.on_result("series", {"selection": {"metric": "heart_rate_bpm", "source_id": 2, "days": 1},
                              "rows": [["2026-10-01T00:00:00Z", 100]]})
    assert win.trend.points == [(1.0, 60.0)]


def test_menu_quit_boolean_does_not_bypass_recording_confirmation(window, monkeypatch):
    win, _ = window
    win.live_state = {"session": {"label": "Recording"}}
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    win.request_quit(False)  # QAction.triggered(bool) must not mean confirm=False.
    assert not win._closing


def test_completed_shutdown_accepts_native_close_event(window):
    win, _ = window
    win._closing = True
    event = QCloseEvent()
    win.closeEvent(event)
    assert event.isAccepted()


def test_no_background_preference_without_a_tray(window):
    win, _ = window
    win.on_snapshot({"metrics": [], "sources": [], "desktop": {"keep_running": True}})
    assert not win.keep_running.isEnabled()


def test_rapid_settings_changes_keep_the_latest_choice(window):
    win, controller = window
    win.submit("settings", desktop_appearance="Light")
    win.submit("settings", desktop_appearance="Dark")
    assert controller.calls[-2:] == [
        ("settings", {"desktop_appearance": "Light"}),
        ("settings", {"desktop_appearance": "Dark"})]


def test_stopped_backend_disables_actions_and_preserves_error(window):
    win, controller = window
    controller.error.emit("startup", "Test failure")
    controller.stopped.emit()
    assert not win._ready and not win.pages.isEnabled()
    assert win.notice.text() == "Startup: Test failure"


def test_packaged_smoke_does_not_write_to_the_default_data_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["Ticker.exe", "--smoke-test", "result.json"])
    monkeypatch.setattr(cli.tconfig, "DB_PATH", tmp_path / "untouched" / "ticker.sqlite3")
    monkeypatch.setattr(cli, "main", lambda: 0)
    assert cli.gui_main() == 0
    assert not (tmp_path / "untouched").exists()


def test_native_smoke_reports_startup_failure_instead_of_a_crash_dialog(tmp_path, monkeypatch):
    import json
    from ticker.app import desktop
    def fail(_):
        raise ImportError("Test missing runtime")
    monkeypatch.setattr(desktop, "smoke", fail)
    result = tmp_path / "result.json"
    assert cli.smoke_main([str(result)]) == 1
    assert json.loads(result.read_text()) == {"ok": False, "error": "Test missing runtime"}


def test_settings_survive_restart_and_reject_invalid_appearance(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path)
    settings.update(desktop_keep_running=True, desktop_appearance="Dark")
    reopened = Settings(path)
    assert reopened.desktop_keep_running and reopened.desktop_appearance == "Dark"
    reopened.update(desktop_appearance="unsupported")
    assert Settings(path).desktop_appearance == "System"


def test_single_instance_activates_existing_window(qt_app, tmp_path):
    path = tmp_path / "health.sqlite3"
    first, second = Instance(path), Instance(path)
    activated = []
    process = QProcess()
    try:
        assert first.acquire()
        assert not second.acquire()
        first.server.newConnection.connect(lambda: first.receive(lambda: activated.append(True)))
        # A second launch is a separate process; blocking QLocalSocket waits
        # in a Python thread can starve this process's GUI of the GIL.
        process.start(sys.executable, ["-c", "from PySide6.QtCore import QCoreApplication; "
            "from ticker.app.desktop import Instance; import sys; "
            "app=QCoreApplication([]); "
            "sys.exit(0 if Instance(sys.argv[1]).activate_existing() else 1)", str(path)])
        wait_for(lambda: process.state() == QProcess.ProcessState.NotRunning, timeout=10)
        assert process.exitCode() == 0, bytes(process.readAllStandardError()).decode()
        assert activated == [True]
    finally:
        if process.state() != QProcess.ProcessState.NotRunning:
            process.kill()
            process.waitForFinished(2000)
        second.close()
        first.close()
    third = Instance(path)
    try:
        assert third.acquire()
    finally:
        third.close()


def test_different_databases_have_distinct_instance_guards(qt_app, tmp_path):
    first, second = Instance(tmp_path / "one.db"), Instance(tmp_path / "two.db")
    try:
        assert first.name != second.name
        assert first.acquire() and second.acquire()
    finally:
        first.close()
        second.close()


def test_native_backup_refuses_existing_files(data, qt_app, tmp_path):
    worker = Worker(lambda: None)
    worker.runtime = type("Runtime", (), {"db_path": data[0]})()
    existing = tmp_path / "existing.sqlite3"
    existing.write_bytes(b"keep me")
    with pytest.raises(ValueError, match="new backup filename"):
        worker.dispatch("backup", {"path": str(existing)})
    assert existing.read_bytes() == b"keep me"
    with pytest.raises(ValueError):
        worker.dispatch("backup", {"path": str(data[0])})


class Sensor:
    def __init__(self, queue):
        self.queue = queue

    def start(self):
        self.queue.put({"type": "status", "status": "connected", "device_name": "Test sensor",
                        "device_address": "TEST", "message": None})

    def stop(self):
        pass

    def sample(self, value):
        self.queue.put({"type": "sample", "timestamp": iso_utc(now_utc()),
                        "hr": value, "rr_intervals_ms": []})


def test_native_recording_flows_into_real_history(qt_app, tmp_path, monkeypatch):
    monkeypatch.delenv("HRM_SOURCE", raising=False)
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: False)
    path = tmp_path / "native.sqlite3"
    Settings.beside(path).update(live_source="ble")
    sensors, errors = [], []

    def make_source(kind, queue, address):
        sensor = Sensor(queue)
        sensors.append(sensor)
        return sensor

    controller = Controller(lambda: Runtime(path, port=0, builders={}, make_source=make_source))
    controller.error.connect(lambda name, text: errors.append((name, text)))
    win = Window(controller)
    win.show()
    controller.start()
    try:
        wait_for(lambda: win._ready and win.live_state.get("status") == "connected")
        win.nav.setCurrentIndex(1)
        win.recording_name.setText("Native test recording")
        assert win.record_button.isEnabled(), (win.live_state, errors)
        win.record_button.click()
        try:
            wait_for(lambda: bool(win.live_state.get("session")))
        except AssertionError:
            pytest.fail(str((win.live_state, errors, win.pending)))
        sensors[0].sample(72)
        wait_for(lambda: win.live_state.get("session", {}).get("n", 0) > 0)
        win.record_button.click()
        wait_for(lambda: win.live_state.get("session") is None)
        win.nav.setCurrentIndex(2)
        wait_for(lambda: win.history.rowCount() > 0)
        assert win.history.item(0, 0).text() == "Native test recording"
        wait_for(lambda: "Native test recording" in win.history_detail.text())
        assert win._selected_session is not None
        assert not errors
        screenshot = os.environ.get("TICKER_DESKTOP_SCREENSHOT")
        if screenshot:
            win.grab().save(screenshot)
    finally:
        win.poll.stop()
        controller.shutdown()
        wait_for(lambda: not controller.thread.isRunning(), timeout=15)
        win._closing = True
        win.tray.hide()
        win.close()
        win.deleteLater()
        qt_app.processEvents()
    # The worker and all readers have released the file, including on Windows.
    path.unlink()
