-- 0003: metrics Garmin reports that no source did before.
--
-- Seed rows only; no DDL, so schema.sql is unchanged. INSERT OR IGNORE
-- because the registry is extensible (store.ensure_metric) and a database
-- may already have registered one of these names itself.
--
-- Resting heart rate is its own metric rather than more heart_rate_bpm rows:
-- it is one figure a day that the vendor derives, and mixed into the
-- readings it would drag every daily average down.

INSERT OR IGNORE INTO metrics (name, unit, value_kind, description) VALUES
  ('resting_heart_rate_bpm', 'bpm',   'instant', 'Resting heart rate for the day, as the vendor computes it'),
  ('stress_level',           'score', 'instant', 'Stress score from 0 to 100 (Garmin)'),
  ('body_battery',           'score', 'instant', 'Body Battery energy level from 0 to 100 (Garmin)');
