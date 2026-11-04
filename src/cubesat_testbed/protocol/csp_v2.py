"""libcsp-compatible CSP v2 codec over classic CAN, including CFP2 fragmentation.

Target profile:

- classic CAN 2.0;
- extended 29-bit CAN identifiers;
- one CSP packet per CAN frame (:func:`pack` / :func:`decode_frame`) or, when the
  packet does not fit one frame, several CFP2 frames (:func:`fragment_packet` /
  :class:`CspFragmenter` to send, :class:`CspReassembler` to receive);
- golden-vector validation against captures produced by the project-owned C helper built from official libcsp v2.1 commit 48f7fb0.

libcsp's CAN v2 framing stores the first 16 bits of the 48-bit CSP v2
header in the extended CAN identifier. The remaining 32 header bits are the
first four bytes of the CAN data field of the *first* frame, followed by up to
four bytes of CSP payload. Every later frame carries up to eight payload bytes
and no header. The CRC32/HMAC trailer libcsp appends counts as payload, so a
packet with either flag needs a second frame even for a 4-byte payload.

Reassembly follows ``csp_can2_rx`` in libcsp ``v2.1``: fragments are matched to
their packet by priority, destination, sender and the 2-bit packet counter; the
3-bit frame counter must arrive in order and wraps from 7 back to 0; a packet
ends at the frame with the ``end`` bit (there is no length field); an incomplete
packet is dropped after one second of silence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum

CLASSIC_CAN_MAX_DATA_BYTES = 8
STANDARD_CAN_ID_MAX = 0x7FF
EXTENDED_CAN_ID_MAX = 0x1FFF_FFFF

CSP_V2_CAN_HEADER_EXTENSION_BYTES = 4
CSP_V2_SINGLE_FRAME_MAX_PAYLOAD_BYTES = (
    CLASSIC_CAN_MAX_DATA_BYTES - CSP_V2_CAN_HEADER_EXTENSION_BYTES
)

CSP_V2_CFP_CONTINUATION_PAYLOAD_BYTES = CLASSIC_CAN_MAX_DATA_BYTES
CSP_V2_DEFAULT_MAX_PACKET_BYTES = 256
"""libcsp ``CSP_BUFFER_SIZE`` default: the most payload bytes (trailer included)
one packet buffer holds, hence the most a node can send or reassemble."""
CSP_V2_REASSEMBLY_TIMEOUT_US = 1_000_000
"""libcsp ``PBUF_TIMEOUT_MS`` (1000 ms) in virtual microseconds."""

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
    """Decoded view of one CSP v2 packet, from one CAN frame or several."""

    fields: CspFields
    payload: bytes
    frame: CspCanFrame
    can_fields: CspCanIdFields
    frames: tuple[CspCanFrame, ...] = ()
    """Every CAN frame the packet arrived in, in order; ``frame`` is the first."""

    def __post_init__(self) -> None:
        if not self.frames:
            object.__setattr__(self, "frames", (self.frame,))

    @property
    def frame_count(self) -> int:
        """Number of CAN frames this packet was carried in."""

        return len(self.frames)


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

    Only a packet that fits one frame is decoded here. A frame that belongs to a
    multi-frame packet (a first frame without ``end``, or a continuation frame)
    is rejected rather than partially decoded: feed it to a
    :class:`CspReassembler` instead, which turns the whole frame sequence into
    one :class:`DecodedCspPacket`. Standard CAN identifiers and malformed
    header-extension data are rejected as well.
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


def decode_can_id(can_id: int) -> CspCanIdFields:
    """Split an extended CAN identifier into its CFP2 fields."""

    return _unpack_can_id(_validate_uint("can_id", can_id, EXTENDED_CAN_ID_MAX))


def decode_first_frame_header(frame: CspCanFrame) -> CspFields:
    """Read the CSP header from a first frame, whether or not the packet continues.

    The header is complete in the first frame alone (16 bits in the CAN ID, 32
    in the data), so a bus monitor can name a multi-frame packet's route before
    its payload has arrived.
    """

    if not frame.is_extended_id:
        raise CspV2FrameError("CSP v2 over CAN requires an extended 29-bit CAN identifier")
    can_fields = _unpack_can_id(frame.can_id)
    if not can_fields.begin:
        raise CspV2FrameError("only a first (begin) frame carries the CSP header")
    if frame.dlc < CSP_V2_CAN_HEADER_EXTENSION_BYTES:
        raise CspV2FrameError(
            "CSP v2 CAN begin frame must contain the 4-byte header extension, "
            f"got {frame.dlc} data bytes"
        )
    return _unpack_header_extension(
        int.from_bytes(frame.data[:CSP_V2_CAN_HEADER_EXTENSION_BYTES], "big"),
        priority=can_fields.priority,
        destination=can_fields.destination,
    )


def fragment_packet(
    fields: CspFields,
    payload: BytesLike,
    *,
    sender: int | None = None,
    packet_count: int = 0,
) -> tuple[CspCanFrame, ...]:
    """Split one CSP packet into the CAN frames libcsp would send for it.

    The first frame carries the 4-byte header extension plus up to four payload
    bytes; every later frame carries up to eight payload bytes, with
    ``frame_count`` counting 1, 2, ... and wrapping at 8, and ``end`` set on the
    last frame. A packet that fits the first frame is that frame alone
    (identical to :func:`pack`). ``payload`` is the full CSP payload as libcsp
    holds it, including any CRC32/HMAC trailer: this function does not compute
    one.

    ``packet_count`` is the 2-bit CFP2 packet counter that tells consecutive
    packets from one sender apart; use :class:`CspFragmenter` to have it
    advanced per sender.
    """

    _validate_csp_fields(fields)
    payload_bytes = _coerce_bytes("payload", payload)
    sender_value = fields.source & CSP_CAN_V2_SENDER_MAX if sender is None else sender
    header_extension = _pack_header_extension(fields).to_bytes(
        CSP_V2_CAN_HEADER_EXTENSION_BYTES, "big"
    )

    first_chunk = payload_bytes[:CSP_V2_SINGLE_FRAME_MAX_PAYLOAD_BYTES]
    rest = payload_bytes[len(first_chunk) :]
    chunks = [header_extension + first_chunk]
    chunks.extend(
        rest[offset : offset + CSP_V2_CFP_CONTINUATION_PAYLOAD_BYTES]
        for offset in range(0, len(rest), CSP_V2_CFP_CONTINUATION_PAYLOAD_BYTES)
    )

    frames: list[CspCanFrame] = []
    for index, chunk in enumerate(chunks):
        can_fields = CspCanIdFields(
            priority=fields.priority,
            destination=fields.destination,
            sender=sender_value,
            packet_count=packet_count,
            frame_count=index & CSP_CAN_V2_FRAME_COUNT_MAX if index else 0,
            begin=index == 0,
            end=index == len(chunks) - 1,
        )
        frames.append(CspCanFrame(can_id=_pack_can_id(can_fields), data=chunk, is_extended_id=True))
    return tuple(frames)


class CspFragmenter:
    """Per-node packet sender state: the CFP2 packet counter and the size limit.

    libcsp keeps one packet counter per CAN interface and advances it for every
    packet it sends, so two back-to-back packets from one node differ in the
    packet-counter bits of their CAN identifiers. This keeps that counter per
    ``sender`` (the 6-bit CAN-ID sender field).

    ``max_packet_bytes`` is the node's packet buffer size: a payload that does
    not fit it could not even be built by that node's libcsp, so it is refused
    here rather than put on the bus.
    """

    def __init__(self, *, max_packet_bytes: int = CSP_V2_DEFAULT_MAX_PACKET_BYTES) -> None:
        self.max_packet_bytes = _validate_max_packet_bytes(max_packet_bytes)
        self._packet_counts: dict[int, int] = {}

    def fragment(
        self,
        fields: CspFields,
        payload: BytesLike,
        *,
        sender: int | None = None,
    ) -> tuple[CspCanFrame, ...]:
        """Fragment ``payload`` and advance the sender's packet counter."""

        payload_bytes = _coerce_bytes("payload", payload)
        if len(payload_bytes) > self.max_packet_bytes:
            raise CspV2FrameError(
                f"CSP payload of {len(payload_bytes)} bytes exceeds the sender's "
                f"max_packet_bytes={self.max_packet_bytes}"
            )
        sender_value = fields.source & CSP_CAN_V2_SENDER_MAX if sender is None else sender
        count = self._packet_counts.get(sender_value, 0)
        frames = fragment_packet(fields, payload_bytes, sender=sender_value, packet_count=count)
        self._packet_counts[sender_value] = (count + 1) & CSP_CAN_V2_PACKET_COUNT_MAX
        return frames


