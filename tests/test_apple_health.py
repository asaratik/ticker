"""
Tests for the Apple Health import source.

The streaming property is the one that matters and the one that is easy to
lose: a refactor that drops the `root.clear()` still passes every parsing
test and only fails on a real gigabyte export, on the user's machine, after
twenty minutes. So it is tested directly -- a synthetic export large enough
that holding the tree would show up, with the parser's own memory watched
across it.

Everything else here is about not writing wrong numbers: units are converted
or the record is skipped, and the two HealthKit types that have no honest
home in the seed metrics are asserted to stay out.
"""

import gc
import zipfile
from datetime import timezone
from pathlib import Path
from xml.etree import ElementTree

import pytest

from ticker.sources import base
from ticker.sources.apple_health import (QUANTITY_TYPES, AppleHealthSource,
                                         parse_stamp)


def _export(records, path: Path) -> Path:
    """Write a minimal but structurally real export.xml."""
    body = "\n".join(records)
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<HealthData locale="en_US">\n'
        '<ExportDate value="2026-01-15 10:00:00 -0500"/>\n'
        '{}\n</HealthData>\n'.format(body),
        encoding="utf-8")
    return path


def _record(type_, value, start="2026-01-14 07:03:00 -0500",
            end=None, unit="count/min", source="Apple Watch"):
    end = end or start
    return ('<Record type="{}" sourceName="{}" unit="{}" '
            'startDate="{}" endDate="{}" value="{}"/>').format(
                type_, source, unit, start, end, value)


def _parse(records, tmp_path, **kwargs):
    source = AppleHealthSource(**kwargs)
    path = _export(records, tmp_path / "export.xml")
    return source, list(source.parse(path))


# -- Protocol ---------------------------------------------------------------

def test_it_satisfies_the_import_source_protocol():
    assert isinstance(AppleHealthSource(), base.ImportSource)


def test_health_never_raises():
    assert AppleHealthSource().health().ok is True


# -- Streaming, which is the requirement ------------------------------------

def test_parse_is_a_generator_and_not_a_list():
    """A list would mean the whole export is materialised before use."""
    import inspect
    assert inspect.isgeneratorfunction(AppleHealthSource.parse)


