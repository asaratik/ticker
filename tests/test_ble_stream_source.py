"""
Tests for the BLE StreamSource adapter and the stream half of the scheduler.

Hardware-free throughout. The adapter's job is translation and lifecycle, so
these drive it with a fake HRSource that pushes the same message dicts the
real BLE source does -- the parsing and reconnect behaviour underneath is
already covered by tests/test_ble_source.py and isn't re-tested here.
"""

import asyncio
import queue
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from ticker.db import store
from ticker.ingest import scheduler
from ticker.ingest.normalizer import Normalizer
from ticker.model import Observation, iso_utc
from ticker.sources import base
from ticker.sources.ble import BleStreamSource, _QueueBridge

UTC = timezone.utc
TS = "2026-03-01T12:00:00.000+00:00"



@pytest.fixture
def run():
    """Run a coroutine on a loop of this test's own.

    Other modules in this suite close the main thread's event loop, so
    reaching for an ambient one makes these tests pass alone and fail in the
    full run. Each test gets a fresh loop and disposes of it.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def go(coro):
        return loop.run_until_complete(coro)

    try:
        yield go
    finally:
        asyncio.set_event_loop(None)
        loop.close()


class FakeBleSource:
    """Stands in for ble_source.BLEHRSource: same start/stop contract, same
    messages, no adapter."""

    def __init__(self, out_queue, messages=(), **kwargs):
        self.out_queue = out_queue
        self.messages = list(messages)
        self.kwargs = kwargs
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        for message in self.messages:
            self.out_queue.put(message)

    def stop(self):
        self.stopped = True


def sample(hr, rr=None, ts=TS):
    return {"type": "sample", "timestamp": ts, "hr": hr,
            "rr_intervals_ms": rr or []}


def status(state, **kw):
    message = {"type": "status", "status": state, "device_name": None,
               "device_address": None, "message": None}
    message.update(kw)
    return message


def attach(source, messages):
    """Wire a BleStreamSource to a FakeBleSource emitting `messages`."""
    holder = {}

    def make(out_queue):
        holder["fake"] = FakeBleSource(out_queue, messages)
        return holder["fake"]

    source._make_source = make
    return holder


async def take(source, count, timeout=5.0):
    """Collect `count` observations from a stream, then close it."""
    got = []
    stream = source.stream()
    try:
        while len(got) < count:
            got.append(await asyncio.wait_for(stream.__anext__(), timeout))
    finally:
        await stream.aclose()
    return got


# -- protocol conformance ------------------------------------------------

def test_it_satisfies_the_stream_source_protocol():
    source = BleStreamSource()
    assert isinstance(source, base.Source)
    assert isinstance(source, base.StreamSource)
    assert source.vendor == "ble"


def test_capabilities_are_the_metrics_it_actually_produces():
    assert BleStreamSource().capabilities() == {"heart_rate_bpm", "rr_interval_ms"}


def test_health_never_raises_before_anything_has_happened():
    health = BleStreamSource().health()
    assert health.ok is False
    assert health.state == "idle"


# -- message conversion --------------------------------------------------

def test_a_sample_becomes_a_heart_rate_observation(run):
    source = BleStreamSource()
    attach(source, [sample(72)])
    got = run(take(source, 1))
    assert got[0].metric == "heart_rate_bpm"
    assert got[0].value == 72.0
    assert got[0].ts == datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    assert got[0].end_ts is None


def test_rr_intervals_become_one_observation_per_beat(run):
    source = BleStreamSource()
    attach(source, [sample(80, [750.0, 760.0])])
    got = run(take(source, 3))

    rr = [o for o in got if o.metric == "rr_interval_ms"]
    assert [o.value for o in rr] == [750.0, 760.0]
    # Beats are placed by summing backwards from the notification, so the
    # newest lands on the sample time -- the same reconstruction the v1
    # migration uses.
    assert rr[1].ts == datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    assert (rr[1].ts - rr[0].ts).total_seconds() == pytest.approx(0.760)


def test_beats_in_one_packet_get_distinct_external_ids(run):
    """Reconstructed beat times can collide on a millisecond, and the
    natural key would then silently drop one."""
    source = BleStreamSource()
    attach(source, [sample(80, [750.0, 760.0, 740.0])])
    got = run(take(source, 4))
    rr = [o for o in got if o.metric == "rr_interval_ms"]
    assert [o.external_id for o in rr] == ["0", "1", "2"]


def test_heart_rate_carries_no_external_id(run):
    # One value per notification, so there is nothing to disambiguate.
    source = BleStreamSource()
    attach(source, [sample(72)])
    got = run(take(source, 1))
    assert got[0].external_id == ""


def test_a_sample_with_no_rr_data_yields_only_heart_rate(run):
    source = BleStreamSource()
    attach(source, [sample(72), sample(73)])
    got = run(take(source, 2))
    assert [o.metric for o in got] == ["heart_rate_bpm", "heart_rate_bpm"]


# -- status and health ---------------------------------------------------

def test_status_messages_produce_no_observations_but_update_health(run):
    source = BleStreamSource()
    attach(source, [
        status("connected", device_name="HRM-Dual", device_address="AA:BB"),
        sample(72),
    ])

    async def scenario():
        stream = source.stream()
        try:
            first = await asyncio.wait_for(stream.__anext__(), 5)
            # Health is read while the stream is live -- that is when a UI
            # source list asks for it.
            return first, source.health()
        finally:
            await stream.aclose()

    first, health = run(scenario())

    assert first.metric == "heart_rate_bpm"   # the status yielded nothing
    assert health.ok is True
    assert health.state == "connected"
    assert health.detail == "HRM-Dual"
    assert health.last_success == datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def test_health_reports_stopped_once_the_stream_is_closed(run):
    source = BleStreamSource()
    attach(source, [status("connected", device_name="HRM-Dual"), sample(72)])
    run(take(source, 1))
    health = source.health()
    assert health.ok is False
    assert health.state == "stopped"


def test_a_discovered_address_is_remembered_for_reconnects(run):
    source = BleStreamSource()
    attach(source, [status("connected", device_address="AA:BB"), sample(72)])
    run(take(source, 1))
    assert source.address == "AA:BB"


def test_a_pinned_address_is_not_overwritten_by_discovery(run):
    source = BleStreamSource(address="11:22:33")
    attach(source, [status("connected", device_address="AA:BB"), sample(72)])
    run(take(source, 1))
    assert source.address == "11:22:33"


def test_errors_are_surfaced_through_health_not_raised(run):
    source = BleStreamSource()
    attach(source, [{"type": "error", "message": "Scan failed: no adapter"},
                    sample(72)])
    run(take(source, 1))
    assert "no adapter" in source.health().last_error


# -- lifecycle -----------------------------------------------------------

def test_closing_the_stream_stops_the_ble_thread(run):
    source = BleStreamSource()
    holder = attach(source, [sample(72)])
    run(take(source, 1))
    assert holder["fake"].started is True
    assert holder["fake"].stopped is True


def test_cancelling_the_consumer_still_stops_the_ble_thread(run):
    """Cancellation is the normal stop path, so it must not leave the BLE
    thread holding the adapter."""
    source = BleStreamSource()
    holder = attach(source, [sample(72)])

    async def scenario():
        stream = source.stream()
        await stream.__anext__()

        async def consume():
            async for _ in stream:
                pass

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await stream.aclose()

    run(scenario())
    assert holder["fake"].stopped is True


# -- the scheduler's stream half ----------------------------------------

class RecordingNormalizer:
    def __init__(self):
        self.fed = []
        self.flushes = 0

    def feed(self, observations):
        self.fed.extend(observations)
        return len(self.fed)

    def flush(self):
        self.flushes += 1


class ScriptedStream:
    """A StreamSource that yields, then optionally fails."""

    vendor = "test"

    def __init__(self, batches):
        self.batches = list(batches)
        self.runs = 0

    def capabilities(self):
        return frozenset({"heart_rate_bpm"})

    def health(self):
        return base.SourceHealth(ok=True, state="connected")

    async def stream(self):
        self.runs += 1
        batch = self.batches.pop(0) if self.batches else []
        if isinstance(batch, Exception):
            raise batch
        for observation in batch:
            yield observation


def obs(value):
    return Observation("heart_rate_bpm", datetime(2026, 3, 1, 12, tzinfo=UTC),
                       float(value))


def test_backoff_doubles_and_is_capped():
    assert scheduler.backoff_delay(0) == 0.0
    assert scheduler.backoff_delay(1) == 1.0
    assert scheduler.backoff_delay(2) == 2.0
    assert scheduler.backoff_delay(3) == 4.0
    assert scheduler.backoff_delay(20) == scheduler.MAX_DELAY_SEC


def test_observations_reach_the_normalizer(run):
    source = ScriptedStream([[obs(70), obs(71)]])
    normalizer = RecordingNormalizer()
    run(
        scheduler.run_stream_source(source, normalizer))
    assert [o.value for o in normalizer.fed] == [70.0, 71.0]


def test_live_observations_are_flushed_on_arrival(run):
    # Holding a live stream for a full normalizer batch would be seventeen
    # minutes of latency at 1 Hz.
    source = ScriptedStream([[obs(70), obs(71)]])
    normalizer = RecordingNormalizer()
    run(
        scheduler.run_stream_source(source, normalizer))
    assert normalizer.flushes >= 2


def test_a_failing_stream_is_restarted_with_backoff(run):
    source = ScriptedStream([RuntimeError("adapter gone"), [obs(70)]])
    normalizer = RecordingNormalizer()
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    run(
        scheduler.run_stream_source(source, normalizer, sleep=fake_sleep))

    assert source.runs == 2
    assert slept == [1.0]
    assert [o.value for o in normalizer.fed] == [70.0]


def test_repeated_failures_back_off_further_each_time(run):
    source = ScriptedStream([RuntimeError("a"), RuntimeError("b"),
                             RuntimeError("c"), [obs(70)]])
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    run(
        scheduler.run_stream_source(ScriptedStream(source.batches),
                                    RecordingNormalizer(), sleep=fake_sleep))
    assert slept == [1.0, 2.0, 4.0]


def test_a_long_healthy_run_resets_the_backoff(run):
    source = ScriptedStream([RuntimeError("a"), RuntimeError("b"), [obs(70)]])
    slept = []
    ticks = iter([0, 1000, 1000, 2000, 2000, 3000])

    async def fake_sleep(seconds):
        slept.append(seconds)

    run(
        scheduler.run_stream_source(source, RecordingNormalizer(),
                                    sleep=fake_sleep, clock=lambda: next(ticks)))
    # Each failure came after a long healthy run, so neither escalated.
    assert slept == [1.0, 1.0]


def test_failures_are_reported_not_swallowed(run):
    source = ScriptedStream([RuntimeError("adapter gone"), [obs(70)]])
    errors = []

    async def fake_sleep(seconds):
        pass

    run(
        scheduler.run_stream_source(source, RecordingNormalizer(),
                                    on_error=errors.append, sleep=fake_sleep))
    assert "adapter gone" in errors[0]


def test_giving_up_after_max_restarts(run):
    source = ScriptedStream([RuntimeError("a"), RuntimeError("b"),
                             RuntimeError("c")])

    async def fake_sleep(seconds):
        pass

    run(
        scheduler.run_stream_source(source, RecordingNormalizer(),
                                    sleep=fake_sleep, max_restarts=1))
    assert source.runs == 2


def test_cancellation_flushes_what_was_already_read(run):
    normalizer = RecordingNormalizer()

    class Endless:
        vendor = "test"

        def capabilities(self):
            return frozenset()

        def health(self):
            return base.SourceHealth(ok=True, state="connected")

        async def stream(self):
            yield obs(70)
            while True:
                await asyncio.sleep(0.01)

    async def scenario():
        task = asyncio.ensure_future(
            scheduler.run_stream_source(Endless(), normalizer))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert [o.value for o in normalizer.fed] == [70.0]
    assert normalizer.flushes >= 1


def test_messages_cross_from_a_real_background_thread(run):
    """The bridge exists because the BLE source pushes from its own thread,
    never the consumer's loop. Faking it in-thread would test the one case
    that can't go wrong."""

    class ThreadedFake:
        def __init__(self, out_queue, messages):
            self.out_queue = out_queue
            self.messages = messages
            self.thread = None
            self.stopped = False

        def start(self):
            self.thread = threading.Thread(target=self._emit, daemon=True)
            self.thread.start()

        def _emit(self):
            for message in self.messages:
                time.sleep(0.001)
                self.out_queue.put(message)

        def stop(self):
            self.stopped = True
            if self.thread is not None:
                self.thread.join(timeout=5)

    source = BleStreamSource()
    holder = {}

    def make(out_queue):
        holder["fake"] = ThreadedFake(out_queue, [sample(70), sample(71), sample(72)])
        return holder["fake"]

    source._make_source = make

    got = run(take(source, 3))
    assert [o.value for o in got] == [70.0, 71.0, 72.0]
    assert holder["fake"].stopped is True


