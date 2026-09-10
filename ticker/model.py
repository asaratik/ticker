"""
Core value types. Everything that crosses a connector/store boundary is one
of these -- connectors never see SQLite rows, the store never sees vendor
JSON.

Timestamps
----------
Timestamps use ISO 8601 UTC in TEXT so SQLite's string comparison orders
correctly and Grafana parses them without help. We store *milliseconds*
rather than seconds, deliberately.

RR intervals are sub-second by definition -- a 750 ms interval means more
than one observation per second. Truncating to seconds would make several
beats share a timestamp, and since (source_id, metric_id, ts, external_id)
is UNIQUE with an upsert behind it, they would overwrite each other. The v1
v1 migration expands RR intervals exactly this way, so second resolution
would lose data during the migration that introduces it.

Millisecond resolution costs 4 characters per row and keeps string ordering
valid as long as the format never varies, which is what iso_utc() is for:
one canonical spelling, always UTC, always '+00:00', always 3 decimals.

These dataclasses omit slots=True because it needs Python 3.10 and this
project still builds on 3.9. Nothing else depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# Fixed-width canonical form: '2026-01-01T00:00:00.000+00:00'. Fixed width
# is the whole point -- lexicographic order equals chronological order only
# while every timestamp is spelled the same way.
TS_LEN = len("2026-01-01T00:00:00.000+00:00")


def iso_utc(dt: datetime) -> str:
    """Canonical storage form for an instant. Requires an aware datetime."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime; convert at the connector edge")
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return iso_utc(now_utc())


def parse_iso(text: str) -> datetime:
    """Read a stored timestamp back. Accepts 'Z' and any precision so that
    hand-written config values and v1 rows (second resolution) both work."""
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"naive timestamp in storage: {text!r}")
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class Observation:
    """One measurement, from any source, of any metric.

    ts is always timezone-aware UTC. end_ts is None for point samples.
    external_id is the vendor's stable id for this datum when one exists;
    it participates in the uniqueness constraint, so a source that emits
    two values for the same metric at the same instant must supply one.
    """

    metric: str                        # resolved to metric_id at write time
    ts: datetime
    value: float
    end_ts: Optional[datetime] = None
    text_value: Optional[str] = None
    external_id: str = ""
    session_key: Optional[str] = None  # connector-local session grouping key

    def __post_init__(self):
        if self.ts.tzinfo is None:
            raise ValueError("Observation.ts must be timezone-aware")
        if self.end_ts is not None:
            if self.end_ts.tzinfo is None:
                raise ValueError("Observation.end_ts must be timezone-aware")
            if self.end_ts < self.ts:
                raise ValueError("end_ts precedes ts")


@dataclass(frozen=True)
class SessionRecord:
    key: str                           # connector-local, maps to session_key
    start_ts: datetime
    end_ts: Optional[datetime] = None
    kind: str = "manual"
    label: Optional[str] = None
    external_id: Optional[str] = None

    def __post_init__(self):
        if self.start_ts.tzinfo is None:
            raise ValueError("SessionRecord.start_ts must be timezone-aware")
        if self.end_ts is not None:
            if self.end_ts.tzinfo is None:
                raise ValueError("SessionRecord.end_ts must be timezone-aware")
            if self.end_ts < self.start_ts:
                raise ValueError("SessionRecord.end_ts precedes start_ts")