class ReassemblyDropReason(str, Enum):
    """Why a packet, or a frame, was discarded during reassembly."""

    ORPHAN_FRAGMENT = "orphan_fragment"
    """A continuation frame arrived with no packet in progress for it."""
    FRAME_LOST = "frame_lost"
    """``frame_count`` was not the next expected value: a frame went missing."""
    OVERFLOW = "overflow"
    """The packet grew past the receiving node's ``max_packet_bytes``."""
    TIMEOUT = "timeout"
    """No frame for the packet arrived within the reassembly timeout."""
    RESTARTED = "restarted"
    """A new first frame arrived for a packet still in progress."""
    MALFORMED = "malformed"
    """The frame or the reassembled header is not a valid CSP v2 packet."""


@dataclass(frozen=True, slots=True)
class ReassemblyEvent:
    """Outcome of feeding one frame, or of an expiry, to a :class:`CspReassembler`.

    ``kind`` is ``"complete"`` (``packet`` is set), ``"pending"`` (frame stored,
    packet not finished) or ``"dropped"`` (``reason`` and ``detail`` are set;
    ``frame`` is ``None`` for a timeout, which no single frame causes).
    ``time`` is the virtual time the event happened: for a timeout, the instant
    the reassembly window closed rather than the instant it was noticed.
    """

    kind: str
    time: int
    frame: CspCanFrame | None = None
    packet: DecodedCspPacket | None = None
    reason: ReassemblyDropReason | None = None
    detail: str = ""
    sender: int | None = None
    destination: int | None = None
    limit: int | None = None


