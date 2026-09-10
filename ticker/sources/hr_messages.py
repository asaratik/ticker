"""
v1 heart rate messages -> v2 observations.

hr_source defines a small message protocol that both live sources speak: a
BLE strap and a watch POSTing over HTTP push the same dicts onto the same
queue. Two things consume that protocol now -- the BLE StreamSource, and the
Tk app draining its queue on the UI thread -- so the translation lives here
rather than in either of them.

The v1 protocol is not going away when the connectors are rewritten as
StreamSources: it is what the HTTP source still speaks, and what the agent
spools. This module is the seam between it and the v2
data model.
"""

from __future__ import annotations

from typing import List

from ticker.ingest.derive import expand_rr
from ticker.model import Observation, parse_iso


def observations_from_sample(message: dict, session_key=None) -> List[Observation]:
    """One 'sample' message -> a heart rate observation plus one per beat.

    RR beats carry their index within the notification as an external_id.
    Their timestamps are reconstructed by summing backwards from the arrival
    time rather than measured, so two notifications' windows can overlap onto
    the same millisecond -- and with the default '' the natural key would
    make the second beat silently overwrite the first.
    """
    ts = parse_iso(message["timestamp"])
    out = [Observation(metric="heart_rate_bpm", ts=ts,
                       value=float(message["hr"]), session_key=session_key)]
    for index, (beat_ts, rr) in enumerate(
            expand_rr(ts, message.get("rr_intervals_ms") or [])):
        out.append(Observation(metric="rr_interval_ms", ts=beat_ts,
                               value=float(rr), external_id=str(index),
                               session_key=session_key))
    return out
