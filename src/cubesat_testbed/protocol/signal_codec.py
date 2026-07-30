"""Byte-aligned scalar signal codec for product v1.

The v1 signal codec intentionally operates only on whole-byte field offsets. It
supports scalar signed/unsigned integers and IEEE 754 binary32/binary64 floats,
with optional scale/offset conversion between raw wire values and physical
values. Passive metadata such as units/min/max is retained on field definitions
but is not enforced by the codec.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Literal, TypeAlias

BytesLike: TypeAlias = bytes | bytearray | memoryview
ByteOrder: TypeAlias = Literal["big", "little"]
ScalarValue: TypeAlias = int | float


class SignalCodecError(ValueError):
    """Base error raised when a signal layout or value cannot be encoded."""


class SignalLayoutError(SignalCodecError):
    """Raised when a field definition or layout is invalid for the v1 codec."""


class SignalValueError(SignalCodecError):
    """Raised when a concrete signal value or payload cannot be encoded/decoded."""


class SignalEndian(str, Enum):
    """Supported byte orders for scalar field encoding."""

    BIG = "big"
    LITTLE = "little"


class SignalDataType(str, Enum):
    """Supported scalar wire data types for product-v1 signal fields."""

    UINT = "uint"
    INT = "int"
    FLOAT = "float"


@dataclass(frozen=True, slots=True)
class SignalField:
    """One byte-aligned scalar field inside a binary payload.

    ``scale`` and ``offset`` follow the common conversion formula:

    ``physical_value = raw_value * scale + offset``

    Integer fields therefore require the inverse conversion to produce an exact
    integral raw value while packing. ``units``, ``min`` and ``max`` are passive
    metadata retained for configs/UI/tests and are deliberately not range checks.
    """

    name: str
    byte_offset: int
    byte_length: int
    data_type: SignalDataType
    endian: SignalEndian = SignalEndian.BIG
    scale: float = 1.0
    offset: float = 0.0
    units: str | None = None
    min: float | int | None = None
    max: float | int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_name(self.name))
        object.__setattr__(
            self,
            "byte_offset",
            _validate_non_negative_int("byte_offset", self.byte_offset),
        )
        object.__setattr__(
            self,
            "byte_length",
            _validate_positive_int("byte_length", self.byte_length),
        )
        object.__setattr__(self, "data_type", _coerce_data_type(self.data_type))
        object.__setattr__(self, "endian", _coerce_endian(self.endian))
        object.__setattr__(self, "scale", _validate_finite_float("scale", self.scale))
        object.__setattr__(self, "offset", _validate_finite_float("offset", self.offset))

        if self.scale == 0.0:
            raise SignalLayoutError("scale must not be zero")
        if self.data_type is SignalDataType.FLOAT and self.byte_length not in {4, 8}:
            raise SignalLayoutError(
                "IEEE 754 float fields must be 4 bytes (binary32) or 8 bytes (binary64), "
                f"got {self.byte_length}"
            )
        _validate_optional_real_metadata("min", self.min)
        _validate_optional_real_metadata("max", self.max)

    @property
    def end_byte(self) -> int:
        """Exclusive byte offset immediately after this field."""

        return self.byte_offset + self.byte_length

    @property
    def signed(self) -> bool:
        """Whether this field stores a signed integer raw value."""

        return self.data_type is SignalDataType.INT

    def pack_value(self, value: object) -> bytes:
        """Pack one physical signal value into this field's raw bytes."""

        if self.data_type is SignalDataType.FLOAT:
            return self._pack_float(value)
        return self._pack_integer(value)

    def unpack_value(self, payload: BytesLike) -> ScalarValue:
        """Decode one physical signal value from ``payload`` using this field."""

        payload_bytes = _coerce_bytes("payload", payload)
        if len(payload_bytes) < self.end_byte:
            raise SignalValueError(
                f"payload is too short for field {self.name!r}: needs byte range "
                f"{self.byte_offset}..{self.end_byte}, got {len(payload_bytes)} bytes"
            )
        raw_bytes = payload_bytes[self.byte_offset : self.end_byte]
        if self.data_type is SignalDataType.FLOAT:
            return self._unpack_float(raw_bytes)
        return self._unpack_integer(raw_bytes)

    def _pack_integer(self, value: object) -> bytes:
        raw_value = self._physical_to_raw_integer(value)
        minimum, maximum = _integer_raw_range(self.byte_length, signed=self.signed)
        if not minimum <= raw_value <= maximum:
            raise SignalValueError(
                f"field {self.name!r} raw integer value must be in range "
                f"{minimum}..{maximum}, got {raw_value}"
            )
        try:
            return raw_value.to_bytes(
                self.byte_length,
                byteorder=_byte_order(self.endian),
                signed=self.signed,
            )
        except OverflowError as exc:
            raise SignalValueError(f"field {self.name!r} integer value overflows") from exc

    def _unpack_integer(self, raw_bytes: bytes) -> ScalarValue:
        raw_value = int.from_bytes(
            raw_bytes,
            byteorder=_byte_order(self.endian),
            signed=self.signed,
        )
        if self.scale == 1.0 and self.offset == 0.0:
            return raw_value
        return raw_value * self.scale + self.offset

    def _physical_to_raw_integer(self, value: object) -> int:
        if self.scale == 1.0 and self.offset == 0.0:
            if isinstance(value, bool) or not isinstance(value, int):
                raise SignalValueError(f"field {self.name!r} value must be an integer")
            return value

        physical_value = _coerce_real(f"field {self.name!r} value", value)
        raw_float = (physical_value - self.offset) / self.scale
        rounded_raw = round(raw_float)
        if not math.isclose(raw_float, rounded_raw, rel_tol=0.0, abs_tol=1e-9):
            raise SignalValueError(
                f"field {self.name!r} physical value {physical_value} does not map to an "
                f"integral raw value with scale={self.scale} and offset={self.offset}"
            )
        return rounded_raw

    def _pack_float(self, value: object) -> bytes:
        physical_value = _coerce_real(f"field {self.name!r} value", value)
        raw_value = (physical_value - self.offset) / self.scale
        try:
            return struct.pack(_float_struct_format(self.endian, self.byte_length), raw_value)
        except (OverflowError, struct.error) as exc:
            raise SignalValueError(f"field {self.name!r} float value cannot be encoded") from exc

    def _unpack_float(self, raw_bytes: bytes) -> float:
        (raw_value,) = struct.unpack(_float_struct_format(self.endian, self.byte_length), raw_bytes)
        return float(raw_value) * self.scale + self.offset


