-- 0002: one observations table, many sources.
--
-- DDL and seed data only. The v1 data copy lives in migrate.py because two
-- parts of it cannot be expressed in SQL: RR interval strings expand into
-- one row per beat, and v1 wrote timestamps at two different
-- precisions that have to be normalised to one canonical spelling.
--
-- The v1 tables are renamed, not dropped. Section 3 step 6 drops them in a
-- later release, once a bad migration is no longer recoverable only from
-- the .bak file.

ALTER TABLE sessions RENAME TO sessions_v1;
ALTER TABLE samples  RENAME TO samples_v1;

-- The rename above carries the old index name with the table; rename it too
-- so 'idx_samples_session' is free for whatever wants it later.
DROP INDEX IF EXISTS idx_samples_session;
CREATE INDEX IF NOT EXISTS idx_samples_v1_session ON samples_v1(session_id);

-- Where data comes from. One row per configured connector instance.
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

-- Physical hardware, where the notion applies. Optional.
CREATE TABLE devices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    name        TEXT,
    address     TEXT,                         -- BLE address, vendor device id
    model       TEXT,
    UNIQUE (source_id, address)
);

-- Metric registry. Seeded at migration time, extensible.
CREATE TABLE metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,         -- 'heart_rate_bpm'
    unit        TEXT NOT NULL,                -- 'bpm'
    value_kind  TEXT NOT NULL                 -- 'instant' | 'interval' | 'cumulative' | 'categorical'
        CHECK (value_kind IN ('instant', 'interval', 'cumulative', 'categorical')),
    description TEXT
);

-- A bounded recording period. Streaming sessions (user pressed Start) and
-- vendor-side workouts/sleeps both land here.
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

-- The single fact table.
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

-- The idempotency guarantee. Re-syncing an overlapping window is a no-op.
CREATE UNIQUE INDEX ux_obs_natural
    ON observations (source_id, metric_id, ts, external_id);

-- The dashboard access path.
CREATE INDEX idx_obs_metric_ts ON observations (metric_id, ts);
CREATE INDEX idx_obs_session   ON observations (session_id) WHERE session_id IS NOT NULL;

-- Per-(source, metric) sync watermark. Drives incremental pulls.
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

-- Verbatim API responses. Lets the normalizer be rewritten without re-fetching.
CREATE TABLE raw_payloads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    endpoint    TEXT NOT NULL,
    window_from TEXT,
    window_to   TEXT,
    fetched_at  TEXT NOT NULL,
    body        BLOB NOT NULL                 -- gzipped JSON
);

CREATE INDEX idx_raw_source_time ON raw_payloads (source_id, fetched_at);

-- Precomputed aggregates. Rebuilt incrementally; always derivable from observations.
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

INSERT INTO metrics (name, unit, value_kind, description) VALUES
  ('heart_rate_bpm',      'bpm',     'instant',     'Instantaneous heart rate'),
  ('rr_interval_ms',      'ms',      'instant',     'Beat-to-beat interval'),
  ('hrv_rmssd_ms',        'ms',      'instant',     'RMSSD over a rolling window'),
  ('spo2_pct',            '%',       'instant',     'Blood oxygen saturation'),
  ('respiratory_rate_bpm','breaths/min','instant',  'Respiration rate'),
  ('skin_temp_delta_c',   'C',       'instant',     'Skin temperature deviation'),
  ('steps',               'count',   'cumulative',  'Step count over the interval'),
  ('active_energy_kcal',  'kcal',    'cumulative',  'Active energy burned'),
  ('sleep_stage',         'stage',   'categorical', 'Sleep stage over the interval'),
  ('sleep_duration_s',    's',       'interval',    'Total sleep in a sleep session'),
  ('weight_kg',           'kg',      'instant',     'Body mass'),
  ('body_fat_pct',        '%',       'instant',     'Body fat percentage');
