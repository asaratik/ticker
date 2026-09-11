"""
Tests for importing Garmin's files: .fit files and the "Export Your Data"
archive.

FIT files are built with tests/fitbuild.py; the archive is assembled here
from the layout Garmin uses (DI_CONNECT/...). Garmin publishes no schema
for the archive's JSON, so what these pin is the behaviour around it: known
fields land, unknown ones are skipped, and a file that yields nothing is
named in the report rather than passed over in silence.
"""

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

import fitbuild
from ticker.db import store
from ticker.ingest import importer
from ticker.mcp.readonly import ReadOnlyDatabase
from ticker.mcp.tools import Tools
from ticker.sources.base import PermanentError
from ticker.sources.garmin_files import GarminFiles, garmin_time

UTC = timezone.utc
RUN = datetime(2026, 5, 2, 7, 0, tzinfo=UTC)
NIGHT = datetime(2026, 5, 1, 22, 0, tzinfo=UTC)


def parse(path, **kwargs):
    source = GarminFiles(**kwargs)
    return source, list(source.parse(path))


def write(path, data):
    path.write_bytes(data)
    return path


def export_zip(path, sleep_entries=None, daily_entries=None, activity=None,
               unknown_sleep=False):
    """Garmin's archive, in the shape Garmin lays it out."""
    with zipfile.ZipFile(path, "w") as archive:
        if sleep_entries is not None:
            archive.writestr("DI_CONNECT/DI-Connect-Wellness/"
                             "2026-04-01_2026-05-31_123_sleepData.json",
                             json.dumps(sleep_entries))
        if unknown_sleep:
            archive.writestr("DI_CONNECT/DI-Connect-Wellness/"
                             "2025-01-01_2025-03-31_123_sleepData.json",
                             json.dumps([{"somethingElse": 1}]))
        if daily_entries is not None:
            archive.writestr("DI_CONNECT/DI-Connect-Aggregator/"
                             "UDSFile_2026-04-01_2026-05-31.json",
                             json.dumps(daily_entries))
        if activity is not None:
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w") as uploads:
                uploads.writestr("run.fit", activity)
            archive.writestr("DI_CONNECT/DI-Connect-Uploaded-Files/"
                             "UploadedFiles_0-_Part1.zip", inner.getvalue())
    return path


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "garmin.sqlite3"
    conn = store.connect(path)
    writer = store.AsyncStore(path, migrate_first=False)
    yield path, conn, writer
    writer.close()
    conn.close()


# -- FIT files ---------------------------------------------------------------

def test_a_workout_becomes_a_session_with_heart_rate_and_beats(tmp_path):
    sessions = []
    source, observations = parse(write(tmp_path / "run.fit", fitbuild.activity(RUN)),
                                 on_session=sessions.append)
    session, = sessions
    assert (session.kind, session.label) == ("workout", "Run")
    assert session.start_ts == RUN and session.end_ts == RUN + timedelta(minutes=10)

    heart = [o for o in observations if o.metric == "heart_rate_bpm"]
    assert [o.value for o in heart] == [120, 125, 130]
    assert {o.session_key for o in heart} == {session.key}

    beats = [o for o in observations if o.metric == "rr_interval_ms"]
    assert [o.value for o in beats] == [800, 810, 790]
    # No timestamps in the file for beats: they accumulate from the start.
    assert beats[0].ts == RUN + timedelta(milliseconds=800)
    assert beats[2].ts == RUN + timedelta(milliseconds=2400)
    assert len({o.external_id for o in beats}) == 3
    assert "1 workouts" in source.report()


def test_all_day_monitoring_follows_its_16_bit_timestamps(tmp_path):
    start = datetime(2026, 5, 2, 0, 0, tzinfo=UTC)
    source, observations = parse(write(tmp_path / "day.fit",
                                       fitbuild.monitoring(start)))
    assert [(o.ts, o.value) for o in observations] == [
        (start, 60), (start + timedelta(minutes=1), 62),
        (start + timedelta(minutes=2), 64)]
    assert all(o.session_key is None for o in observations)


def test_sleep_levels_become_stage_segments(tmp_path):
    source, observations = parse(write(tmp_path / "sleep.fit",
                                       fitbuild.sleep(NIGHT)))
    # light, deep, rem, (unmeasurable: skipped), awake -- and the last level
    # has no known end, so it is dropped rather than given an invented one.
    assert [(o.text_value, o.value) for o in observations] == [
        ("light", 600), ("deep", 600), ("rem", 600), ("awake", 600)]
    assert observations[0].end_ts == NIGHT + timedelta(minutes=10)


def test_a_file_that_is_not_fit_is_counted_not_fatal(tmp_path):
    archive = tmp_path / "files.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("broken.fit", b"not really")
        z.writestr("run.fit", fitbuild.activity(RUN))
    source, observations = parse(archive)
    assert observations
    assert "1 unreadable FIT files" in source.report()


def test_something_that_is_neither_fit_nor_zip_is_refused(tmp_path):
    with pytest.raises(PermanentError, match="neither"):
        parse(write(tmp_path / "notes.txt", b"hello"))


