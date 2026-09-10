"""
Tests for the pull half of the scheduler.

Nothing here waits on a clock. `now` and `sleep` are injected, so a run that
would take three years of backfill in production finishes in milliseconds,
and the retry policy is asserted on directly rather than observed.

The properties that matter are that windows overlap
deliberately, the watermark only moves after a window completes, a partial
failure re-fetches rather than skips, and backfill is resumable.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from ticker.db import queries, store
from ticker.ingest import scheduler
from ticker.ingest.normalizer import Normalizer
from ticker.model import Observation
from ticker.sources import base

UTC = timezone.utc
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def run():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop.run_until_complete
    finally:
        asyncio.set_event_loop(None)
        loop.close()


class FakePull:
    """A PullSource that records its windows and returns scripted data."""

    vendor = "fake"

    def __init__(self, metrics=("heart_rate_bpm",), per_window=1, raises=None,
                 empty_before=None):
        self.metrics = list(metrics)
        self.per_window = per_window
        self.raises = raises
        self.empty_before = empty_before
        self.windows = []

    def capabilities(self):
        return frozenset(self.metrics)

    def health(self):
        return base.SourceHealth(ok=True, state="ok")

    def fetch(self, metric, since, until):
        self.windows.append((metric, since, until))
        if self.raises is not None:
            error, self.raises = self.raises, None
            raise error
        if self.empty_before is not None and until <= self.empty_before:
            return []
        return [Observation(metric, since + timedelta(seconds=i + 1), 60.0 + i)
                for i in range(self.per_window)]


class RecordingStore:
    def __init__(self):
        self.syncs = []

    def record_sync(self, source_id, metric, watermark_ts=None, cursor=None,
                    last_error=None, succeeded=True):
        self.syncs.append({"metric": metric, "watermark": watermark_ts,
                           "cursor": cursor, "error": last_error,
                           "succeeded": succeeded})

    def insert_observations(self, source_id, batch):
        pass


class NullNormalizer:
    def __init__(self):
        self.fed = []

    def feed(self, observations):
        observations = list(observations)
        self.fed.extend(observations)
        return len(observations)

    def flush(self):
        pass


def clock(start=NOW):
    """A `now` that stands still unless a test advances it."""
    holder = {"t": start}

    def now():
        return holder["t"]

    now.set = lambda value: holder.__setitem__("t", value)
    return now


async def noop_sleep(seconds):
    pass


def state(metrics=("heart_rate_bpm",), **kwargs):
    return scheduler.PullState(1, metrics, **kwargs)


# -- live windows --------------------------------------------------------

def test_the_first_window_reaches_back_by_the_overlap(run):
    source, norm, st = FakePull(), NullNormalizer(), state()
    run(scheduler.run_pull_source(
        source, norm, RecordingStore(), st, now=clock(), sleep=noop_sleep,
        max_cycles=1, horizon=timedelta(0)))

    metric, since, until = source.windows[0]
    assert until == NOW
    assert since == NOW - scheduler.OVERLAP


def test_later_windows_overlap_the_watermark_deliberately(run):
    """'Vendors amend recent data after the fact, and the unique index makes
    re-fetching free.'"""
    source, norm, st = FakePull(), NullNormalizer(), state()
    now = clock()
    run(scheduler.run_pull_source(
        source, norm, RecordingStore(), st, now=now, sleep=noop_sleep,
        max_cycles=1, horizon=timedelta(0)))

    later = NOW + timedelta(hours=1)
    now.set(later)
    run(scheduler.run_pull_source(
        source, norm, RecordingStore(), st, now=now, sleep=noop_sleep,
        max_cycles=1, horizon=timedelta(0)))

    _, since, until = source.windows[-1]
    assert until == later
    assert since == NOW - scheduler.OVERLAP     # watermark minus overlap


def test_every_capability_is_synced(run):
    source = FakePull(metrics=("heart_rate_bpm", "spo2_pct", "steps"))
    run(scheduler.run_pull_source(
        source, NullNormalizer(), RecordingStore(),
        state(source.metrics), now=clock(), sleep=noop_sleep, max_cycles=1,
        horizon=timedelta(0)))
    assert {m for m, _, _ in source.windows} == {
        "heart_rate_bpm", "spo2_pct", "steps"}


def test_observations_reach_the_normalizer(run):
    norm = NullNormalizer()
    run(scheduler.run_pull_source(
        FakePull(per_window=3), norm, RecordingStore(), state(),
        now=clock(), sleep=noop_sleep, max_cycles=1, horizon=timedelta(0)))
    assert len(norm.fed) == 3


