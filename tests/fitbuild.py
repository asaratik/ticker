"""
Just enough of a FIT encoder to build test files -- definitions, data
messages in either byte order, developer fields, compressed timestamps.

The reader under test is ticker/sources/fit.py; this is its mirror image,
kept deliberately simple so a bug in one can't hide behind the same bug in
the other.
"""

import struct

UINT8, SINT8, ENUM, STRING = 0x02, 0x01, 0x00, 0x07
UINT16, SINT16, UINT32, UINT32Z = 0x84, 0x83, 0x86, 0x8C


class FitWriter:
    def __init__(self):
        self.records = bytearray()

    def define(self, local, num, fields, endian="<", developer=()):
        """fields: [(number, size, base type)]; developer: [(number, size, index)]."""
        header = 0x40 | local | (0x20 if developer else 0)
        self.records += bytes([header, 0, 1 if endian == ">" else 0])
        self.records += struct.pack(endian + "H", num) + bytes([len(fields)])
        for number, size, base in fields:
            self.records += bytes([number, size, base])
        if developer:
            self.records += bytes([len(developer)])
            for number, size, index in developer:
                self.records += bytes([number, size, index])
        return self

    def data(self, local, payload, compressed_offset=None):
        if compressed_offset is None:
            header = local
        else:
            header = 0x80 | ((local & 0x03) << 5) | (compressed_offset & 0x1F)
        self.records += bytes([header]) + payload
        return self

    def build(self, header_size=14):
        header = struct.pack("<BBHI4s", header_size, 0x20, 2132,
                             len(self.records), b".FIT")
        if header_size == 14:
            header += b"\x00\x00"
        return header + bytes(self.records) + b"\x00\x00"   # CRC: not checked


def seconds(dt):
    """A datetime as FIT seconds."""
    from ticker.sources.fit import FIT_EPOCH
    return int((dt - FIT_EPOCH).total_seconds())


def activity(start, sport=1, heart_rates=(120, 125, 130), rr=(800, 810, 790),
             serial=1234):
    """A workout: file_id, a session, one record a second, one hrv message."""
    t0 = seconds(start)
    w = FitWriter()
    w.define(0, 0, [(0, 1, ENUM), (3, 4, UINT32Z), (4, 4, UINT32)])
    w.data(0, struct.pack("<BII", 4, serial, t0))
    w.define(1, 20, [(253, 4, UINT32), (3, 1, UINT8)])
    for i, hr in enumerate(heart_rates):
        w.data(1, struct.pack("<IB", t0 + i, hr))
    w.define(2, 78, [(0, 2 * len(rr), UINT16)])
    w.data(2, struct.pack("<" + "H" * len(rr), *rr))
    w.define(3, 18, [(253, 4, UINT32), (2, 4, UINT32), (5, 1, ENUM), (7, 4, UINT32)])
    w.data(3, struct.pack("<IIBI", t0 + 600, t0, sport, 600000))
    return w.build()


def monitoring(start, heart_rates=(60, 62, 64), step=60, serial=55):
    """All-day heart rate: one full timestamp, then 16-bit ones."""
    t0 = seconds(start)
    w = FitWriter()
    w.define(0, 0, [(0, 1, ENUM), (3, 4, UINT32Z), (4, 4, UINT32)])
    w.data(0, struct.pack("<BII", 32, serial, t0))
    w.define(1, 55, [(253, 4, UINT32), (27, 1, UINT8)])
    w.data(1, struct.pack("<IB", t0, heart_rates[0]))
    w.define(2, 55, [(26, 2, UINT16), (27, 1, UINT8)])
    for i, hr in enumerate(heart_rates[1:], start=1):
        w.data(2, struct.pack("<HB", (t0 + i * step) & 0xFFFF, hr))
    return w.build()


def sleep(start, levels=(2, 3, 4, 0, 1, 2), step=600, serial=77):
    """Sleep levels every `step` seconds."""
    t0 = seconds(start)
    w = FitWriter()
    w.define(0, 0, [(0, 1, ENUM), (3, 4, UINT32Z), (4, 4, UINT32)])
    w.data(0, struct.pack("<BII", 49, serial, t0))
    w.define(1, 275, [(253, 4, UINT32), (0, 1, ENUM)])
    for i, level in enumerate(levels):
        w.data(1, struct.pack("<IB", t0 + i * step, level))
    return w.build()
