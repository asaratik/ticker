"""
The ingest scheduler.

Stream sources get one long-lived task each, restarted with exponential
backoff on failure. Pull sources are woken on an interval and asked for
`[watermark - overlap, now)` per metric, then walk backwards in chunks to
fill in history. Import sources are user-triggered and have no runner here.

Why the scheduler owns the backoff -- and the clock
---------------------------------------------------
A connector that sleeps on its own is a connector whose retry policy can
only be tested by waiting, and a connector that decides what "now" means is
one you cannot drive through a week of history in a test. Both belong here:
every delay is a pure function of failure count, and every window boundary
arrives as an argument. `sleep` and `now` are injectable throughout, so the
whole of this module's behaviour is exercisable in milliseconds.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional, Sequence, Set

from ticker.db import queries
from ticker.model import iso_utc, parse_iso
from ticker.sources.base import AuthExpired, PermanentError, RateLimited

log = logging.getLogger(__name__)

# Restart backoff for a stream that fails: 1s, 2s, 4s ... capped.
BASE_DELAY_SEC = 1.0
MAX_DELAY_SEC = 60.0

# A stream that ran this long before failing is treated as healthy, and its
# backoff resets. Without this, a strap that reconnects fine every few
# minutes for hours would eventually be waiting a full minute to retry.
HEALTHY_AFTER_SEC = 60.0

# How often a pull source is woken. Sleep and readiness scores are computed
# overnight and amended for hours afterwards; polling faster buys nothing.
PULL_INTERVAL_SEC = 15 * 60

# Every live window starts this far behind the watermark. Vendors amend
# recent data after the fact, and the unique index makes re-fetching it free,
# so the overlap is pure upside.
OVERLAP = timedelta(hours=24)

# Backfill walks backwards in chunks this size.
BACKFILL_CHUNK = timedelta(days=30)

# How far back a backfill is willing to go before giving up.
BACKFILL_HORIZON = timedelta(days=365 * 3)


def backoff_delay(failures: int, base: float = BASE_DELAY_SEC,
                  maximum: float = MAX_DELAY_SEC) -> float:
    """Delay before restart attempt `failures`. Pure, so it can be tested
    without a clock."""
    if failures <= 0:
        return 0.0
    return min(maximum, base * (2 ** (failures - 1)))


def _report(on_error, message: str) -> None:
    log.warning(message)
    if on_error is not None:
        on_error(message)


# -- stream sources ------------------------------------------------------

async def run_stream_source(source, normalizer, *,
                            on_error: Optional[Callable[[str], None]] = None,
                            sleep=asyncio.sleep,
                            clock=None,
                            max_restarts: Optional[int] = None) -> None:
    """Drive a StreamSource into a Normalizer until cancelled.

    Cancellation is the normal stop path -- it closes the source's generator,
    which is what shuts the underlying connection down -- so this returns
    rather than propagating CancelledError only when max_restarts is reached.
    """
    clock = clock or asyncio.get_running_loop().time
    failures = 0

    while True:
        started = clock()
        try:
            await _drain_once(source, normalizer)
            # A stream returning cleanly means the source decided it was
            # done; there is nothing to restart.
            return
        except asyncio.CancelledError:
            normalizer.flush()
            raise
        except Exception as exc:
            normalizer.flush()
            if clock() - started >= HEALTHY_AFTER_SEC:
                failures = 0
            failures += 1
            message = "{} stream failed ({}): {}".format(
                getattr(source, "vendor", "source"), failures, exc)
            log.warning(message, exc_info=True)
            if on_error is not None:
                on_error(message)
            if max_restarts is not None and failures > max_restarts:
                return
            await sleep(backoff_delay(failures))


async def _drain_once(source, normalizer) -> None:
    """One pass over the source's generator, closing it on the way out."""
    stream = source.stream()
    try:
        async for observation in stream:
            normalizer.feed([observation])
            # Live data is flushed on arrival rather than held for a full
            # normalizer batch: at 1 Hz a 1000-row batch is seventeen
            # minutes of latency. The store's coalescing window is what
            # actually batches a live stream's writes.
            normalizer.flush()
    finally:
        try:
            await stream.aclose()
        except asyncio.CancelledError:
            # Already being torn down; the generator's own finally has run.
            pass


