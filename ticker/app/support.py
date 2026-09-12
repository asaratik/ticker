"""User-facing diagnostics, update checks, and native file selection."""

from __future__ import annotations

import json
import os
import platform
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from ticker.db import migrate
from ticker.mcp.protocol import package_version

LATEST_RELEASE_URL = (
    "https://api.github.com/repos/ashokaratikatla/ticker/releases/latest")


def build_identity() -> Dict[str, Optional[str]]:
    result = {"version": package_version(), "commit": None}
    source_root = Path(__file__).resolve().parents[2]
    candidates = [source_root / "build-info.json",
                  source_root / "build" / "build-info.json"]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.insert(0, Path(bundle) / "build-info.json")
    for path in candidates:
        try:
            result.update(json.loads(path.read_text(encoding="utf-8")))
            break
        except (OSError, ValueError, TypeError):
            continue
    return result


def diagnostics(db_path: Path, url: str = "") -> Dict[str, Any]:
    """Return support metadata without paths, tokens, or health records."""
    identity = build_identity()
    result: Dict[str, Any] = {
        "ticker_version": identity.get("version") or package_version(),
        "build_commit": identity.get("commit"),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "packaged": bool(getattr(sys, "frozen", False)),
        "server": url,
        "database_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "database_schema": None,
        "integrity": "missing",
    }
    if db_path.exists():
        conn = sqlite3.connect(
            "{}?mode=ro".format(db_path.resolve().as_uri()), uri=True)
        try:
            result["database_schema"] = migrate.current_version(conn)
            result["integrity"] = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()
    return result


def _version(value: str):
    clean = value.lstrip("v").split("-", 1)[0]
    try:
        return tuple(int(part) for part in clean.split("."))
    except ValueError:
        return (0,)


def check_update(timeout: float = 5.0) -> Dict[str, Any]:
    request = urllib.request.Request(
        LATEST_RELEASE_URL,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "Ticker/{}".format(package_version())})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            release = json.loads(response.read())
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise RuntimeError("could not check GitHub for updates: {}".format(exc))
    current, latest = package_version(), str(release.get("tag_name") or "")
    return {"current": current, "latest": latest.lstrip("v"),
            "available": _version(latest) > _version(current),
            "url": release.get("html_url")}


def pick_import_file() -> Optional[str]:
    """Open the OS file picker. Empty means the user cancelled."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        selected = filedialog.askopenfilename(
            parent=root, title="Choose a health export",
            filetypes=[("Health exports", "*.zip *.fit"),
                       ("All files", "*.*")])
        return os.path.normpath(selected) if selected else None
    finally:
        root.destroy()
