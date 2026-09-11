"""
Every connected cloud account, kept in sync for as long as the app runs.

This is `ticker sync`'s job done by the app itself, plus what an always-on
process needs that a sync you start and stop does not:

* An account connected or disconnected while it runs is picked up without a
  restart. The sources table is re-read every REFRESH_SEC, and the app asks
  straight away after a connect.
* "Sync now" works. request_sync is safe from any thread -- the page, or
  POST /api/sync/{id} -- and cuts the source's idle wait short through
  scheduler.Waker.
* Daily rollups are rebuilt every ROLLUP_SEC over whatever changed, and raw
  vendor responses past their retention are swept daily. `ticker sync`
  does both only on the way out, so one left running for a week serves a
  daily view a week stale.

Everything runs on one event loop on its own thread. The sources table is
read through a connection that belongs to that thread; every write goes
through the app's single writer.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Dict, Optional, Set

from ticker.db import queries, store
from ticker.ingest import scheduler, sync
from ticker.ingest.normalizer import Normalizer
from ticker.model import now_utc

log = logging.getLogger("ticker.pulls")

REFRESH_SEC = 60.0
ROLLUP_SEC = 300.0
SWEEP_SEC = 24 * 3600.0

# How long a request from another thread waits for the loop to answer.
CALL_TIMEOUT_SEC = 5.0


class PullSupervisor:
    def __init__(self, db_path, writer, *, builders=None,
                 interval: float = scheduler.PULL_INTERVAL_SEC, now=now_utc,
                 refresh_sec: float = REFRESH_SEC,
                 rollup_sec: float = ROLLUP_SEC,
                 sweep_sec: float = SWEEP_SEC):
        self.db_path = db_path
        self.writer = writer
        self.builders = builders
        self.interval = interval
        self.now = now
        self.refresh_sec = refresh_sec
        self.rollup_sec = rollup_sec
        self.sweep_sec = sweep_sec

        self._lock = threading.Lock()
        self._tasks: Dict[int, "asyncio.Task"] = {}
        self._wakers: Dict[int, scheduler.Waker] = {}
        self._skipped: Dict[int, str] = {}      # source_id -> why it isn't running
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._main: Optional["asyncio.Task"] = None
        self._thread: Optional[threading.Thread] = None
        self._conn = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run_thread, args=(ready,),
                                        name="ticker-pulls", daemon=True)
        self._thread.start()
        ready.wait(10)

    def stop(self, timeout: float = 15.0) -> None:
        loop, main = self._loop, self._main
        if loop is not None and main is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(main.cancel)
            except RuntimeError:
                pass                    # closed in between; nothing to cancel
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _run_thread(self, ready: threading.Event) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            self._main = loop.create_task(self._supervise())
            ready.set()
            loop.run_until_complete(self._main)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("cloud sync stopped unexpectedly")
        finally:
            ready.set()
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                # Not shutdown_default_executor: a fetch in flight would hold
                # the app's exit for as long as the vendor takes to answer.
                loop.close()

    async def _supervise(self) -> None:
        self._conn = store.connect(self.db_path, migrate_first=False)
        loop = asyncio.get_running_loop()
        last_rollup = loop.time()
        self._sweep()
        last_sweep = loop.time()
        try:
            while True:
                self.refresh()
                await asyncio.sleep(self.refresh_sec)
                now = loop.time()
                if now - last_rollup >= self.rollup_sec:
                    last_rollup = now
                    self.writer.rebuild_rollups()
                if now - last_sweep >= self.sweep_sec:
                    last_sweep = now
                    self._sweep()
        finally:
            with self._lock:
                tasks = list(self._tasks.values())
                self._tasks.clear()
                self._wakers.clear()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._conn.close()
            self._conn = None

    def _sweep(self) -> None:
        try:
            removed = queries.sweep_raw_payloads(self._conn)
            if removed:
                log.info("removed %d raw vendor responses past retention",
                         removed)
        except Exception:
            log.exception("raw payload sweep failed")

    # -- the loop thread ---------------------------------------------------

    def refresh(self) -> None:
        """Start newly enabled pull sources, drop disabled or dead ones."""
        rows = self._conn.execute(
            "SELECT id, vendor, display_name, auth_ref FROM sources "
            "WHERE kind = 'pull' AND enabled = 1 ORDER BY id").fetchall()
        wanted = {row[0]: row for row in rows}
        with self._lock:
            for source_id in list(self._tasks):
                if source_id not in wanted or self._tasks[source_id].done():
                    self._tasks.pop(source_id).cancel()
                    self._wakers.pop(source_id, None)
            for source_id in list(self._skipped):
                if source_id not in wanted:
                    del self._skipped[source_id]
            for source_id, (_, vendor, name, auth_ref) in wanted.items():
                if source_id not in self._tasks and source_id not in self._skipped:
                    self._launch(source_id, vendor, name, auth_ref)

    def _launch(self, source_id: int, vendor: str, name: str,
                auth_ref: Optional[str]) -> None:
        connector = sync.build_source(self.writer, source_id, vendor, name,
                                      auth_ref, builders=self.builders)
        if connector is None:
            self._skipped[source_id] = "no connector for {} in this version".format(vendor)
            return
        state = scheduler.PullState.load(self._conn, source_id,
                                         sorted(connector.capabilities()))
        waker = scheduler.Waker()
        self._wakers[source_id] = waker
        self._tasks[source_id] = asyncio.ensure_future(scheduler.run_pull_source(
            connector, Normalizer(self.writer, source_id), self.writer, state,
            now=self.now, interval=self.interval, wake=waker))
        log.info("syncing %s (source #%s)", vendor, source_id)

    async def _wake(self, source_id: int) -> bool:
        if self._conn is None:
            return False
        with self._lock:
            waker = self._wakers.get(source_id)
        if waker is None:
            # Connected since the last refresh: launching it is the sync.
            self.refresh()
            with self._lock:
                return source_id in self._tasks
        return waker.wake()

    async def _restart(self, source_id: int) -> bool:
        if self._conn is None:
            return False
        with self._lock:
            task = self._tasks.pop(source_id, None)
            self._wakers.pop(source_id, None)
            self._skipped.pop(source_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.refresh()
        with self._lock:
            return source_id in self._tasks

    # -- any thread --------------------------------------------------------

    def request_sync(self, source_id: int) -> bool:
        """Sync `source_id` now. False when it is already mid-sync or isn't
        a pull source this process runs -- the read API's on_sync contract."""
        return bool(self._on_loop(self._wake(source_id)))

    def restart(self, source_id: int) -> bool:
        """Rebuild one source's connector -- its credentials just changed --
        and start syncing it immediately."""
        return bool(self._on_loop(self._restart(source_id)))

    def status(self) -> Dict[int, dict]:
        """source_id -> {"running", "syncing", "problem"} for every pull
        source this process knows about."""
        with self._lock:
            out = {source_id: {"running": True,
                               "syncing": not waker.idle, "problem": None}
                   for source_id, waker in self._wakers.items()}
            for source_id, why in self._skipped.items():
                out[source_id] = {"running": False, "syncing": False,
                                  "problem": why}
        return out

    def running(self) -> Set[int]:
        with self._lock:
            return set(self._tasks)

    def _on_loop(self, coro):
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return None
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            return None
        try:
            return future.result(CALL_TIMEOUT_SEC)
        except Exception:
            log.exception("cloud sync request failed")
            return None