# -- watermarks ----------------------------------------------------------

def test_the_watermark_advances_after_a_complete_window(run):
    store_ = RecordingStore()
    run(scheduler.run_pull_source(
        FakePull(), NullNormalizer(), store_, state(), now=clock(),
        sleep=noop_sleep, max_cycles=1, horizon=timedelta(0)))
    assert store_.syncs[0]["watermark"] == NOW
    assert store_.syncs[0]["succeeded"] is True


def test_a_failed_window_leaves_the_watermark_alone(run):
    """'A partial failure leaves it untouched, and the next run re-fetches.'"""
    store_ = RecordingStore()
    st = state()
    run(scheduler.run_pull_source(
        FakePull(raises=base.TransientError("boom")), NullNormalizer(), store_,
        st, now=clock(), sleep=noop_sleep, max_cycles=1, horizon=timedelta(0)))

    assert store_.syncs[0]["watermark"] is None
    assert store_.syncs[0]["succeeded"] is False
    assert st.watermarks == {}


def test_a_failure_re_fetches_the_same_window_next_time(run):
    source = FakePull(raises=base.TransientError("boom"))
    st = state()
    now = clock()
    run(scheduler.run_pull_source(
        source, NullNormalizer(), RecordingStore(), st, now=now,
        sleep=noop_sleep, max_cycles=1, horizon=timedelta(0)))
    first_window = source.windows[0][1]

    run(scheduler.run_pull_source(
        source, NullNormalizer(), RecordingStore(), st, now=now,
        sleep=noop_sleep, max_cycles=1, horizon=timedelta(0)))
    # Same start: nothing was marked as done, so nothing was skipped.
    assert source.windows[1][1] == first_window


def test_one_metric_failing_does_not_stop_the_others(run):
    class Picky(FakePull):
        def fetch(self, metric, since, until):
            if metric == "spo2_pct":
                raise base.TransientError("no spo2 today")
            return super().fetch(metric, since, until)

    source = Picky(metrics=("heart_rate_bpm", "spo2_pct", "steps"))
    norm = NullNormalizer()
    run(scheduler.run_pull_source(
        source, norm, RecordingStore(), state(source.metrics), now=clock(),
        sleep=noop_sleep, max_cycles=1, horizon=timedelta(0)))
    assert {o.metric for o in norm.fed} == {"heart_rate_bpm", "steps"}


# -- rate limiting -------------------------------------------------------

def test_rate_limiting_waits_the_vendor_s_own_delay(run):
    slept = []

    async def record_sleep(seconds):
        slept.append(seconds)

    run(scheduler.run_pull_source(
        FakePull(raises=base.RateLimited(90.0)), NullNormalizer(),
        RecordingStore(), state(), now=clock(), sleep=record_sleep,
        max_cycles=1, horizon=timedelta(0), interval=0))
    assert 90.0 in slept


def test_rate_limiting_does_not_advance_the_watermark(run):
    store_ = RecordingStore()
    run(scheduler.run_pull_source(
        FakePull(raises=base.RateLimited(5.0)), NullNormalizer(), store_,
        state(), now=clock(), sleep=noop_sleep, max_cycles=1,
        horizon=timedelta(0)))
    assert store_.syncs[0]["watermark"] is None


def test_an_expired_token_is_reported_not_retried_forever(run):
    errors = []
    run(scheduler.run_pull_source(
        FakePull(raises=base.AuthExpired("token revoked")), NullNormalizer(),
        RecordingStore(), state(), now=clock(), sleep=noop_sleep,
        on_error=errors.append, max_cycles=1, horizon=timedelta(0)))
    assert any("token revoked" in e for e in errors)


# -- backfill ------------------------------------------------------------

def test_backfill_walks_backwards_a_chunk_at_a_time(run):
    source, st = FakePull(), state()
    run(scheduler.run_pull_source(
        source, NullNormalizer(), RecordingStore(), st, now=clock(),
        sleep=noop_sleep, max_cycles=3, chunk=timedelta(days=30),
        horizon=timedelta(days=365)))

    backfill = [w for w in source.windows if w[2] <= NOW][1:]
    starts = [since for _, since, _ in backfill]
    # Each chunk starts 30 days before the previous one's end.
    assert NOW - timedelta(days=30) in starts
    assert NOW - timedelta(days=60) in starts


