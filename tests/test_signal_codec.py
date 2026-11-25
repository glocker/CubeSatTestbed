from __future__ import annotations

import math
import struct

import pytest

from cubesat_testbed.protocol.signal_codec import (
    SignalCodecError,
    SignalDataType,
    SignalEndian,
    SignalField,
    SignalLayout,
    SignalLayoutError,
    SignalValueError,
    pack_signals,
    unpack_signals,
)


def test_layout_packs_and_unpacks_mixed_scalar_fields() -> None:
    layout = SignalLayout(
        (
            SignalField(
                name="battery_percent",
                byte_offset=0,
                byte_length=1,
                data_type=SignalDataType.UINT,
                scale=0.5,
                units="percent",
                min=0,
                max=100,
            ),
            SignalField(
                name="temperature_c",
                byte_offset=1,
                byte_length=2,
                data_type=SignalDataType.INT,
                endian=SignalEndian.LITTLE,
                scale=0.1,
                offset=-40.0,
                units="degC",
            ),
            SignalField(
                name="bus_voltage_v",
                byte_offset=3,
                byte_length=4,
                data_type=SignalDataType.FLOAT,
                endian=SignalEndian.BIG,
                units="V",
            ),
        ),
        frame_length=8,
    )

    payload = layout.pack(
        {
            "battery_percent": 42.5,
            "temperature_c": -12.3,
            "bus_voltage_v": 12.5,
        }
    )

    assert payload == b"\x55\x15\x01" + struct.pack(">f", 12.5) + b"\x00"
    decoded = layout.unpack(payload)
    assert decoded["battery_percent"] == 42.5
    assert decoded["temperature_c"] == pytest.approx(-12.3)
    assert decoded["bus_voltage_v"] == 12.5


def test_integer_endianness_is_applied_per_field() -> None:
    fields = (
        SignalField("little_u16", 0, 2, SignalDataType.UINT, SignalEndian.LITTLE),
        SignalField("big_u16", 2, 2, SignalDataType.UINT, SignalEndian.BIG),
    )

    payload = pack_signals(fields, {"little_u16": 0x1234, "big_u16": 0x1234})

    assert payload == bytes.fromhex("34 12 12 34")
    assert unpack_signals(fields, payload) == {"little_u16": 0x1234, "big_u16": 0x1234}


def test_signed_integer_fields_round_trip_twos_complement_values() -> None:
    layout = SignalLayout((SignalField("current_ma", 0, 2, SignalDataType.INT),))

    assert layout.pack({"current_ma": -250}) == (-250).to_bytes(2, "big", signed=True)
    assert layout.unpack(bytes.fromhex("ff 06")) == {"current_ma": -250}


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        (SignalField("u8", 0, 1, SignalDataType.UINT), -1, "0..255"),
        (SignalField("u8", 0, 1, SignalDataType.UINT), 256, "0..255"),
        (SignalField("i8", 0, 1, SignalDataType.INT), -129, "-128..127"),
        (SignalField("i8", 0, 1, SignalDataType.INT), 128, "-128..127"),
    ],
)
def test_integer_fields_reject_raw_values_outside_wire_range(
    field: SignalField,
    value: int,
    match: str,
) -> None:
    with pytest.raises(SignalValueError, match=match):
        field.pack_value(value)


def test_integer_scaling_must_map_to_exact_raw_integer() -> None:
    field = SignalField("scaled", 0, 1, SignalDataType.UINT, scale=2.0)

    with pytest.raises(SignalValueError, match="integral raw value"):
        field.pack_value(3.0)


@pytest.mark.parametrize("byte_length", [4, 8])
def test_float_fields_pack_ieee754_binary32_and_binary64(byte_length: int) -> None:
    field = SignalField(
        "temperature",
        0,
        byte_length,
        SignalDataType.FLOAT,
        SignalEndian.LITTLE,
        scale=0.5,
        offset=-10.0,
    )
    fmt = "<f" if byte_length == 4 else "<d"

    payload = field.pack_value(15.0)

    assert payload == struct.pack(fmt, 50.0)
    assert field.unpack_value(payload) == pytest.approx(15.0)


def test_passive_metadata_is_preserved_but_not_enforced() -> None:
    field = SignalField(
        "battery_percent",
        0,
        1,
        SignalDataType.UINT,
        units="percent",
        min=0,
        max=100,
    )
    layout = SignalLayout((field,))

    assert layout.field("battery_percent").units == "percent"
    assert layout.field("battery_percent").min == 0
    assert layout.field("battery_percent").max == 100
    assert layout.pack({"battery_percent": 150}) == b"\x96"


