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

CREATE TABLE devices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    name        TEXT,
    address     TEXT,                         -- BLE address, vendor device id
    model       TEXT,
    UNIQUE (source_id, address)
);

CREATE TABLE metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,         -- 'heart_rate_bpm'
    unit        TEXT NOT NULL,                -- 'bpm'
    value_kind  TEXT NOT NULL                 -- 'instant' | 'interval' | 'cumulative' | 'categorical'
        CHECK (value_kind IN ('instant', 'interval', 'cumulative', 'categorical')),
    description TEXT
);

CREATE TABLE observations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    metric_id    INTEGER NOT NULL REFERENCES metrics(id),
    session_id   INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    ts           TEXT    NOT NULL,            -- ISO 8601 UTC, start of the observation
    end_ts       TEXT,                        -- NULL for point samples
    value        REAL    NOT NULL,
    text_value   TEXT,                        -- categorical metrics ('deep', 'rem')
    external_id  TEXT    NOT NULL DEFAULT '', -- '' when the source has no stable id
    ingested_at  TEXT    NOT NULL
);

CREATE TABLE raw_payloads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    endpoint    TEXT NOT NULL,
    window_from TEXT,
    window_to   TEXT,
    fetched_at  TEXT NOT NULL,
    body        BLOB NOT NULL                 -- gzipped JSON
);

CREATE TABLE rollups_daily (
    metric_id   INTEGER NOT NULL REFERENCES metrics(id),
    day         TEXT    NOT NULL,             -- 'YYYY-MM-DD' in local tz
    n           INTEGER NOT NULL,
    sum_value   REAL    NOT NULL,
    min_value   REAL    NOT NULL,
    max_value   REAL    NOT NULL,
    avg_value   REAL    NOT NULL,
    p50_value   REAL,
    computed_at TEXT    NOT NULL,
    PRIMARY KEY (metric_id, day)
);

CREATE TABLE "samples_v1" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES "sessions_v1"(id),
    timestamp TEXT NOT NULL,
    heart_rate INTEGER NOT NULL,
    rr_intervals_ms TEXT
);

CREATE TABLE schema_version (    version     INTEGER NOT NULL,    applied_at  TEXT    NOT NULL);

CREATE TABLE sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    device_id    INTEGER REFERENCES devices(id),
    external_id  TEXT,                        -- vendor's id, when there is one
    start_ts     TEXT NOT NULL,
    end_ts       TEXT,
    kind         TEXT,                        -- 'manual', 'workout', 'sleep'
    label        TEXT,
    UNIQUE (source_id, external_id)
);

CREATE TABLE "sessions_v1" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TEXT NOT NULL,
    end_time TEXT,
    device_name TEXT,
    device_address TEXT,
    label TEXT
);

CREATE TABLE sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL CHECK (kind IN ('stream', 'pull', 'import')),
    vendor        TEXT NOT NULL,              -- 'ble', 'oura', 'fitbit', 'whoop', 'apple_health'
    display_name  TEXT NOT NULL,
    auth_ref      TEXT,                       -- keyring entry name, never a secret itself
    config_json   TEXT NOT NULL DEFAULT '{}',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    UNIQUE (vendor, display_name)
);

CREATE TABLE sync_state (
    source_id      INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    metric_id      INTEGER NOT NULL REFERENCES metrics(id),
    cursor         TEXT,                      -- vendor pagination token, if any
    watermark_ts   TEXT,                      -- fetched cleanly up to here
    last_attempt   TEXT,
    last_success   TEXT,
    last_error     TEXT,
    PRIMARY KEY (source_id, metric_id)
);

CREATE INDEX idx_obs_metric_ts ON observations (metric_id, ts);

CREATE INDEX idx_obs_session   ON observations (session_id) WHERE session_id IS NOT NULL;

CREATE INDEX idx_raw_source_time ON raw_payloads (source_id, fetched_at);

CREATE INDEX idx_samples_v1_session ON samples_v1(session_id);

CREATE UNIQUE INDEX ux_obs_natural
    ON observations (source_id, metric_id, ts, external_id);