def test_a_message_arriving_after_shutdown_is_dropped_not_raised(run):
    """The BLE thread can outlive the consumer by a few milliseconds; a late
    put() must not raise inside a callback the BLE code cannot handle."""
    loop = asyncio.new_event_loop()
    bridge = _QueueBridge(loop)
    loop.close()
    bridge.put(sample(70))          # must not raise


# -- end to end ----------------------------------------------------------

def test_the_real_source_is_built_from_config(monkeypatch):
    """The adapter must hand the v1 source its pinned address and timeouts,
    or a pinned strap silently stops being pinned under v2."""
    import ble_source
    import config
    monkeypatch.setattr(config, "DEVICE_ADDRESS", "CC:DD")
    monkeypatch.setattr(config, "SCAN_TIMEOUT_SEC", 3.0)
    monkeypatch.setattr(config, "RECONNECT_DELAY_SEC", 7.0)

    built = BleStreamSource()._make_source(queue.Queue())
    assert isinstance(built, ble_source.BLEHRSource)
    assert built.address == "CC:DD"
    assert built.scan_timeout == 3.0
    assert built.reconnect_delay == 7.0

    # An explicit address wins over the configured default.
    assert BleStreamSource(address="AA:BB")._make_source(queue.Queue()).address == "AA:BB"


