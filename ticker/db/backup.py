"""Verified backup and restore helpers for Ticker's SQLite database."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ticker import config as tconfig
from ticker.db import migrate


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def create(source: Path, destination: Optional[Path] = None) -> Path:
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError("no database at {}".format(source))
    destination = Path(destination) if destination else source.with_name(
        "ticker-backup-{}.sqlite3".format(_stamp()))
    conn = sqlite3.connect(str(source))
    try:
        migrate.snapshot(conn, destination)
    finally:
        conn.close()
    return destination


def restore(source: Path, destination: Path) -> Path:
    """Restore a verified snapshot, preserving the current database first."""
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise FileNotFoundError("no backup at {}".format(source))
    check = sqlite3.connect("{}?mode=ro".format(source.resolve().as_uri()), uri=True)
    try:
        if check.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("backup failed SQLite's integrity check")
        version = migrate.current_version(check)
        if version > migrate.LATEST_VERSION:
            raise ValueError(
                "backup schema {} is newer than this Ticker supports ({})".format(
                    version, migrate.LATEST_VERSION))
    finally:
        check.close()

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        create(destination, destination.with_name(
            "ticker-before-restore-{}.sqlite3".format(_stamp())))
    source_conn = sqlite3.connect(
        "{}?mode=ro".format(source.resolve().as_uri()), uri=True)
    scratch = destination.with_name(destination.name + ".restore.tmp")
    if scratch.exists():
        scratch.unlink()
    target = sqlite3.connect(str(scratch))
    try:
        source_conn.backup(target)
        if target.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("restored database failed SQLite's integrity check")
    finally:
        target.close()
        source_conn.close()
    # A restored main file must never be paired with the old database's WAL
    # or shared-memory index. Ticker is stopped for restore, so these files
    # can be removed safely; leaving them would make SQLite replay stale pages.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(destination) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    scratch.replace(destination)
    return destination


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "restore"))
    parser.add_argument("path", type=Path, nargs="?")
    parser.add_argument("--db", type=Path, default=tconfig.DB_PATH)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "create":
            made = create(args.db, args.path)
            print("verified backup: {}".format(made))
        else:
            if args.path is None:
                parser.error("restore needs the backup file")
            restore(args.path, args.db)
            print("restored {} from {}".format(args.db, args.path))
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
