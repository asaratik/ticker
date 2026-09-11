"""
A reader for FIT, Garmin's binary format for activities, all-day monitoring
and sleep.

Every data message in a FIT file is laid out by a definition message earlier
in the same file, so a reader can walk the whole thing knowing nothing about
what the messages mean -- and that is what this does. It turns bytes into
(message number, {field number: value}) and stops there. Deciding what a
heart-rate field or a sleep level *is* belongs to garmin_files.py.

Stdlib only. The message and field numbers used elsewhere come from Garmin's
FIT SDK profile (fit-csharp-sdk, Dynastream/Fit/Profile).

The format, in brief: a 12- or 14-byte header ending '.FIT', then records,
then a two-byte CRC; files can be chained back to back. A record starts
with a header byte that is one of
  * a definition: local message type -> global number, byte order, and the
    number, size and base type of each field (plus developer fields);
  * a data message: that local type's fields, in that order;
  * a compressed-timestamp data message: five bits of seconds added to the
    last full timestamp, for messages that would otherwise carry their own.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Tuple

# Seconds in a FIT timestamp count from here.
FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)

TIMESTAMP = 253                  # the field number every message uses

# Global message numbers (fit-csharp-sdk, Types/MesgNum.cs).
FILE_ID = 0
SESSION = 18
RECORD = 20
MONITORING = 55
HRV = 78
MONITORING_INFO = 103
SLEEP_LEVEL = 275

# Base types by their low five bits: struct code, width, invalid value.
# The high bit only says whether the type has a byte order.
_BASE_TYPES: Dict[int, Tuple[str, int, Any]] = {
    0: ("B", 1, 0xFF),                   # enum
    1: ("b", 1, 0x7F),                   # sint8
    2: ("B", 1, 0xFF),                   # uint8
    3: ("h", 2, 0x7FFF),                 # sint16
    4: ("H", 2, 0xFFFF),                 # uint16
    5: ("i", 4, 0x7FFFFFFF),             # sint32
    6: ("I", 4, 0xFFFFFFFF),             # uint32
    8: ("f", 4, None),                   # float32; invalid is a NaN pattern
    9: ("d", 8, None),                   # float64
    10: ("B", 1, 0x00),                  # uint8z
    11: ("H", 2, 0x0000),                # uint16z
    12: ("I", 4, 0x00000000),            # uint32z
    14: ("q", 8, 0x7FFFFFFFFFFFFFFF),    # sint64
    15: ("Q", 8, 0xFFFFFFFFFFFFFFFF),    # uint64
    16: ("Q", 8, 0),                     # uint64z
}
_STRING = 7
_BYTES = 13


class FitError(ValueError):
    """Not a FIT file, or one that ends before it says it does."""


@dataclass
class Message:
    num: int                      # global message number
    fields: Dict[int, Any]        # field number -> value; None when invalid


@dataclass
class _Definition:
    num: int
    endian: str
    fields: List[Tuple[int, int, int]]     # (number, size, base type)
    developer_size: int


def fit_time(seconds: int) -> datetime:
    return FIT_EPOCH + timedelta(seconds=seconds)


def is_fit(data: bytes) -> bool:
    return len(data) >= 12 and data[0] in (12, 14) and data[8:12] == b".FIT"


def read_messages(data: bytes) -> Iterator[Message]:
    """Every data message in `data`, which may be several chained files."""
    if not is_fit(data):
        # Checked up front: a file too short to hold even a header would
        # otherwise read as a FIT file with nothing in it.
        raise FitError("not a FIT file")
    offset = 0
    while offset + 12 <= len(data):
        if not is_fit(data[offset:]):
            return                      # padding after the last chained file
        header_size = data[offset]
        (records_size,) = struct.unpack_from("<I", data, offset + 4)
        start = offset + header_size
        end = start + records_size
        if end > len(data):
            raise FitError("the file ends before its header says it does")
        yield from _records(data, start, end)
        offset = end + 2                # skip the file's CRC


def _records(data: bytes, pos: int, end: int) -> Iterator[Message]:
    definitions: Dict[int, _Definition] = {}
    last_timestamp = None
    while pos < end:
        header = data[pos]
        pos += 1

        if header & 0x80:
            # Compressed timestamp: local type in bits 5-6, and a five-bit
            # offset that rolls over the last full timestamp's low bits.
            definition = _defined(definitions, (header >> 5) & 0x03)
            fields, pos = _read_fields(data, pos, definition)
            if last_timestamp is not None:
                low = header & 0x1F
                stamp = (last_timestamp & ~0x1F) + low
                if low < (last_timestamp & 0x1F):
                    stamp += 0x20
                last_timestamp = stamp
                fields.setdefault(TIMESTAMP, stamp)
            yield Message(definition.num, fields)
            continue

        local = header & 0x0F
        if header & 0x40:
            pos, definitions[local] = _read_definition(data, pos, bool(header & 0x20))
            continue

        definition = _defined(definitions, local)
        fields, pos = _read_fields(data, pos, definition)
        stamp = fields.get(TIMESTAMP)
        if isinstance(stamp, int):
            last_timestamp = stamp
        yield Message(definition.num, fields)


def _defined(definitions: Dict[int, _Definition], local: int) -> _Definition:
    definition = definitions.get(local)
    if definition is None:
        raise FitError("a data message came before its definition")
    return definition


def _read_definition(data: bytes, pos: int, developer: bool
                     ) -> Tuple[int, _Definition]:
    endian = ">" if data[pos + 1] == 1 else "<"       # after a reserved byte
    (num,) = struct.unpack_from(endian + "H", data, pos + 2)
    count = data[pos + 4]
    pos += 5
    fields = []
    for _ in range(count):
        fields.append((data[pos], data[pos + 1], data[pos + 2]))
        pos += 3
    developer_size = 0
    if developer:
        # Developer fields carry app-defined data; only their size matters
        # here, so they can be stepped over.
        dev_count = data[pos]
        pos += 1
        for _ in range(dev_count):
            developer_size += data[pos + 1]
            pos += 3
    return pos, _Definition(num, endian, fields, developer_size)


def _read_fields(data: bytes, pos: int, definition: _Definition
                 ) -> Tuple[Dict[int, Any], int]:
    fields: Dict[int, Any] = {}
    for number, size, base in definition.fields:
        fields[number] = _decode(data[pos:pos + size], base, definition.endian)
        pos += size
    return fields, pos + definition.developer_size


def _decode(raw: bytes, base: int, endian: str) -> Any:
    kind = base & 0x1F
    if kind == _STRING:
        text = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
        return text or None
    spec = _BASE_TYPES.get(kind)
    if kind == _BYTES or spec is None or not raw or len(raw) % spec[1]:
        return raw
    code, width, invalid = spec
    values = list(struct.unpack(endian + code * (len(raw) // width), raw))
    if invalid is None:
        values = [None if value != value else value for value in values]   # NaN
    else:
        values = [None if value == invalid else value for value in values]
    return values[0] if len(values) == 1 else values
