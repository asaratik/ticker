"""
P0 write-path and reporting baseline.

    python benchmarks/baseline.py            # full run, about a minute
    python benchmarks/baseline.py --quick    # smaller fixtures, for a smoke run
    python benchmarks/baseline.py --json out.json

Everything runs against throwaway databases in a temporary folder; nothing
touches the configured data directory. Throughput numbers are measured to
*committed* rows (after a flush), not to rows handed to the writer, because
the P0 integrity work moves exactly that boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ticker.app.desktop_data import Reader  # noqa: E402
from ticker.db import backup, store  # noqa: E402
from ticker.ingest import importer, scheduler  # noqa: E402
from ticker.ingest.normalizer import Normalizer  # noqa: E402
from ticker.model import Observation, iso_utc  # noqa: E402

UTC = timezone.utc
FLUSH_TIMEOUT = 600.0


def _median_ms(samples):
    return round(statistics.median(samples) * 1000.0, 2)


def _count(db_path, sql="SELECT COUNT(*) FROM observations"):
    conn = store.connect(db_path, migrate_first=False)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def bench_store_insert(folder, rows):
    db = folder / "insert.sqlite3"
    conn = store.connect(db)
    sid = store.ensure_source(conn, "stream", "bench", "insert")
    conn.close()
    writer = store.AsyncStore(db, migrate_first=False)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    batch = []
    began = time.perf_counter()
    for i in range(rows):
        batch.append(Observation("heart_rate_bpm", start + timedelta(seconds=i), 60.0 + i % 40))
        if len(batch) == 1000:
            writer.insert_observations(sid, batch)
            batch = []
    if batch:
        writer.insert_observations(sid, batch)
    assert writer.flush(timeout=FLUSH_TIMEOUT)
    elapsed = time.perf_counter() - began
    writer.close()
    assert _count(db) == rows
    return {"rows": rows, "seconds": round(elapsed, 3),
            "rows_per_sec": round(rows / elapsed)}


def bench_flush_latency(folder, rounds=20, rows=50):
    """The delayed-batch case: fewer rows than COALESCE_ROWS, then a flush."""
    db = folder / "flush.sqlite3"
    conn = store.connect(db)
    sid = store.ensure_source(conn, "stream", "bench", "flush")
    conn.close()
    writer = store.AsyncStore(db, migrate_first=False)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = []
    for r in range(rounds):
        batch = [Observation("heart_rate_bpm",
                             start + timedelta(seconds=r * rows + i), 70.0)
                 for i in range(rows)]
        began = time.perf_counter()
        writer.insert_observations(sid, batch)
        assert writer.flush(timeout=FLUSH_TIMEOUT)
        samples.append(time.perf_counter() - began)
    writer.close()
    return {"rounds": rounds, "rows_per_round": rows,
            "median_ms": _median_ms(samples), "max_ms": round(max(samples) * 1000, 2)}


def _apple_export(path, records):
    start = datetime(2025, 1, 1, tzinfo=UTC)
    with path.open("w", encoding="utf-8") as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n')
        for i in range(records):
            stamp = (start + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S +0000")
            out.write(' <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Watch" '
                      'unit="count/min" value="{}" startDate="{}" endDate="{}"/>\n'.format(
                          55 + i % 50, stamp, stamp))
        out.write("</HealthData>\n")


def bench_import(folder, records):
    export = folder / "export.xml"
    _apple_export(export, records)
    db = folder / "import.sqlite3"
    conn = store.connect(db)
    writer = store.AsyncStore(db, migrate_first=False)
    began = time.perf_counter()
    summary = importer.run_import(conn, writer, "apple_health", export)
    handed_off = time.perf_counter() - began
    assert writer.flush(timeout=FLUSH_TIMEOUT)
    committed = time.perf_counter() - began
    writer.rebuild_rollups()
    assert writer.flush(timeout=FLUSH_TIMEOUT)
    with_rollups = time.perf_counter() - began
    writer.close()
    conn.close()
    assert _count(db) == summary["accepted"]
    return {"records": records, "accepted": summary["accepted"],
            "parse_and_enqueue_sec": round(handed_off, 3),
            "committed_sec": round(committed, 3),
            "committed_with_rollups_sec": round(with_rollups, 3),
            "records_per_sec": round(records / committed)}


class _BackfillSource:
    """A pull source with `per_day` readings a day, back to `days` ago."""

    vendor = "bench"

    def __init__(self, metrics, per_day, days, now):
        self.metrics = list(metrics)
        self.per_day = per_day
        self.floor = now - timedelta(days=days)
        self.windows = 0

    def capabilities(self):
        return frozenset(self.metrics)

    def health(self):
        return None

    def fetch(self, metric, since, until):
        self.windows += 1
        since = max(since, self.floor)
        if since >= until:
            return []
        step = timedelta(days=1) / self.per_day
        out, t = [], since
        while t < until:
            out.append(Observation(metric, t, 60.0))
            t += step
        return out


def bench_sync(folder, days, per_day):
    db = folder / "sync.sqlite3"
    conn = store.connect(db)
    sid = store.ensure_source(conn, "pull", "bench", "sync")
    writer = store.AsyncStore(db, migrate_first=False)
    now = datetime(2026, 3, 10, tzinfo=UTC)
    metrics = ("heart_rate_bpm", "resting_heart_rate_bpm")
    source = _BackfillSource(metrics, per_day, days, now)
    state = scheduler.PullState(sid, metrics)
    normalizer = Normalizer(writer, sid, derive_hrv=False)

    async def noop(seconds):
        pass

    cycles = len(metrics) * (days // 30 + 2) + 2
    began = time.perf_counter()
    asyncio.run(scheduler.run_pull_source(
        source, normalizer, writer, state, now=lambda: now, sleep=noop,
        horizon=timedelta(days=days + 30), max_cycles=cycles))
    assert writer.flush(timeout=FLUSH_TIMEOUT)
    elapsed = time.perf_counter() - began
    writer.close()
    conn.close()
    rows = _count(db)
    return {"windows": source.windows, "rows": rows, "seconds": round(elapsed, 3),
            "rows_per_sec": round(rows / elapsed),
            "backfill_done": sorted(state.done) == sorted(metrics)}


def _history_fixture(db, days):
    """One minute-resolution heart-rate source plus daily summary metrics
    from a second source -- the large-history shape the dashboard reads."""
    conn = store.connect(db)
    strap = store.ensure_source(conn, "stream", "bench", "strap")
    ring = store.ensure_source(conn, "import", "apple_health", "ring")
    mids = store.metric_ids(conn)
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    stamp = iso_utc(end)
    rows = []
    conn.execute("BEGIN")
    t = start
    while t < end:
        rows.append((strap, mids["heart_rate_bpm"], iso_utc(t), 60.0, stamp))
        t += timedelta(minutes=1)
        if len(rows) >= 50000:
            conn.executemany("INSERT INTO observations (source_id, metric_id, ts, value, "
                             "ingested_at) VALUES (?,?,?,?,?)", rows)
            rows = []
    for d in range(days):
        day = iso_utc(start + timedelta(days=d, hours=7))
        for name, value in (("resting_heart_rate_bpm", 52.0), ("hrv_rmssd_ms", 48.0),
                            ("sleep_duration_s", 27000.0), ("steps", 8000.0)):
            rows.append((ring, mids[name], day, value, stamp))
    conn.executemany("INSERT INTO observations (source_id, metric_id, ts, value, "
                     "ingested_at) VALUES (?,?,?,?,?)", rows)
    conn.execute("COMMIT")
    from ticker.db import rollup
    rollup.rebuild_all(conn)
    total = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    conn.close()
    return strap, total


def _timed(fn, rounds):
    samples = []
    for _ in range(rounds):
        began = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - began)
    return _median_ms(samples)


def bench_reporting(folder, days):
    db = folder / "history.sqlite3"
    began = time.perf_counter()
    strap, total = _history_fixture(db, days)
    fixture_sec = time.perf_counter() - began

    from ticker.app.runtime import Runtime

    class NoHardware:
        def start(self):
            pass

        def stop(self):
            pass

    os.environ.pop("HRM_SOURCE", None)
    began = time.perf_counter()
    runtime = Runtime(db, host="127.0.0.1", port=0, builders={},
                      make_source=lambda *args: NoHardware())
    runtime.start()
    startup = time.perf_counter() - began
    try:
        state_ms = _timed(runtime.state, 5)
    finally:
        began = time.perf_counter()
        runtime.stop()
        shutdown = time.perf_counter() - began

    reader = Reader(db)
    try:
        latest_ms = _timed(reader.latest, 5)
        series = {"{}d".format(n): _timed(lambda n=n: reader.series("heart_rate_bpm", strap, n), 5)
                  for n in (7, 30, 90)}
    finally:
        reader.close()

    began = time.perf_counter()
    made = backup.create(db, folder / "history-backup.sqlite3")
    backup_sec = time.perf_counter() - began
    made.unlink()
    return {"observations": total, "fixture_sec": round(fixture_sec, 2),
            "runtime_start_sec": round(startup, 3), "runtime_stop_sec": round(shutdown, 3),
            "overview_state_median_ms": state_ms, "latest_cards_median_ms": latest_ms,
            "series_median_ms": series, "backup_create_sec": round(backup_sec, 3)}


def hardware():
    return {"platform": platform.platform(), "machine": platform.machine(),
            "processor": platform.processor(), "cpus": os.cpu_count(),
            "python": platform.python_version()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--quick", action="store_true", help="smaller fixtures")
    parser.add_argument("--json", type=Path, help="also write results here")
    args = parser.parse_args(argv)
    scale = 0.1 if args.quick else 1.0

    results = {"hardware": hardware(), "quick": args.quick,
               "measured_at": datetime.now(UTC).isoformat(timespec="seconds")}
    with tempfile.TemporaryDirectory(prefix="ticker-bench-") as tmp:
        folder = Path(tmp)
        steps = [
            ("store_insert", lambda: bench_store_insert(folder, int(200_000 * scale))),
            ("flush_latency", lambda: bench_flush_latency(folder)),
            ("import", lambda: bench_import(folder, int(100_000 * scale))),
            ("sync_backfill", lambda: bench_sync(folder, 365, int(288 * scale) or 1)),
            ("reporting", lambda: bench_reporting(folder, int(365 * scale) or 30)),
        ]
        for name, step in steps:
            print("running {} ...".format(name), file=sys.stderr, flush=True)
            results[name] = step()
    print(json.dumps(results, indent=2))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
