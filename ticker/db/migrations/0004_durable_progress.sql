-- 0004: sync progress and rollup invalidation that survive a crash.
--
-- backfill_done: a finished backfill used to be remembered only in memory,
-- and its cursor was overwritten by every live window, so each restart
-- walked years of history again. The cursor now only ever moves with
-- backfill, and completion has a column of its own.
--
-- rollup_dirty: days whose rollups need recomputing used to be an in-memory
-- set on the writer, lost on a crash. Triggers now record them in the same
-- transaction as the change, from any connection -- including deletes that
-- never pass through the writer. Days are UTC dates of observations.ts; the
-- rebuild maps each to the local days it overlaps, so the markers stay
-- right whatever the reporting zone is. Keyed by source as well, for
-- source-aware summaries.
--
-- writer_probe: one row the writer rewrites to prove storage accepts a real
-- commit, at startup and before a storage fault is cleared.

ALTER TABLE sync_state ADD COLUMN backfill_done INTEGER NOT NULL DEFAULT 0;

CREATE TABLE rollup_dirty (
    source_id  INTEGER NOT NULL,
    metric_id  INTEGER NOT NULL,
    utc_day    TEXT    NOT NULL,              -- 'YYYY-MM-DD' of observations.ts
    PRIMARY KEY (source_id, metric_id, utc_day)
) WITHOUT ROWID;

-- NOT EXISTS rather than INSERT OR IGNORE: inside a trigger, SQLite applies
-- the outer statement's conflict policy -- for the writer's upsert, ABORT --
-- so an IGNORE here would not be honoured.
CREATE TRIGGER observations_dirty_insert AFTER INSERT ON observations
BEGIN
    INSERT INTO rollup_dirty (source_id, metric_id, utc_day)
    SELECT NEW.source_id, NEW.metric_id, substr(NEW.ts, 1, 10)
    WHERE NOT EXISTS (SELECT 1 FROM rollup_dirty
                      WHERE source_id = NEW.source_id AND metric_id = NEW.metric_id
                        AND utc_day = substr(NEW.ts, 1, 10));
END;

-- Only what a summary reads: a re-sync that just attaches a session is not
-- a changed day.
CREATE TRIGGER observations_dirty_update AFTER UPDATE ON observations
WHEN OLD.value IS NOT NEW.value OR OLD.ts IS NOT NEW.ts
  OR OLD.end_ts IS NOT NEW.end_ts OR OLD.text_value IS NOT NEW.text_value
  OR OLD.metric_id IS NOT NEW.metric_id OR OLD.source_id IS NOT NEW.source_id
BEGIN
    INSERT INTO rollup_dirty (source_id, metric_id, utc_day)
    SELECT OLD.source_id, OLD.metric_id, substr(OLD.ts, 1, 10)
    WHERE NOT EXISTS (SELECT 1 FROM rollup_dirty
                      WHERE source_id = OLD.source_id AND metric_id = OLD.metric_id
                        AND utc_day = substr(OLD.ts, 1, 10));
    INSERT INTO rollup_dirty (source_id, metric_id, utc_day)
    SELECT NEW.source_id, NEW.metric_id, substr(NEW.ts, 1, 10)
    WHERE NOT EXISTS (SELECT 1 FROM rollup_dirty
                      WHERE source_id = NEW.source_id AND metric_id = NEW.metric_id
                        AND utc_day = substr(NEW.ts, 1, 10));
END;

CREATE TRIGGER observations_dirty_delete AFTER DELETE ON observations
BEGIN
    INSERT INTO rollup_dirty (source_id, metric_id, utc_day)
    SELECT OLD.source_id, OLD.metric_id, substr(OLD.ts, 1, 10)
    WHERE NOT EXISTS (SELECT 1 FROM rollup_dirty
                      WHERE source_id = OLD.source_id AND metric_id = OLD.metric_id
                        AND utc_day = substr(OLD.ts, 1, 10));
END;

CREATE TABLE writer_probe (
    id  INTEGER PRIMARY KEY CHECK (id = 1),
    at  TEXT NOT NULL
);

-- Rollups written before this migration may be stale after an earlier
-- crash, so every existing day is rebuilt once by the next writer.
INSERT OR IGNORE INTO rollup_dirty (source_id, metric_id, utc_day)
SELECT DISTINCT source_id, metric_id, substr(ts, 1, 10) FROM observations;
