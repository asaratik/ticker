"""
One owning process per database.

Every entry point that can write -- the desktop app, the browser/headless
runtime, the standalone API server, and the terminal import, sync, setup,
maintenance and restore commands -- takes the same lock before migrating or
writing. Read-only access (MCP, `ticker status`, backup creation) does not
need it; WAL lets readers run alongside the one writer.

The lock is an OS file lock on `<canonical-db-path>.lock`: msvcrt on
Windows, flock elsewhere. The OS drops it when the owning process dies, so
there is no stale-lock heuristic to get wrong. It is deliberately not
re-entrant -- a second handle in the same process is refused too -- which is
what lets the desktop's single-instance check be tested in one process. A
caller that already holds the lock hands it on instead (Runtime(lock=...)).

The HTTP port is not a lock: a runtime can run with HTTP off, and something
else can hold the port.
"""

from __future__ import annotations

import os
from pathlib import Path


class DatabaseInUse(RuntimeError):
    """Another process -- or another handle in this one -- owns the database."""


def lock_path(db_path: Path) -> Path:
    """`<canonical-db-path>.lock`. resolve() makes two spellings of one file
    agree; NTFS case-insensitivity covers a difference in case."""
    path = Path(db_path).expanduser().resolve()
    return path.with_name(path.name + ".lock")


class DatabaseLock:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.path = lock_path(db_path)
        self._stream = None

    @property
    def held(self) -> bool:
        return self._stream is not None

    def acquire(self) -> bool:
        """Take the lock without waiting. False if someone else has it."""
        if self._stream is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        stream = os.fdopen(fd, "r+b")
        try:
            if os.name == "nt":
                import msvcrt
                # msvcrt locks bytes that exist; give it one to lock.
                if os.fstat(fd).st_size == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self._stream = stream
        return True

    def require(self) -> "DatabaseLock":
        """acquire(), or raise DatabaseInUse with a message for the user."""
        if not self.acquire():
            raise DatabaseInUse(
                "Ticker is already using {} -- quit it first, or wait for the "
                "other command to finish.".format(self.db_path))
        return self

    def release(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            # Closing the handle releases the lock on both platforms.
            stream.close()

    def __enter__(self) -> "DatabaseLock":
        return self.require()

    def __exit__(self, *exc) -> None:
        self.release()


def in_use(db_path: Path) -> bool:
    """Whether some process owns the database right now. A probe: it takes
    and immediately drops the lock when nobody has it."""
    probe = DatabaseLock(db_path)
    if probe.acquire():
        probe.release()
        return False
    return True
