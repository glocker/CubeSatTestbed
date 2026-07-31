from __future__ import annotations

import pytest

from cubesat_testbed.config import TelemetryMapping
from cubesat_testbed.protocol.telemetry_codec import (
    TelemetryCodecError,
    decode_telemetry_value,
    encode_telemetry_value,
    signal_field_for_mapping,
)


def _mapping(**overrides: object) -> TelemetryMapping:
    fields: dict[str, object] = {
        "source_port": 20,
        "destination_port": 20,
        "offset": 0,
        "length": 4,
        "type": "float",
    }
    fields.update(overrides)
    return TelemetryMapping.model_validate(fields)


def test_scalar_value_round_trips_through_encode_and_decode() -> None:
    mapping = _mapping(offset=0, length=2, type="uint", scale=0.5)

    payload = encode_telemetry_value("voltage", mapping, 21.0)

    assert payload == (42).to_bytes(2, "big")
    assert decode_telemetry_value("voltage", mapping, payload) == pytest.approx(21.0)


def test_float_value_round_trips_without_requiring_exact_quantization() -> None:
    mapping = _mapping(offset=0, length=4, type="float")

    payload = encode_telemetry_value("battery_percent", mapping, 80.00041666666667)

    assert decode_telemetry_value("battery_percent", mapping, payload) == pytest.approx(
        80.00041666666667, rel=1e-6
    )


def test_enum_label_round_trips_to_its_configured_raw_value() -> None:
    mapping = _mapping(
        offset=0,
        length=1,
        type="uint",
        enum={"0": "offline", "1": "online"},
    )

    payload = encode_telemetry_value("power_status", mapping, "online")

    assert payload == b"\x01"
    assert decode_telemetry_value("power_status", mapping, payload) == "online"


def test_encode_rejects_a_value_that_is_not_a_configured_enum_label() -> None:
    mapping = _mapping(offset=0, length=1, type="uint", enum={"0": "offline", "1": "online"})

    with pytest.raises(TelemetryCodecError, match="not one of the configured enum labels"):
        encode_telemetry_value("power_status", mapping, "standby")


def test_decode_rejects_a_raw_value_with_no_configured_enum_label() -> None:
    mapping = _mapping(offset=0, length=1, type="uint", enum={"0": "offline", "1": "online"})

    with pytest.raises(TelemetryCodecError, match="has no configured enum label"):
        decode_telemetry_value("power_status", mapping, b"\x02")


def test_encode_rejects_a_value_that_does_not_fit_the_declared_layout() -> None:
    mapping = _mapping(offset=0, length=1, type="uint")  # 0..255 only

    with pytest.raises(TelemetryCodecError, match="cannot encode telemetry"):
        encode_telemetry_value("counter", mapping, 1000)


def test_signal_field_for_mapping_uses_the_mapping_wire_layout() -> None:
    mapping = _mapping(offset=2, length=2, type="int", endian="little", scale=1.0)

    field = signal_field_for_mapping("temperature", mapping)

    assert field.byte_offset == 2
    assert field.byte_length == 2
    assert field.signed is True
