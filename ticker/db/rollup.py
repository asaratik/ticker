"""
Daily rollup maintenance.

Two ways in:

* Incrementally, from the writer thread -- AsyncStore.rebuild_rollups()
  drains the days a commit actually changed. That is the normal path and
  costs nothing when nothing changed.
* In bulk, from here -- for a database whose observations predate rollups
  existing, or after changing TICKER_TZ, which re-buckets every day. It
  clears the matching rollup_dirty markers in the same transaction.

    python -m ticker.db.rollup --all
    python -m ticker.db.rollup --all --metric heart_rate_bpm

Rollups are always derivable from observations, so a full rebuild is safe at
any time; it is just slow, which is the whole reason the incremental path
exists.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional

from ticker import config as tconfig
from ticker.db import queries, store
from ticker.db.lock import DatabaseInUse, DatabaseLock


def rebuild_all(conn, metric: Optional[str] = None, zone=None) -> int:
    """Recompute every day that has observations. Returns days written."""
    zone = zone or tconfig.local_zone()
    metric_id = queries.metric_id(conn, metric) if metric else None
    days = queries.all_days(conn, metric_id, zone)
    conn.execute("BEGIN")
    try:
        written = queries.rebuild_days(conn, days, zone)
        if metric_id is None:
            conn.execute("DELETE FROM rollup_dirty")
        else:
            conn.execute("DELETE FROM rollup_dirty WHERE metric_id = ?", (metric_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--all", action="store_true",
                        help="rebuild every day that has observations")
    parser.add_argument("--metric", help="limit a full rebuild to one metric")
    parser.add_argument("--db", type=Path, default=None,
                        help="database path (default: the configured one)")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not args.all:
        parser.error("nothing to do: pass --all "
                     "(incremental rebuilds happen in the writer)")

    db_path = args.db or tconfig.DB_PATH
    if not Path(db_path).exists():
        print("no database at {}".format(db_path), file=sys.stderr)
        return 1

    try:
        lock = DatabaseLock(db_path).require()
    except DatabaseInUse as exc:
        print(exc, file=sys.stderr)
        return 1
    conn = store.connect(db_path)
    try:
        started = time.monotonic()
        written = rebuild_all(conn, args.metric)
        elapsed = time.monotonic() - started
        print("rebuilt {} metric-days in {:.2f}s".format(written, elapsed))
        for row in conn.execute(
                "SELECT m.name, COUNT(*), MIN(r.day), MAX(r.day) "
                "FROM rollups_daily r JOIN metrics m ON m.id = r.metric_id "
                "GROUP BY m.name ORDER BY m.name"):
            print("  {:20s} {:>5} days  {} .. {}".format(*row))
    finally:
        conn.close()
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
