"""Signal codec: named fields at fixed positions inside a binary payload.

A field is either *byte-aligned* (a byte offset and a byte length, an integer of
either byte order or an IEEE 754 binary32/binary64 float) or a *bit field* (a byte
offset, a bit offset inside that byte and a bit length, an unsigned or two's
complement integer). Bits are numbered from the most significant bit of a byte and a
bit field is read most significant bit first, across byte boundaries if it needs to:
the order of Kaitai Struct's ``bN`` types, CCSDS and network headers. There is one bit
order on purpose; ``endian`` does not apply to a bit field.

Either kind can be a fixed-length array (``count`` elements laid out back to back, each
of the field's own width); its value is a tuple. Optional scale/offset conversion maps
raw wire values to physical values. Passive metadata such as units/min/max is retained
on field definitions but is not enforced by the codec.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import Enum
from itertools import pairwise
from typing import Literal, TypeAlias

BytesLike: TypeAlias = bytes | bytearray | memoryview
ByteOrder: TypeAlias = Literal["big", "little"]
ScalarValue: TypeAlias = int | float
FieldValue: TypeAlias = ScalarValue | tuple[ScalarValue, ...]
"""A decoded field: a scalar, or a tuple of scalars for an array field."""

BIT_FIELD_MAX_BITS = 64


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
    """One field inside a binary payload: byte-aligned or a bit field, scalar or array.

    A byte-aligned field gives ``byte_length``; a bit field gives ``byte_length=None``
    with ``bit_offset`` (0-7, counted from the most significant bit of the byte at
    ``byte_offset``) and ``bit_length`` (1-64). ``count`` above 1 makes the field an
    array of that many elements, element ``i`` starting ``i`` element widths after the
    first; the value is then a sequence on packing and a tuple on unpacking.

    ``scale`` and ``offset`` follow the common conversion formula:

    ``physical_value = raw_value * scale + offset``

    Integer fields therefore require the inverse conversion to produce an exact
    integral raw value while packing. ``units``, ``min`` and ``max`` are passive
    metadata retained for configs/UI/tests and are deliberately not range checks.
    """

    name: str
    byte_offset: int
    byte_length: int | None
    data_type: SignalDataType
    endian: SignalEndian = SignalEndian.BIG
    scale: float = 1.0
    offset: float = 0.0
    units: str | None = None
    min: float | int | None = None
    max: float | int | None = None
    bit_offset: int | None = dataclass_field(default=None, kw_only=True)
    bit_length: int | None = dataclass_field(default=None, kw_only=True)
    count: int = dataclass_field(default=1, kw_only=True)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_name(self.name))
        object.__setattr__(
            self,
            "byte_offset",
            _validate_non_negative_int("byte_offset", self.byte_offset),
        )
        object.__setattr__(self, "data_type", _coerce_data_type(self.data_type))
        object.__setattr__(self, "endian", _coerce_endian(self.endian))
        object.__setattr__(self, "scale", _validate_finite_float("scale", self.scale))
        object.__setattr__(self, "offset", _validate_finite_float("offset", self.offset))
        object.__setattr__(self, "count", _validate_positive_int("count", self.count))

        if self.bit_offset is None and self.bit_length is None:
            if self.byte_length is None:
                raise SignalLayoutError(
                    f"field {self.name!r} needs byte_length, or bit_offset and bit_length "
                    "for a bit field"
                )
            object.__setattr__(
                self,
                "byte_length",
                _validate_positive_int("byte_length", self.byte_length),
            )
        else:
            self._validate_bit_field()

        if self.scale == 0.0:
            raise SignalLayoutError("scale must not be zero")
        if self.data_type is SignalDataType.FLOAT and self.byte_length not in {4, 8}:
            raise SignalLayoutError(
                "IEEE 754 float fields must be 4 bytes (binary32) or 8 bytes (binary64), "
                f"got {self.byte_length}"
            )
        _validate_optional_real_metadata("min", self.min)
        _validate_optional_real_metadata("max", self.max)

    def _validate_bit_field(self) -> None:
        if self.bit_offset is None or self.bit_length is None:
            raise SignalLayoutError(f"bit field {self.name!r} needs both bit_offset and bit_length")
        if self.byte_length is not None:
            raise SignalLayoutError(
                f"bit field {self.name!r} takes bit_offset and bit_length, not byte_length"
            )
        bit_offset = _validate_non_negative_int("bit_offset", self.bit_offset)
        if bit_offset > 7:
            raise SignalLayoutError(
                f"bit_offset must be 0-7 (a bit inside the byte at byte_offset), got {bit_offset}"
            )
        bit_length = _validate_positive_int("bit_length", self.bit_length)
        if bit_length > BIT_FIELD_MAX_BITS:
            raise SignalLayoutError(
                f"bit_length must be at most {BIT_FIELD_MAX_BITS}, got {bit_length}"
            )
        if self.data_type is SignalDataType.FLOAT:
            raise SignalLayoutError(
                f"bit field {self.name!r} must be 'uint' or 'int'; a float is byte-aligned"
            )
        if self.endian is not SignalEndian.BIG:
            raise SignalLayoutError(
                f"bit field {self.name!r} is read most significant bit first; endian does "
                "not apply to a bit field"
            )

    @property
    def is_bit_field(self) -> bool:
        """Whether this field is a bit field rather than byte-aligned."""

        return self.bit_length is not None

    @property
    def is_array(self) -> bool:
        """Whether this field holds ``count`` elements (a tuple) rather than one scalar."""

        return self.count > 1

    @property
    def element_bits(self) -> int:
        """Width of one element in bits."""

        if self.bit_length is not None:
            return self.bit_length
        assert self.byte_length is not None
        return self.byte_length * 8

    @property
    def start_bit(self) -> int:
        """Position of the field's first bit, counted from the payload's first bit (MSB)."""

        return self.byte_offset * 8 + (self.bit_offset or 0)

    @property
    def end_bit(self) -> int:
        """Exclusive bit position immediately after the field's last element."""

        return self.start_bit + self.element_bits * self.count

    @property
    def end_byte(self) -> int:
        """Exclusive byte offset immediately after this field."""

        return -(-self.end_bit // 8)

    @property
    def signed(self) -> bool:
        """Whether this field stores a signed integer raw value."""

        return self.data_type is SignalDataType.INT

    def pack_into(self, payload: bytearray, value: object) -> None:
        """Pack one physical value (a sequence of ``count`` for an array) into ``payload``."""

        elements = self._elements(value)
        for index, element in enumerate(elements):
            start = self.start_bit + index * self.element_bits
            if self.is_bit_field:
                self._pack_bits(payload, start, element)
            else:
                first = start // 8
                payload[first : first + self.element_bits // 8] = self._pack_scalar(element)

    def pack_value(self, value: object) -> bytes:
        """Pack one physical value into the bytes ``byte_offset..end_byte`` it occupies."""

        payload = bytearray(self.end_byte)
        self.pack_into(payload, value)
        return bytes(payload[self.byte_offset :])

    def unpack_value(self, payload: BytesLike) -> FieldValue:
        """Decode one physical value (a tuple for an array) from ``payload``."""

        payload_bytes = _coerce_bytes("payload", payload)
        if len(payload_bytes) < self.end_byte:
            raise SignalValueError(
                f"payload is too short for field {self.name!r}: needs byte range "
                f"{self.byte_offset}..{self.end_byte}, got {len(payload_bytes)} bytes"
            )
        values: list[ScalarValue] = []
        for index in range(self.count):
            start = self.start_bit + index * self.element_bits
            if self.is_bit_field:
                values.append(self._unpack_bits(payload_bytes, start))
            else:
                first = start // 8
                values.append(
                    self._unpack_scalar(payload_bytes[first : first + self.element_bits // 8])
                )
        if self.is_array:
            return tuple(values)
        return values[0]

    def _elements(self, value: object) -> tuple[object, ...]:
        if not self.is_array:
            return (value,)
        if isinstance(value, str | bytes | bytearray) or not isinstance(value, Iterable):
            raise SignalValueError(
                f"array field {self.name!r} value must be a sequence of {self.count} values"
            )
        elements = tuple(value)
        if len(elements) != self.count:
            raise SignalValueError(
                f"array field {self.name!r} needs {self.count} values, got {len(elements)}"
            )
        return elements

    def _pack_scalar(self, value: object) -> bytes:
        if self.data_type is SignalDataType.FLOAT:
            return self._pack_float(value)
        return self._pack_integer(value)

    def _unpack_scalar(self, raw_bytes: bytes) -> ScalarValue:
        if self.data_type is SignalDataType.FLOAT:
            return self._unpack_float(raw_bytes)
        return self._unpack_integer(raw_bytes)

    def _pack_bits(self, payload: bytearray, start_bit: int, value: object) -> None:
        raw_value = self._checked_raw_integer(value, self.element_bits)
        width = self.element_bits
        if raw_value < 0:
            raw_value += 1 << width
        first, last = start_bit // 8, -(-(start_bit + width) // 8)
        span_bits = (last - first) * 8
        shift = span_bits - (start_bit - first * 8) - width
        mask = ((1 << width) - 1) << shift
        span = int.from_bytes(payload[first:last], "big")
        span = (span & ~mask) | (raw_value << shift)
        payload[first:last] = span.to_bytes(last - first, "big")

    def _unpack_bits(self, payload: bytes, start_bit: int) -> ScalarValue:
        width = self.element_bits
        first, last = start_bit // 8, -(-(start_bit + width) // 8)
        span_bits = (last - first) * 8
        shift = span_bits - (start_bit - first * 8) - width
        raw_value = (int.from_bytes(payload[first:last], "big") >> shift) & ((1 << width) - 1)
        if self.signed and raw_value >= 1 << (width - 1):
            raw_value -= 1 << width
        return self._raw_integer_to_physical(raw_value)

    def _checked_raw_integer(self, value: object, bit_count: int) -> int:
        raw_value = self._physical_to_raw_integer(value)
        minimum, maximum = _integer_raw_range(bit_count, signed=self.signed)
        if not minimum <= raw_value <= maximum:
            raise SignalValueError(
                f"field {self.name!r} raw integer value must be in range "
                f"{minimum}..{maximum}, got {raw_value}"
            )
        return raw_value

    def _pack_integer(self, value: object) -> bytes:
        assert self.byte_length is not None
        raw_value = self._checked_raw_integer(value, self.byte_length * 8)
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
        return self._raw_integer_to_physical(raw_value)

    def _raw_integer_to_physical(self, raw_value: int) -> ScalarValue:
        if self.scale == 1.0 and self.offset == 0.0:
            return raw_value
        return raw_value * self.scale + self.offset

    def _physical_to_raw_integer(self, value: object) -> int:
        if self.scale == 1.0 and self.offset == 0.0:
            if isinstance(value, bool):
                # A flag is the common content of a 1-bit field; True/False are 1/0.
                return int(value)
            if not isinstance(value, int):
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
        assert self.byte_length is not None
        physical_value = _coerce_real(f"field {self.name!r} value", value)
        raw_value = (physical_value - self.offset) / self.scale
        try:
            return struct.pack(_float_struct_format(self.endian, self.byte_length), raw_value)
        except (OverflowError, struct.error) as exc:
            raise SignalValueError(f"field {self.name!r} float value cannot be encoded") from exc

    def _unpack_float(self, raw_bytes: bytes) -> float:
        assert self.byte_length is not None
        (raw_value,) = struct.unpack(_float_struct_format(self.endian, self.byte_length), raw_bytes)
        return float(raw_value) * self.scale + self.offset


@dataclass(frozen=True, slots=True)
class SignalLayout:
    """A collection of named, non-overlapping fields making up one payload."""

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
        for signal_field in self.fields:
            signal_field.pack_into(payload, values[signal_field.name])
        return bytes(payload)

    def unpack(self, payload: BytesLike) -> dict[str, FieldValue]:
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
        ordered = sorted(self.fields, key=lambda candidate: candidate.start_bit)
        for previous, current in pairwise(ordered):
            if current.start_bit < previous.end_bit:
                byte_offset, bit = divmod(current.start_bit, 8)
                where = f"byte offset {byte_offset}" + (f" bit {bit}" if bit else "")
                raise SignalLayoutError(
                    f"signal fields {previous.name!r} and {current.name!r} overlap at {where}"
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
) -> dict[str, FieldValue]:
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


def _integer_raw_range(bit_count: int, *, signed: bool) -> tuple[int, int]:
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
    "BIT_FIELD_MAX_BITS",
    "BytesLike",
    "FieldValue",
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