def test_a_ble_stream_lands_in_sqlite(run, tmp_path):
    """Milestone 3 end to end: strap messages -> Observations -> normalizer
    -> batched writer -> rows, with HRV derived on the way through."""
    db_path = tmp_path / "e2e.sqlite3"
    conn = store.connect(db_path)
    source_id = store.ensure_source(conn, "stream", "ble", "Test strap")

    # Forty beats of alternating 790/810 ms: enough for the HRV window.
    messages = []
    beat_ts = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    for i in range(40):
        rr = 810.0 if i % 2 else 790.0
        beat_ts += timedelta(milliseconds=rr)
        messages.append(sample(75, [rr], ts=iso_utc(beat_ts)))

    source = BleStreamSource()
    attach(source, messages)

    writer = store.AsyncStore(db_path, coalesce_rows=1000, coalesce_ms=50,
                              migrate_first=False)
    normalizer = Normalizer(writer, source_id)
    try:
        async def scenario():
            task = asyncio.ensure_future(
                scheduler.run_stream_source(source, normalizer))
            # Let every queued message drain, then stop the way a real
            # shutdown does.
            for _ in range(200):
                await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        run(scenario())
        assert writer.flush()
    finally:
        writer.close()

    counts = dict(conn.execute(
        "SELECT m.name, COUNT(*) FROM observations o "
        "JOIN metrics m ON m.id = o.metric_id GROUP BY m.name").fetchall())
    assert counts["heart_rate_bpm"] == 40
    assert counts["rr_interval_ms"] == 40
    # HRV came from the pipeline, not the connector: the strap never sent it.
    assert counts.get("hrv_rmssd_ms", 0) >= 1
    assert "hrv_rmssd_ms" not in source.capabilities()

    rmssd_value, = conn.execute(
        "SELECT value FROM observations o JOIN metrics m ON m.id = o.metric_id "
        "WHERE m.name = 'hrv_rmssd_ms' LIMIT 1").fetchone()
    assert rmssd_value == pytest.approx(20.0, abs=1.0)

    # Every timestamp is stored in the one canonical spelling.
    for (ts,) in conn.execute("SELECT ts FROM observations"):
        assert len(ts) == len("2026-01-01T00:00:00.000+00:00")
    conn.close()


def test_replaying_the_same_stream_writes_nothing_new(run, tmp_path):
    """A spooled agent draining twice must be harmless."""
    db_path = tmp_path / "replay.sqlite3"
    conn = store.connect(db_path)
    source_id = store.ensure_source(conn, "stream", "ble", "Test strap")
    messages = [sample(70, [800.0], ts="2026-03-01T12:00:00.000+00:00"),
                sample(71, [810.0], ts="2026-03-01T12:00:01.000+00:00")]

    def drain():
        source = BleStreamSource()
        attach(source, messages)
        writer = store.AsyncStore(db_path, coalesce_rows=1000, coalesce_ms=50,
                                  migrate_first=False)
        normalizer = Normalizer(writer, source_id, derive_hrv=False)
        try:
            async def scenario():
                task = asyncio.ensure_future(
                    scheduler.run_stream_source(source, normalizer))
                for _ in range(100):
                    await asyncio.sleep(0)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            run(scenario())
            writer.flush()
        finally:
            writer.close()

    drain()
    first = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    drain()
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == first
    conn.close()
