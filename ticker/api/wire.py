"""
The JSON shape of an observation on the wire.

Both halves of the agent/server split depend on this being one definition:
the agent serialises with it and the server parses with it, and they are
routinely different versions of the code -- the strap machine gets updated
when someone remembers, the homelab box when it reboots. So the parser is
deliberately forgiving about what it accepts and strict about what it
requires: unknown keys are ignored (a newer agent may send fields this
server has never heard of), missing optional keys default, and only a
genuinely unusable row raises.

An observation carries no source_id. Which configured source row a batch
belongs to is the server's business -- the agent knows it is 'the BLE strap
called X', not that it is source #3 in a database it cannot see.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from ticker.model import Observation, SessionRecord, iso_utc, parse_iso


class BadObservation(ValueError):
    """A row that cannot be turned into an Observation. Carries the reason
    so the server can answer with it instead of a bare 400."""


def observation_to_json(obs: Observation) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "metric": obs.metric,
        "ts": iso_utc(obs.ts),
        "value": obs.value,
    }
    # Optional fields are omitted rather than sent as null. At 1 Hz with RR
    # intervals this is most of the bytes on the wire, and a spool holding a
    # week of them is on a machine that may not have much disk.
    if obs.end_ts is not None:
        payload["end_ts"] = iso_utc(obs.end_ts)
    if obs.text_value is not None:
        payload["text_value"] = obs.text_value
    if obs.external_id:
        payload["external_id"] = obs.external_id
    if obs.session_key:
        payload["session_key"] = obs.session_key
    return payload


def observation_from_json(payload: Any) -> Observation:
    if not isinstance(payload, dict):
        raise BadObservation("observation must be an object")
    try:
        metric = payload["metric"]
        ts = parse_iso(payload["ts"])
        raw_value = payload["value"]
        if isinstance(raw_value, bool):
            raise TypeError("value must be a finite number")
        value = float(raw_value)
    except KeyError as exc:
        raise BadObservation("missing {}".format(exc.args[0]))
    except (TypeError, ValueError) as exc:
        raise BadObservation(str(exc))
    if not isinstance(metric, str) or not metric:
        raise BadObservation("metric must be a non-empty string")
    if not math.isfinite(value):
        raise BadObservation("value must be a finite number")
    end_ts = payload.get("end_ts")
    try:
        return Observation(
            metric=metric,
            ts=ts,
            value=value,
            end_ts=parse_iso(end_ts) if end_ts else None,
            text_value=_optional_string(payload, "text_value"),
            external_id=_optional_string(payload, "external_id") or "",
            session_key=_optional_string(payload, "session_key"),
        )
    except (TypeError, ValueError) as exc:
        # Observation.__post_init__ rejects end_ts < ts, among others.
        raise BadObservation(str(exc))


def observations_from_json(payload: Any) -> List[Observation]:
    if not isinstance(payload, list):
        raise BadObservation("observations must be a list")
    return [observation_from_json(item) for item in payload]


def session_to_json(record: SessionRecord) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "key": record.key,
        "start_ts": iso_utc(record.start_ts),
        "kind": record.kind,
    }
    if record.end_ts is not None:
        payload["end_ts"] = iso_utc(record.end_ts)
    if record.label is not None:
        payload["label"] = record.label
    if record.external_id is not None:
        payload["external_id"] = record.external_id
    return payload


def session_from_json(payload: Any) -> SessionRecord:
    if not isinstance(payload, dict):
        raise BadObservation("session must be an object")
    try:
        key = payload["key"]
        start_ts = parse_iso(payload["start_ts"])
    except KeyError as exc:
        raise BadObservation("session missing {}".format(exc.args[0]))
    except (TypeError, ValueError) as exc:
        raise BadObservation(str(exc))
    if not isinstance(key, str) or not key:
        raise BadObservation("session key must be a non-empty string")
    end_ts = payload.get("end_ts")
    try:
        return SessionRecord(
            key=key,
            start_ts=start_ts,
            end_ts=parse_iso(end_ts) if end_ts else None,
            kind=_optional_string(payload, "kind") or "manual",
            label=_optional_string(payload, "label"),
            external_id=_optional_string(payload, "external_id"),
        )
    except (TypeError, ValueError) as exc:
        raise BadObservation(str(exc))


def _optional_string(payload: Dict[str, Any], key: str) -> Optional[str]:
    value = payload.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise BadObservation("{} must be a string".format(key))
    return value


def device_from_json(payload: Any) -> Optional[Dict[str, Any]]:
    """The strap the agent is talking to, if it named one.

    Optional throughout: an agent that hasn't connected yet, or a source
    with nothing to key a device row on, simply omits it.
    """
    if not isinstance(payload, dict):
        return None
    address = payload.get("address")
    if not address:
        return None
    return {"address": str(address),
            "name": payload.get("name") or None,
            "model": payload.get("model") or None}
