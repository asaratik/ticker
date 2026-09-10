"""
Import a file export into the database.

    python -m ticker.ingest.importer apple_health export.zip
    python -m ticker.ingest.importer apple_health export.xml --name "iPhone"

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
from pathlib import Path

from ticker import config as tconfig
from ticker.db import store
from ticker.ingest.normalizer import Normalizer
from ticker.sources.apple_health import AppleHealthSource
from ticker.sources.base import PermanentError

log = logging.getLogger("ticker.importer")

# vendor -> (factory, default display name)
IMPORTERS = {
    "apple_health": (AppleHealthSource, "Apple Health"),
}


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
    parser.add_argument("vendor", choices=sorted(IMPORTERS))
    parser.add_argument("path", type=Path, help="the export file or zip")
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

    if not args.path.exists():
        print("no such file: {}".format(args.path), file=sys.stderr)
        return 1

    def progress(count):
        # A gigabyte export takes minutes; silence for that long looks like
        # a hang. Carriage return so it stays on one line.
        print("\r  {:,} records read...".format(count), end="", flush=True)

    db_path = args.db or tconfig.DB_PATH
    conn = store.connect(db_path)
    writer = store.AsyncStore(db_path, migrate_first=False)
    try:
        summary = run_import(conn, writer, args.vendor, args.path,
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
