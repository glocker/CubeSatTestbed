"""Bridges declarative telemetry config to the signal codec.

A telemetry *packet* is one CSP packet carrying one or more fields. A
``[nodes.<node>.packets.<name>]`` definition declares several; a plain
``[nodes.<node>.telemetry.<name>]`` mapping is the shorthand for a packet holding
that one signal. Either way this module sees a list of ``(field name, layout)``
pairs and the whole payload: fragmentation is the transport layer's concern. A
packet may be longer than one CAN frame (it is then sent as several CFP frames).

``enum`` bridges non-numeric Python telemetry values (e.g. ``power_status =
"offline"``) to the codec's integer wire values, element by element for an array.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cubesat_testbed.protocol.signal_codec import (
    FieldValue,
    ScalarValue,
    SignalCodecError,
    SignalDataType,
    SignalEndian,
    SignalField,
    SignalLayout,
)

if TYPE_CHECKING:
    from cubesat_testbed.config.parser import TelemetryFieldLayout


class TelemetryCodecError(ValueError):
    """Raised when a configured telemetry signal cannot be encoded/decoded."""


def signal_field_for_mapping(name: str, mapping: TelemetryFieldLayout) -> SignalField:
    """Build the :class:`SignalField` declared by ``mapping``."""

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
            bit_offset=mapping.bit_offset,
            bit_length=mapping.bit_length,
            count=mapping.count,
        )
    except SignalCodecError as exc:
        raise TelemetryCodecError(f"invalid telemetry wire layout for {name!r}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class TelemetryPacketCodec:
    """Encode and decode the fields of one telemetry packet together."""

    fields: tuple[tuple[str, TelemetryFieldLayout], ...]
    layout: SignalLayout

    @classmethod
    def build(cls, fields: Sequence[tuple[str, TelemetryFieldLayout]]) -> TelemetryPacketCodec:
        signal_fields = tuple(signal_field_for_mapping(name, mapping) for name, mapping in fields)
        try:
            layout = SignalLayout(signal_fields)
        except SignalCodecError as exc:
            raise TelemetryCodecError(f"invalid telemetry packet layout: {exc}") from exc
        return cls(tuple(fields), layout)

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(name for name, _mapping in self.fields)

    @property
    def payload_length(self) -> int:
        """Payload bytes this packet occupies (the end of its last field)."""

        return self.layout.required_length

    def encode(self, values: Mapping[str, object]) -> bytes:
        """Encode one Python value per field into the packet's payload."""

        raw_values = {name: _to_raw(name, mapping, values[name]) for name, mapping in self.fields}
        try:
            return self.layout.pack(raw_values)
        except SignalCodecError as exc:
            names = ", ".join(repr(name) for name in self.field_names)
            raise TelemetryCodecError(f"cannot encode telemetry {names}: {exc}") from exc

    def decode(self, payload: bytes) -> dict[str, object]:
        """Decode every field of the packet from a received payload."""

        try:
            raw_values = self.layout.unpack(payload)
        except SignalCodecError as exc:
            names = ", ".join(repr(name) for name in self.field_names)
            raise TelemetryCodecError(f"cannot decode telemetry {names}: {exc}") from exc
        return {name: _from_raw(name, mapping, raw_values[name]) for name, mapping in self.fields}


def encode_telemetry_value(name: str, mapping: TelemetryFieldLayout, value: object) -> bytes:
    """Encode one Python telemetry value into its configured wire payload."""

    return TelemetryPacketCodec.build(((name, mapping),)).encode({name: value})


def decode_telemetry_value(name: str, mapping: TelemetryFieldLayout, payload: bytes) -> object:
    """Decode one configured telemetry value from a received wire payload."""

    return TelemetryPacketCodec.build(((name, mapping),)).decode(payload)[name]


def _to_raw(name: str, mapping: TelemetryFieldLayout, value: object) -> object:
    if mapping.enum is None:
        return value
    if mapping.count > 1 and isinstance(value, Sequence) and not isinstance(value, str):
        return tuple(_label_to_raw(name, mapping.enum, element) for element in value)
    return _label_to_raw(name, mapping.enum, value)


def _label_to_raw(name: str, enum: Mapping[str, str], value: object) -> int:
    label = str(value)
    for raw_key, mapped_label in enum.items():
        if mapped_label == label:
            return int(raw_key)
    raise TelemetryCodecError(
        f"value {value!r} for telemetry {name!r} is not one of the configured enum labels: "
        f"{', '.join(sorted(enum.values()))}"
    )


def _from_raw(name: str, mapping: TelemetryFieldLayout, raw_value: FieldValue) -> object:
    if mapping.enum is None:
        return raw_value
    if isinstance(raw_value, tuple):
        return tuple(_raw_to_label(name, mapping.enum, element) for element in raw_value)
    return _raw_to_label(name, mapping.enum, raw_value)


def _raw_to_label(name: str, enum: Mapping[str, str], raw_value: ScalarValue) -> str:
    key = str(int(raw_value))
    try:
        return enum[key]
    except KeyError:
        raise TelemetryCodecError(
            f"decoded raw value {raw_value!r} for telemetry {name!r} has no configured enum label"
        ) from None


__all__ = [
    "TelemetryCodecError",
    "TelemetryPacketCodec",
    "decode_telemetry_value",
    "encode_telemetry_value",
    "signal_field_for_mapping",
]