@pytest.mark.parametrize(
    ("field", "payload"),
    [
        (SignalField("a", 0, 1, SignalDataType.UINT), b""),
        (SignalField("b", 1, 2, SignalDataType.INT), b"\x00"),
    ],
)
def test_field_unpack_rejects_short_payload(field: SignalField, payload: bytes) -> None:
    with pytest.raises(SignalValueError, match="too short"):
        field.unpack_value(payload)


def test_layout_rejects_duplicate_or_overlapping_fields() -> None:
    with pytest.raises(SignalLayoutError, match="duplicate"):
        SignalLayout(
            (
                SignalField("same", 0, 1, SignalDataType.UINT),
                SignalField("same", 1, 1, SignalDataType.UINT),
            )
        )

    with pytest.raises(SignalLayoutError, match="overlap"):
        SignalLayout(
            (
                SignalField("first", 0, 2, SignalDataType.UINT),
                SignalField("second", 1, 2, SignalDataType.UINT),
            )
        )


def test_layout_pack_rejects_missing_and_unexpected_values() -> None:
    layout = SignalLayout((SignalField("value", 0, 1, SignalDataType.UINT),))

    with pytest.raises(SignalValueError, match="missing"):
        layout.pack({})

    with pytest.raises(SignalValueError, match="unexpected"):
        layout.pack({"value": 1, "extra": 2})


def test_layout_with_fixed_frame_length_rejects_wrong_payload_length() -> None:
    layout = SignalLayout((SignalField("value", 0, 1, SignalDataType.UINT),), frame_length=2)

    assert layout.pack({"value": 7}) == b"\x07\x00"
    with pytest.raises(SignalValueError, match="exactly 2 bytes"):
        layout.unpack(b"\x07")
    with pytest.raises(SignalValueError, match="exactly 2 bytes"):
        layout.unpack(b"\x07\x00\x00")


def test_v1_layout_rejects_unsupported_field_shapes() -> None:
    with pytest.raises(SignalLayoutError, match="data_type"):
        SignalField("enum_like", 0, 1, "enum")  # type: ignore[arg-type]

    with pytest.raises(SignalLayoutError, match="endian"):
        SignalField("network", 0, 1, SignalDataType.UINT, "network")  # type: ignore[arg-type]

    with pytest.raises(SignalLayoutError, match="IEEE 754"):
        SignalField("half", 0, 2, SignalDataType.FLOAT)

    with pytest.raises(SignalLayoutError, match="scale"):
        SignalField("bad_scale", 0, 1, SignalDataType.UINT, scale=0.0)

    with pytest.raises(SignalCodecError):
        SignalField("nan_scale", 0, 1, SignalDataType.UINT, scale=math.nan)


# --- bit fields and arrays (backlog item 17) ---------------------------------------------------


def _bits(
    name: str, byte_offset: int, bit_offset: int, bit_length: int, **kwargs: object
) -> SignalField:
    return SignalField(
        name,
        byte_offset,
        None,
        kwargs.pop("data_type", SignalDataType.UINT),  # type: ignore[arg-type]
        bit_offset=bit_offset,
        bit_length=bit_length,
        **kwargs,  # type: ignore[arg-type]
    )


def test_bits_count_from_the_most_significant_bit_of_the_byte() -> None:
    fields = (_bits("top", 0, 0, 1), _bits("mode", 0, 1, 3), _bits("low", 0, 7, 1))

    payload = pack_signals(fields, {"top": 1, "mode": 0b101, "low": 1})

    assert payload == bytes([0b1101_0001])
    assert unpack_signals(fields, payload) == {"top": 1, "mode": 5, "low": 1}


def test_a_bit_field_crosses_byte_boundaries_most_significant_bit_first() -> None:
    # 2-bit priority, then a 14-bit address spanning the rest of byte 0 and all of byte 1.
    fields = (_bits("priority", 0, 0, 2), _bits("address", 0, 2, 14))

    payload = pack_signals(fields, {"priority": 2, "address": 0x1234})

    assert payload == bytes([0b10_010010, 0x34])
    assert unpack_signals(fields, payload) == {"priority": 2, "address": 0x1234}