@dataclass(slots=True)
class _PartialPacket:
    can_fields: CspCanIdFields
    header_extension: int
    frames: list[CspCanFrame]
    payload: bytearray = field(default_factory=bytearray)
    next_frame_count: int = 1
    last_used: int = 0


_ConnectionKey = tuple[int, int, int, int]


class CspReassembler:
    """Rebuild CSP packets from CFP2 CAN frames in virtual time.

    One instance models what one receiving node's libcsp CAN interface does.
    It is a pure state machine: every method takes the current virtual time
    in microseconds, nothing reads a clock, so the same frames at the same
    times always give the same events.

    ``max_packet_bytes`` is that node's packet buffer size (libcsp
    ``CSP_BUFFER_SIZE``): reassembled payload, trailer included, may not exceed
    it, and ``None`` removes the limit (for a passive observer). Like libcsp,
    stale partial packets are discarded when a packet from a new sender starts
    (``feed``); call :meth:`expire` to flush them without waiting for that, for
    example when a run ends.
    """

    def __init__(
        self,
        *,
        max_packet_bytes: int | None = CSP_V2_DEFAULT_MAX_PACKET_BYTES,
        timeout_us: int = CSP_V2_REASSEMBLY_TIMEOUT_US,
    ) -> None:
        self.max_packet_bytes = (
            None if max_packet_bytes is None else _validate_max_packet_bytes(max_packet_bytes)
        )
        self.timeout_us = _validate_uint("timeout_us", timeout_us, 2**62)
        self._partials: dict[_ConnectionKey, _PartialPacket] = {}

    @property
    def pending_count(self) -> int:
        """Number of packets currently partway through reassembly."""

        return len(self._partials)

    def feed(self, frame: CspCanFrame, *, now: int) -> tuple[ReassemblyEvent, ...]:
        """Process one received frame; return what happened, in order."""

        if not frame.is_extended_id:
            return (
                self._dropped(
                    ReassemblyDropReason.MALFORMED,
                    now,
                    frame,
                    "CSP v2 over CAN requires an extended 29-bit CAN identifier",
                ),
            )

        can_fields = _unpack_can_id(frame.can_id)
        key: _ConnectionKey = (
            can_fields.priority,
            can_fields.destination,
            can_fields.sender,
            can_fields.packet_count,
        )
        events: list[ReassemblyEvent] = []
        partial = self._partials.get(key)

        if partial is None:
            if not can_fields.begin:
                events.append(
                    self._dropped(
                        ReassemblyDropReason.ORPHAN_FRAGMENT,
                        now,
                        frame,
                        f"continuation frame_count={can_fields.frame_count} with no packet "
                        "in progress",
                        can_fields,
                    )
                )
                return tuple(events)
            events.extend(self.expire(now))
        else:
            partial.last_used = now

        if can_fields.begin:
            if partial is not None:
                events.append(
                    self._dropped(
                        ReassemblyDropReason.RESTARTED,
                        now,
                        frame,
                        f"new first frame while {len(partial.frames)} frame(s) were collected",
                        can_fields,
                    )
                )
            if frame.dlc < CSP_V2_CAN_HEADER_EXTENSION_BYTES:
                self._partials.pop(key, None)
                events.append(
                    self._dropped(
                        ReassemblyDropReason.MALFORMED,
                        now,
                        frame,
                        "first frame must carry the 4-byte header extension, "
                        f"got {frame.dlc} data bytes",
                        can_fields,
                    )
                )
                return tuple(events)
            partial = _PartialPacket(
                can_fields=can_fields,
                header_extension=int.from_bytes(
                    frame.data[:CSP_V2_CAN_HEADER_EXTENSION_BYTES], "big"
                ),
                frames=[],
                last_used=now,
            )
            self._partials[key] = partial
            chunk = frame.data[CSP_V2_CAN_HEADER_EXTENSION_BYTES:]
        else:
            assert partial is not None
            if can_fields.frame_count != partial.next_frame_count:
                del self._partials[key]
                events.append(
                    self._dropped(
                        ReassemblyDropReason.FRAME_LOST,
                        now,
                        frame,
                        f"expected frame_count={partial.next_frame_count}, "
                        f"got {can_fields.frame_count} after {len(partial.frames)} frame(s)",
                        can_fields,
                    )
                )
                return tuple(events)
            partial.next_frame_count = (partial.next_frame_count + 1) & CSP_CAN_V2_FRAME_COUNT_MAX
            chunk = frame.data

        if (
            self.max_packet_bytes is not None
            and len(partial.payload) + len(chunk) > self.max_packet_bytes
        ):
            del self._partials[key]
            events.append(
                self._dropped(
                    ReassemblyDropReason.OVERFLOW,
                    now,
                    frame,
                    f"payload would reach {len(partial.payload) + len(chunk)} bytes, "
                    f"over max_packet_bytes={self.max_packet_bytes}",
                    can_fields,
                    limit=self.max_packet_bytes,
                )
            )
            return tuple(events)

        partial.frames.append(frame)
        partial.payload.extend(chunk)

        if not can_fields.end:
            events.append(
                ReassemblyEvent(
                    kind="pending",
                    time=now,
                    frame=frame,
                    sender=can_fields.sender,
                    destination=can_fields.destination,
                )
            )
            return tuple(events)

        del self._partials[key]
        try:
            fields = _unpack_header_extension(
                partial.header_extension,
                priority=partial.can_fields.priority,
                destination=partial.can_fields.destination,
            )
            _validate_csp_fields(fields)
        except CspV2CodecError as exc:
            events.append(
                self._dropped(ReassemblyDropReason.MALFORMED, now, frame, str(exc), can_fields)
            )
            return tuple(events)

        packet = DecodedCspPacket(
            fields=fields,
            payload=bytes(partial.payload),
            frame=partial.frames[0],
            can_fields=partial.can_fields,
            frames=tuple(partial.frames),
        )
        events.append(
            ReassemblyEvent(
                kind="complete",
                time=now,
                frame=frame,
                packet=packet,
                sender=can_fields.sender,
                destination=can_fields.destination,
            )
        )
        return tuple(events)

    def expire(self, now: int) -> tuple[ReassemblyEvent, ...]:
        """Drop every partial packet idle for more than the reassembly timeout."""

        events: list[ReassemblyEvent] = []
        for key in list(self._partials):
            partial = self._partials[key]
            if now - partial.last_used > self.timeout_us:
                del self._partials[key]
                events.append(
                    self._dropped(
                        ReassemblyDropReason.TIMEOUT,
                        partial.last_used + self.timeout_us,
                        None,
                        f"no frame for {self.timeout_us} us after {len(partial.frames)} frame(s)",
                        partial.can_fields,
                    )
                )
        return tuple(events)

    @staticmethod
    def _dropped(
        reason: ReassemblyDropReason,
        time: int,
        frame: CspCanFrame | None,
        detail: str,
        can_fields: CspCanIdFields | None = None,
        *,
        limit: int | None = None,
    ) -> ReassemblyEvent:
        return ReassemblyEvent(
            kind="dropped",
            time=time,
            frame=frame,
            reason=reason,
            detail=detail,
            sender=None if can_fields is None else can_fields.sender,
            destination=None if can_fields is None else can_fields.destination,
            limit=limit,
        )


