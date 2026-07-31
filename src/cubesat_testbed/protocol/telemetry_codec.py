"""Bridges declarative telemetry config to the byte-aligned signal codec.

Each configured :class:`~cubesat_testbed.config.parser.TelemetryMapping` owns
exactly one product-v1 signal field occupying its own single-frame CSP
payload; multi-field telemetry frames are not part of the v1 profile (this
mirrors the CSP v2 codec's own single-frame-only scope). ``enum`` bridges
non-numeric Python telemetry values (e.g. ``power_status = "offline"``) to the
codec's scalar-only wire values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cubesat_testbed.protocol.signal_codec import (
    ScalarValue,
    SignalCodecError,
    SignalDataType,
    SignalEndian,
    SignalField,
    SignalLayout,
)

if TYPE_CHECKING:
    from cubesat_testbed.config.parser import TelemetryMapping


class TelemetryCodecError(ValueError):
    """Raised when a configured telemetry signal cannot be encoded/decoded."""


def signal_field_for_mapping(name: str, mapping: TelemetryMapping) -> SignalField:
    """Build the single-frame :class:`SignalField` declared by ``mapping``."""

    try:
        return SignalField(
            name=name,
            byte_offset=mapping.offset,
            byte_length=mapping.length,
            data_type=SignalDataType(mapping.type),
            endian=SignalEndian(mapping.endian),
            scale=mapping.scale,
            offset=mapping.offset_value,
            units=mapping.units,
            min=mapping.min,
            max=mapping.max,
        )
    except SignalCodecError as exc:
        raise TelemetryCodecError(f"invalid telemetry wire layout for {name!r}: {exc}") from exc


def encode_telemetry_value(name: str, mapping: TelemetryMapping, value: object) -> bytes:
    """Encode one Python telemetry value into its configured wire payload."""

    field = signal_field_for_mapping(name, mapping)
    raw_value = _to_raw(name, mapping, value)
    try:
        return SignalLayout((field,)).pack({name: raw_value})
    except SignalCodecError as exc:
        raise TelemetryCodecError(f"cannot encode telemetry {name!r}: {exc}") from exc


def decode_telemetry_value(name: str, mapping: TelemetryMapping, payload: bytes) -> object:
    """Decode one configured telemetry value from a received wire payload."""

    field = signal_field_for_mapping(name, mapping)
    try:
        raw_values = SignalLayout((field,)).unpack(payload)
    except SignalCodecError as exc:
        raise TelemetryCodecError(f"cannot decode telemetry {name!r}: {exc}") from exc
    return _from_raw(name, mapping, raw_values[name])


def _to_raw(name: str, mapping: TelemetryMapping, value: object) -> object:
    if mapping.enum is None:
        return value
    label = str(value)
    for raw_key, mapped_label in mapping.enum.items():
        if mapped_label == label:
            return int(raw_key)
    raise TelemetryCodecError(
        f"value {value!r} for telemetry {name!r} is not one of the configured enum labels: "
        f"{', '.join(sorted(mapping.enum.values()))}"
    )


def _from_raw(name: str, mapping: TelemetryMapping, raw_value: ScalarValue) -> object:
    if mapping.enum is None:
        return raw_value
    key = str(int(raw_value))
    try:
        return mapping.enum[key]
    except KeyError:
        raise TelemetryCodecError(
            f"decoded raw value {raw_value!r} for telemetry {name!r} has no configured enum label"
        ) from None


__all__ = [
    "TelemetryCodecError",
    "decode_telemetry_value",
    "encode_telemetry_value",
    "signal_field_for_mapping",
]