@dataclass(frozen=True, slots=True)
class SignalLayout:
    """A byte-aligned collection of named scalar fields."""

    fields: tuple[SignalField, ...]
    frame_length: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", _coerce_fields(self.fields))
        if self.frame_length is not None:
            object.__setattr__(
                self,
                "frame_length",
                _validate_non_negative_int("frame_length", self.frame_length),
            )
        self._validate_unique_names()
        self._validate_non_overlapping_fields()
        self._validate_frame_length()

    @property
    def required_length(self) -> int:
        """Minimum payload length required by this layout."""

        return max((field.end_byte for field in self.fields), default=0)

    def field_names(self) -> frozenset[str]:
        """Return configured field names."""

        return frozenset(field.name for field in self.fields)

    def field(self, name: str) -> SignalField:
        """Return a field definition by name."""

        for field in self.fields:
            if field.name == name:
                return field
        raise SignalLayoutError(f"signal field {name!r} is not in this layout")

    def pack(self, values: Mapping[str, object]) -> bytes:
        """Pack named physical signal values into a binary payload."""

        expected_names = self.field_names()
        provided_names = frozenset(values)
        missing_names = expected_names - provided_names
        extra_names = provided_names - expected_names
        if missing_names:
            joined = ", ".join(sorted(missing_names))
            raise SignalValueError(f"missing values for signal field(s): {joined}")
        if extra_names:
            joined = ", ".join(sorted(extra_names))
            raise SignalValueError(f"unexpected signal value(s): {joined}")

        payload = bytearray(self._encoded_length())
        for field in self.fields:
            payload[field.byte_offset : field.end_byte] = field.pack_value(values[field.name])
        return bytes(payload)

    def unpack(self, payload: BytesLike) -> dict[str, ScalarValue]:
        """Unpack a binary payload into named physical signal values."""

        payload_bytes = _coerce_bytes("payload", payload)
        self._validate_payload_length(payload_bytes)
        return {field.name: field.unpack_value(payload_bytes) for field in self.fields}

    def _encoded_length(self) -> int:
        if self.frame_length is not None:
            return self.frame_length
        return self.required_length

    def _validate_unique_names(self) -> None:
        seen: set[str] = set()
        for field in self.fields:
            if field.name in seen:
                raise SignalLayoutError(f"duplicate signal field name {field.name!r}")
            seen.add(field.name)

    def _validate_non_overlapping_fields(self) -> None:
        occupied_by_offset: dict[int, str] = {}
        for field in self.fields:
            for byte_offset in range(field.byte_offset, field.end_byte):
                previous_field = occupied_by_offset.setdefault(byte_offset, field.name)
                if previous_field != field.name:
                    raise SignalLayoutError(
                        f"signal fields {previous_field!r} and {field.name!r} overlap at "
                        f"byte offset {byte_offset}"
                    )

    def _validate_frame_length(self) -> None:
        if self.frame_length is not None and self.required_length > self.frame_length:
            raise SignalLayoutError(
                f"frame_length {self.frame_length} is shorter than required layout length "
                f"{self.required_length}"
            )

    def _validate_payload_length(self, payload: bytes) -> None:
        expected_length = self._encoded_length()
        if len(payload) < self.required_length:
            raise SignalValueError(
                f"payload is too short for signal layout: needs at least "
                f"{self.required_length} bytes, got {len(payload)}"
            )
        if self.frame_length is not None and len(payload) != expected_length:
            raise SignalValueError(
                f"payload length must be exactly {expected_length} bytes for this signal layout, "
                f"got {len(payload)}"
            )