def test_memory_stays_flat_across_a_large_export(tmp_path):
    """The tree must not accumulate: this is what root.clear() buys.

    Counting live XML elements rather than bytes -- it is the tree growing
    that matters, and it is a signal that doesn't wobble with the allocator.
    """
    records = [_record("HKQuantityTypeIdentifierHeartRate", 60 + (n % 30),
                       start="2026-01-14 07:{:02d}:{:02d} -0500".format(
                           n // 60 % 60, n % 60))
               for n in range(20000)]
    path = _export(records, tmp_path / "export.xml")

    source = AppleHealthSource()
    peaks = []
    for index, _observation in enumerate(source.parse(path)):
        if index % 5000 == 0:
            gc.collect()
            peaks.append(sum(1 for obj in gc.get_objects()
                             if isinstance(obj, ElementTree.Element)))

    assert len(peaks) >= 3
    # Live elements must not grow with how far through the file we are.
    assert max(peaks) - min(peaks) < 100, (
        "XML elements accumulated across the parse: {}".format(peaks))


def test_every_record_in_a_large_export_is_seen(tmp_path):
    records = [_record("HKQuantityTypeIdentifierHeartRate", 60,
                       start="2026-01-14 07:00:{:02d} -0500".format(n % 60))
               for n in range(5000)]
    source, got = _parse(records, tmp_path)
    assert source.seen == 5000
    assert len(got) == 5000


# -- Timestamps -------------------------------------------------------------

def test_apple_timestamps_carry_their_own_offset():
    stamp = parse_stamp("2026-01-14 07:03:00 -0500")
    assert stamp.utcoffset().total_seconds() == -5 * 3600
    assert stamp.astimezone(timezone.utc).hour == 12


def test_a_malformed_timestamp_is_skipped_not_guessed(tmp_path):
    source, got = _parse(
        [_record("HKQuantityTypeIdentifierHeartRate", 60, start="yesterday")],
        tmp_path)
    assert got == []
    assert source.skipped_malformed == 1


def test_every_observation_is_timezone_aware(tmp_path):
    _source, got = _parse(
        [_record("HKQuantityTypeIdentifierHeartRate", 60)], tmp_path)
    assert got[0].ts.tzinfo is not None


# -- Units, and refusing to guess -------------------------------------------

def test_heart_rate_comes_through_unchanged(tmp_path):
    _source, got = _parse(
        [_record("HKQuantityTypeIdentifierHeartRate", 61)], tmp_path)
    assert got[0].metric == "heart_rate_bpm"
    assert got[0].value == 61.0


def test_pounds_are_converted_to_kilograms(tmp_path):
    _source, got = _parse(
        [_record("HKQuantityTypeIdentifierBodyMass", 165, unit="lb")],
        tmp_path)
    assert got[0].metric == "weight_kg"
    assert got[0].value == pytest.approx(74.84, abs=0.01)


def test_stone_is_converted_too(tmp_path):
    _source, got = _parse(
        [_record("HKQuantityTypeIdentifierBodyMass", 11, unit="st")], tmp_path)
    assert got[0].value == pytest.approx(69.85, abs=0.01)


def test_kilojoules_are_converted_to_kilocalories(tmp_path):
    _source, got = _parse(
        [_record("HKQuantityTypeIdentifierActiveEnergyBurned", 1000,
                 unit="kJ")], tmp_path)
    assert got[0].value == pytest.approx(239.0, abs=0.1)


def test_apples_capital_cal_is_a_kilocalorie(tmp_path):
    _source, got = _parse(
        [_record("HKQuantityTypeIdentifierActiveEnergyBurned", 420,
                 unit="Cal")], tmp_path)
    assert got[0].value == pytest.approx(420.0)


def test_an_unrecognised_unit_is_skipped_and_counted(tmp_path):
    """Taking a furlong at face value is how a metric gets corrupted."""
    source, got = _parse(
        [_record("HKQuantityTypeIdentifierBodyMass", 165, unit="furlong")],
        tmp_path)
    assert got == []
    assert source.skipped_unknown_unit == 1
    assert "furlong" in source.report()


def test_a_non_numeric_value_is_skipped(tmp_path):
    source, got = _parse(
        [_record("HKQuantityTypeIdentifierHeartRate", "n/a")], tmp_path)
    assert got == []
    assert source.skipped_malformed == 1


# -- The two deliberate omissions -------------------------------------------

def test_sdnn_is_not_written_into_the_rmssd_metric(tmp_path):
    """They are different measures; sharing a series would corrupt both."""
    source, got = _parse([_record(
        "HKQuantityTypeIdentifierHeartRateVariabilitySDNN", 42, unit="ms")],
        tmp_path)
    assert got == []
    assert source.skipped_unknown_type == 1


def test_wrist_temperature_is_not_written_as_a_delta(tmp_path):
    source, got = _parse([_record(
        "HKQuantityTypeIdentifierAppleSleepingWristTemperature", 36.2,
        unit="degC")], tmp_path)
    assert got == []


def test_no_mapped_type_targets_a_metric_outside_the_seed_set():
    import re
    seed = Path("ticker/db/migrations/0002_multi_source.sql").read_text()
    seeded = set(re.findall(r"\('([a-z0-9_]+)'\s*,", seed))
    assert AppleHealthSource().capabilities() <= seeded


# -- Intervals and sleep ----------------------------------------------------

def test_a_point_sample_has_no_end(tmp_path):
    """start == end in the export means an instant, not a zero-width span."""
    _source, got = _parse(
        [_record("HKQuantityTypeIdentifierHeartRate", 61,
                 start="2026-01-14 07:03:00 -0500",
                 end="2026-01-14 07:03:00 -0500")], tmp_path)
    assert got[0].end_ts is None


def test_a_step_count_keeps_its_interval(tmp_path):
    _source, got = _parse(
        [_record("HKQuantityTypeIdentifierStepCount", 240, unit="count",
                 start="2026-01-14 07:00:00 -0500",
                 end="2026-01-14 07:10:00 -0500")], tmp_path)
    assert got[0].end_ts is not None
    assert (got[0].end_ts - got[0].ts).total_seconds() == 600


def test_sleep_stages_are_mapped_with_their_duration(tmp_path):
    record = ('<Record type="HKCategoryTypeIdentifierSleepAnalysis" '
              'sourceName="Apple Watch" startDate="2026-01-14 23:00:00 -0500" '
              'endDate="2026-01-14 23:30:00 -0500" '
              'value="HKCategoryValueSleepAnalysisAsleepDeep"/>')
    _source, got = _parse([record], tmp_path)
    assert got[0].metric == "sleep_stage"
    assert got[0].text_value == "deep"
    assert got[0].value == 1800


def test_in_bed_is_not_counted_as_sleep(tmp_path):
    """Lying down is not a stage; counting it would inflate every night."""
    record = ('<Record type="HKCategoryTypeIdentifierSleepAnalysis" '
              'sourceName="Apple Watch" startDate="2026-01-14 22:30:00 -0500" '
              'endDate="2026-01-14 23:00:00 -0500" '
              'value="HKCategoryValueSleepAnalysisInBed"/>')
    _source, got = _parse([record], tmp_path)
    assert got == []


def test_an_older_export_without_stage_detail_keeps_its_own_value(tmp_path):
    """Calling unspecified sleep 'light' would invent precision."""
    record = ('<Record type="HKCategoryTypeIdentifierSleepAnalysis" '
              'sourceName="iPhone" startDate="2026-01-14 23:00:00 -0500" '
              'endDate="2026-01-15 06:00:00 -0500" '
              'value="HKCategoryValueSleepAnalysisAsleepUnspecified"/>')
    _source, got = _parse([record], tmp_path)
    assert got[0].text_value == "asleep"


def test_a_sleep_record_with_no_span_is_skipped(tmp_path):
    record = ('<Record type="HKCategoryTypeIdentifierSleepAnalysis" '
              'sourceName="Apple Watch" startDate="2026-01-14 23:00:00 -0500" '
              'endDate="2026-01-14 23:00:00 -0500" '
              'value="HKCategoryValueSleepAnalysisAsleepDeep"/>')
    source, got = _parse([record], tmp_path)
    assert got == []
    assert source.skipped_malformed == 1


# -- Identity ---------------------------------------------------------------

def test_two_devices_at_one_instant_do_not_collide(tmp_path):
    """A phone and a watch both counting steps must not overwrite each other."""
    records = [
        _record("HKQuantityTypeIdentifierStepCount", 100, unit="count",
                source="iPhone", end="2026-01-14 07:10:00 -0500"),
        _record("HKQuantityTypeIdentifierStepCount", 130, unit="count",
                source="Apple Watch", end="2026-01-14 07:10:00 -0500"),
    ]
    _source, got = _parse(records, tmp_path)
    assert got[0].external_id != got[1].external_id


# -- Files ------------------------------------------------------------------

def test_a_zip_export_is_read_without_extracting(tmp_path):
    inner = _export([_record("HKQuantityTypeIdentifierHeartRate", 61)],
                    tmp_path / "export.xml")
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(inner, "apple_health_export/export.xml")
    inner.unlink()

    got = list(AppleHealthSource().parse(archive))
    assert len(got) == 1


def test_a_zip_without_an_export_is_a_permanent_error(tmp_path):
    archive = tmp_path / "wrong.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("readme.txt", "not an export")
    with pytest.raises(base.PermanentError):
        list(AppleHealthSource().parse(archive))


def test_a_missing_file_is_a_permanent_error(tmp_path):
    with pytest.raises(base.PermanentError):
        list(AppleHealthSource().parse(tmp_path / "nope.xml"))


def test_truncated_xml_is_a_permanent_error(tmp_path):
    path = tmp_path / "export.xml"
    path.write_text('<HealthData><Record type="x" ', encoding="utf-8")
    with pytest.raises(base.PermanentError):
        list(AppleHealthSource().parse(path))


def test_progress_is_reported_for_long_imports(tmp_path):
    records = [_record("HKQuantityTypeIdentifierHeartRate", 60)
               for _ in range(250)]
    ticks = []
    _source, _got = _parse(records, tmp_path, on_progress=ticks.append,
                           progress_every=100)
    assert ticks == [100, 200]


def test_the_report_says_what_was_skipped(tmp_path):
    records = [
        _record("HKQuantityTypeIdentifierHeartRate", 61),
        _record("HKQuantityTypeIdentifierBodyMass", 80, unit="furlong"),
        _record("HKQuantityTypeIdentifierHeartRateVariabilitySDNN", 42,
                unit="ms"),
    ]
    source, _got = _parse(records, tmp_path)
    report = source.report()
    assert "3 records read" in report
    assert "1 observations" in report
    assert "furlong" in report


def test_every_quantity_type_maps_to_a_capability():
    for metric, units in QUANTITY_TYPES.values():
        assert metric in AppleHealthSource().capabilities()
        assert units, "a type with no accepted unit can never emit anything"
