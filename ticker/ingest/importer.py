"""
Import a file export into the database.

    ticker import export.zip                 # Apple Health, detected
    ticker import garmin-export.zip          # Garmin's data export, detected
    ticker import activity.fit
    ticker import apple_health export.xml --name "iPhone"

(Or from the page: Connect -> Import a file, which runs this in the app.)

The counterpart to `ticker.ingest.sync` for sources that have no API to
poll. Section 7's Apple Health row is the case this exists for: there is no
server side, so the only way in is the zip the Health app produces.

Streaming all the way through. `parse()` is a generator, the normalizer
batches into the writer, and nothing accumulates the file -- an export is
routinely over a gigabyte and the whole point of the ImportSource protocol
is that its size is not the caller's problem.

Re-importing the same export is safe and is the expected way to use this:
the store dedupes on (source, metric, ts, external_id), so a later export
that overlaps an earlier one updates what changed and adds what is new.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import zipfile
from pathlib import Path

from ticker import config as tconfig
from ticker.db import store
from ticker.ingest.normalizer import Normalizer
from ticker.sources.apple_health import AppleHealthSource
from ticker.sources.base import PermanentError
from ticker.sources.garmin_files import GarminFiles

log = logging.getLogger("ticker.importer")

# vendor -> (factory, default display name). Garmin's files are named apart
# from a Garmin Connect account ("Garmin"), so the two are separate sources
# and a day both report is never added together.
IMPORTERS = {
    "apple_health": (AppleHealthSource, "Apple Health"),
    "garmin": (GarminFiles, "Garmin files"),
}


def detect(path: Path) -> str:
    """Which importer a file is for, from its name and -- for a zip -- what
    is inside it."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".fit":
        return "garmin"
    if suffix == ".xml":
        return "apple_health"
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = [name.lower() for name in archive.namelist()]
        if any(name.endswith("export.xml") for name in names):
            return "apple_health"
        if any(name.startswith("di_connect/") or "/di_connect/" in name
               or name.endswith(".fit") for name in names):
            return "garmin"
    raise PermanentError(
        "can't tell what {} is. Ticker imports Apple Health exports "
        "(export.zip or export.xml), Garmin's data export, and .fit "
        "files.".format(path.name))


def run_import(conn, writer, vendor: str, path: Path,
               display_name: str = "", progress=None) -> dict:
    """Parse `path` and write what it yields. Returns a summary.

    The source row is created on first import and reused afterwards, so a
    year of monthly exports all land under one source rather than making a
    new one each time.
    """
    if vendor not in IMPORTERS:
        raise PermanentError("no importer for {!r}".format(vendor))
    factory, default_name = IMPORTERS[vendor]
    name = display_name or default_name

    source_id = store.ensure_source(conn, "import", vendor, name)
    connector = factory(on_progress=progress)
    if hasattr(connector, "on_session"):
        # Workouts in a file become sessions. Straight to the writer, which
        # applies work in order -- ahead of the readings that refer to them.
        connector.on_session = lambda record: writer.begin_session(source_id, record)
    normalizer = Normalizer(writer, source_id)

    started = time.monotonic()
    # feed() consumes the generator lazily, so the export is never all in
    # memory at once.
    normalizer.feed(connector.parse(path))
    normalizer.flush()
    elapsed = time.monotonic() - started

    return {
        "source_id": source_id,
        "read": connector.seen,
        "accepted": normalizer.accepted,
        "rejected": normalizer.rejected,
        "seconds": elapsed,
        "detail": connector.report(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("target", nargs="+", metavar="[FORMAT] PATH",
                        help="the file to import, optionally after its format "
                             "({}); left out, it is worked out from the "
                             "file".format(", ".join(sorted(IMPORTERS))))
    parser.add_argument("--name", default="",
                        help="display name, if you import from more than one "
                             "device")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true",
                        help="no progress output")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    if len(args.target) == 2 and args.target[0] in IMPORTERS:
        vendor, path = args.target[0], Path(args.target[1])
    elif len(args.target) == 1:
        vendor, path = None, Path(args.target[0])
    else:
        parser.error("give the file to import, optionally after its format")
    if not path.exists():
        print("no such file: {}".format(path), file=sys.stderr)
        return 1
    if vendor is None:
        try:
            vendor = detect(path)
        except PermanentError as exc:
            print(exc, file=sys.stderr)
            return 1

    def progress(count):
        # A gigabyte export takes minutes; silence for that long looks like
        # a hang. Carriage return so it stays on one line.
        print("\r  {:,} records read...".format(count), end="", flush=True)

    db_path = args.db or tconfig.DB_PATH
    conn = store.connect(db_path)
    writer = store.AsyncStore(db_path, migrate_first=False)
    try:
        summary = run_import(conn, writer, vendor, path,
                             display_name=args.name,
                             progress=None if args.quiet else progress)
    except PermanentError as exc:
        print("\n{}".format(exc), file=sys.stderr)
        return 1
    finally:
        # Commit whatever is still coalescing, then bring the rollups up to
        # date over the days this import actually touched -- an import lands
        # history, so its rollups are almost always stale.
        writer.rebuild_rollups()
        writer.close()
        conn.close()

    if not args.quiet:
        print("\r" + " " * 40 + "\r", end="")
    print("imported into source #{} in {:.1f}s".format(
        summary["source_id"], summary["seconds"]))
    print("  {}".format(summary["detail"]))
    if summary["rejected"]:
        print("  {} rejected by the sanity ranges".format(summary["rejected"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
