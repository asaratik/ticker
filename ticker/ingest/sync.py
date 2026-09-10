"""
Run the pull sources.

    python -m ticker.ingest.sync            # keep syncing until interrupted
    python -m ticker.ingest.sync --once     # one cycle, then stop

One asyncio loop owns every configured pull source, each on
its own task. The BLE stream still runs inside the app; this is the half
that talks to vendor APIs, and it is deliberately a separate process so a
cloud outage can't affect the thing recording your heart rate.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import List

from ticker import config as tconfig
from ticker.auth import secrets
from ticker.db import store
from ticker.ingest import scheduler
from ticker.ingest.normalizer import Normalizer
from ticker.model import now_utc
from ticker.sources.fitbit import FitbitSource
from ticker.sources.oura import OuraSource

log = logging.getLogger("ticker.sync")

# vendor -> factory(auth_ref, on_payload, on_session) -> PullSource
#
# Fitbit needs two things Oura does not, both from config rather than the
# keyring: a client id (public, it identifies the application) and the
# profile's IANA zone, without which its unlabelled local timestamps cannot
# be converted at all.
BUILDERS = {
    "oura": lambda **kwargs: OuraSource(**kwargs),
    "fitbit": lambda **kwargs: FitbitSource(
        client_id=tconfig.FITBIT_CLIENT_ID,
        profile_tz=tconfig.FITBIT_PROFILE_TZ, **kwargs),
}


def build_sources(conn, writer) -> List[tuple]:
    """(source_id, connector) for every enabled pull source in the database."""
    built = []
    rows = conn.execute(
        "SELECT id, vendor, display_name, auth_ref FROM sources "
        "WHERE kind = 'pull' AND enabled = 1 ORDER BY id").fetchall()
    for source_id, vendor, display_name, auth_ref in rows:
        builder = BUILDERS.get(vendor)
        if builder is None:
            log.warning("no connector for vendor %r (source #%s)", vendor, source_id)
            continue
        connector = builder(
            auth_ref=auth_ref or secrets.auth_ref(vendor, display_name),
            # The connector never touches SQLite; these hand what it found to
            # the writer, which does.
            on_payload=_payload_sink(writer, source_id),
            on_session=_session_sink(writer, source_id),
        )
        built.append((source_id, connector))
    return built


def _payload_sink(writer, source_id):
    def sink(endpoint, window_from, window_to, body):
        writer.insert_raw_payload(source_id, endpoint, window_from, window_to, body)
    return sink


def _session_sink(writer, source_id):
    seen = set()

    def sink(record):
        # One sleep period turns up in every window that overlaps it; the
        # store would upsert either way, but there is no point queueing the
        # same row a hundred times during a backfill.
        if record.external_id in seen:
            return
        seen.add(record.external_id)
        writer.begin_session(source_id, record)
    return sink


async def run(conn, writer, sources, *, once: bool = False,
              interval: float = scheduler.PULL_INTERVAL_SEC) -> None:
    if not sources:
        log.warning("no pull sources configured; "
                    "run 'python -m ticker.auth.setup add oura' first")
        return

    tasks = []
    for source_id, connector in sources:
        metrics = sorted(connector.capabilities())
        state = scheduler.PullState.load(conn, source_id, metrics)
        normalizer = Normalizer(writer, source_id)
        log.info("syncing %s (source #%s): %s", connector.vendor, source_id,
                 ", ".join(metrics))
        tasks.append(asyncio.ensure_future(scheduler.run_pull_source(
            connector, normalizer, writer, state,
            now=now_utc, interval=interval,
            on_error=lambda message: log.warning("%s", message),
            max_cycles=1 if once else None)))
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--once", action="store_true",
                        help="run one cycle instead of looping")
    parser.add_argument("--interval", type=float,
                        default=scheduler.PULL_INTERVAL_SEC,
                        help="seconds between cycles")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    db_path = args.db or tconfig.DB_PATH
    conn = store.connect(db_path)
    writer = store.AsyncStore(db_path, migrate_first=False)
    try:
        sources = build_sources(conn, writer)
        # asyncio.run, not get_event_loop().run_until_complete: the latter is
        # deprecated without a running loop and errors outright on newer
        # Pythons.
        asyncio.run(run(conn, writer, sources, once=args.once,
                        interval=args.interval))
    except KeyboardInterrupt:
        print("stopping")
    finally:
        # Commit whatever is still coalescing, then bring the rollups up to
        # date over the days this run actually changed.
        writer.rebuild_rollups()
        writer.close()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
