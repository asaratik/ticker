import json
import sqlite3

import pytest

from ticker.app import support
from ticker.db import migrate, store
from ticker.mcp.readonly import open_readonly


def test_diagnostics_exclude_user_paths_and_records(tmp_path):
    path = tmp_path / "private-name.sqlite3"
    conn = store.connect(path)
    source = store.ensure_source(conn, "import", "private-vendor", "My device")
    conn.commit()
    conn.close()
    report = support.diagnostics(path, "http://127.0.0.1:8477")
    encoded = json.dumps(report)
    assert report["integrity"] == "ok"
    assert "private-name" not in encoded
    assert "private-vendor" not in encoded
    assert "My device" not in encoded


def test_an_older_reader_rejects_a_newer_database(tmp_path):
    path = tmp_path / "future.sqlite3"
    conn = store.connect(path)
    conn.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, '')",
                 (migrate.LATEST_VERSION + 1,))
    conn.commit()
    conn.close()
    with pytest.raises(Exception, match="newer"):
        open_readonly(path)
