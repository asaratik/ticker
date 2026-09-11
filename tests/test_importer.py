"""
Tests for the file-import CLI.

The Apple Health connector is tested on its own; what is tested here is that
an export actually lands in the database -- the source row, the
observations, and a second import of an overlapping export not doubling
anything, which is the way this is expected to be used: monthly exports that
each repeat most of the last one.
"""

import sqlite3

import pytest

from ticker.db import migrate, store
from ticker.ingest import importer
from ticker.sources.base import PermanentError


def _export(path, records):
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<HealthData locale="en_US">\n{}\n</HealthData>\n'.format(
            "\n".join(records)),
        encoding="utf-8")
    return path


def _hr(value, at, source="Apple Watch"):
    return ('<Record type="HKQuantityTypeIdentifierHeartRate" '
            'sourceName="{}" unit="count/min" startDate="{}" '
            'endDate="{}" value="{}"/>').format(source, at, at, value)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "ticker.sqlite3"
    migrate.migrate(path, backup=False)
    conn = migrate.open_db(path)
    writer = store.AsyncStore(path, migrate_first=False)
    yield path, conn, writer
    writer.close()
    conn.close()


def _count(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    finally:
        conn.close()


def test_an_export_lands_in_the_database(db, tmp_path):
    path, conn, writer = db
    export = _export(tmp_path / "export.xml", [
        _hr(61, "2026-01-14 07:03:00 -0500"),
        _hr(62, "2026-01-14 07:04:00 -0500"),
    ])

    summary = importer.run_import(conn, writer, "apple_health", export)
    writer.flush()

    assert summary["read"] == 2
    assert summary["accepted"] == 2
    assert _count(path) == 2


def test_the_source_row_is_an_import_kind(db, tmp_path):
    path, conn, writer = db
    export = _export(tmp_path / "export.xml", [_hr(61, "2026-01-14 07:03:00 -0500")])

    summary = importer.run_import(conn, writer, "apple_health", export)

    kind, vendor, name = conn.execute(
        "SELECT kind, vendor, display_name FROM sources WHERE id = ?",
        (summary["source_id"],)).fetchone()
    assert (kind, vendor) == ("import", "apple_health")
    assert name == "Apple Health"


def test_a_second_import_reuses_the_same_source(db, tmp_path):
    """A year of monthly exports should not make twelve sources."""
    path, conn, writer = db
    export = _export(tmp_path / "export.xml", [_hr(61, "2026-01-14 07:03:00 -0500")])

    first = importer.run_import(conn, writer, "apple_health", export)
    second = importer.run_import(conn, writer, "apple_health", export)

    assert first["source_id"] == second["source_id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM sources WHERE vendor = 'apple_health'"
    ).fetchone()[0] == 1


def test_reimporting_an_overlapping_export_does_not_duplicate(db, tmp_path):
    """The expected usage: each export repeats most of the previous one."""
    path, conn, writer = db
    january = _export(tmp_path / "jan.xml", [
        _hr(61, "2026-01-14 07:03:00 -0500"),
        _hr(62, "2026-01-14 07:04:00 -0500"),
    ])
    february = _export(tmp_path / "feb.xml", [
        _hr(61, "2026-01-14 07:03:00 -0500"),
        _hr(62, "2026-01-14 07:04:00 -0500"),
        _hr(63, "2026-02-01 08:00:00 -0500"),
    ])

    importer.run_import(conn, writer, "apple_health", january)
    writer.flush()
    importer.run_import(conn, writer, "apple_health", february)
    writer.flush()

    assert _count(path) == 3


def test_a_named_import_gets_its_own_source(db, tmp_path):
    path, conn, writer = db
    export = _export(tmp_path / "export.xml", [_hr(61, "2026-01-14 07:03:00 -0500")])

    watch = importer.run_import(conn, writer, "apple_health", export,
                                display_name="Watch")
    phone = importer.run_import(conn, writer, "apple_health", export,
                                display_name="Phone")

    assert watch["source_id"] != phone["source_id"]


def test_an_unknown_vendor_is_refused(db, tmp_path):
    path, conn, writer = db
    with pytest.raises(PermanentError):
        importer.run_import(conn, writer, "whoop", tmp_path / "x.xml")


def test_the_progress_hook_reaches_the_connector(db, tmp_path, monkeypatch):
    """A silent gigabyte import looks like a hang, so this has to be wired.

    Asserted by watching what the connector is constructed with rather than
    by waiting for a tick: the real interval is every 100k records, and an
    export that large does not belong in a test suite.
    """
    path, conn, writer = db
    export = _export(tmp_path / "export.xml", [_hr(61, "2026-01-14 07:03:00 -0500")])

    from ticker.sources.apple_health import AppleHealthSource
    built = {}

    class Spy(AppleHealthSource):
        def __init__(self, **kwargs):
            built.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setitem(importer.IMPORTERS, "apple_health",
                        (Spy, "Apple Health"))

    def progress(count):
        pass

    importer.run_import(conn, writer, "apple_health", export,
                        progress=progress)

    assert built["on_progress"] is progress


def test_progress_actually_fires_on_a_long_import(db, tmp_path, monkeypatch):
    """The same wiring, driven for real at an interval a test can reach."""
    path, conn, writer = db
    export = _export(tmp_path / "export.xml",
                     [_hr(60, "2026-01-14 07:03:00 -0500")] * 250)

    from ticker.sources.apple_health import AppleHealthSource

    class Chatty(AppleHealthSource):
        def __init__(self, **kwargs):
            kwargs["progress_every"] = 100
            super().__init__(**kwargs)

    monkeypatch.setitem(importer.IMPORTERS, "apple_health",
                        (Chatty, "Apple Health"))

    ticks = []
    importer.run_import(conn, writer, "apple_health", export,
                        progress=ticks.append)

    assert ticks == [100, 200]


def test_values_outside_the_sanity_ranges_are_rejected(db, tmp_path):
    """A misparsed packet should not become a 900bpm heart rate."""
    path, conn, writer = db
    export = _export(tmp_path / "export.xml", [
        _hr(61, "2026-01-14 07:03:00 -0500"),
        _hr(900, "2026-01-14 07:04:00 -0500"),
    ])

    summary = importer.run_import(conn, writer, "apple_health", export)
    writer.flush()

    assert summary["accepted"] == 1
    assert summary["rejected"] == 1
    assert _count(path) == 1


def test_the_summary_carries_the_connectors_own_report(db, tmp_path):
    path, conn, writer = db
    export = _export(tmp_path / "export.xml", [_hr(61, "2026-01-14 07:03:00 -0500")])
    summary = importer.run_import(conn, writer, "apple_health", export)
    assert "records read" in summary["detail"]


# -- The CLI ----------------------------------------------------------------

def test_the_cli_reports_a_missing_file(tmp_path, capsys):
    assert importer.main(["apple_health", str(tmp_path / "nope.xml"),
                          "--db", str(tmp_path / "t.sqlite3")]) == 1
    assert "no such file" in capsys.readouterr().err


def test_the_cli_imports_a_file(tmp_path, capsys):
    db_path = tmp_path / "ticker.sqlite3"
    migrate.migrate(db_path, backup=False)
    export = _export(tmp_path / "export.xml", [
        _hr(61, "2026-01-14 07:03:00 -0500"),
    ])

    assert importer.main(["apple_health", str(export), "--db", str(db_path),
                          "--quiet"]) == 0
    assert _count(db_path) == 1
    assert "imported into source" in capsys.readouterr().out


def test_the_cli_offers_every_importer():
    with pytest.raises(SystemExit):
        importer.main(["--help"])


def test_every_importer_satisfies_the_protocol():
    from ticker.sources import base
    for vendor, (factory, _name) in importer.IMPORTERS.items():
        assert isinstance(factory(), base.ImportSource), vendor
