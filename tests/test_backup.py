import sqlite3

from ticker.db import backup, migrate, store


def test_backup_includes_committed_wal_rows_while_a_reader_is_active(tmp_path):
    path = tmp_path / "ticker.sqlite3"
    writer = store.connect(path)
    source = store.ensure_source(writer, "import", "test", "Test")
    writer.commit()

    reader = store.connect(path, migrate_first=False)
    reader.execute("BEGIN")
    baseline = reader.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    store.ensure_source(writer, "import", "test", "Second")
    writer.commit()

    made = backup.create(path, tmp_path / "snapshot.sqlite3")
    check = sqlite3.connect(made)
    try:
        assert check.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == baseline + 1
        assert check.execute("PRAGMA quick_check").fetchone() == ("ok",)
    finally:
        check.close()
        reader.close()
        writer.close()


def test_restore_preserves_the_replaced_database(tmp_path):
    target = tmp_path / "ticker.sqlite3"
    conn = store.connect(target)
    store.ensure_source(conn, "import", "old", "Old")
    conn.commit()
    conn.close()
    source = backup.create(target, tmp_path / "good.sqlite3")

    conn = store.connect(target, migrate_first=False)
    store.ensure_source(conn, "import", "new", "New")
    conn.commit()
    conn.close()
    (tmp_path / "ticker.sqlite3-wal").write_bytes(b"stale")
    (tmp_path / "ticker.sqlite3-shm").write_bytes(b"stale")
    backup.restore(source, target)

    restored = sqlite3.connect(target)
    try:
        vendors = {row[0] for row in
                   restored.execute("SELECT vendor FROM sources").fetchall()}
        assert "old" in vendors and "new" not in vendors
        assert migrate.current_version(restored) == migrate.LATEST_VERSION
    finally:
        restored.close()
    assert not (tmp_path / "ticker.sqlite3-wal").exists()
    assert not (tmp_path / "ticker.sqlite3-shm").exists()
    assert list(tmp_path.glob("ticker-before-restore-*.sqlite3"))
