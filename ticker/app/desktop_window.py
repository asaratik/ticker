"""Native Qt Widgets screens over the shared runtime command bridge."""

import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QStackedWidget, QStyle,
    QSystemTrayIcon, QTabBar, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from PySide6.QtGui import QDesktopServices

from ticker.app.desktop_chart import Chart
from ticker.app.desktop_data import chart_points, display_value, metric_label
from ticker.model import parse_iso


def label(text="", size=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if size:
        font = widget.font()
        font.setPointSize(size)
        widget.setFont(font)
    return widget


def button(text, callback, parent_layout=None):
    widget = QPushButton(text)
    widget.clicked.connect(callback)
    if parent_layout is not None:
        parent_layout.addWidget(widget)
    return widget


def stamp(value):
    if not value:
        return "Not yet"
    try:
        return parse_iso(value).astimezone().strftime("%b %d, %H:%M")
    except (ValueError, TypeError):
        return value


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    widget.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    widget.verticalHeader().hide()
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return widget


class Window(QMainWindow):
    PAGES = ("Overview", "Live recording", "History", "Connections", "Ask", "Settings")

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.state = {}
        self.live_state = {}
        self.last_live = 0
        self.pending = set()
        self._closing = False
        self._ready = False
        self._metric_names = []
        self._selected_session = None
        self._ask_id = None
        self._ask_history = []
        self._ask_config = {}
        self._device_dialog = None
        self._device_combo = None
        self.setWindowTitle("Ticker")
        self.resize(1050, 800)
        self.setMinimumSize(720, 520)
        self.setWindowIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        tray_menu = QMenu(self)
        tray_menu.addAction("Open Ticker", self.activate)
        tray_menu.addAction("Quit Ticker", self.request_quit)
        self.tray.setContextMenu(tray_menu)
        self.tray.setToolTip("Ticker")
        self.tray.activated.connect(self.tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)
        self.nav = QTabBar()
        self.nav.setAccessibleName("Ticker navigation")
        self.nav.setExpanding(True)
        for name in self.PAGES:
            self.nav.addTab(name)
        layout.addWidget(self.nav)
        self.notice = label("Starting Ticker…")
        self.notice.setAccessibleName("Application status")
        layout.addWidget(self.notice)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        for build in (self.overview_page, self.live_page, self.history_page,
                      self.connections_page, self.ask_page, self.settings_page):
            page = QWidget()
            body = QVBoxLayout(page)
            body.setContentsMargins(6, 6, 6, 6)
            body.setSpacing(16)
            build(body)
            body.addStretch()
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(page)
            self.pages.addWidget(scroll)
        self.setCentralWidget(central)
        self.nav.currentChanged.connect(self.navigate)
        self.pages.setEnabled(False)
        self.footer = label("Local data · starting")
        self.recording_indicator = label("")
        self.statusBar().addWidget(self.footer, 1)
        self.statusBar().addPermanentWidget(self.recording_indicator)
        self.menus()
        controller.ready.connect(self.on_ready)
        controller.snapshot.connect(self.on_snapshot)
        controller.live.connect(self.on_live)
        controller.result.connect(self.on_result)
        controller.error.connect(self.on_error)
        controller.stopped.connect(self.on_stopped)
        self.poll = QTimer(self)
        self.poll.setInterval(1000)
        self.poll.timeout.connect(self.poll_tasks)
        self.poll.start()

    def menus(self):
        file_menu = self.menuBar().addMenu("&File")
        import_action = QAction("Import health data…", self)
        import_action.setShortcut(QKeySequence.StandardKey.Open)
        import_action.triggered.connect(self.import_file)
        file_menu.addAction(import_action)
        file_menu.addAction("Back up data…", self.backup)
        file_menu.addSeparator()
        quit_action = QAction("Quit Ticker", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        quit_action.triggered.connect(self.request_quit)
        file_menu.addAction(quit_action)
        view_menu = self.menuBar().addMenu("&View")
        for i, name in enumerate(self.PAGES):
            action = QAction(name, self)
            action.setShortcut(QKeySequence("Ctrl+{}".format(i + 1)))
            action.triggered.connect(lambda checked=False, index=i: self.nav.setCurrentIndex(index))
            if name == "Settings":
                action.setMenuRole(QAction.MenuRole.PreferencesRole)
            view_menu.addAction(action)
        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction("Check for updates", lambda: self.submit("update"))
        about = QAction("About Ticker", self)
        about.setMenuRole(QAction.MenuRole.AboutRole)
        about.triggered.connect(lambda: QMessageBox.about(
            self, "Ticker", "Your health data in one local app.\n"
            "Native interface built with Qt for Python (PySide6), licensed under LGPLv3.\n"
            "https://www.qt.io/licensing/open-source-lgpl-obligations"))
        help_menu.addAction(about)

    def title(self, body, title, subtitle):
        body.addWidget(label(title, 21))
        body.addWidget(label(subtitle))

    def overview_page(self, body):
        self.title(body, "Overview", "Your latest available measurements, with their sources and dates.")
        self.welcome = QGroupBox("Welcome to Ticker")
        empty = QVBoxLayout(self.welcome)
        empty.addWidget(label("Connect a health account, pair a heart-rate sensor, or import an export."))
        actions = QHBoxLayout()
        button("Connect a source", lambda: self.nav.setCurrentIndex(3), actions)
        button("Import health data…", self.import_file, actions)
        empty.addLayout(actions)
        body.addWidget(self.welcome)
        self.summaries = QWidget()
        cards = QHBoxLayout(self.summaries)
        cards.setContentsMargins(0, 0, 0, 0)
        self.summary_labels = []
        for name in ("Resting heart rate", "Heart-rate variability", "Sleep duration"):
            card = QGroupBox(name)
            card_layout = QVBoxLayout(card)
            value, source = label("—", 24), label("No data yet")
            card_layout.addWidget(value)
            card_layout.addWidget(source)
            cards.addWidget(card)
            self.summary_labels.append((value, source))
        body.addWidget(self.summaries)
        chart_group = QGroupBox("Trends")
        chart_layout = QVBoxLayout(chart_group)
        selectors = QHBoxLayout()
        self.metric_combo = QComboBox()
        self.metric_combo.setAccessibleName("Trend metric")
        self.source = QComboBox()
        self.source.setAccessibleName("Trend source")
        self.range = QComboBox()
        self.range.setAccessibleName("Trend range")
        for name, days in (("Today", 1), ("7 days", 7), ("30 days", 30)):
            self.range.addItem(name, days)
        selectors.addWidget(self.metric_combo, 2)
        selectors.addWidget(self.source, 2)
        selectors.addWidget(self.range, 1)
        chart_layout.addLayout(selectors)
        self.trend = Chart()
        chart_layout.addWidget(self.trend)
        body.addWidget(chart_group)
        self.metric_combo.currentIndexChanged.connect(self.metric_changed)
        self.source.currentIndexChanged.connect(self.request_series)
        self.range.currentIndexChanged.connect(self.request_series)
        actions = QHBoxLayout()
        button("Start a recording", lambda: self.nav.setCurrentIndex(1), actions)
        button("Browse history", lambda: self.nav.setCurrentIndex(2), actions)
        body.addLayout(actions)

    def live_page(self, body):
        self.title(body, "Live recording", "Pair a sensor and record without leaving Ticker.")
        self.live_status = label("No sensor connected")
        body.addWidget(self.live_status)
        source_row = QHBoxLayout()
        self.live_source = QComboBox()
        self.live_source.setAccessibleName("Live heart-rate source")
        for title, value in (("Off", "off"), ("Bluetooth sensor", "ble"), ("Watch / HTTP", "http")):
            self.live_source.addItem(title, value)
        self.live_source.activated.connect(lambda index: self.submit(
            "live_source", source=self.live_source.itemData(index)))
        source_row.addWidget(self.live_source)
        button("Choose Bluetooth device…", self.choose_device, source_row)
        body.addLayout(source_row)
        self.bpm = label("— bpm", 44)
        body.addWidget(self.bpm)
        self.live_chart = Chart()
        body.addWidget(self.live_chart)
        recording = QHBoxLayout()
        self.recording_name = QLineEdit()
        self.recording_name.setAccessibleName("Recording name")
        self.recording_name.setPlaceholderText("Recording name (optional)")
        recording.addWidget(self.recording_name, 1)
        self.record_button = button("Start recording", self.record, recording)
        self.record_button.setEnabled(False)
        body.addLayout(recording)
        self.session_status = label("Not recording")
        body.addWidget(self.session_status)

    def history_page(self, body):
        self.title(body, "History", "Newest 50 saved sessions. Select one to inspect its data.")
        self.history = table(("Recording", "Source", "Started", "Minutes"))
        self.history.setAccessibleName("Saved recordings")
        self.history.setMinimumHeight(175)
        self.history.itemSelectionChanged.connect(self.session_selected)
        body.addWidget(self.history)
        self.history_detail = label("No saved sessions yet. Start a recording or import health data.")
        body.addWidget(self.history_detail)
        self.session_chart = Chart()
        body.addWidget(self.session_chart)
        self.export_button = button("Export heart-rate trace…", self.export_session)
        self.export_button.setEnabled(False)
        body.addWidget(self.export_button)

    def connections_page(self, body):
        self.title(body, "Connections", "Accounts, sensors, and imports in one place.")
        providers = QHBoxLayout()
        self.provider_buttons = {}
        for vendor in ("oura", "fitbit", "garmin"):
            self.provider_buttons[vendor] = button("Connect " + vendor.capitalize() + "…",
                lambda checked=False, vendor=vendor: self.connect_provider(vendor), providers)
        body.addLayout(providers)
        self.accounts = table(("Source", "Provider", "Status", "Last sync", "Actions"))
        self.accounts.setAccessibleName("Connected health sources")
        self.accounts.setMinimumHeight(190)
        body.addWidget(self.accounts)
        local = QHBoxLayout()
        button("Pair Bluetooth device…", self.choose_device, local)
        button("Import Apple Health / FIT…", self.import_file, local)
        body.addLayout(local)
        body.addWidget(label("Activity · activate a waiting sign-in to enter its verification code."))
        self.jobs = QListWidget()
        self.jobs.setAccessibleName("Sync and import activity")
        self.jobs.setMinimumHeight(100)
        self.jobs.itemActivated.connect(self.job_activated)
        body.addWidget(self.jobs)

    def ask_page(self, body):
        self.title(body, "Ask your data", "Answers use your saved data and your configured model.")
        self.model_status = label("Set up a local model to ask questions.")
        body.addWidget(self.model_status)
        button("Model settings…", self.model_settings, body)
        self.answer = QPlainTextEdit()
        self.answer.setReadOnly(True)
        self.answer.setAccessibleName("Answer and progress")
        self.answer.setPlaceholderText("Answers and their progress appear here.")
        self.answer.setMinimumHeight(190)
        body.addWidget(self.answer)
        self.question = QPlainTextEdit()
        self.question.setAccessibleName("Question about your data")
        self.question.setPlaceholderText("What would you like to know?")
        self.question.setMaximumHeight(100)
        body.addWidget(self.question)
        self.ask_button = button("Ask", self.ask, body)

    def settings_page(self, body):
        self.title(body, "Settings", "App behavior, privacy, and local data.")
        form = QFormLayout()
        self.appearance = QComboBox()
        self.appearance.addItems(("System", "Light", "Dark"))
        self.appearance.activated.connect(self.change_appearance)
        form.addRow("Appearance", self.appearance)
        self.keep_running = QCheckBox("Keep syncing when the window is closed")
        self.keep_running.setToolTip("Requires a usable system tray / menu-bar indicator.")
        self.keep_running.toggled.connect(lambda value: self.submit(
            "settings", desktop_keep_running=value))
        form.addRow(self.keep_running)
        body.addLayout(form)
        body.addWidget(label("Data stays in one file on this device. Account credentials use the OS keyring."))
        actions = QHBoxLayout()
        button("Back up data…", self.backup, actions)
        button("Check for updates", lambda: self.submit("update"), actions)
        body.addLayout(actions)
        agents = QGroupBox("Optional AI application access")
        group = QVBoxLayout(agents)
        group.addWidget(label("Other assistants can read this app's data through its existing MCP service."))
        self.agent_url = QLineEdit()
        self.agent_url.setReadOnly(True)
        self.agent_url.setAccessibleName("Local MCP endpoint")
        group.addWidget(self.agent_url)
        button("Copy MCP endpoint", lambda: QApplication.clipboard().setText(
            self.agent_url.text()), group)
        body.addWidget(agents)

    @Slot()
    def on_ready(self):
        self._ready = True
        self.pages.setEnabled(not self._closing)
        self.notice.setText("Ready · data stays on this device")

    @Slot(int)
    def navigate(self, index):
        self.pages.setCurrentIndex(index)
        if not self._ready:
            return
        if index == 2:
            self.controller.submit("sessions")
        elif index == 4:
            self.submit("ask_config")

    def submit(self, name, **args):
        if not self._ready or self._closing or (name in self.pending and name != "settings"):
            return
        self.pending.add(name)
        self.notice.setText("Working… " + name.replace("_", " "))
        self.controller.submit(name, **args)

    @Slot(dict)
    def on_snapshot(self, state):
        self.state = state
        metrics = [item for item in state.get("metrics", []) if item.get("kind") != "categorical"]
        empty = not state.get("metrics")
        self.welcome.setVisible(empty)
        self.summaries.setVisible(not empty)
        for (value, source), item in zip(self.summary_labels, state.get("latest", [])):
            value.setText(display_value(item["value"], item["unit"]))
            source.setText("{} · {}".format(item["source"], stamp(item["timestamp"]))
                           if item["source"] else "No data yet")
        names = [item["metric"] for item in metrics]
        if names != self._metric_names:
            selected = self.metric_combo.currentData()
            self._metric_names = names
            self.metric_combo.blockSignals(True)
            self.metric_combo.clear()
            for name in names:
                self.metric_combo.addItem(metric_label(name), name)
            preferred = selected if selected in names else "heart_rate_bpm"
            self.metric_combo.setCurrentIndex(max(0, self.metric_combo.findData(preferred)))
            self.metric_combo.blockSignals(False)
            self.metric_changed()
        desktop = state.get("desktop", {})
        self.keep_running.blockSignals(True)
        self.keep_running.setChecked(desktop.get("keep_running", False))
        self.keep_running.blockSignals(False)
        self.keep_running.setEnabled(QSystemTrayIcon.isSystemTrayAvailable())
        self.appearance.setCurrentText(desktop.get("appearance", "System"))
        self.apply_appearance(desktop.get("appearance", "System"))
        self.agent_url.setText(state.get("app", {}).get("url", "") + "/mcp")
        self.render_accounts(state)
        self.jobs.clear()
        for job in state.get("jobs", []):
            item = QListWidgetItem("{} · {}{}".format(job["title"], job["status"],
                " · " + str(job["detail"]) if job.get("detail") else ""))
            item.setData(Qt.ItemDataRole.UserRole, job)
            self.jobs.addItem(item)
        connect = state.get("connect", {})
        running = {job["kind"] for job in state.get("jobs", [])
                   if job["status"] in ("running", "needs_code")}
        for vendor, widget in self.provider_buttons.items():
            widget.setEnabled(bool(connect.get(vendor)) and vendor not in running)
            widget.setToolTip("A secure OS keyring and the provider's configured integration are required."
                              if not widget.isEnabled() else "")
        self.footer.setText("Local data · {} source(s)".format(len(state.get("sources", []))))
        if state.get("app", {}).get("problem"):
            self.notice.setText("Data unavailable: " + state["app"]["problem"])
        if self.nav.currentIndex() == 2:
            self.controller.submit("sessions")
        if self.metric_combo.currentData() and self.source.currentData() is not None:
            self.request_series()

    def render_accounts(self, state):
        sources = state.get("sources", [])
        self.accounts.setRowCount(len(sources))
        for row, source in enumerate(sources):
            problem = source.get("problem") or next(iter(source.get("sync_errors", {}).values()), None)
            status = "Syncing" if source.get("syncing") else "Needs attention" if problem else (
                "Enabled" if source.get("enabled") else "Disconnected")
            values = (source["name"], source["vendor"], status,
                      stamp(source.get("last_sync") or source.get("last_data")))
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if problem:
                    item.setToolTip(str(problem))
                self.accounts.setItem(row, column, item)
            actions = QWidget()
            row_layout = QHBoxLayout(actions)
            row_layout.setContentsMargins(2, 2, 2, 2)
            if source["type"] == "cloud sync" and source["enabled"]:
                sync = button("Sync", lambda checked=False, sid=source["id"]: self.submit(
                    "sync", source_id=sid), row_layout)
                sync.setEnabled(not source.get("syncing"))
                button("Disconnect", lambda checked=False, sid=source["id"], name=source["name"]:
                       self.disconnect(sid, name), row_layout)
            self.accounts.setCellWidget(row, 4, actions)
        self.accounts.resizeRowsToContents()

    @Slot(dict)
    def on_live(self, data):
        self.live_state = data
        self.last_live = time.monotonic()
        status = data.get("error") or data.get("message") or data.get("status", "off")
        self.live_status.setText("{} · {}".format(data.get("device") or "Heart-rate source", status))
        self.bpm.setText(display_value(data.get("bpm"), "bpm"))
        self.live_chart.set_points([(point[0] / 1000, point[1]) for point in data.get("points", [])])
        index = self.live_source.findData(data.get("source"))
        if index >= 0:
            self.live_source.setCurrentIndex(index)
        self.live_source.setEnabled(not data.get("pinned") and not data.get("session"))
        session = data.get("session")
        self.record_button.setText("Stop and save" if session else "Start recording")
        self.record_button.setEnabled(bool(session or (data.get("status") == "connected"
                                                     and data.get("logging"))) and not self._closing)
        self.recording_name.setEnabled(not session)
        if session:
            seconds = session["elapsed_s"]
            message = "Recording · {:02d}:{:02d} · {} readings · average {}".format(
                seconds // 60, seconds % 60, session["n"], display_value(session["avg"], "bpm"))
            self.session_status.setText(message)
            self.recording_indicator.setText("● Recording {:02d}:{:02d}".format(seconds // 60, seconds % 60))
            self.tray.setToolTip("Ticker · recording")
        else:
            self.session_status.setText("Not recording")
            self.recording_indicator.setText("")
            self.tray.setToolTip("Ticker")

    def metric_changed(self, *_):
        self.source.clear()
        self.trend.set_points([], empty="Loading available sources…")
        metric = self.metric_combo.currentData()
        if metric and self._ready:
            self.controller.submit("sources", metric=metric)
        elif not metric:
            self.trend.set_points([], empty="Connect a source or import data to see trends")

    def series_selection(self):
        return {"metric": self.metric_combo.currentData(), "source_id": self.source.currentData(),
                "days": self.range.currentData()}

    def request_series(self, *_):
        selection = self.series_selection()
        if self._ready and selection["metric"] and selection["source_id"] is not None:
            self.controller.submit("series", **selection)

    def record(self):
        self.record_button.setEnabled(False)
        if self.live_state.get("session"):
            self.submit("stop")
        else:
            self.submit("start", label=self.recording_name.text())

    def session_selected(self):
        selected = self.history.selectedItems()
        if selected:
            self._selected_session = self.history.item(selected[0].row(), 0).data(Qt.ItemDataRole.UserRole)
            self.controller.submit("session", session_id=self._selected_session)

    def render_sessions(self, payload):
        rows = payload.get("rows", [])
        current = self._selected_session
        self.history.blockSignals(True)
        self.history.setRowCount(len(rows))
        for row, entry in enumerate(rows):
            values = (entry[2] or entry[1].capitalize(), entry[3], stamp(entry[5]),
                      "{:g}".format(round(entry[7] or 0, 1)))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry[0])
                self.history.setItem(row, column, item)
            if entry[0] == current:
                self.history.selectRow(row)
        self.history.blockSignals(False)
        if rows and current is None:
            self.history.selectRow(0)
        elif not rows:
            self.history_detail.setText("No saved sessions yet. Start a recording or import health data.")
            self.session_chart.set_points([])

    def connect_provider(self, vendor):
        fields = {"oura": (("client_id", "Application client ID", False),
                           ("client_secret", "Application client secret", True)),
                  "fitbit": (), "garmin": (("email", "Email", False), ("password", "Password", True))}
        dialog = QDialog(self)
        dialog.setWindowTitle("Connect " + vendor.capitalize())
        body = QVBoxLayout(dialog)
        body.addWidget(label("Credentials are kept in your OS keyring. Provider sign-in may open your browser."))
        form = QFormLayout()
        inputs = {}
        for key, title, secret in (*fields[vendor], ("name", "Connection name", False)):
            entry = QLineEdit()
            entry.setAccessibleName(title)
            if key == "name":
                entry.setText(vendor.capitalize())
            if secret:
                entry.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow(title, entry)
            inputs[key] = entry
        body.addLayout(form)
        if vendor == "oura":
            body.addWidget(label("Registered redirect: " + self.state.get("connect", {}).get("oura_redirect", "")))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        body.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.submit(vendor, **{key: entry.text() for key, entry in inputs.items()})
        for entry in inputs.values():
            entry.clear()
        dialog.deleteLater()

    def disconnect(self, source_id, name):
        answer = QMessageBox.question(self, "Disconnect " + name,
            "Stop syncing this account? Previously saved data will stay on this device.")
        if answer == QMessageBox.StandardButton.Yes:
            self.submit("disconnect", source_id=source_id)

    def choose_device(self):
        if self.live_state.get("session"):
            self.notice.setText("Stop and save the current recording before changing devices.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Choose Bluetooth heart-rate device")
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("Turn on your sensor. Only devices advertising the heart-rate service are listed."))
        combo = QComboBox()
        combo.setAccessibleName("Available heart-rate devices")
        layout.addWidget(combo)
        self._device_dialog, self._device_combo = dialog, combo
        button("Scan for devices", lambda: self.submit("scan"), layout)
        address = QLineEdit()
        address.setAccessibleName("Device address (optional)")
        address.setPlaceholderText("Or enter a known device address")
        layout.addWidget(address)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)
        layout.addWidget(actions)
        self.submit("scan")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            chosen = address.text().strip() or combo.currentData()
            if chosen:
                self.submit("live_source", source="ble", address=chosen)
            else:
                self.notice.setText("No device selected. Scan again or enter its address.")
        self._device_dialog, self._device_combo = None, None
        dialog.deleteLater()

    def job_activated(self, item):
        job = item.data(Qt.ItemDataRole.UserRole)
        if job.get("status") == "needs_code" and job.get("kind") == "garmin":
            code, accepted = QInputDialog.getText(self, "Garmin verification", "Enter the code Garmin sent you:")
            if accepted:
                self.submit("garmin_code", job_id=job["id"], code=code)

    def import_file(self):
        if not self._ready:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose a health export", "",
            "Health exports (*.zip *.xml *.fit);;All files (*)")
        if path:
            self.nav.setCurrentIndex(3)
            self.submit("import", path=path)

    def backup(self):
        if not self._ready:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Back up Ticker data", "ticker-backup.sqlite3",
                                             "SQLite backup (*.sqlite3)")
        if path:
            if Path(path).exists():
                self.notice.setText("Choose a new backup filename; existing files are never overwritten.")
            else:
                self.submit("backup", path=path)

    def export_session(self):
        if self._selected_session is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export heart-rate trace", "recording.csv", "CSV (*.csv)")
        if path:
            self.submit("export", path=path, session_id=self._selected_session)

    def model_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Model settings")
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("Use Ollama locally, or a compatible model server. Remote servers receive health-data context."))
        form = QFormLayout()
        url = QLineEdit(self._ask_config.get("url", "http://127.0.0.1:11434"))
        models = QComboBox()
        models.setEditable(True)
        models.addItems(self._ask_config.get("models", []))
        models.setCurrentText(self._ask_config.get("model", ""))
        form.addRow("Server URL", url)
        form.addRow("Model", models)
        layout.addLayout(form)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)
        layout.addWidget(actions)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.submit("set_ask_config", url=url.text(), model=models.currentText())
        dialog.deleteLater()

    def ask(self):
        question = self.question.toPlainText().strip()
        if question:
            self.ask_button.setEnabled(False)
            self.answer.setPlainText("Answering…")
            self.submit("ask", question=question, history=self._ask_history[-3:])

    @Slot()
    def poll_tasks(self):
        if self._ask_id and "ask_status" not in self.pending:
            self.submit("ask_status", ask_id=self._ask_id)
        if self.last_live and time.monotonic() - self.last_live > 3:
            self.bpm.setText("— bpm")
            self.live_status.setText("Waiting for a fresh status update…")
            self.record_button.setEnabled(bool(self.live_state.get("session")) and not self._closing)

    @Slot(str, object)
    def on_result(self, name, payload):
        self.pending.discard(name)
        self.notice.setText("Ready · " + name.replace("_", " ") + " completed")
        if name == "sources" and payload["metric"] == self.metric_combo.currentData():
            selected = self.source.currentData()
            self.source.blockSignals(True)
            self.source.clear()
            for item in payload["sources"]:
                self.source.addItem(item["name"], item["id"])
            self.source.setCurrentIndex(max(0, self.source.findData(selected)))
            self.source.blockSignals(False)
            self.request_series()
        elif name == "series" and payload.get("selection") == self.series_selection():
            self.trend.set_points(chart_points(payload), payload.get("unit"))
        elif name == "sessions":
            self.render_sessions(payload)
        elif name == "session" and payload["session"]["id"] == self._selected_session:
            session = payload["session"]
            self.history_detail.setText("{} · {} · {} → {} · {:g} min\n{}".format(
                session["label"] or session["kind"], session["source"], stamp(session["start"]),
                stamp(session["end"]) if session["end"] else "In progress", session["duration_min"],
                " · ".join("{}: {}".format(metric_label(m["metric"]), display_value(m["mean"], m["unit"]))
                            for m in payload.get("metrics", []))))
            self.session_chart.set_points(chart_points(payload.get("heart_rate", {})))
            self.export_button.setEnabled(bool(payload.get("heart_rate")))
        elif name == "scan" and self._device_combo is not None:
            self._device_combo.clear()
            for device in payload:
                self._device_combo.addItem(device["name"], device["address"])
            if not payload:
                self.notice.setText("No compatible sensors found. Check Bluetooth permission and turn on your sensor.")
        elif name in ("oura", "fitbit") and payload.get("url"):
            if not QDesktopServices.openUrl(QUrl(payload["url"])):
                self.notice.setText("Could not open the sign-in browser. Check your default browser configuration.")
            else:
                self.notice.setText("Finish provider sign-in, then return here. Progress appears in Connections.")
        elif name in ("ask_config", "set_ask_config"):
            self._ask_config = payload
            self.model_status.setText("{} · {}".format(payload.get("model") or "Choose a model",
                "Ready" if payload.get("reachable") else payload.get("error") or "Not connected"))
        elif name == "ask":
            self._ask_id = payload["id"]
        elif name == "ask_status":
            if payload["status"] == "done":
                self.answer.setPlainText(payload["answer"])
                self._ask_history.append((payload["question"], payload["answer"]))
                self._ask_id = None
                self.ask_button.setEnabled(True)
            elif payload["status"] == "failed":
                self.answer.setPlainText(payload.get("error") or "Answering failed.")
                self._ask_id = None
                self.ask_button.setEnabled(True)
            else:
                self.answer.setPlainText("Answering…\n" + "\n".join(str(step.get("message") or step.get("kind", "Working")) for step in payload.get("steps", [])))
        elif name == "update":
            self.notice.setText(str(payload.get("message") or payload))
        elif name == "export":
            self.notice.setText("Trace exported · {} resolution; large sessions are bucketed.".format(payload["bucket"]))
        elif name == "backup":
            self.notice.setText("Verified backup created: " + str(payload))

    @Slot(str, str)
    def on_error(self, name, message):
        self.pending.discard(name)
        self.notice.setText("{}: {}".format(name.replace("_", " ").capitalize(), message))
        if name in ("start", "stop", "live_source") and self.live_state:
            self.on_live(self.live_state)
        if name in ("ask", "ask_status"):
            self.ask_button.setEnabled(True)
            self._ask_id = None
            self.answer.setPlainText(message)

    def change_appearance(self, _):
        appearance = self.appearance.currentText()
        self.apply_appearance(appearance)
        self.submit("settings", desktop_appearance=appearance)

    @staticmethod
    def apply_appearance(value):
        scheme = {"System": Qt.ColorScheme.Unknown, "Light": Qt.ColorScheme.Light,
                  "Dark": Qt.ColorScheme.Dark}[value]
        QApplication.styleHints().setColorScheme(scheme)

    @Slot()
    def activate(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    @Slot(QSystemTrayIcon.ActivationReason)
    def tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.activate()

    def closeEvent(self, event):
        if self._closing:
            if self.controller.thread.isRunning():
                event.ignore()
            else:
                event.accept()
            return
        event.ignore()
        can_hide = self.tray.isVisible() and QSystemTrayIcon.isSystemTrayAvailable()
        if self.keep_running.isChecked() and can_hide and not self.live_state.get("session"):
            self.hide()
            return
        box = QMessageBox(self)
        box.setWindowTitle("Close Ticker")
        box.setText("Keep recording in the background?" if self.live_state.get("session") else "Keep Ticker syncing in the background?")
        box.setInformativeText("Quit saves the recording and stops syncing. Saved data stays on this device.")
        background = box.addButton("Keep running", QMessageBox.ButtonRole.AcceptRole)
        background.setEnabled(can_hide)
        quit_button = box.addButton("Save and quit" if self.live_state.get("session") else "Quit Ticker", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is background:
            self.hide()
        elif box.clickedButton() is quit_button:
            self.request_quit(confirm=False)

    def request_quit(self, _checked=False, *, confirm=True):
        if self._closing:
            return
        if confirm and self.live_state.get("session"):
            answer = QMessageBox.question(self, "Stop recording and quit?", "The active recording will be saved and background syncing will stop.")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._closing = True
        self.pages.setEnabled(False)
        self.nav.setEnabled(False)
        self.notice.setText("Saving pending data and stopping Ticker…")
        self.activate()
        if self.controller.thread.isRunning():
            self.controller.shutdown()
        else:
            self.on_stopped()

    @Slot()
    def on_stopped(self):
        if self._closing:
            self.tray.hide()
            self.hide()
            QApplication.instance().quit()
        else:
            self._ready = False
            self.pages.setEnabled(False)
            self.record_button.setEnabled(False)
            self.poll.stop()
            if not self.notice.text().startswith(("Startup:", "Shutdown:")):
                self.notice.setText("Ticker has stopped. Quit and reopen the app to resume.")
