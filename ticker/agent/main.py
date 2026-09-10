"""
The BLE agent.

    python -m ticker.agent.main                          # post to loopback
    python -m ticker.agent.main --server http://nas:8477 --token ...
    python -m ticker.agent.main --status                 # what's spooled

Runs on the machine the strap is near, and nothing else: it scans, connects,
reads heart rate and RR intervals, and posts them to a Ticker server. No
database, no dashboards, no window. If the server is unreachable it spools
locally and drains when it comes back, which is the point of splitting on
this line at all -- BLE is physically local, and the box with the disk in it
usually isn't.

Nothing here is BLE-specific except which source it builds. The stack above
the uplink -- StreamSource, Normalizer, the scheduler's restart policy -- is
byte for byte the code the single-machine app runs, because `Uplink` answers
the same calls a store does. That is deliberate: a split deployment that
runs different code from the combined one is a split deployment whose bugs
only appear in the deployment nobody tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

from ticker import config as tconfig
from ticker.agent.spool import Spool
from ticker.agent.uplink import Uplink, UplinkError, post_json
from ticker.ingest import scheduler
from ticker.ingest.normalizer import Normalizer

log = logging.getLogger("ticker.agent")

# How often the agent says what it is doing when nothing is wrong. A quiet
# process that turns out to have been spooling for a week is worse than a
# line an hour.
STATUS_INTERVAL_SEC = 300.0


def build_source(address: Optional[str] = None):
    """The BLE StreamSource. Imported late so `--status` works on a machine
    with no Bluetooth stack and no bleak installed."""
    from ticker.sources.ble import BleStreamSource
    return BleStreamSource(address=address)


def check_server(url: str, token: Optional[str] = None,
                 timeout: float = 5.0) -> bool:
    """Say up front whether the server is reachable.

    Not required -- the agent runs fine against a server that is down, which
    is the whole design -- but starting up against a typo'd URL and silently
    spooling forever is a bad first five minutes.
    """
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health",
                                    timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


async def run(source, uplink, *, on_error=None,
              status_interval: float = STATUS_INTERVAL_SEC,
              max_restarts: Optional[int] = None) -> None:
    """Stream until cancelled, reporting what the uplink is doing.

    source_id is None throughout: the agent genuinely does not know which
    source row the server files this under, and passing a made-up number
    would be a lie that shows up in a rejection message later.
    """
    normalizer = Normalizer(uplink, source_id=None)
    streaming = asyncio.ensure_future(scheduler.run_stream_source(
        source, normalizer, on_error=on_error, max_restarts=max_restarts))
    reporting = asyncio.ensure_future(_report(uplink, status_interval))
    try:
        await streaming
    finally:
        reporting.cancel()
        normalizer.flush()


async def _report(uplink, interval: float) -> None:
    if interval <= 0:
        return
    was_connected = uplink.connected
    while True:
        await asyncio.sleep(interval)
        status = uplink.status()
        if status["pending"] or not status["connected"] or was_connected != status["connected"]:
            log.info("uplink %s: %d delivered, %d spooled since %s",
                     "up" if status["connected"] else "down",
                     status["delivered"], status["pending"],
                     status["oldest"] or "-")
        was_connected = status["connected"]


def print_status(spool: Spool, server: str, token: Optional[str]) -> int:
    reachable = check_server(server, token)
    print("server:   {} ({})".format(server,
                                     "reachable" if reachable else "unreachable"))
    print("spool:    {}".format(spool.path))
    print("pending:  {}".format(spool.pending()))
    oldest = spool.oldest()
    if oldest:
        print("oldest:   {}".format(oldest))
    return 0


def drain(spool: Spool, server: str, token: Optional[str],
          batch: int, timeout: float) -> int:
    """Push whatever is spooled and exit. For a machine that recorded
    offline and is now on the same network as the server."""
    uplink = Uplink(server_url=server, token=token, spool=spool, batch=batch,
                    timeout=timeout, start=False)
    sent = 0
    while spool.pending():
        ids, body = spool.take(batch)
        body["source"] = {"vendor": uplink.vendor, "kind": uplink.kind,
                          "display_name": uplink.display_name}
        try:
            post_json(uplink.server_url + "/api/ingest", body, token, timeout)
        except UplinkError as exc:
            print("drain stopped: {}".format(exc), file=sys.stderr)
            print("{} rows still spooled at {}".format(spool.pending(),
                                                       spool.path),
                  file=sys.stderr)
            return 1
        spool.ack(ids)
        sent += len(body.get("observations", ()))
    print("drained {} observations to {}".format(sent, uplink.server_url))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--server", default=tconfig.AGENT_SERVER_URL,
                        help="base URL of the Ticker server")
    parser.add_argument("--token", default=tconfig.API_TOKEN,
                        help="shared secret the server requires")
    parser.add_argument("--address", default=None,
                        help="pin a strap by BLE address instead of scanning")
    parser.add_argument("--spool", type=Path, default=None,
                        help="where to spool when the server is unreachable")
    parser.add_argument("--batch", type=int, default=tconfig.AGENT_BATCH,
                        help="observations per uplink POST")
    parser.add_argument("--timeout", type=float, default=tconfig.AGENT_TIMEOUT_SEC)
    parser.add_argument("--status", action="store_true",
                        help="report the spool and exit")
    parser.add_argument("--drain", action="store_true",
                        help="post whatever is spooled and exit")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable --status output")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    spool = Spool(args.spool)
    owns_spool = False
    try:
        if args.status:
            if args.json:
                print(json.dumps({"server": args.server,
                                  "spool": str(spool.path),
                                  "pending": spool.pending(),
                                  "oldest": spool.oldest()}, indent=2))
                return 0
            return print_status(spool, args.server, args.token)

        if args.drain:
            return drain(spool, args.server, args.token, args.batch,
                         args.timeout)

        uplink = Uplink(server_url=args.server, token=args.token, spool=spool,
                        batch=args.batch, timeout=args.timeout)
        owns_spool = True
        if not check_server(args.server, args.token):
            log.warning("%s is not answering /health -- starting anyway and "
                        "spooling to %s until it does", args.server, spool.path)
        else:
            log.info("posting to %s", args.server)

        source = build_source(args.address)
        try:
            asyncio.run(_serve(source, uplink))
        except KeyboardInterrupt:
            print("stopping")
        finally:
            # Whatever is still queued gets one honest attempt, and then the
            # spool keeps it. The exit code stays 0 either way: rows waiting
            # in a durable spool are not a failed run.
            if not uplink.flush(timeout=args.timeout):
                log.warning("%d observations left spooled at %s",
                            spool.pending(), spool.path)
            uplink.close()
        return 0
    finally:
        # Only the paths that never built an Uplink close the spool here.
        # In the streaming path close() owns it, and it deliberately leaves
        # the file open when a post is still in flight -- closing it again
        # from out here would undo exactly that.
        if not owns_spool:
            spool.close()


async def _serve(source, uplink) -> None:
    """Run the stream, stopping cleanly on a signal.

    add_signal_handler is POSIX-only; on Windows KeyboardInterrupt out of
    asyncio.run is the equivalent path and main() catches it.
    """
    task = asyncio.ensure_future(run(source, uplink,
                                     on_error=lambda m: log.warning("%s", m)))
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, task.cancel)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