# -- the export archive ---------------------------------------------------------

SLEEP = [{"calendarDate": "2026-05-02",
          "sleepStartTimestampGMT": "2026-05-01T22:10:00.0",
          "sleepEndTimestampGMT": "2026-05-02T06:10:00.0",
          "deepSleepSeconds": 5400, "lightSleepSeconds": 14400,
          "remSleepSeconds": 5400, "awakeSleepSeconds": 1200}]

DAILY = [{"calendarDate": "2026-05-02", "totalSteps": 10234,
          "activeKilocalories": 540, "restingHeartRate": 52,
          "averageStressLevel": -1}]


def test_the_export_yields_nights_days_and_the_workouts_inside_it(tmp_path):
    sessions = []
    path = export_zip(tmp_path / "garmin.zip", SLEEP, DAILY, fitbuild.activity(RUN))
    source, observations = parse(path, on_session=sessions.append)

    night, = [o for o in observations if o.metric == "sleep_duration_s"]
    assert night.value == 25200                     # deep + light + REM
    assert night.ts == datetime(2026, 5, 1, 22, 10, tzinfo=UTC)
    assert night.end_ts == datetime(2026, 5, 2, 6, 10, tzinfo=UTC)

    daily = {o.metric: o.value for o in observations if o.external_id.startswith("export:day")}
    # -1 is Garmin's 'not measured', and is skipped rather than stored.
    assert daily == {"steps": 10234, "active_energy_kcal": 540,
                     "resting_heart_rate_bpm": 52}
    assert len(sessions) == 1                       # the nested activity
    report = source.report()
    assert "1 nights" in report and "1 daily summaries" in report
    assert "1 workouts" in report


def test_a_file_whose_fields_are_unknown_is_named_in_the_report(tmp_path):
    source, observations = parse(export_zip(tmp_path / "garmin.zip", SLEEP,
                                            unknown_sleep=True))
    assert len(observations) == 1
    assert "nothing recognised in 2025-01-01_2025-03-31_123_sleepData.json" in \
        source.report()


def test_garmin_times_in_both_spellings():
    assert garmin_time("2019-08-04T20:28:00.0") == datetime(2019, 8, 4, 20, 28, tzinfo=UTC)
    assert garmin_time(1564950480000) == datetime(2019, 8, 4, 20, 28, tzinfo=UTC)
    assert garmin_time("yesterday") is None and garmin_time(True) is None


# -- the importer --------------------------------------------------------------

def test_detection_tells_the_formats_apart(tmp_path):
    assert importer.detect(write(tmp_path / "a.fit", b"x")) == "garmin"
    assert importer.detect(export_zip(tmp_path / "g.zip", SLEEP)) == "garmin"
    apple = tmp_path / "export.zip"
    with zipfile.ZipFile(apple, "w") as z:
        z.writestr("apple_health_export/export.xml", "<HealthData/>")
    assert importer.detect(apple) == "apple_health"
    with pytest.raises(PermanentError, match="can't tell"):
        importer.detect(write(tmp_path / "x.csv", b"a,b"))


def test_importing_a_workout_twice_changes_nothing(db, tmp_path):
    path, conn, writer = db
    run = write(tmp_path / "run.fit", fitbuild.activity(RUN))
    importer.run_import(conn, writer, "garmin", run)
    importer.run_import(conn, writer, "garmin", run)
    assert writer.flush()
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE kind = 'workout'"
                        ).fetchone() == (1,)
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] > 0
    counts = conn.execute("SELECT COUNT(*) FROM observations o JOIN sources s "
                          "ON s.id = o.source_id WHERE s.display_name = 'Garmin files'"
                          ).fetchone()[0]
    assert counts == 3 + 3                          # heart rate + beats
    # HRV is derived from the beats as they pass the normalizer; three beats
    # are not enough for it, which is the derivation's rule, not a bug here.


def test_imported_sleep_is_read_back_by_get_sleep(db, tmp_path):
    path, conn, writer = db
    importer.run_import(conn, writer, "garmin",
                        write(tmp_path / "sleep.fit", fitbuild.sleep(NIGHT)))
    assert writer.flush()
    readonly = ReadOnlyDatabase(path)
    try:
        tools = Tools(readonly, zone=UTC, now=lambda: NIGHT + timedelta(days=1))
        # The last segment ends 22:50 the same evening, and a night is
        # labelled with the date it ends on.
        result = tools.call("get_sleep", {"start": "2026-05-01", "end": "2026-05-01"})
    finally:
        readonly.close()
    row = dict(zip(result["columns"], result["rows"][0]))
    assert (row["light_min"], row["deep_min"], row["rem_min"], row["awake_min"]) == \
        (10, 10, 10, 10)


def test_the_cli_works_out_the_format_itself(tmp_path, capsys):
    db_path = tmp_path / "cli.sqlite3"
    store.connect(db_path).close()
    run = write(tmp_path / "run.fit", fitbuild.activity(RUN))
    assert importer.main([str(run), "--db", str(db_path), "--quiet"]) == 0
    assert "1 workouts" in capsys.readouterr().out
