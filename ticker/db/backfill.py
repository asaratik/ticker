"""
Compute derived metrics over observations that are already stored.

Derived metrics are normally produced in the ingest pipeline, as the data
arrives. That leaves two gaps this fills:

* data migrated from v1, which was recorded before the pipeline existed;
* data recorded while a derivation was switched off, or before it was
  written.

Runs through the same upsert as live ingest, so it is idempotent -- a second
pass over the same window writes nothing and marks no rollup dirty.

    python -m ticker.db.backfill --hrv
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ticker import config as tconfig
from ticker.db import queries, store
from ticker.ingest.derive import RmssdWindow
from ticker.model import iso_utc, now_iso, parse_iso


def backfill_hrv(conn, source_id: Optional[int] = None) -> int:
    """Derive hrv_rmssd_ms from stored RR intervals. Returns rows changed.

    RR intervals are grouped by (source, session) before being fed to the
    rolling window: a window that spanned two sessions -- or two devices --
    would compute a beat-to-beat difference across a gap of hours and report
    it as heart rate variability.
    """
    rr_metric = queries.metric_id(conn, "rr_interval_ms")
    hrv_metric = queries.metric_id(conn, "hrv_rmssd_ms")

    sql = ("SELECT source_id, session_id, ts, value FROM observations "
           "WHERE metric_id = ?")
    params: List[object] = [rr_metric]
    if source_id is not None:
        sql += " AND source_id = ?"
        params.append(source_id)
    sql += " ORDER BY source_id, session_id, ts, id"

    groups: Dict[Tuple[int, Optional[int]], RmssdWindow] = {}
    rows: List[tuple] = []
    ingested = now_iso()

    for src, session_id, ts, value in conn.execute(sql, params):
        key = (src, session_id)
        window = groups.get(key)
        if window is None:
            window = groups[key] = RmssdWindow()
        derived = window.feed(parse_iso(ts), value)
        if derived is not None:
            rows.append((src, hrv_metric, session_id,
                         iso_utc(derived.ts),
                         iso_utc(derived.end_ts) if derived.end_ts else None,
                         float(derived.value), None, "", ingested))

    if not rows:
        return 0
    conn.execute("BEGIN")
    try:
        cur = conn.executemany(store.UPSERT_OBSERVATION, rows)
        # Rows *changed*, not rows sent: the upsert's WHERE clause means a
        # second pass over the same window writes nothing, and reporting the
        # number sent would make a no-op look like work.
        changed = cur.rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--hrv", action="store_true",
                        help="derive hrv_rmssd_ms from stored RR intervals")
    parser.add_argument("--source-id", type=int, default=None,
                        help="limit to one source")
    parser.add_argument("--db", type=Path, default=None,
                        help="database path (default: the configured one)")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not args.hrv:
        parser.error("nothing to do: pass --hrv")

    db_path = args.db or tconfig.DB_PATH
    if not Path(db_path).exists():
        print("no database at {}".format(db_path), file=sys.stderr)
        return 1

    conn = store.connect(db_path)
    try:
        started = time.monotonic()
        written = backfill_hrv(conn, args.source_id)
        print("{} hrv_rmssd_ms observations changed in {:.2f}s".format(
            written, time.monotonic() - started))
        if written:
            print("run 'python -m ticker.db.rollup --all' to update the "
                  "daily rollups")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
