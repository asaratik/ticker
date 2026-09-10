"""
The agent's half of the wire.

`Uplink` looks like a store to everything upstream of it -- same
`insert_observations`, `begin_session`, `end_session`, `flush`, `close` --
so the Normalizer, the scheduler and the BLE StreamSource are the same code
whether the database is on this machine or a homelab box. That duck type is
the whole trick: the split changes one object at the bottom of the stack and
nothing above it.

What it actually does:

1. Everything is spooled first, synchronously. Nothing is ever held only in
   memory while a socket is being attempted -- a crash mid-post costs
   nothing; the spool provides the ordering guarantee.
2. A drain thread posts batches to /api/ingest, deletes them only on a 2xx,
   and backs off when the server is unreachable.
3. A permanently-rejected batch (a 4xx that is not auth) is dropped rather
   than retried forever, with a loud message. A malformed row that the
   server will never accept must not wedge everything queued behind it.

`source_id` arguments are accepted and ignored. The agent has no idea which
row in the server's `sources` table it is -- that is exactly the knowledge
the split takes away from it -- so it sends the vendor and display name and
lets the server resolve them. Keeping the signature identical is what lets
the Normalizer stay unaware of which side it is on.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime
from typing import Any, Dict, Optional, Sequence

from ticker import config as tconfig
from ticker.agent.spool import Spool
from ticker.api.server import TOKEN_HEADER
from ticker.model import Observation, SessionRecord, iso_utc

log = logging.getLogger("ticker.agent")

# Reconnect backoff, same shape as the scheduler's: 1s, 2s, 4s ... capped.
BASE_DELAY_SEC = 1.0
MAX_DELAY_SEC = 60.0

# Statuses that mean 'this batch will never be accepted'. 401/403 are
# excluded on purpose: a token fixed while the agent runs should drain what
# accumulated, not find it discarded.
_PERMANENT = frozenset({400, 404, 405, 413, 422})


def backoff_delay(failures: int, base: float = BASE_DELAY_SEC,
                  maximum: float = MAX_DELAY_SEC) -> float:
    if failures <= 0:
        return 0.0
    return min(maximum, base * (2 ** (failures - 1)))


def stable_session_id(record: SessionRecord) -> SessionRecord:
    """Give a session an external_id that survives being sent twice.

    Everything else on this path is idempotent because of the observations'
    unique index, but a session without an external_id is INSERTed every
    time the server sees it -- so a batch re-sent after an ambiguous
    timeout would leave two session rows, one of them empty. Section 10's
    'a replayed spool is harmless' has to cover sessions too.

    The session *key* cannot be that id: it is a per-process counter, so an
    agent restarted between two sessions would call them both '1' and the
    second would overwrite the first. The start instant can: two sessions
    from one strap cannot begin in the same millisecond, and a replay of
    the same record carries the same one.
    """
    if record.external_id:
        return record
    return replace(record, external_id="agent:{}".format(iso_utc(record.start_ts)))


class UplinkError(RuntimeError):
    """A post that failed, with whether it is worth retrying."""

    def __init__(self, message: str, status: Optional[int] = None,
                 permanent: bool = False):
        super().__init__(message)
        self.status = status
        self.permanent = permanent


def post_json(url: str, body: Dict[str, Any], token: Optional[str] = None,
              timeout: Optional[float] = None) -> Dict[str, Any]:
    """One POST to the ingest endpoint. Raises UplinkError on anything but
    a 2xx, so callers never have to inspect a status themselves."""
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header(TOKEN_HEADER, token)
    timeout = tconfig.AGENT_TIMEOUT_SEC if timeout is None else timeout
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise UplinkError("server said {} {}".format(exc.code, detail or exc.reason),
                          status=exc.code, permanent=exc.code in _PERMANENT)
    except (urllib.error.URLError, OSError) as exc:
        # Unreachable, refused, DNS, timeout -- the normal offline case, and
        # the one the spool exists for.
        raise UplinkError(str(exc))
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        # A 2xx with a body we can't parse still means it was accepted;
        # something in front of the server rewrote the response.
        return {}


class Uplink:
    """A store-shaped client for a remote Ticker server."""

    def __init__(self, server_url: Optional[str] = None,
                 token: Optional[str] = None,
                 spool: Optional[Spool] = None,
                 vendor: str = "ble",
                 display_name: Optional[str] = None,
                 kind: str = "stream",
                 batch: Optional[int] = None,
                 timeout: Optional[float] = None,
                 poster=post_json,
                 sleep=time.sleep,
                 start: bool = True):
        self.server_url = (server_url or tconfig.AGENT_SERVER_URL).rstrip("/")
        self.token = token if token is not None else tconfig.API_TOKEN
        self.spool = spool if spool is not None else Spool()
        self.vendor = vendor
        self.display_name = display_name or tconfig.source_display_name(vendor)
        self.kind = kind
        self.batch = tconfig.AGENT_BATCH if batch is None else batch
        self.timeout = timeout
        self._poster = poster
        self._sleep = sleep
        self.device: Optional[Dict[str, Any]] = None

        self.delivered = 0
        self.discarded = 0
        self.failures = 0
        self.last_error: Optional[str] = None
        self.connected = False

        self._wake = threading.Event()
        self._stop = threading.Event()
        # One drain at a time. flush() drains on the caller's thread while
        # the background thread may be mid-post; without this both take()
        # the same rows and post them twice, and a session posted twice is
        # two session rows rather than one.
        self._draining = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        if start:
            self._thread = threading.Thread(target=self._run, name="ticker-uplink",
                                            daemon=True)
            self._thread.start()

    # -- the store-shaped surface ----------------------------------------

    def insert_observations(self, source_id, batch: Sequence[Observation]) -> None:
        if batch:
            self.spool.add_observations(batch)
            self._wake.set()

    def begin_session(self, source_id, record: SessionRecord,
                      device_id=None) -> None:
        # device_id is the server's, so it cannot mean anything here. What
        # the agent knows about the strap travels as self.device instead.
        self.spool.add_session(stable_session_id(record), self.device)
        self._wake.set()

    def end_session(self, source_id, key: str,
                    end_ts: Optional[datetime] = None) -> None:
        self.spool.add_session_end(key, end_ts)
        self._wake.set()

    def record_sync(self, *args, **kwargs) -> None:
        """Pull-source bookkeeping, which the agent never does -- it runs
        streams. Accepted so the duck type is complete and a misconfigured
        agent fails with 'no pull sources' rather than AttributeError."""

    def insert_raw_payload(self, *args, **kwargs) -> None:
        """Same: raw vendor payloads belong to the pull path, on the server."""

    def rebuild_rollups(self) -> None:
        """The server owns the rollups; it has the observations."""

    def flush(self, timeout: float = 10.0) -> bool:
        """Drain everything spooled so far, blocking until it lands.

        Returns False if the server could not be reached in the time given.
        The rows are still spooled -- 'not delivered yet' is not 'lost' --
        so a caller that gets False can stop anyway without losing data.
        """
        deadline = time.monotonic() + timeout
        while self.spool.pending():
            if time.monotonic() >= deadline:
                return False
            if not self._drain_once():
                # Unreachable. Wait a beat rather than spinning on a socket
                # that is going to refuse just as fast the second time.
                self._sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        return True

    def close(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            still_posting = self._thread.is_alive()
            self._thread = None
            if still_posting:
                # The drain thread is inside a post that has not timed out
                # yet. Closing the spool underneath it would make its ack
                # fail on a dead connection and end the process on a
                # traceback -- over a batch that is already durable on disk
                # and will be re-sent by the next run anyway.
                log.warning("uplink still posting after %.0fs; leaving the "
                            "spool open (%d rows waiting)", timeout,
                            self.spool.pending())
                return
        self.spool.close()

    # -- status ----------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return {"server": self.server_url, "connected": self.connected,
                "pending": self.spool.pending(), "delivered": self.delivered,
                "discarded": self.discarded, "dropped": self.spool.dropped,
                "oldest": self.spool.oldest(), "last_error": self.last_error}

    # -- the drain thread ------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.spool.pending():
                if self._drain_once():
                    continue          # more waiting; keep going immediately
                # Unreachable or refused: back off, but wake early if the
                # stream queues something (which won't help) or close() is
                # called (which should not wait out a 60-second sleep).
                self._wake.wait(backoff_delay(self.failures))
                self._wake.clear()
                continue
            self._wake.wait(1.0)
            self._wake.clear()
        # A last attempt on the way out, so a clean stop with the server up
        # leaves nothing spooled.
        if self.spool.pending():
            self._drain_once()

    def _drain_once(self) -> bool:
        """Post one batch. True if it landed (or was permanently rejected
        and dropped), False if the server could not be reached."""
        with self._draining:
            return self._drain_locked()

    def _drain_locked(self) -> bool:
        ids, body = self.spool.take(self.batch)
        if not ids:
            return True
        body["source"] = {"vendor": self.vendor, "kind": self.kind,
                          "display_name": self.display_name}
        if self.device and "device" not in body:
            body["device"] = self.device
        observations = len(body.get("observations", ()))
        try:
            self._poster(self.server_url + "/api/ingest", body, self.token,
                         self.timeout)
        except UplinkError as exc:
            self.last_error = str(exc)
            if exc.permanent:
                # Dropping data is the last thing this should ever do, so it
                # says exactly what and why. The alternative is a batch the
                # server will never accept blocking every row behind it,
                # which loses far more.
                self.spool.ack(ids)
                self.discarded += observations
                self.failures = 0
                self.connected = True
                log.error("server permanently rejected %d observations, "
                          "dropped: %s", observations, exc)
                return True
            self.failures += 1
            if self.connected or self.failures == 1:
                log.warning("uplink to %s failed (%d): %s", self.server_url,
                            self.failures, exc)
            self.connected = False
            return False

        self.spool.ack(ids)
        self.delivered += observations
        if not self.connected and self.failures:
            log.info("uplink to %s restored; %d rows still spooled",
                     self.server_url, self.spool.pending())
        self.connected = True
        self.failures = 0
        self.last_error = None
        return True