# -- pull sources --------------------------------------------------------

class PullState:
    """One pull source's progress, in memory and in sync_state.

    The scheduler is the only writer of a source's watermarks, so it keeps
    them here and persists after each successful window rather than reading
    back what it just wrote.
    """

    def __init__(self, source_id: int, metrics: Sequence[str],
                 watermarks: Optional[Dict[str, datetime]] = None,
                 backfill_cursors: Optional[Dict[str, datetime]] = None):
        self.source_id = source_id
        self.metrics = list(metrics)
        self.watermarks: Dict[str, datetime] = dict(watermarks or {})
        # How far back the backfill has reached, per metric. Absent means it
        # hasn't started; `done` is how a finished one is remembered.
        self.backfill: Dict[str, datetime] = dict(backfill_cursors or {})
        self.done: Set[str] = set()

    @classmethod
    def load(cls, conn, source_id: int, metrics: Sequence[str]) -> "PullState":
        """Read persisted progress, so a restart resumes rather than restarts.

        Section 5.2 requires backfill to be resumable, and sync_state.cursor
        is where its progress lives.
        """
        stored = queries.sync_state(conn, source_id)
        watermarks, cursors = {}, {}
        for metric in metrics:
            row = stored.get(metric)
            if not row:
                continue
            if row.get("watermark_ts"):
                watermarks[metric] = parse_iso(row["watermark_ts"])
            cursor = row.get("cursor")
            if cursor:
                try:
                    cursors[metric] = parse_iso(cursor)
                except ValueError:
                    pass       # a cursor this version doesn't understand
        return cls(source_id, metrics, watermarks, cursors)