def pack_signals(
    fields: Iterable[SignalField],
    values: Mapping[str, object],
    *,
    frame_length: int | None = None,
) -> bytes:
    """Convenience wrapper that packs values using an ad-hoc layout."""

    return SignalLayout(tuple(fields), frame_length=frame_length).pack(values)


def unpack_signals(
    fields: Iterable[SignalField],
    payload: BytesLike,
    *,
    frame_length: int | None = None,
) -> dict[str, ScalarValue]:
    """Convenience wrapper that unpacks values using an ad-hoc layout."""

    return SignalLayout(tuple(fields), frame_length=frame_length).unpack(payload)


def _coerce_fields(fields: tuple[SignalField, ...]) -> tuple[SignalField, ...]:
    try:
        return tuple(fields)
    except TypeError as exc:
        raise SignalLayoutError("fields must be an iterable of SignalField definitions") from exc


def _coerce_data_type(value: SignalDataType) -> SignalDataType:
    try:
        return SignalDataType(value)
    except ValueError as exc:
        raise SignalLayoutError("data_type must be one of 'uint', 'int' or 'float'") from exc


def _coerce_endian(value: SignalEndian) -> SignalEndian:
    try:
        return SignalEndian(value)
    except ValueError as exc:
        raise SignalLayoutError("endian must be 'big' or 'little'") from exc


def _validate_name(value: str) -> str:
    if not isinstance(value, str) or value == "":
        raise SignalLayoutError("signal field name must be a non-empty string")
    return value


def _validate_non_negative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SignalLayoutError(f"{name} must be an integer")
    if value < 0:
        raise SignalLayoutError(f"{name} must be non-negative")
    return value


def _validate_positive_int(name: str, value: int) -> int:
    value = _validate_non_negative_int(name, value)
    if value == 0:
        raise SignalLayoutError(f"{name} must be positive")
    return value


def _validate_finite_float(name: str, value: float) -> float:
    number = _coerce_real(name, value)
    return float(number)


def _validate_optional_real_metadata(name: str, value: float | None) -> None:
    if value is None:
        return
    _coerce_real(name, value)


def _coerce_real(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SignalValueError(f"{name} must be a finite integer or float")
    number = float(value)
    if not math.isfinite(number):
        raise SignalValueError(f"{name} must be finite")
    return number


def _coerce_bytes(name: str, value: BytesLike) -> bytes:
    try:
        return bytes(value)
    except TypeError as exc:
        raise SignalValueError(f"{name} must be bytes-like") from exc


def _integer_raw_range(byte_length: int, *, signed: bool) -> tuple[int, int]:
    bit_count = byte_length * 8
    if signed:
        return -(1 << (bit_count - 1)), (1 << (bit_count - 1)) - 1
    return 0, (1 << bit_count) - 1


def _byte_order(endian: SignalEndian) -> ByteOrder:
    if endian is SignalEndian.BIG:
        return "big"
    return "little"


def _float_struct_format(endian: SignalEndian, byte_length: int) -> str:
    prefix = ">" if endian is SignalEndian.BIG else "<"
    if byte_length == 4:
        return f"{prefix}f"
    return f"{prefix}d"


__all__ = [
    "BytesLike",
    "ScalarValue",
    "SignalCodecError",
    "SignalDataType",
    "SignalEndian",
    "SignalField",
    "SignalLayout",
    "SignalLayoutError",
    "SignalValueError",
    "pack_signals",
    "unpack_signals",
]