def test_backfill_runs_behind_live_sync(run):
    """Section 5.2: lower priority than live sync. One chunk per cycle,
    after the live window, is what that means here."""
    source, st = FakePull(), state()
    run(scheduler.run_pull_source(
        source, NullNormalizer(), RecordingStore(), st, now=clock(),
        sleep=noop_sleep, max_cycles=1, chunk=timedelta(days=30),
        horizon=timedelta(days=365)))
    assert len(source.windows) == 2                 # one live, one backfill
    assert source.windows[0][2] == NOW              # live first


def test_backfill_stops_at_an_empty_window(run):
    """An empty window is the vendor saying there is nothing older."""
    source = FakePull(empty_before=NOW - timedelta(days=45))
    st = state()
    run(scheduler.run_pull_source(
        source, NullNormalizer(), RecordingStore(), st, now=clock(),
        sleep=noop_sleep, max_cycles=6, chunk=timedelta(days=30),
        horizon=timedelta(days=365)))
    assert "heart_rate_bpm" in st.done
    # It stopped rather than walking all the way to the horizon.
    oldest = min(since for _, since, _ in source.windows)
    assert oldest > NOW - timedelta(days=365)


def test_a_failed_backfill_window_is_retried(run):
    class BackfillFails(FakePull):
        def fetch(self, metric, since, until):
            self.windows.append((metric, since, until))
            if until - since > scheduler.OVERLAP:
                raise base.TransientError("backfill unavailable")
            return []

    source = BackfillFails()
    st = state()
    run(scheduler.run_pull_source(
        source, NullNormalizer(), RecordingStore(), st, now=clock(),
        sleep=noop_sleep, max_cycles=2, chunk=timedelta(days=30),
        horizon=timedelta(days=365)))

    backfill_windows = [window for window in source.windows
                        if window[2] - window[1] > scheduler.OVERLAP]
    assert len(backfill_windows) == 2
    assert backfill_windows[0] == backfill_windows[1]
    assert "heart_rate_bpm" not in st.done
    assert "heart_rate_bpm" not in st.backfill


def test_backfill_stops_at_the_horizon(run):
    source, st = FakePull(), state()
    run(scheduler.run_pull_source(
        source, NullNormalizer(), RecordingStore(), st, now=clock(),
        sleep=noop_sleep, max_cycles=10, chunk=timedelta(days=30),
        horizon=timedelta(days=60)))
    assert "heart_rate_bpm" in st.done
    assert min(since for _, since, _ in source.windows) >= NOW - timedelta(days=60)


def test_backfill_progress_is_persisted_as_a_cursor(run):
    store_ = RecordingStore()
    run(scheduler.run_pull_source(
        FakePull(), NullNormalizer(), store_, state(), now=clock(),
        sleep=noop_sleep, max_cycles=1, chunk=timedelta(days=30),
        horizon=timedelta(days=365)))
    assert any(s["cursor"] for s in store_.syncs)


def test_backfill_resumes_from_a_persisted_cursor(run):
    resumed_from = NOW - timedelta(days=90)
    source = FakePull()
    st = state(backfill_cursors={"heart_rate_bpm": resumed_from})
    run(scheduler.run_pull_source(
        source, NullNormalizer(), RecordingStore(), st, now=clock(),
        sleep=noop_sleep, max_cycles=1, chunk=timedelta(days=30),
        horizon=timedelta(days=365)))

    backfill_window = source.windows[-1]
    assert backfill_window[2] == resumed_from       # picked up where it left off
    assert backfill_window[1] == resumed_from - timedelta(days=30)


# -- persistence ---------------------------------------------------------

def test_pull_state_round_trips_through_sync_state(tmp_path, run):
    """A restart has to resume, which means what the scheduler wrote is what
    PullState.load reads back."""
    db_path = tmp_path / "sync.sqlite3"
    conn = store.connect(db_path)
    source_id = store.ensure_source(conn, "pull", "oura", "Ring")

    writer = store.AsyncStore(db_path, migrate_first=False, coalesce_rows=1)
    try:
        st = scheduler.PullState(source_id, ["heart_rate_bpm"])
        run(scheduler.run_pull_source(
            FakePull(), Normalizer(writer, source_id), writer, st,
            now=clock(), sleep=noop_sleep, max_cycles=1,
            chunk=timedelta(days=30), horizon=timedelta(days=365)))
        assert writer.flush()
    finally:
        writer.close()

    reloaded = scheduler.PullState.load(conn, source_id, ["heart_rate_bpm"])
    assert reloaded.watermarks["heart_rate_bpm"] == NOW
    assert reloaded.backfill["heart_rate_bpm"] == NOW - timedelta(days=30)
    conn.close()