def test_a_signed_bit_field_is_twos_complement_over_its_own_width() -> None:
    field = _bits("delta", 1, 4, 4, data_type=SignalDataType.INT)

    assert field.pack_value(-3) == bytes([0b0000_1101])
    assert field.unpack_value(bytes([0xFF, 0b0000_1101])) == -3
    with pytest.raises(SignalValueError, match="-8..7"):
        field.pack_value(8)


def test_a_bit_field_applies_scale_and_offset() -> None:
    field = _bits("temp", 0, 4, 12, scale=0.5, offset=-40.0)

    assert field.unpack_value(field.pack_value(21.5)) == 21.5


def test_a_bool_packs_as_one_and_zero() -> None:
    field = _bits("flag", 0, 0, 1)

    assert field.pack_value(True) == b"\x80"
    assert field.pack_value(False) == b"\x00"


def test_an_unsigned_bit_field_refuses_a_value_wider_than_its_bits() -> None:
    with pytest.raises(SignalValueError, match="0..7"):
        _bits("mode", 0, 0, 3).pack_value(8)


def test_a_byte_array_lays_its_elements_back_to_back() -> None:
    field = SignalField("cells", 2, 2, SignalDataType.UINT, SignalEndian.LITTLE, count=3)

    payload = pack_signals((field,), {"cells": [0x0102, 0x0304, 0x0506]})

    assert payload == bytes.fromhex("0000 0201 0403 0605")
    assert unpack_signals((field,), payload) == {"cells": (0x0102, 0x0304, 0x0506)}
    assert field.end_byte == 8


def test_a_float_array_round_trips() -> None:
    field = SignalField("v", 0, 4, SignalDataType.FLOAT, count=2)

    assert field.unpack_value(field.pack_value((3.5, -1.25))) == (3.5, -1.25)


def test_a_bit_array_packs_elements_bit_after_bit() -> None:
    field = _bits("flags", 0, 2, 2, count=3)

    payload = field.pack_value([1, 2, 3])

    assert payload == bytes([0b00_01_10_11])
    assert field.unpack_value(payload) == (1, 2, 3)


def test_an_array_needs_exactly_count_values() -> None:
    field = SignalField("cells", 0, 1, SignalDataType.UINT, count=2)

    with pytest.raises(SignalValueError, match="needs 2 values, got 3"):
        field.pack_value([1, 2, 3])
    with pytest.raises(SignalValueError, match="sequence of 2"):
        field.pack_value(7)


def test_bit_fields_sharing_a_byte_are_fine_but_overlapping_bits_are_not() -> None:
    SignalLayout((_bits("a", 0, 0, 4), _bits("b", 0, 4, 4)))

    with pytest.raises(SignalLayoutError, match="overlap at byte offset 0 bit 3"):
        SignalLayout((_bits("a", 0, 0, 4), _bits("b", 0, 3, 2)))
    with pytest.raises(SignalLayoutError, match="overlap"):
        SignalLayout(
            (
                SignalField("cells", 0, 1, SignalDataType.UINT, count=3),
                _bits("flag", 2, 7, 1),
            )
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"bit_offset": 8, "bit_length": 1}, "bit_offset must be 0-7"),
        ({"bit_offset": 0, "bit_length": 65}, "at most 64"),
        ({"bit_offset": 0, "bit_length": 0}, "bit_length must be positive"),
        ({"bit_offset": 0}, "needs both bit_offset and bit_length"),
        ({"bit_offset": 0, "bit_length": 8, "data_type": SignalDataType.FLOAT}, "'uint' or 'int'"),
        (
            {"bit_offset": 0, "bit_length": 8, "endian": SignalEndian.LITTLE},
            "endian does not apply",
        ),
        ({"bit_offset": 0, "bit_length": 8, "count": 0}, "count must be positive"),
    ],
)
def test_bit_field_definitions_are_validated(kwargs: dict[str, object], message: str) -> None:
    data_type = kwargs.pop("data_type", SignalDataType.UINT)
    with pytest.raises(SignalLayoutError, match=message):
        SignalField("f", 0, None, data_type, **kwargs)  # type: ignore[arg-type]


def test_a_field_needs_byte_length_or_bits_but_not_both() -> None:
    with pytest.raises(SignalLayoutError, match="needs byte_length"):
        SignalField("f", 0, None, SignalDataType.UINT)
    with pytest.raises(SignalLayoutError, match="not byte_length"):
        SignalField("f", 0, 1, SignalDataType.UINT, bit_offset=0, bit_length=4)
