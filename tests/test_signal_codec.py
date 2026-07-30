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
