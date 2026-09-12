"""Startup, diagnostics and a per-database single-instance guard."""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import sys

VERSION = "0.2.0"


def build_info():
    path = Path(__file__).with_name("build-info.json")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": VERSION, "commit": "source"}


def diagnostics():
    # Intentionally excludes paths, device identifiers, recordings and logs.
    return {**build_info(), "os": platform.system(), "os_version": platform.release(),
            "architecture": platform.machine(), "python": platform.python_version(),
            "packaged": bool(getattr(sys, "frozen", False))}


class LogStream:
    def write(self, message):
        if message.strip():
            logging.getLogger("ticker").error(message.rstrip())
        return len(message)

    def flush(self):
        pass


def setup_logging(folder):
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / "hrm_app.log"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    os.close(fd)
    if os.name == "posix":
        path.chmod(0o600)
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger("ticker").addHandler(handler)
    logging.getLogger("ticker").setLevel(logging.INFO)
    if getattr(sys, "frozen", False):
        sys.stdout = sys.stderr = LogStream()
    logging.getLogger("ticker").info("Startup %s", diagnostics())


class InstanceLock:
    def __init__(self, db_path):
        path = db_path.with_suffix(db_path.suffix + ".lock")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.stream = os.fdopen(fd, "r+b")
        try:
            if os.name == "nt":
                import msvcrt
                if path.stat().st_size == 0:
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RuntimeError("Ticker is already using this database. Close the other window first.") from None

    def close(self):
        self.stream.close()