def test_sync_errors_are_recorded_without_losing_the_watermark(tmp_path, run):
    db_path = tmp_path / "sync.sqlite3"
    conn = store.connect(db_path)
    source_id = store.ensure_source(conn, "pull", "oura", "Ring")

    writer = store.AsyncStore(db_path, migrate_first=False, coalesce_rows=1)
    try:
        st = scheduler.PullState(source_id, ["heart_rate_bpm"])
        run(scheduler.run_pull_source(
            FakePull(), Normalizer(writer, source_id), writer, st,
            now=clock(), sleep=noop_sleep, max_cycles=1, horizon=timedelta(0)))
        writer.flush()
        # Now a failure: the watermark must survive it.
        run(scheduler.run_pull_source(
            FakePull(raises=base.TransientError("outage")),
            Normalizer(writer, source_id), writer, st,
            now=clock(), sleep=noop_sleep, max_cycles=1, horizon=timedelta(0)))
        assert writer.flush()
    finally:
        writer.close()

    row = queries.sync_state(conn, source_id)["heart_rate_bpm"]
    assert row["watermark_ts"] is not None
    assert "outage" in row["last_error"]
    assert row["last_success"] is not None
    conn.close()


def test_raw_payloads_are_stored_gzipped(tmp_path):
    import gzip

    db_path = tmp_path / "raw.sqlite3"
    conn = store.connect(db_path)
    source_id = store.ensure_source(conn, "pull", "oura", "Ring")
    body = b'{"data": [{"bpm": 60}]}'

    writer = store.AsyncStore(db_path, migrate_first=False)
    try:
        writer.insert_raw_payload(source_id, "heartrate", NOW,
                                  NOW + timedelta(hours=1), body)
        assert writer.flush()
    finally:
        writer.close()

    endpoint, stored_body = conn.execute(
        "SELECT endpoint, body FROM raw_payloads").fetchone()
    assert endpoint == "heartrate"
    # Verbatim after decompression: that is what makes re-normalising possible.
    assert gzip.decompress(stored_body) == body
    conn.close()


# -- end to end ----------------------------------------------------------

def test_an_oura_sync_lands_in_sqlite(tmp_path, run):
    """Milestone 5 end to end: a canned Oura API through the real connector,
    the real scheduler, the real normalizer and the real writer."""
    import json
    from ticker.ingest import sync as sync_module
    from ticker.sources.oura import OuraSource

    day = "2026-03-09"
    payloads = {
        "heartrate": {"data": [
            {"bpm": 58, "source": "rest", "timestamp": "2026-03-09T08:00:00+00:00"},
            {"bpm": 62, "source": "awake", "timestamp": "2026-03-09T08:05:00+00:00"}]},
        "daily_spo2": {"data": [
            {"id": "s1", "day": day, "spo2_percentage": {"average": 96.5}}]},
        "daily_activity": {"data": [
            {"id": "a1", "day": day, "steps": 8412, "active_calories": 430}]},
        "sleep": {"data": [{
            "id": "z1", "day": day,
            "bedtime_start": "2026-03-08T23:00:00+00:00",
            "bedtime_end": "2026-03-09T07:00:00+00:00",
            "total_sleep_duration": 26400,
            "sleep_phase_5_min": "4213"}]},
    }

    def transport(url, headers):
        endpoint = url.split("/")[-1].split("?")[0]
        return 200, {}, json.dumps(payloads[endpoint]).encode("utf-8")

    db_path = tmp_path / "oura.sqlite3"
    conn = store.connect(db_path)
    source_id = store.ensure_source(conn, "pull", "oura", "Ring")
    writer = store.AsyncStore(db_path, migrate_first=False, coalesce_rows=10000)
    try:
        connector = OuraSource(
            token="t", transport=transport,
            on_payload=sync_module._payload_sink(writer, source_id),
            on_session=sync_module._session_sink(writer, source_id))
        st = scheduler.PullState(source_id, sorted(connector.capabilities()))
        run(scheduler.run_pull_source(
            connector, Normalizer(writer, source_id), writer, st,
            now=clock(), sleep=noop_sleep, max_cycles=1, horizon=timedelta(0)))
        assert writer.flush()
    finally:
        writer.close()

    counts = dict(conn.execute(
        "SELECT m.name, COUNT(*) FROM observations o "
        "JOIN metrics m ON m.id = o.metric_id GROUP BY m.name"))
    assert counts["heart_rate_bpm"] == 2
    assert counts["spo2_pct"] == 1
    assert counts["steps"] == 1
    assert counts["active_energy_kcal"] == 1
    assert counts["sleep_duration_s"] == 1
    assert counts["sleep_stage"] == 4

    # The sleep period became a session, and its observations are in it.
    session_id, kind, external_id = conn.execute(
        "SELECT id, kind, external_id FROM sessions").fetchone()
    assert (kind, external_id) == ("sleep", "z1")
    assert conn.execute(
        "SELECT COUNT(*) FROM observations WHERE session_id = ?",
        (session_id,)).fetchone() == (5,)          # duration plus four stages

    # Categorical values kept their text.
    stages = [t for (t,) in conn.execute(
        "SELECT o.text_value FROM observations o JOIN metrics m "
        "ON m.id = o.metric_id WHERE m.name = 'sleep_stage' ORDER BY o.ts")]
    assert stages == ["awake", "light", "deep", "rem"]

    # And the raw responses were kept for re-normalising later.
    assert conn.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0] >= 4
    conn.close()


