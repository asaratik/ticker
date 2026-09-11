"""
Tests for scheduler.Waker, the hook behind "Sync now".

It may only ever shorten the idle wait between cycles. The failure worth
guarding against is a wake that leaks: one that lands mid-cycle and makes
the *next* idle wait return at once, which would turn a single button press
into two syncs and, against a rate limit, into a 429.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from ticker.ingest import scheduler
from ticker.model import Observation

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakePull:
    vendor = "fake"

    def __init__(self):
        self.windows = []

    def capabilities(self):
        return frozenset({"heart_rate_bpm"})

    def fetch(self, metric, since, until):
        self.windows.append((metric, since, until))
        return [Observation(metric, since + timedelta(seconds=1), 60.0)]


class NullNormalizer:
    def feed(self, observations):
        return len(list(observations))

    def flush(self):
        pass


class RecordingStore:
    def record_sync(self, *args, **kwargs):
        pass


def test_a_waker_is_idle_only_while_it_waits():
    async def body():
        waker = scheduler.Waker()
        assert waker.wake() is False         # no wait in progress: a cycle is
        task = asyncio.ensure_future(waker.wait(3600))
        await asyncio.sleep(0)
        assert waker.idle
        assert waker.wake() is True
        await asyncio.wait_for(task, 1)
        assert not waker.idle
    run(body())


def test_a_wait_ends_on_its_own_after_the_interval():
    async def body():
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        await asyncio.wait_for(scheduler.Waker().wait(900, sleep=fake_sleep), 1)
        assert slept == [900]
    run(body())


def test_a_wake_during_a_cycle_does_not_leak_into_the_next_wait():
    async def body():
        waker = scheduler.Waker()
        waker.wake()                         # refused: nothing is waiting
        task = asyncio.ensure_future(waker.wait(3600))
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    run(body())


def test_waking_a_pull_source_starts_its_next_cycle_early():
    async def body():
        source, waker = FakePull(), scheduler.Waker()
        task = asyncio.ensure_future(scheduler.run_pull_source(
            source, NullNormalizer(), RecordingStore(),
            scheduler.PullState(1, ["heart_rate_bpm"]), now=lambda: NOW,
            interval=3600, horizon=timedelta(0), max_cycles=2, wake=waker))
        while not waker.idle:
            await asyncio.sleep(0.01)
        assert len(source.windows) == 1
        assert waker.wake()
        await asyncio.wait_for(task, 2)      # would take an hour unwoken
        assert len(source.windows) == 2
    run(body())


def test_without_a_waker_the_interval_is_slept_as_before():
    async def body():
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        await scheduler.run_pull_source(
            FakePull(), NullNormalizer(), RecordingStore(),
            scheduler.PullState(1, ["heart_rate_bpm"]), now=lambda: NOW,
            interval=900, horizon=timedelta(0), max_cycles=2, sleep=fake_sleep)
        assert slept == [900]
    run(body())