def _validate_max_packet_bytes(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CspV2RangeError(f"max_packet_bytes must be a positive integer, got {value!r}")
    return value


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


def _validate_csp_fields(fields: CspFields) -> None:
    if fields.flags & CSP_FLAG_FRAGMENT:
        raise CspV2FrameError("CSP_FFRAG packets are not supported")


_validate_single_frame_csp_fields = _validate_csp_fields


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
    "CSP_V2_CFP_CONTINUATION_PAYLOAD_BYTES",
    "CSP_V2_DEFAULT_MAX_PACKET_BYTES",
    "CSP_V2_FLAGS_MAX",
    "CSP_V2_PORT_MAX",
    "CSP_V2_PRIORITY_MAX",
    "CSP_V2_REASSEMBLY_TIMEOUT_US",
    "CSP_V2_SINGLE_FRAME_MAX_PAYLOAD_BYTES",
    "EXTENDED_CAN_ID_MAX",
    "STANDARD_CAN_ID_MAX",
    "CspCanFrame",
    "CspCanIdFields",
    "CspFields",
    "CspFragmenter",
    "CspPriority",
    "CspReassembler",
    "CspV2CodecError",
    "CspV2FrameError",
    "CspV2RangeError",
    "DecodedCspPacket",
    "ReassemblyDropReason",
    "ReassemblyEvent",
    "decode_can_id",
    "decode_first_frame_header",
    "decode_frame",
    "fragment_packet",
    "pack",
    "unpack",
]
