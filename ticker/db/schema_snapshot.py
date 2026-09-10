"""
Regenerates db/schema.sql from the migrations.

schema.sql is documentation -- the thing you read to see what the database
looks like today -- and documentation that is written by hand next to
migrations always ends up describing a database that no longer exists. So it
is generated from a freshly migrated temporary database instead, and
tests/test_schema.py fails if the checked-in copy differs.

    python -m ticker.db.schema_snapshot          # rewrite schema.sql
    python -m ticker.db.schema_snapshot --check  # exit 1 if it is stale
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

from ticker.db import migrate

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

HEADER = """\
-- Current schema, as produced by running every migration in migrations/.
--
-- Generated, not hand-edited: tests/test_schema.py rebuilds this from a
-- freshly migrated database and fails if it differs, so it cannot drift away
-- from what the migrations actually create. To change the schema, add a
-- migration and regenerate:
--
--     python -m ticker.db.schema_snapshot
--
-- sessions_v1 and samples_v1 are the renamed v1 tables. They are kept on
-- purpose; a later release can drop them after the migration has proven safe, so a
-- bad migration stays recoverable in the field.

PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;
"""


def render() -> str:
    """Migrate a throwaway database and dump its schema, tables first."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "snapshot.sqlite3"
        migrate.migrate(db_path, backup=False)
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name"
            ).fetchall()
        finally:
            conn.close()
    body = "\n\n".join(sql.strip() + ";" for (sql,) in rows)
    return HEADER + "\n" + body + "\n"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    rendered = render()
    if "--check" in argv:
        current = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else ""
        if current != rendered:
            print("schema.sql is stale; run: python -m ticker.db.schema_snapshot",
                  file=sys.stderr)
            return 1
        print("schema.sql is current")
        return 0
    SCHEMA_PATH.write_text(rendered, encoding="utf-8")
    print("wrote {}".format(SCHEMA_PATH))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
