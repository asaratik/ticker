"""Qt command bridge: blocking services live on one worker, not the UI thread."""

import asyncio
import csv
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from ticker.app.desktop_data import Reader


class Worker(QObject):
    ready = Signal()
    snapshot = Signal(dict)
    live = Signal(dict)
    result = Signal(str, object)
    error = Signal(str, str)
    stopped = Signal()

    def __init__(self, factory):
        super().__init__()
        self.factory = factory
        self.runtime = None
        self.reader = None
        self.timer = None
        self.ticks = 0
        self.closing = False

    @Slot()
    def start(self):
        try:
            self.runtime = self.factory()
            self.runtime.start()
            self.reader = Reader(self.runtime.db_path)
            self.timer = QTimer(self)
            self.timer.setInterval(1000)
            self.timer.timeout.connect(self.refresh)
            self.timer.start()
            self.ready.emit()
            self.refresh()
        except Exception as exc:
            self.error.emit("startup", str(exc))
            self.stop()

    @Slot()
    def refresh(self):
        if self.closing or self.reader is None:
            return
        try:
            self.live.emit(self.runtime.live.snapshot())
            if self.ticks % 5 == 0:
                state = self.runtime.state()
                state["latest"] = self.reader.latest()
                state["desktop"] = {
                    "keep_running": self.runtime.settings.desktop_keep_running,
                    "appearance": self.runtime.settings.desktop_appearance}
                self.snapshot.emit(state)
            self.ticks += 1
            if self.runtime.stop_requested:
                self.stop()
        except Exception as exc:
            self.error.emit("refresh", str(exc))

    @Slot(str, dict)
    def command(self, name, args):
        if self.closing or self.reader is None:
            self.error.emit(name, "Ticker is not ready or is shutting down.")
            return
        try:
            answer = self.dispatch(name, args)
            self.result.emit(name, answer)
            self.ticks = 0
            self.refresh()
        except Exception as exc:
            # Never log command arguments: sign-in payloads contain secrets.
            self.error.emit(name, str(exc))

    def dispatch(self, name, args):
        rt = self.runtime
        if name == "sources":
            return {"metric": args["metric"], "sources": self.reader.sources(args["metric"])}
        if name == "series":
            return dict(self.reader.series(**args), selection=dict(args))
        if name == "sessions":
            return self.reader.sessions()
        if name == "session":
            return self.reader.session(args["session_id"])
        if name == "live_source":
            if "address" in args:
                if rt.live.snapshot().get("session"):
                    raise ValueError("Stop and save the current recording before changing devices.")
                rt.settings.update(ble_address=args["address"])
                rt.live.set_source("off")
            return rt.live.set_source(args["source"])
        if name == "scan":
            from bleak import BleakScanner

            async def discover():
                devices = await BleakScanner.discover(timeout=5, return_adv=True)
                return [{"name": adv.local_name or device.name or device.address,
                         "address": device.address}
                        for device, adv in devices.values()
                        if "0000180d-0000-1000-8000-00805f9b34fb" in
                        [uuid.lower() for uuid in adv.service_uuids]]

            return asyncio.run(discover())
        if name == "start":
            return rt.live.start_session(args.get("label"))
        if name == "stop":
            return rt.live.stop_session()
        if name == "sync":
            return rt.sync_now(args["source_id"])
        if name == "disconnect":
            return rt.disconnect(args["source_id"])
        if name == "oura":
            return rt.connect_oura(**args)
        if name == "fitbit":
            return rt.connect_fitbit(**args)
        if name == "garmin":
            return rt.connect_garmin(**args)
        if name == "garmin_code":
            return rt.garmin_code(**args)
        if name == "import":
            return rt.start_import(args["path"])
        if name == "backup":
            from ticker.db.backup import create
            destination = Path(args["path"]).resolve()
            if destination == rt.db_path.resolve() or any(path.exists() for path in (
                    destination, destination.with_name(destination.name + ".tmp"),
                    Path(str(destination) + "-wal"), Path(str(destination) + "-shm"))):
                raise ValueError("Choose a new backup filename; existing files are not overwritten.")
            if not rt.writer.flush(timeout=15):
                raise ValueError("Pending database writes could not be saved. Try again.")
            return str(create(rt.db_path, destination))
        if name == "export":
            detail = self.reader.session(args["session_id"])
            trace = detail.get("heart_rate", {})
            # Exclusive creation protects an existing file selected by mistake.
            with Path(args["path"]).open("x", newline="", encoding="utf-8") as output:
                writer = csv.writer(output)
                writer.writerow(trace.get("columns", ["time", "value"]))
                writer.writerows(trace.get("rows", []))
            return {"path": args["path"], "bucket": trace.get("bucket", "raw")}
        if name == "ask_config":
            return rt.ask_config()
        if name == "set_ask_config":
            return rt.set_ask_config(**args)
        if name == "ask":
            return rt.ask(**args)
        if name == "ask_status":
            return rt.ask_status(args["ask_id"])
        if name == "settings":
            rt.settings.update(**args)
            return args
        if name == "update":
            return rt.check_update()
        raise ValueError("Unknown desktop action: {}".format(name))

    @Slot()
    def stop(self):
        if self.closing:
            return
        self.closing = True
        if self.timer is not None:
            self.timer.stop()
        try:
            if self.reader is not None:
                self.reader.close()
            if self.runtime is not None:
                self.runtime.stop()
        except Exception as exc:
            self.error.emit("shutdown", str(exc))
        finally:
            self.stopped.emit()


class Controller(QObject):
    ready = Signal()
    snapshot = Signal(dict)
    live = Signal(dict)
    result = Signal(str, object)
    error = Signal(str, str)
    stopped = Signal()
    requested = Signal(str, dict)
    stop_requested = Signal()

    def __init__(self, factory, parent=None):
        super().__init__(parent)
        self.thread = QThread(self)
        self.worker = Worker(factory)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start)
        self.requested.connect(self.worker.command)
        self.stop_requested.connect(self.worker.stop)
        self.worker.ready.connect(self.ready)
        self.worker.snapshot.connect(self.snapshot)
        self.worker.live.connect(self.live)
        self.worker.result.connect(self.result)
        self.worker.error.connect(self.error)
        self.worker.stopped.connect(self.thread.quit, Qt.ConnectionType.DirectConnection)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.stopped)
        self.closing = False

    def start(self):
        self.thread.start()

    def submit(self, name, **args):
        if not self.closing:
            self.requested.emit(name, args)

    def shutdown(self):
        if not self.closing:
            self.closing = True
            self.stop_requested.emit()
