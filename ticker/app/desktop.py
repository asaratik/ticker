"""Native desktop entry point, single-instance activation, and package smoke test."""

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from ticker import config as tconfig
from ticker.db.lock import DatabaseLock

APP_USER_MODEL_ID = "turbobunny.Ticker"


def set_windows_taskbar_identity():
    if sys.platform != "win32":
        return
    from ctypes import WinDLL, c_long, c_wchar_p
    setter = WinDLL("shell32").SetCurrentProcessExplicitAppUserModelID
    setter.argtypes = [c_wchar_p]
    setter.restype = c_long
    result = setter(APP_USER_MODEL_ID)
    if result != 0:
        logging.warning("Could not set Ticker taskbar identity: HRESULT 0x%08x", result & 0xFFFFFFFF)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--headless" in argv or "--web" in argv:
        from ticker.app.main import run_main
        return run_main(argv)
    parser = argparse.ArgumentParser(description="Ticker native desktop app")
    parser.add_argument("--db", type=Path, default=tconfig.DB_PATH)
    parser.add_argument("--host", default=tconfig.API_HOST)
    parser.add_argument("--port", type=int, default=tconfig.API_PORT)
    parser.add_argument("--token", default=tconfig.API_TOKEN)
    parser.add_argument("--allow-remote", action="store_true", default=tconfig.API_ALLOW_REMOTE)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    from ticker.api.server import RemoteBindRefused, check_bind
    try:
        check_bind(args.host, args.token, args.allow_remote)
    except RemoteBindRefused as exc:
        print(exc, file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    set_windows_taskbar_identity()
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox
    from ticker.app.desktop_backend import Controller
    from ticker.app.desktop_window import ICON_PATH, Window
    from ticker.app.runtime import Runtime
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("Ticker")
    app.setOrganizationName("Ticker")
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    app.setQuitOnLastWindowClosed(False)
    guard = Instance(args.db)
    try:
        if not guard.acquire():
            if guard.activate_existing():
                return 0
            QMessageBox.information(None, "Ticker is already running",
                "Ticker is already running for this data file, or a Ticker command (import, "
                "sync, restore) is using it. Try again when it finishes.")
            return 1
    except OSError as exc:
        QMessageBox.critical(None, "Cannot start Ticker", str(exc))
        return 1

    def factory():
        if args.port:
            from ticker.app.main import already_running, page_url
            if already_running(page_url(args.host, args.port)):
                raise RuntimeError("Another Ticker service is using this port. Stop its browser/headless instance before starting the native app.")
        return Runtime(args.db, host=args.host, port=args.port, token=args.token,
                       allow_remote=args.allow_remote, lock=guard.lock)

    controller = Controller(factory)
    window = Window(controller)
    guard.server.newConnection.connect(lambda: guard.receive(window.activate))
    errors = []
    controller.error.connect(lambda name, text: errors.append(name)
                             if name in ("startup", "shutdown") else None)
    window.show()
    controller.start()

    def cleanup():
        controller.shutdown()
        # Worker.quit is connected directly; this also works after the GUI
        # event loop has ended. Never terminate a writer thread forcibly.
        controller.thread.wait()
        guard.close()

    app.aboutToQuit.connect(cleanup)
    try:
        result = app.exec()
    finally:
        cleanup()
    return result or (1 if errors else 0)


class Instance:
    """The shared per-database lock plus current-user-only local activation IPC.

    The lock is the same OS lock every writable entry point takes
    (ticker.db.lock), so a browser/headless runtime or a terminal import on
    this database also counts as "already running". It is also what makes
    the IPC safe on Windows, where multiple named-pipe servers can otherwise
    listen on the same name: stale IPC is removed only while this process
    owns the lock. No health data or credentials travel over IPC.
    """
    def __init__(self, path):
        from PySide6.QtNetwork import QLocalServer
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest()[:24]
        self.name = "ticker-desktop-" + digest
        self.lock = DatabaseLock(path)
        self.server = QLocalServer()
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.clients = []
        self.owned = False

    def acquire(self):
        from PySide6.QtNetwork import QLocalServer
        if not self.lock.acquire():
            return False
        self.owned = True
        if not self.server.listen(self.name):
            QLocalServer.removeServer(self.name)
            if not self.server.listen(self.name):
                message = self.server.errorString()
                self.close()
                raise OSError("Cannot create Ticker's activation channel: " + message)
        return True

    def activate_existing(self):
        from PySide6.QtNetwork import QLocalSocket
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(2000):
            return False
        socket.write(b"activate\n")
        socket.flush()
        if not socket.bytesAvailable():
            socket.waitForReadyRead(2000)
        answer = bytes(socket.readAll()) == b"ok\n"
        socket.disconnectFromServer()
        return answer

    def receive(self, activate):
        while self.server.hasPendingConnections():
            client = self.server.nextPendingConnection()
            self.clients.append(client)

            def read(client=client):
                if client.bytesAvailable() > 32:
                    client.abort()
                elif client.canReadLine():
                    if bytes(client.readLine(32)) == b"activate\n":
                        activate()
                        client.write(b"ok\n")
                        client.flush()
                    client.disconnectFromServer()

            def remove(client=client):
                if client in self.clients:
                    self.clients.remove(client)
                client.deleteLater()

            client.readyRead.connect(read)
            client.disconnected.connect(remove)
            read()

    def close(self):
        self.server.close()
        for client in list(self.clients):
            client.abort()
        if self.owned:
            self.lock.release()
            self.owned = False


def smoke(result):
    """Create actual native screens against throwaway data without hardware."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from ticker.app.desktop_backend import Controller
    from ticker.app.desktop_window import Window
    from ticker.app.runtime import Runtime
    app = QApplication.instance() or QApplication(["ticker-smoke"])
    app.setQuitOnLastWindowClosed(False)
    errors, seen = [], []
    class NoHardware:
        def start(self):
            pass

        def stop(self):
            pass

    with tempfile.TemporaryDirectory(prefix="ticker-native-smoke-") as directory:
        controller = Controller(lambda: Runtime(Path(directory) / "ticker.sqlite3",
                                               host="127.0.0.1", port=0, builders={},
                                               make_source=lambda *args: NoHardware()))
        window = Window(controller)
        window.tray.hide()
        window.show()

        def inspect(state):
            if seen:
                return
            try:
                for index in range(len(window.PAGES)):
                    # Inspect widgets without triggering model discovery or
                    # hardware scans. This must remain a network-free UI test.
                    window.pages.setCurrentIndex(index)
                    seen.append(window.PAGES[index])
                if window.grab().isNull():
                    raise RuntimeError("Native window did not render")
            except Exception as exc:
                errors.append(str(exc))
            window.request_quit(confirm=False)

        controller.snapshot.connect(inspect)
        controller.error.connect(lambda name, message: errors.append(name + ": " + message))
        controller.stopped.connect(app.quit)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(lambda: (errors.append("Native smoke test timed out"), window.request_quit(confirm=False)))
        timeout.start(20000)
        controller.start()
        try:
            app.exec()
        finally:
            timeout.stop()
            controller.shutdown()
            controller.thread.wait()
            window.tray.hide()
            window.hide()
        if errors or len(seen) != len(window.PAGES):
            raise RuntimeError("; ".join(errors) or "Native screens were not created")
        Path(result).write_text(json.dumps({"ok": True, "native": True, "pages": seen}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