class Waker:
    """Lets someone else cut a pull source's wait between cycles short.

    The runtime gives one to each source so that "Sync now" -- a button, or
    POST /api/sync/{id} -- starts a cycle at once instead of up to fifteen
    minutes later. It only ever shortens the *idle* wait. A rate-limit
    back-off still sleeps in full: waking a source the vendor has just told
    to slow down would earn another 429 and nothing else.

    Make it on the loop that runs the source; on Python 3.9 an asyncio.Event
    belongs to whichever loop was current when it was created.
    """

    def __init__(self):
        self._event = asyncio.Event()
        self.idle = False

    def wake(self) -> bool:
        """Start the next cycle now. False while a cycle is running -- the
        sync being asked for is already happening. Loop thread only; from
        anywhere else, go through call_soon_threadsafe or
        run_coroutine_threadsafe."""
        if not self.idle:
            return False
        self._event.set()
        return True

    async def wait(self, seconds: float, sleep=asyncio.sleep) -> None:
        """Idle for `seconds`, or until woken, whichever is first."""
        self._event.clear()
        self.idle = True
        sleeper = asyncio.ensure_future(sleep(seconds))
        waiter = asyncio.ensure_future(self._event.wait())
        try:
            await asyncio.wait({sleeper, waiter},
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.idle = False
            sleeper.cancel()
            waiter.cancel()


async def run_pull_source(source, normalizer, store, state: PullState, *,
                          now: Callable[[], datetime],
                          interval: float = PULL_INTERVAL_SEC,
                          overlap: timedelta = OVERLAP,
                          chunk: timedelta = BACKFILL_CHUNK,
                          horizon: timedelta = BACKFILL_HORIZON,
                          on_error: Optional[Callable[[str], None]] = None,
                          sleep=asyncio.sleep,
                          max_cycles: Optional[int] = None,
                          wake: Optional[Waker] = None) -> None:
    """Sync one pull source until cancelled.

    Each cycle syncs every metric's live window, then does at most one
    backfill chunk. That ordering makes backfill lower priority than live
    sync without needing a priority queue.

    With a Waker, the wait between cycles ends early when it is woken.
    """
    cycles = 0
    while True:
        cycles += 1
        try:
            await _sync_live(source, normalizer, store, state, now, overlap,
                             on_error, sleep)
            await _backfill_chunk(source, normalizer, store, state, now, chunk,
                                  horizon, on_error, sleep)
        except asyncio.CancelledError:
            normalizer.flush()
            raise
        except Exception as exc:
            normalizer.flush()
            _report(on_error, "{} sync failed: {}".format(source.vendor, exc))
        if max_cycles is not None and cycles >= max_cycles:
            return
        if wake is not None:
            await wake.wait(interval, sleep)
        else:
            await sleep(interval)


async def _sync_live(source, normalizer, store, state, now, overlap,
                     on_error, sleep) -> None:
    """Fetch [watermark - overlap, now) for each metric."""
    until = now()
    for metric in state.metrics:
        watermark = state.watermarks.get(metric)
        since = (watermark - overlap) if watermark else (until - overlap)
        await _fetch_window(source, normalizer, store, state, metric, since,
                            until, on_error, sleep, advance_to=until)


async def _backfill_chunk(source, normalizer, store, state, now, chunk,
                          horizon, on_error, sleep) -> None:
    """One step backwards through history, for one metric.

    A metric is finished when it reaches the horizon or a window comes back
    empty, which is the vendor saying there is nothing older.
    """
    pending = [m for m in state.metrics if m not in state.done]
    if not pending:
        return
    metric = pending[0]

    floor = now() - horizon
    edge = state.backfill.get(metric) or now()
    if edge <= floor:
        state.done.add(metric)
        return

    since = max(edge - chunk, floor)
    count = await _fetch_window(source, normalizer, store, state, metric, since,
                                edge, on_error, sleep, advance_to=None,
                                cursor=since)
    if count is None:
        return
    if count == 0:
        state.done.add(metric)
        store.record_sync(state.source_id, metric, cursor=None)
    else:
        state.backfill[metric] = since


async def _fetch_window(source, normalizer, store, state, metric, since, until,
                        on_error, sleep, advance_to, cursor=None) -> Optional[int]:
    """One fetch, plus the retry policy the connector isn't allowed to own.

    Returns the accepted count, or None when the fetch failed. The distinction
    matters to backfill: an empty successful window ends history, while a
    failed window must be retried.
    """
    try:
        observations = await _in_thread(source.fetch, metric, since, until)
    except RateLimited as exc:
        _report(on_error, "{} rate limited on {}; waiting {:.0f}s".format(
            source.vendor, metric, exc.retry_after))
        store.record_sync(state.source_id, metric, last_error=str(exc),
                          succeeded=False)
        # The connector raised rather than slept, so the waiting happens
        # here, where a test can replace it.
        await sleep(exc.retry_after)
        return None
    except (AuthExpired, PermanentError) as exc:
        _report(on_error, "{} {}: {}".format(source.vendor, metric, exc))
        store.record_sync(state.source_id, metric, last_error=str(exc),
                          succeeded=False)
        return None
    except Exception as exc:
        _report(on_error, "{} {} fetch failed: {}".format(
            source.vendor, metric, exc))
        store.record_sync(state.source_id, metric, last_error=str(exc),
                          succeeded=False)
        return None

    accepted = normalizer.feed(observations)
    normalizer.flush()
    # The watermark moves only here, after a window completed end to end. A
    # partial failure returned above and left it untouched, so the next run
    # re-fetches -- free, because of the unique index.
    if advance_to is not None:
        state.watermarks[metric] = advance_to
    store.record_sync(state.source_id, metric, watermark_ts=advance_to,
                      cursor=iso_utc(cursor) if cursor else None,
                      last_error=None, succeeded=True)
    return accepted


async def _in_thread(function, *args):
    """Run a blocking connector call without stalling the loop.

    PullSource.fetch is synchronous by design, which keeps connectors
    writable without async plumbing, so
    keeping it off the event loop is the scheduler's job.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: list(function(*args)))