def test_re_syncing_an_overlapping_window_writes_nothing_new(tmp_path, run):
    """The whole point of the overlap: vendors amend recent data, and
    re-fetching what didn't change has to be free."""
    import json
    from ticker.sources.oura import OuraSource

    payload = {"data": [
        {"bpm": 58, "source": "rest", "timestamp": "2026-03-09T08:00:00+00:00"},
        {"bpm": 62, "source": "rest", "timestamp": "2026-03-09T08:05:00+00:00"}]}

    def transport(url, headers):
        return 200, {}, json.dumps(payload).encode("utf-8")

    db_path = tmp_path / "overlap.sqlite3"
    conn = store.connect(db_path)
    source_id = store.ensure_source(conn, "pull", "oura", "Ring")
    writer = store.AsyncStore(db_path, migrate_first=False, coalesce_rows=10000)
    try:
        connector = OuraSource(token="t", transport=transport)
        st = scheduler.PullState(source_id, ["heart_rate_bpm"])
        normalizer = Normalizer(writer, source_id)
        for _ in range(3):
            run(scheduler.run_pull_source(
                connector, normalizer, writer, st, now=clock(),
                sleep=noop_sleep, max_cycles=1, horizon=timedelta(0)))
        assert writer.flush()
        writer.take_dirty_days()
        # A fourth identical pass must dirty nothing at all.
        run(scheduler.run_pull_source(
            connector, normalizer, writer, st, now=clock(), sleep=noop_sleep,
            max_cycles=1, horizon=timedelta(0)))
        assert writer.flush()
        assert writer.take_dirty_days() == set()
    finally:
        writer.close()

    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone() == (2,)
    conn.close()


def test_an_amended_vendor_value_overwrites(tmp_path, run):
    """Oura rewrites a sleep score hours later; the overlap is what catches
    it, and the upsert is what applies it."""
    import json
    from ticker.sources.oura import OuraSource

    state_holder = {"steps": 5000}

    def transport(url, headers):
        return 200, {}, json.dumps({"data": [
            {"id": "a1", "day": "2026-03-09", "steps": state_holder["steps"],
             "active_calories": 100}]}).encode("utf-8")

    db_path = tmp_path / "amend.sqlite3"
    conn = store.connect(db_path)
    source_id = store.ensure_source(conn, "pull", "oura", "Ring")
    writer = store.AsyncStore(db_path, migrate_first=False, coalesce_rows=10000)
    try:
        connector = OuraSource(token="t", transport=transport)
        st = scheduler.PullState(source_id, ["steps"])
        normalizer = Normalizer(writer, source_id)
        run(scheduler.run_pull_source(
            connector, normalizer, writer, st, now=clock(), sleep=noop_sleep,
            max_cycles=1, horizon=timedelta(0)))
        writer.flush()
        state_holder["steps"] = 9000
        run(scheduler.run_pull_source(
            connector, normalizer, writer, st, now=clock(), sleep=noop_sleep,
            max_cycles=1, horizon=timedelta(0)))
        assert writer.flush()
    finally:
        writer.close()

    rows = conn.execute(
        "SELECT value FROM observations o JOIN metrics m ON m.id = o.metric_id "
        "WHERE m.name = 'steps'").fetchall()
    assert rows == [(9000.0,)]        # one row, updated -- not two
    conn.close()
