-- v1 baseline: the schema storage.py created implicitly, recorded here so
-- every database has one linear history whether it predates the migration
-- runner or not.
--
-- IF NOT EXISTS throughout, because the common case for this migration is
-- adopting a database that already has these tables and no schema_version
-- row to say so.

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TEXT NOT NULL,
    end_time TEXT,
    device_name TEXT,
    device_address TEXT,
    label TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    timestamp TEXT NOT NULL,
    heart_rate INTEGER NOT NULL,
    rr_intervals_ms TEXT
);

CREATE INDEX IF NOT EXISTS idx_samples_session ON samples(session_id);
