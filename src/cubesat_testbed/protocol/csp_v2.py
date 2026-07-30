"""libcsp-compatible CSP v2 codec for product v1.

v1 target profile:

- classic CAN 2.0;
- extended 29-bit CAN identifiers;
- single-frame packets only;
- no fragmentation/reassembly;
- golden-vector validation against captures produced by the project-owned C helper built from official libcsp v2.1 commit 48f7fb0.

libcsp's CAN v2 framing stores the first 16 bits of the 48-bit CSP v2
header in the extended CAN identifier. The remaining 32 header bits are the
first four bytes of the CAN data field, followed by up to four bytes of CSP
payload in this product-v1 single-frame profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

CLASSIC_CAN_MAX_DATA_BYTES = 8
STANDARD_CAN_ID_MAX = 0x7FF
EXTENDED_CAN_ID_MAX = 0x1FFF_FFFF

CSP_V2_CAN_HEADER_EXTENSION_BYTES = 4
CSP_V2_SINGLE_FRAME_MAX_PAYLOAD_BYTES = (
    CLASSIC_CAN_MAX_DATA_BYTES - CSP_V2_CAN_HEADER_EXTENSION_BYTES
)

CSP_V2_PRIORITY_MAX = 0x3
CSP_V2_ADDRESS_MAX = 0x3FFF
CSP_V2_PORT_MAX = 0x3F
CSP_V2_FLAGS_MAX = 0x3F

CSP_CAN_V2_SENDER_MAX = 0x3F
CSP_CAN_V2_PACKET_COUNT_MAX = 0x3
CSP_CAN_V2_FRAME_COUNT_MAX = 0x7

CSP_PORT_CMP = 0
CSP_PORT_PING = 1
CSP_PORT_PS = 2
CSP_PORT_MEMFREE = 3
CSP_PORT_REBOOT = 4
CSP_PORT_BUF_FREE = 5
CSP_PORT_UPTIME = 6

CSP_FLAG_FRAGMENT = 0x10
CSP_FLAG_HMAC = 0x08
CSP_FLAG_RDP = 0x02
CSP_FLAG_CRC32 = 0x01

_CFP2_PRIO_MASK = 0x3
_CFP2_PRIO_OFFSET = 27
_CFP2_DST_MASK = 0x3FFF
_CFP2_DST_OFFSET = 13
_CFP2_SENDER_MASK = 0x3F
_CFP2_SENDER_OFFSET = 7
_CFP2_PACKET_COUNT_MASK = 0x3
_CFP2_PACKET_COUNT_OFFSET = 5
_CFP2_FRAME_COUNT_MASK = 0x7
_CFP2_FRAME_COUNT_OFFSET = 2
_CFP2_BEGIN_MASK = 0x1
_CFP2_BEGIN_OFFSET = 1
_CFP2_END_MASK = 0x1
_CFP2_END_OFFSET = 0

_CSP2_SRC_MASK = 0x3FFF
_CSP2_SRC_OFFSET = 18
_CSP2_DPORT_MASK = 0x3F
_CSP2_DPORT_OFFSET = 12
_CSP2_SPORT_MASK = 0x3F
_CSP2_SPORT_OFFSET = 6
_CSP2_FLAGS_MASK = 0x3F
_CSP2_FLAGS_OFFSET = 0

BytesLike = bytes | bytearray | memoryview


class CspV2CodecError(ValueError):
    """Base error raised when a CSP v2 frame cannot be packed or unpacked."""


class CspV2RangeError(CspV2CodecError):
    """Raised when a logical CSP or CAN field is outside its protocol range."""


class CspV2FrameError(CspV2CodecError):
    """Raised when a CAN frame is outside the supported product-v1 profile."""


class CspPriority(IntEnum):
    """libcsp CSP priority values."""

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


@dataclass(frozen=True, slots=True)
class CspFields:
    """Logical CSP v2 packet fields exposed to higher layers."""

    priority: int
    source: int
    destination: int
    destination_port: int
    source_port: int
    flags: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "priority",
            _validate_uint("priority", self.priority, CSP_V2_PRIORITY_MAX),
        )
        object.__setattr__(
            self,
            "source",
            _validate_uint("source", self.source, CSP_V2_ADDRESS_MAX),
        )
        object.__setattr__(
            self,
            "destination",
            _validate_uint("destination", self.destination, CSP_V2_ADDRESS_MAX),
        )
        object.__setattr__(
            self,
            "destination_port",
            _validate_uint("destination_port", self.destination_port, CSP_V2_PORT_MAX),
        )
        object.__setattr__(
            self,
            "source_port",
            _validate_uint("source_port", self.source_port, CSP_V2_PORT_MAX),
        )
        object.__setattr__(self, "flags", _validate_uint("flags", self.flags, CSP_V2_FLAGS_MAX))


@dataclass(frozen=True, slots=True)
class CspCanIdFields:
    """Fields carried by the CSP v2 extended CAN identifier."""

    priority: int
    destination: int
    sender: int
    packet_count: int
    frame_count: int
    begin: bool
    end: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "priority",
            _validate_uint("priority", self.priority, CSP_V2_PRIORITY_MAX),
        )
        object.__setattr__(
            self,
            "destination",
            _validate_uint("destination", self.destination, CSP_V2_ADDRESS_MAX),
        )
        object.__setattr__(
            self,
            "sender",
            _validate_uint("sender", self.sender, CSP_CAN_V2_SENDER_MAX),
        )
        object.__setattr__(
            self,
            "packet_count",
            _validate_uint("packet_count", self.packet_count, CSP_CAN_V2_PACKET_COUNT_MAX),
        )
        object.__setattr__(
            self,
            "frame_count",
            _validate_uint("frame_count", self.frame_count, CSP_CAN_V2_FRAME_COUNT_MAX),
        )
        _validate_bool("begin", self.begin)
        _validate_bool("end", self.end)


@dataclass(frozen=True, slots=True)
class CspCanFrame:
    """One classic CAN 2.0 frame carrying CSP v2-over-CAN bytes."""

    can_id: int
    data: bytes
    is_extended_id: bool = True

    def __post_init__(self) -> None:
        is_extended_id = _validate_bool("is_extended_id", self.is_extended_id)
        object.__setattr__(self, "is_extended_id", is_extended_id)

        max_can_id = EXTENDED_CAN_ID_MAX if is_extended_id else STANDARD_CAN_ID_MAX
        object.__setattr__(self, "can_id", _validate_uint("can_id", self.can_id, max_can_id))

        data = _coerce_bytes("data", self.data)
        if len(data) > CLASSIC_CAN_MAX_DATA_BYTES:
            raise CspV2FrameError(
                f"classic CAN 2.0 data length must be <= {CLASSIC_CAN_MAX_DATA_BYTES} bytes, "
                f"got {len(data)}"
            )
        object.__setattr__(self, "data", data)

    @property
    def dlc(self) -> int:
        """Classic CAN data length code for this data frame."""

        return len(self.data)

    @property
    def payload(self) -> bytes:
        """Alias for the raw CAN data bytes."""

        return self.data

    @property
    def can_id_hex(self) -> str:
        """Eight-digit hex representation matching committed candump fixtures."""

        return f"0x{self.can_id:08X}"


@dataclass(frozen=True, slots=True)
class DecodedCspPacket:
    """Decoded view of one supported CSP v2 single-frame CAN packet."""

    fields: CspFields
    payload: bytes
    frame: CspCanFrame
    can_fields: CspCanIdFields


# Common libcsp spellings as importable constants.
CSP_PRIO_CRITICAL = CspPriority.CRITICAL
CSP_PRIO_HIGH = CspPriority.HIGH
CSP_PRIO_NORM = CspPriority.NORMAL
CSP_PRIO_LOW = CspPriority.LOW


def pack(
    fields: CspFields,
    payload: BytesLike,
    *,
    sender: int | None = None,
    packet_count: int = 0,
) -> CspCanFrame:
    """Pack logical CSP fields and application payload into one extended CAN frame.

    ``sender`` is the CSP v2 CAN sender field: libcsp uses the low six bits of
    the outgoing interface address. For non-forwarded packets this normally
    matches ``fields.source & 0x3F``, which is the default used here. Pass an
    explicit value when modelling routed/forwarded frames.
    """

    _validate_single_frame_csp_fields(fields)

    payload_bytes = _coerce_bytes("payload", payload)
    if len(payload_bytes) > CSP_V2_SINGLE_FRAME_MAX_PAYLOAD_BYTES:
        raise CspV2FrameError(
            "single-frame CSP v2 over classic CAN can carry at most "
            f"{CSP_V2_SINGLE_FRAME_MAX_PAYLOAD_BYTES} payload bytes, got {len(payload_bytes)}"
        )

    sender_value = fields.source & CSP_CAN_V2_SENDER_MAX if sender is None else sender
    can_fields = CspCanIdFields(
        priority=fields.priority,
        destination=fields.destination,
        sender=sender_value,
        packet_count=packet_count,
        frame_count=0,
        begin=True,
        end=True,
    )
    header_extension = _pack_header_extension(fields)

    return CspCanFrame(
        can_id=_pack_can_id(can_fields),
        data=header_extension.to_bytes(CSP_V2_CAN_HEADER_EXTENSION_BYTES, "big") + payload_bytes,
        is_extended_id=True,
    )


def unpack(can_id: int, data: BytesLike, *, is_extended_id: bool = True) -> DecodedCspPacket:
    """Unpack one CAN frame into logical CSP fields and application payload."""

    return decode_frame(
        CspCanFrame(can_id=can_id, data=_coerce_bytes("data", data), is_extended_id=is_extended_id)
    )


def decode_frame(frame: CspCanFrame) -> DecodedCspPacket:
    """Decode one already materialized CAN frame.

    Only product-v1 single-frame CSP v2 packets are accepted. Multi-frame CAN
    fragments, standard CAN identifiers and malformed header-extension data are
    rejected instead of being partially decoded.
    """

    if not frame.is_extended_id:
        raise CspV2FrameError("CSP v2 over CAN requires an extended 29-bit CAN identifier")
    if frame.dlc < CSP_V2_CAN_HEADER_EXTENSION_BYTES:
        raise CspV2FrameError(
            "CSP v2 CAN begin frame must contain the 4-byte header extension, "
            f"got {frame.dlc} data bytes"
        )

    can_fields = _unpack_can_id(frame.can_id)
    _validate_single_frame_can_fields(can_fields)

    header_extension = int.from_bytes(frame.data[:CSP_V2_CAN_HEADER_EXTENSION_BYTES], "big")
    fields = _unpack_header_extension(
        header_extension,
        priority=can_fields.priority,
        destination=can_fields.destination,
    )
    _validate_single_frame_csp_fields(fields)

    return DecodedCspPacket(
        fields=fields,
        payload=frame.data[CSP_V2_CAN_HEADER_EXTENSION_BYTES:],
        frame=frame,
        can_fields=can_fields,
    )


def _pack_can_id(fields: CspCanIdFields) -> int:
    return (
        ((fields.priority & _CFP2_PRIO_MASK) << _CFP2_PRIO_OFFSET)
        | ((fields.destination & _CFP2_DST_MASK) << _CFP2_DST_OFFSET)
        | ((fields.sender & _CFP2_SENDER_MASK) << _CFP2_SENDER_OFFSET)
        | ((fields.packet_count & _CFP2_PACKET_COUNT_MASK) << _CFP2_PACKET_COUNT_OFFSET)
        | ((fields.frame_count & _CFP2_FRAME_COUNT_MASK) << _CFP2_FRAME_COUNT_OFFSET)
        | ((int(fields.begin) & _CFP2_BEGIN_MASK) << _CFP2_BEGIN_OFFSET)
        | ((int(fields.end) & _CFP2_END_MASK) << _CFP2_END_OFFSET)
    )


def _unpack_can_id(can_id: int) -> CspCanIdFields:
    return CspCanIdFields(
        priority=(can_id >> _CFP2_PRIO_OFFSET) & _CFP2_PRIO_MASK,
        destination=(can_id >> _CFP2_DST_OFFSET) & _CFP2_DST_MASK,
        sender=(can_id >> _CFP2_SENDER_OFFSET) & _CFP2_SENDER_MASK,
        packet_count=(can_id >> _CFP2_PACKET_COUNT_OFFSET) & _CFP2_PACKET_COUNT_MASK,
        frame_count=(can_id >> _CFP2_FRAME_COUNT_OFFSET) & _CFP2_FRAME_COUNT_MASK,
        begin=bool((can_id >> _CFP2_BEGIN_OFFSET) & _CFP2_BEGIN_MASK),
        end=bool((can_id >> _CFP2_END_OFFSET) & _CFP2_END_MASK),
    )


def _pack_header_extension(fields: CspFields) -> int:
    return (
        ((fields.source & _CSP2_SRC_MASK) << _CSP2_SRC_OFFSET)
        | ((fields.destination_port & _CSP2_DPORT_MASK) << _CSP2_DPORT_OFFSET)
        | ((fields.source_port & _CSP2_SPORT_MASK) << _CSP2_SPORT_OFFSET)
        | ((fields.flags & _CSP2_FLAGS_MASK) << _CSP2_FLAGS_OFFSET)
    )


def _unpack_header_extension(
    header_extension: int,
    *,
    priority: int,
    destination: int,
) -> CspFields:
    return CspFields(
        priority=priority,
        source=(header_extension >> _CSP2_SRC_OFFSET) & _CSP2_SRC_MASK,
        destination=destination,
        destination_port=(header_extension >> _CSP2_DPORT_OFFSET) & _CSP2_DPORT_MASK,
        source_port=(header_extension >> _CSP2_SPORT_OFFSET) & _CSP2_SPORT_MASK,
        flags=(header_extension >> _CSP2_FLAGS_OFFSET) & _CSP2_FLAGS_MASK,
    )


def _validate_single_frame_csp_fields(fields: CspFields) -> None:
    if fields.flags & CSP_FLAG_FRAGMENT:
        raise CspV2FrameError("CSP_FFRAG packets are outside the v1 single-frame profile")


def _validate_single_frame_can_fields(fields: CspCanIdFields) -> None:
    if not fields.begin:
        raise CspV2FrameError("cannot decode CSP v2 continuation frames in the v1 profile")
    if not fields.end:
        raise CspV2FrameError("cannot decode fragmented CSP v2 CAN packets in the v1 profile")
    if fields.frame_count != 0:
        raise CspV2FrameError(
            f"single-frame CSP v2 CAN packets must have frame_count 0, got {fields.frame_count}"
        )


def _coerce_bytes(name: str, value: BytesLike) -> bytes:
    try:
        return bytes(value)
    except TypeError as exc:
        raise CspV2FrameError(f"{name} must be bytes-like") from exc


def _validate_uint(name: str, value: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CspV2RangeError(f"{name} must be an integer")
    if not 0 <= value <= maximum:
        raise CspV2RangeError(f"{name} must be in range 0..{maximum}, got {value}")
    return int(value)


def _validate_bool(name: str, value: bool) -> bool:
    if not isinstance(value, bool):
        raise CspV2RangeError(f"{name} must be a bool")
    return value


__all__ = [
    "CLASSIC_CAN_MAX_DATA_BYTES",
    "CSP_CAN_V2_PACKET_COUNT_MAX",
    "CSP_CAN_V2_SENDER_MAX",
    "CSP_FLAG_CRC32",
    "CSP_FLAG_FRAGMENT",
    "CSP_FLAG_HMAC",
    "CSP_FLAG_RDP",
    "CSP_PORT_BUF_FREE",
    "CSP_PORT_CMP",
    "CSP_PORT_MEMFREE",
    "CSP_PORT_PING",
    "CSP_PORT_PS",
    "CSP_PORT_REBOOT",
    "CSP_PORT_UPTIME",
    "CSP_PRIO_CRITICAL",
    "CSP_PRIO_HIGH",
    "CSP_PRIO_LOW",
    "CSP_PRIO_NORM",
    "CSP_V2_ADDRESS_MAX",
    "CSP_V2_CAN_HEADER_EXTENSION_BYTES",
    "CSP_V2_FLAGS_MAX",
    "CSP_V2_PORT_MAX",
    "CSP_V2_PRIORITY_MAX",
    "CSP_V2_SINGLE_FRAME_MAX_PAYLOAD_BYTES",
    "EXTENDED_CAN_ID_MAX",
    "STANDARD_CAN_ID_MAX",
    "CspCanFrame",
    "CspCanIdFields",
    "CspFields",
    "CspPriority",
    "CspV2CodecError",
    "CspV2FrameError",
    "CspV2RangeError",
    "DecodedCspPacket",
    "decode_frame",
    "pack",
    "unpack",
]
