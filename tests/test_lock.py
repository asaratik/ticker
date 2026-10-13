"""One owning process per database, whichever entry point it came in by."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from ticker.app.runtime import Runtime
from ticker.db import backup, store
from ticker.db.lock import DatabaseInUse, DatabaseLock, in_use, lock_path
from ticker.ingest import importer, sync

# Takes the lock, reports, then -- once told to -- dies without releasing
# it: os._exit skips every finalizer, as a crash would.
HOLD = ("import os, sys; sys.path.insert(0, sys.argv[2]); "
        "from ticker.db.lock import DatabaseLock; "
        "lock = DatabaseLock(sys.argv[1]); "
        "print('held' if lock.acquire() else 'refused', flush=True); "
        "sys.stdin.readline(); os._exit(1)")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "ticker.sqlite3"
    store.connect(path).close()
    return path


def other_process(path):
    root = str(Path(__file__).resolve().parents[1])
    return subprocess.Popen([sys.executable, "-c", HOLD, str(path), root],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)


def crash(child):
    child.stdin.write("\n")
    child.stdin.flush()
    child.wait(10)


def test_the_lock_sits_beside_the_database(db):
    assert lock_path(db) == db.resolve().with_name(db.name + ".lock")


def test_a_second_owner_is_refused_even_in_the_same_process(db):
    first, second = DatabaseLock(db), DatabaseLock(db)
    assert first.acquire()
    try:
        assert not second.acquire()
        with pytest.raises(DatabaseInUse):
            second.require()
        assert in_use(db)
    finally:
        first.release()
    assert second.acquire()
    second.release()
    assert not in_use(db)


def test_another_process_is_refused(db):
    with DatabaseLock(db):
        child = other_process(db)
        assert child.stdout.readline().strip() == "refused"
        crash(child)


def test_the_lock_goes_when_its_process_dies(db):
    child = other_process(db)
    assert child.stdout.readline().strip() == "held"
    assert in_use(db)
    crash(child)
    # No stale-lock cleanup needed. Windows releases a dead process's locks
    # asynchronously, so allow it a moment.
    deadline = time.monotonic() + 5
    while in_use(db) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not in_use(db)


def test_writable_commands_refuse_an_owned_database(db, tmp_path, capsys):
    export = tmp_path / "export.xml"
    export.write_text("<HealthData/>", encoding="utf-8")
    with DatabaseLock(db):
        assert importer.main([str(export), "--db", str(db), "--quiet"]) == 1
        assert sync.main(["--once", "--db", str(db)]) == 1
        assert backup.main(["restore", str(db), "--db", str(db)]) == 1
        with pytest.raises(DatabaseInUse):
            Runtime(db, host="127.0.0.1", port=0, builders={}).start()
    assert capsys.readouterr().err.count("already using") >= 3


def test_a_runtime_holds_the_lock_until_it_stops(db, monkeypatch):
    monkeypatch.delenv("HRM_SOURCE", raising=False)
    runtime = Runtime(db, host="127.0.0.1", port=0, builders={})
    runtime.start()
    try:
        assert in_use(db)
        with pytest.raises(DatabaseInUse):
            Runtime(db, host="127.0.0.1", port=0, builders={}).start()
    finally:
        runtime.stop()
    assert not in_use(db)


def test_a_runtime_uses_a_lock_it_is_handed(db, monkeypatch):
    monkeypatch.delenv("HRM_SOURCE", raising=False)
    lock = DatabaseLock(db)
    assert lock.acquire()
    runtime = Runtime(db, host="127.0.0.1", port=0, builders={}, lock=lock)
    runtime.start()
    runtime.stop()
    # The desktop took it before its window existed; it releases it too.
    assert lock.held
    lock.release()
