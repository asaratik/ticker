"""
Tests for the FIT reader.

Files are built byte by byte with tests/fitbuild.py rather than checked in:
each test then says exactly which corner of the format it exercises --
byte order, invalid values, arrays, developer fields, compressed timestamps
-- instead of trusting that a sample file happens to contain it.
"""

import struct

import pytest

from fitbuild import SINT16, STRING, UINT8, UINT16, UINT32, FitWriter
from ticker.sources import fit


def messages(data):
    return list(fit.read_messages(data))


def test_a_data_message_decodes_by_its_definition():
    w = FitWriter().define(0, fit.RECORD, [(253, 4, UINT32), (3, 1, UINT8)])
    w.data(0, struct.pack("<IB", 1000000000, 72))
    message, = messages(w.build())
    assert message.num == fit.RECORD
    assert message.fields == {253: 1000000000, 3: 72}


def test_invalid_values_come_back_as_none():
    w = FitWriter().define(0, fit.RECORD, [(3, 1, UINT8), (4, 2, SINT16)])
    w.data(0, struct.pack("<Bh", 0xFF, 0x7FFF))
    assert messages(w.build())[0].fields == {3: None, 4: None}


def test_a_field_wider_than_its_type_is_an_array():
    w = FitWriter().define(0, fit.HRV, [(0, 6, UINT16)])
    w.data(0, struct.pack("<HHH", 800, 810, 0xFFFF))
    assert messages(w.build())[0].fields[0] == [800, 810, None]


def test_big_endian_definitions_are_read_big_endian():
    w = FitWriter().define(0, fit.RECORD, [(253, 4, UINT32), (3, 1, UINT8)],
                           endian=">")
    w.data(0, struct.pack(">IB", 1000000000, 90))
    assert messages(w.build())[0].fields == {253: 1000000000, 3: 90}


def test_developer_fields_are_stepped_over():
    w = FitWriter().define(0, fit.RECORD, [(3, 1, UINT8)], developer=[(0, 2, 0)])
    w.data(0, bytes([70]) + b"\xAA\xBB")
    w.data(0, bytes([71]) + b"\xCC\xDD")
    assert [m.fields[3] for m in messages(w.build())] == [70, 71]


def test_strings_stop_at_their_terminator():
    w = FitWriter().define(0, 23, [(27, 8, STRING)])
    w.data(0, b"H10\x00\x00\x00\x00\x00")
    assert messages(w.build())[0].fields[27] == "H10"


def test_a_compressed_timestamp_rolls_over_the_last_full_one():
    # The last full timestamp's low five bits are 30; an offset of 2 means
    # the counter wrapped, so the time is 4 seconds on, not 28 back.
    last = 1000000030
    w = FitWriter().define(0, fit.RECORD, [(253, 4, UINT32), (3, 1, UINT8)])
    w.data(0, struct.pack("<IB", last, 70))
    w.define(1, fit.RECORD, [(3, 1, UINT8)])
    w.data(1, bytes([71]), compressed_offset=2)
    first, second = messages(w.build())
    assert second.fields[253] == 1000000034
    assert second.fields[3] == 71


def test_chained_files_are_read_one_after_another():
    one = FitWriter().define(0, fit.RECORD, [(3, 1, UINT8)]).data(0, bytes([60]))
    two = FitWriter().define(0, fit.RECORD, [(3, 1, UINT8)]).data(0, bytes([61]))
    data = one.build(header_size=12) + two.build()
    assert [m.fields[3] for m in messages(data)] == [60, 61]


def test_something_that_is_not_fit_is_refused():
    with pytest.raises(fit.FitError, match="not a FIT file"):
        messages(b"PK\x03\x04" + b"\x00" * 20)


def test_a_file_shorter_than_its_header_says_is_refused():
    data = FitWriter().define(0, fit.RECORD, [(3, 1, UINT8)]).data(0, bytes([60])).build()
    with pytest.raises(fit.FitError, match="ends before"):
        messages(data[:-6])


def test_data_before_its_definition_is_refused():
    w = FitWriter().data(3, bytes([60]))
    with pytest.raises(fit.FitError, match="before its definition"):
        messages(w.build())


def test_fit_time_counts_from_the_fit_epoch():
    assert fit.fit_time(0) == fit.FIT_EPOCH
    assert fit.fit_time(86400).day == 1 and fit.fit_time(86400).year == 1990
