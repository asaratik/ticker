"""
The write path for a queue-driven live source.

Sits between an hr_source queue and the v2 store, and owns everything the Tk
app used to do inline: opening (and migrating) the database, registering the
source and device, opening and closing sessions, and turning each message
into observations.

It lives here rather than in the app because none of it is UI. The app is
left with live bpm, connection state,
Start/Stop -- and this is testable without a window.

Everything is best-effort by design. A database that cannot be opened must
leave the app working as a live display rather than refusing to start, so
failures are recorded and reported, never raised at the caller.
"""

from __future__ import annotations

import itertools
import traceback
from pathlib import Path
from typing import Optional

from ticker import config as tconfig
from ticker.db import store as ticker_store
from ticker.ingest.normalizer import Normalizer
from ticker.model import SessionRecord, now_utc
from ticker.sources.hr_messages import observations_from_sample


class SessionLogger:
    """Logs one live source's samples into the v2 database."""

    def __init__(self, vendor: str, db_path: Optional[Path] = None,
                 error_queue=None, display_name: Optional[str] = None):
        self.vendor = vendor
        self.db_path = Path(db_path) if db_path else tconfig.DB_PATH
        self.display_name = display_name or tconfig.source_display_name(vendor)
        self._error_queue = error_queue

        self.db = None
        self.store = None
        self.normalizer = None
        self.source_id = None
        self.error: Optional[str] = None
        self.session_key: Optional[str] = None
        self._keys = itertools.count(1)

        self._open()

    def _open(self) -> None:
        try:
            # connect() applies pending migrations, so this is the moment a
            # v1 database becomes a v2 one. It is the only synchronous
            # database work on the caller's thread, and it happens once.
            self.db = ticker_store.connect(self.db_path)
            self.source_id = ticker_store.ensure_source(
                self.db, "stream", self.vendor, self.display_name)
            # Already migrated on this thread, so the writer needn't retry.
            self.store = ticker_store.AsyncStore(
                self.db_path, error_queue=self._error_queue, migrate_first=False)
            self.normalizer = Normalizer(self.store, self.source_id)
        except Exception as exc:
            traceback.print_exc()
            self.error = "Cannot log to {} - {}".format(self.db_path, exc)
            self._report(self.error)

    def _report(self, message: str) -> None:
        if self._error_queue is not None:
            self._error_queue.put({"type": "error", "message": message})

    @property
    def available(self) -> bool:
        """False when the database could not be opened. The caller stays
        usable as a live display either way."""
        return self.store is not None

    @property
    def active(self) -> bool:
        return self.session_key is not None

    # -- session lifecycle -----------------------------------------------

    def start_session(self, label: Optional[str] = None,
                      device_name: Optional[str] = None,
                      device_address: Optional[str] = None) -> Optional[str]:
        """Open a session; returns its key, or None if there's no database."""
        if not self.available:
            return None
        self.session_key = str(next(self._keys))
        try:
            device_id = ticker_store.ensure_device(
                self.db, self.source_id, device_address, device_name)
            self.store.begin_session(
                self.source_id,
                SessionRecord(key=self.session_key, start_ts=now_utc(),
                              kind="manual", label=label),
                device_id=device_id)
        except Exception as exc:
            traceback.print_exc()
            self._report("Could not start logging - {}".format(exc))
            self.session_key = None
        return self.session_key

    def log_sample(self, message: dict) -> None:
        """Persist one 'sample' message. A no-op outside a session.

        One message becomes several rows: heart rate, one per RR interval in
        the packet, and HRV once the normalizer has enough beats -- the strap
        never sends HRV itself.
        """
        if not self.available or self.session_key is None:
            return
        try:
            self.normalizer.feed(
                observations_from_sample(message, session_key=self.session_key))
            # Flushed on arrival rather than held for a full batch: at 1 Hz a
            # 1000-row batch would be minutes of latency. The store's
            # coalescing window is what actually batches these writes.
            self.normalizer.flush()
        except Exception as exc:
            traceback.print_exc()
            self._report("Dropped a reading - {}".format(exc))

    def end_session(self) -> None:
        if not self.available or self.session_key is None:
            self.session_key = None
            return
        try:
            # Flush first: the writer applies queued work in order, so the
            # partial batch has to be enqueued before the session closes for
            # those observations to land inside it.
            self.normalizer.flush()
            self.store.end_session(self.source_id, self.session_key, now_utc())
            # End of a session is the natural moment to bring the daily
            # rollups up to date: it runs on the writer thread, and only over
            # the days this session actually touched.
            self.store.rebuild_rollups()
        except Exception as exc:
            traceback.print_exc()
            self._report("Could not close the session cleanly - {}".format(exc))
        finally:
            self.session_key = None

    def close(self) -> None:
        if self.session_key is not None:
            self.end_session()
        if self.normalizer is not None:
            self.normalizer.flush()
        if self.store is not None:
            self.store.rebuild_rollups()
            self.store.close()      # commits whatever is still coalescing
        if self.db is not None:
            self.db.close()
        self.store = None
        self.db = None
