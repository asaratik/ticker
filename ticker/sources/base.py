"""
Connector protocols.

Three rules keep connectors honest:

* A connector never touches SQLite. It yields Observation objects and
  nothing else.
* A connector never sleeps for rate limiting. It raises RateLimited and lets
  the scheduler decide, which makes retry policy testable without
  wall-clock waits.
* A connector never decides what "now" means. The scheduler passes explicit
  windows.

These are Protocols, not base classes: a connector satisfies one by having
the right methods, so nothing here has to be imported to write one, and
nothing here can accumulate behaviour that connectors quietly depend on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Iterable, Optional, Protocol, runtime_checkable

from ticker.model import Observation


# -- health --------------------------------------------------------------

@dataclass(frozen=True)
class SourceHealth:
    """What the UI shows in the source list. Cheap to produce, never raises."""

    ok: bool
    state: str                                  # 'connected', 'idle', 'auth_expired', ...
    detail: Optional[str] = None
    last_success: Optional[datetime] = None
    last_error: Optional[str] = None

    @classmethod
    def unknown(cls, detail: Optional[str] = None) -> "SourceHealth":
        return cls(ok=False, state="unknown", detail=detail)


# -- exceptions ----------------------------------------------------------

class SourceError(Exception):
    """Base for anything a connector raises deliberately."""


class RateLimited(SourceError):
    """The vendor said slow down.

    retry_after is seconds, from the vendor's own header where there is one.
    The connector does not sleep -- it raises this and the scheduler backs
    the source off, so retry policy is testable without wall-clock waits.
    """

    def __init__(self, retry_after: float, message: Optional[str] = None):
        super().__init__(message or "rate limited, retry in {}s".format(retry_after))
        self.retry_after = float(retry_after)


class AuthExpired(SourceError):
    """Credentials need a refresh or a re-authorisation the user must do."""


class TransientError(SourceError):
    """A failure worth retrying on the normal schedule: a timeout, a 5xx.

    Distinct from an unexpected exception, which the scheduler treats as a
    bug and reports rather than quietly retrying forever.
    """


class PermanentError(SourceError):
    """Retrying will not help: a deleted account, a revoked scope, a 400."""


# -- protocols -----------------------------------------------------------

@runtime_checkable
class Source(Protocol):
    vendor: str

    def capabilities(self) -> "frozenset[str]":
        """Metric names this source can produce."""

    def health(self) -> SourceHealth:
        """Connectivity/auth status, for the UI. Must not raise."""


@runtime_checkable
class StreamSource(Source, Protocol):
    def stream(self) -> AsyncIterator[Observation]:
        """Yield observations as they arrive. Reconnects internally.
        Must be cancellable -- cancellation is the normal stop path."""


@runtime_checkable
class PullSource(Source, Protocol):
    def fetch(self, metric: str, since: datetime, until: datetime
              ) -> Iterable[Observation]:
        """Return everything in [since, until) for one metric.
        May be called with overlapping windows; the store dedupes.
        Raises RateLimited(retry_after) rather than sleeping internally."""


@runtime_checkable
class ImportSource(Source, Protocol):
    def parse(self, path: Path) -> Iterable[Observation]:
        """Parse a file export. Streaming/iterative -- Apple Health XML
        exports are routinely over a gigabyte."""
