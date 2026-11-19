"""libcsp-compatible CSP v1 codec over classic CAN (CFP 1.x).

A separate code path from :mod:`cubesat_testbed.protocol.csp_v2` on purpose: the two
protocol versions are not meant to share a network, so they share no codec code either. Nothing
here imports the v2 module, and a v1 frame is never decoded by it or the other way round.

Target profile:

- classic CAN 2.0, extended 29-bit identifiers;
- CSP v1: a 4-byte big-endian header (priority 2 bits, source 5, destination 5, destination
  port 6, source port 6, flags 8), addresses 0-31, ports 0-63;
- every packet is sent as one or more CFP1 frames (:func:`fragment_packet` /
  :class:`CspV1Fragmenter` to send, :class:`CspV1Reassembler` to receive);
- golden-vector validation against the captures under ``tests/golden_vectors/csp_v1/can/``,
  taken from the same official libcsp v2.1 build (commit 48f7fb0) run as CSP v1.

The CAN identifier carries, most significant bit first, the *source* (5 bits), the
*destination* (5), the frame *type* (1: 0 for the first frame of a packet, 1 for the rest),
the number of frames still to come (*remaining*, 8) and an *identification* (10) that is
the same in every frame of a packet and counts up from one packet to the next. The first
frame's data holds the CSP header, a 2-byte big-endian payload length and up to two payload
bytes; every later frame holds up to eight payload bytes. Even an empty packet is a frame, and
a packet fits one frame only up to two payload bytes. The CRC32/HMAC trailer libcsp appends is
part of the payload and of the length.

Reassembly follows ``csp_can1_rx`` in libcsp ``v2.1``: fragments are matched to their packet
by source, destination and identification; the remaining-frames counter must count down by one
per frame; a packet is complete when as many payload bytes have arrived as the length field
declared; an incomplete packet is dropped after one second of silence.

Not modelled here: the loopback libcsp does for a packet addressed to its own interface (it
never reaches the bus) and the rewrite of the broadcast address (31) to the receiving node's
own address, which depend on the node that receives and are the business of the layer above.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum

CLASSIC_CAN_MAX_DATA_BYTES = 8
STANDARD_CAN_ID_MAX = 0x7FF
EXTENDED_CAN_ID_MAX = 0x1FFF_FFFF

CSP_V1_PRIORITY_MAX = 0x3
CSP_V1_ADDRESS_MAX = 0x1F
CSP_V1_BROADCAST_ADDRESS = 0x1F
"""The highest address is the broadcast address in libcsp, so the highest usable node is 30."""
CSP_V1_PORT_MAX = 0x3F
CSP_V1_FLAGS_MAX = 0xFF

CSP_V1_HEADER_BYTES = 4
CSP_V1_LENGTH_BYTES = 2
CSP_V1_FIRST_FRAME_PAYLOAD_BYTES = (
    CLASSIC_CAN_MAX_DATA_BYTES - CSP_V1_HEADER_BYTES - CSP_V1_LENGTH_BYTES
)
CSP_V1_CONTINUATION_PAYLOAD_BYTES = CLASSIC_CAN_MAX_DATA_BYTES
CSP_V1_SINGLE_FRAME_MAX_PAYLOAD_BYTES = CSP_V1_FIRST_FRAME_PAYLOAD_BYTES

CSP_V1_DEFAULT_MAX_PACKET_BYTES = 256
"""libcsp ``CSP_BUFFER_SIZE`` default: the most payload bytes (trailer included) one packet
buffer holds, hence the most a node can send or reassemble."""
CSP_V1_REASSEMBLY_TIMEOUT_US = 1_000_000
"""libcsp ``PBUF_TIMEOUT_MS`` (1000 ms) in virtual microseconds."""

CFP1_REMAINING_MAX = 0xFF
CFP1_IDENTIFICATION_MAX = 0x3FF
CSP_V1_MAX_PAYLOAD_BYTES = (
    CSP_V1_FIRST_FRAME_PAYLOAD_BYTES + CFP1_REMAINING_MAX * CSP_V1_CONTINUATION_PAYLOAD_BYTES
)
"""The most payload the remaining-frames field can describe (2 + 255 * 8)."""

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

_CFP1_SRC_OFFSET = 24
_CFP1_DST_OFFSET = 19
_CFP1_TYPE_OFFSET = 18
_CFP1_REMAIN_OFFSET = 10
_CFP1_ADDRESS_MASK = 0x1F
_CFP1_TYPE_MASK = 0x1
_CFP1_REMAIN_MASK = 0xFF
_CFP1_ID_MASK = 0x3FF

_CSP1_PRIO_OFFSET = 30
_CSP1_SRC_OFFSET = 25
_CSP1_DST_OFFSET = 20
_CSP1_DPORT_OFFSET = 14
_CSP1_SPORT_OFFSET = 8
_CSP1_FLAGS_OFFSET = 0
_CSP1_PRIO_MASK = 0x3
_CSP1_ADDRESS_MASK = 0x1F
_CSP1_PORT_MASK = 0x3F
_CSP1_FLAGS_MASK = 0xFF

BytesLike = bytes | bytearray | memoryview


class CspV1CodecError(ValueError):
    """Base error raised when a CSP v1 frame cannot be packed or unpacked."""


class CspV1RangeError(CspV1CodecError):
    """Raised when a logical CSP or CAN field is outside its protocol range."""


class CspV1FrameError(CspV1CodecError):
    """Raised when a CAN frame is outside the supported CSP v1 profile."""


class CspPriority(IntEnum):
    """libcsp CSP priority values."""

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


@dataclass(frozen=True, slots=True)
class CspV1Fields:
    """Logical CSP v1 packet fields exposed to higher layers."""

    priority: int
    source: int
    destination: int
    destination_port: int
    source_port: int
    flags: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "priority", _validate_uint("priority", self.priority, CSP_V1_PRIORITY_MAX)
        )
        object.__setattr__(
            self, "source", _validate_uint("source", self.source, CSP_V1_ADDRESS_MAX)
        )
        object.__setattr__(
            self,
            "destination",
            _validate_uint("destination", self.destination, CSP_V1_ADDRESS_MAX),
        )
        object.__setattr__(
            self,
            "destination_port",
            _validate_uint("destination_port", self.destination_port, CSP_V1_PORT_MAX),
        )
        object.__setattr__(
            self,
            "source_port",
            _validate_uint("source_port", self.source_port, CSP_V1_PORT_MAX),
        )
        object.__setattr__(self, "flags", _validate_uint("flags", self.flags, CSP_V1_FLAGS_MAX))


@dataclass(frozen=True, slots=True)
class CspV1CanIdFields:
    """Fields carried by the CFP1 extended CAN identifier."""

    source: int
    destination: int
    begin: bool
    remaining: int
    identification: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source", _validate_uint("source", self.source, CSP_V1_ADDRESS_MAX)
        )
        object.__setattr__(
            self,
            "destination",
            _validate_uint("destination", self.destination, CSP_V1_ADDRESS_MAX),
        )
        _validate_bool("begin", self.begin)
        object.__setattr__(
            self, "remaining", _validate_uint("remaining", self.remaining, CFP1_REMAINING_MAX)
        )
        object.__setattr__(
            self,
            "identification",
            _validate_uint("identification", self.identification, CFP1_IDENTIFICATION_MAX),
        )


@dataclass(frozen=True, slots=True)
class CspV1CanFrame:
    """One classic CAN 2.0 frame carrying CSP v1-over-CAN bytes."""

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
            raise CspV1FrameError(
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
class DecodedCspV1Packet:
    """Decoded view of one CSP v1 packet and the CAN frames it arrived in."""

    fields: CspV1Fields
    payload: bytes
    frames: tuple[CspV1CanFrame, ...]
    """Every CAN frame the packet arrived in, in order."""
    can_fields: CspV1CanIdFields
    """The identifier fields of the first frame."""

    @property
    def frame(self) -> CspV1CanFrame:
        """The first frame."""

        return self.frames[0]

    @property
    def frame_count(self) -> int:
        """Number of CAN frames this packet was carried in."""

        return len(self.frames)


CSP_PRIO_CRITICAL = CspPriority.CRITICAL
CSP_PRIO_HIGH = CspPriority.HIGH
CSP_PRIO_NORM = CspPriority.NORMAL
CSP_PRIO_LOW = CspPriority.LOW


def pack_header(fields: CspV1Fields) -> bytes:
    """The 4-byte big-endian CSP v1 header for ``fields``."""

    header = (
        ((fields.priority & _CSP1_PRIO_MASK) << _CSP1_PRIO_OFFSET)
        | ((fields.source & _CSP1_ADDRESS_MASK) << _CSP1_SRC_OFFSET)
        | ((fields.destination & _CSP1_ADDRESS_MASK) << _CSP1_DST_OFFSET)
        | ((fields.destination_port & _CSP1_PORT_MASK) << _CSP1_DPORT_OFFSET)
        | ((fields.source_port & _CSP1_PORT_MASK) << _CSP1_SPORT_OFFSET)
        | ((fields.flags & _CSP1_FLAGS_MASK) << _CSP1_FLAGS_OFFSET)
    )
    return header.to_bytes(CSP_V1_HEADER_BYTES, "big")


def unpack_header(data: BytesLike) -> CspV1Fields:
    """Read the CSP v1 fields out of the first four bytes of ``data``."""

    raw = _coerce_bytes("header", data)
    if len(raw) < CSP_V1_HEADER_BYTES:
        raise CspV1FrameError(f"a CSP v1 header is {CSP_V1_HEADER_BYTES} bytes, got {len(raw)}")
    header = int.from_bytes(raw[:CSP_V1_HEADER_BYTES], "big")
    return CspV1Fields(
        priority=(header >> _CSP1_PRIO_OFFSET) & _CSP1_PRIO_MASK,
        source=(header >> _CSP1_SRC_OFFSET) & _CSP1_ADDRESS_MASK,
        destination=(header >> _CSP1_DST_OFFSET) & _CSP1_ADDRESS_MASK,
        destination_port=(header >> _CSP1_DPORT_OFFSET) & _CSP1_PORT_MASK,
        source_port=(header >> _CSP1_SPORT_OFFSET) & _CSP1_PORT_MASK,
        flags=(header >> _CSP1_FLAGS_OFFSET) & _CSP1_FLAGS_MASK,
    )


def decode_can_id(can_id: int) -> CspV1CanIdFields:
    """Split an extended CAN identifier into its CFP1 fields."""

    return _unpack_can_id(_validate_uint("can_id", can_id, EXTENDED_CAN_ID_MAX))


def decode_first_frame_header(frame: CspV1CanFrame) -> CspV1Fields:
    """Read the CSP header from a first frame, whether or not the packet continues.

    The whole header is in the first frame, so a bus monitor can name a multi-frame packet's
    route before its payload has arrived.
    """

    can_fields = _unpack_frame_id(frame)
    if not can_fields.begin:
        raise CspV1FrameError("only a first (begin) frame carries the CSP header")
    if frame.dlc < CSP_V1_HEADER_BYTES + CSP_V1_LENGTH_BYTES:
        raise CspV1FrameError(
            "a CSP v1 first frame must hold the 4-byte header and the 2-byte length, "
            f"got {frame.dlc} data bytes"
        )
    return unpack_header(frame.data)


def decode_frame(frame: CspV1CanFrame) -> DecodedCspV1Packet:
    """Decode a packet that fits one frame (at most two payload bytes).

    A first frame that announces more frames, and a continuation frame, are rejected rather
    than partially decoded: feed them to a :class:`CspV1Reassembler`.
    """

    can_fields = _unpack_frame_id(frame)
    if not can_fields.begin:
        raise CspV1FrameError("cannot decode a CSP v1 continuation frame on its own")
    if can_fields.remaining != 0:
        raise CspV1FrameError(
            f"the packet continues in {can_fields.remaining} more frame(s); use a reassembler"
        )
    fields = decode_first_frame_header(frame)
    _validate_csp_fields(fields)
    length = _declared_length(frame)
    payload = frame.data[CSP_V1_HEADER_BYTES + CSP_V1_LENGTH_BYTES :]
    if len(payload) != length:
        raise CspV1FrameError(
            f"the length field says {length} payload bytes, the frame carries {len(payload)}"
        )
    return DecodedCspV1Packet(
        fields=fields, payload=payload, frames=(frame,), can_fields=can_fields
    )


def fragment_packet(
    fields: CspV1Fields,
    payload: BytesLike,
    *,
    identification: int = 0,
    via: int | None = None,
) -> tuple[CspV1CanFrame, ...]:
    """Split one CSP packet into the CAN frames libcsp would send for it.

    The first frame carries the 4-byte header, the 2-byte payload length and up to two payload
    bytes; every later frame up to eight, with the remaining-frames field counting down to 0
    on the last frame. ``payload`` is the full CSP payload as libcsp holds it, trailer
    included: this function does not compute a CRC32 or HMAC.

    The CAN identifier's source is the packet's source and its destination the next hop,
    ``via`` if given (a routed packet) and the packet's destination otherwise, as in libcsp.
    ``identification`` is the 10-bit packet identification; use :class:`CspV1Fragmenter` to have
    it advance per sender.
    """

    _validate_csp_fields(fields)
    payload_bytes = _coerce_bytes("payload", payload)
    if len(payload_bytes) > CSP_V1_MAX_PAYLOAD_BYTES:
        raise CspV1FrameError(
            f"a CSP v1 payload is at most {CSP_V1_MAX_PAYLOAD_BYTES} bytes (the remaining-frames "
            f"field is {CFP1_REMAINING_MAX} at most), got {len(payload_bytes)}"
        )
    identification = _validate_uint("identification", identification, CFP1_IDENTIFICATION_MAX)
    destination = (
        fields.destination if via is None else _validate_uint("via", via, CSP_V1_ADDRESS_MAX)
    )

    first_chunk = payload_bytes[:CSP_V1_FIRST_FRAME_PAYLOAD_BYTES]
    rest = payload_bytes[len(first_chunk) :]
    chunks = [
        pack_header(fields) + len(payload_bytes).to_bytes(CSP_V1_LENGTH_BYTES, "big") + first_chunk
    ]
    chunks.extend(
        rest[offset : offset + CSP_V1_CONTINUATION_PAYLOAD_BYTES]
        for offset in range(0, len(rest), CSP_V1_CONTINUATION_PAYLOAD_BYTES)
    )

    return tuple(
        CspV1CanFrame(
            can_id=_pack_can_id(
                CspV1CanIdFields(
                    source=fields.source,
                    destination=destination,
                    begin=index == 0,
                    remaining=len(chunks) - 1 - index,
                    identification=identification,
                )
            ),
            data=chunk,
            is_extended_id=True,
        )
        for index, chunk in enumerate(chunks)
    )


class CspV1Fragmenter:
    """Per-node packet sender state: the CFP identification counter and the size limit.

    libcsp keeps one counter per CAN interface and advances it for every packet it puts on the
    bus, so two packets from one node differ in the identification field. It is 10 bits wide
    and wraps. This keeps one counter per source address (one node, one interface).

    ``max_packet_bytes`` is the node's packet buffer size: a payload that does not fit it could
    not even be built by that node's libcsp, so it is refused here rather than put on the bus.
    """

    def __init__(self, *, max_packet_bytes: int = CSP_V1_DEFAULT_MAX_PACKET_BYTES) -> None:
        self.max_packet_bytes = _validate_max_packet_bytes(max_packet_bytes)
        self._identifications: dict[int, int] = {}

    def fragment(
        self,
        fields: CspV1Fields,
        payload: BytesLike,
        *,
        via: int | None = None,
    ) -> tuple[CspV1CanFrame, ...]:
        """Fragment ``payload`` and advance the sender's identification counter."""

        payload_bytes = _coerce_bytes("payload", payload)
        if len(payload_bytes) > self.max_packet_bytes:
            raise CspV1FrameError(
                f"CSP payload of {len(payload_bytes)} bytes exceeds the sender's "
                f"max_packet_bytes={self.max_packet_bytes}"
            )
        identification = self._identifications.get(fields.source, 0)
        frames = fragment_packet(fields, payload_bytes, identification=identification, via=via)
        self._identifications[fields.source] = (identification + 1) & CFP1_IDENTIFICATION_MAX
        return frames


class ReassemblyDropReason(str, Enum):
    """Why a packet, or a frame, was discarded during reassembly."""

    ORPHAN_FRAGMENT = "orphan_fragment"
    """A continuation frame arrived with no packet in progress for it."""
    FRAME_LOST = "frame_lost"
    """The remaining-frames field was not the expected value: a frame went missing."""
    OVERFLOW = "overflow"
    """The packet announced more payload than the receiving node's ``max_packet_bytes``."""
    TIMEOUT = "timeout"
    """No frame for the packet arrived within the reassembly timeout."""
    RESTARTED = "restarted"
    """A new first frame arrived for a packet still in progress."""
    MALFORMED = "malformed"
    """The frame or the reassembled packet is not valid CSP v1."""


@dataclass(frozen=True, slots=True)
class ReassemblyEvent:
    """Outcome of feeding one frame, or of an expiry, to a :class:`CspV1Reassembler`.

    ``kind`` is ``"complete"`` (``packet`` is set), ``"pending"`` (frame stored, packet not
    finished) or ``"dropped"`` (``reason`` and ``detail`` are set; ``frame`` is ``None`` for a
    timeout, which no single frame causes). ``time`` is the virtual time the event happened: for
    a timeout, the instant the reassembly window closed rather than the instant it was noticed.
    """

    kind: str
    time: int
    frame: CspV1CanFrame | None = None
    packet: DecodedCspV1Packet | None = None
    reason: ReassemblyDropReason | None = None
    detail: str = ""
    sender: int | None = None
    destination: int | None = None
    limit: int | None = None


@dataclass(slots=True)
class _PartialPacket:
    can_fields: CspV1CanIdFields
    fields: CspV1Fields
    length: int
    frames: list[CspV1CanFrame]
    payload: bytearray = field(default_factory=bytearray)
    remain: int = 0
    last_used: int = 0


_ConnectionKey = tuple[int, int, int]


class CspV1Reassembler:
    """Rebuild CSP v1 packets from CFP1 CAN frames in virtual time.

    One instance models what one receiving node's libcsp CAN interface does. It is a pure
    state machine: every method takes the current virtual time in microseconds, nothing reads a
    clock, so the same frames at the same times always give the same events.

    ``max_packet_bytes`` is that node's packet buffer size (libcsp ``CSP_BUFFER_SIZE``): a
    packet announcing more payload than that, trailer included, is dropped when its first frame
    arrives, and ``None`` removes the limit (for a passive observer). Like libcsp, stale partial
    packets are discarded when a packet from a new connection starts (``feed``); call
    :meth:`expire` to flush them without waiting for that, for example when a run ends.
    """

    def __init__(
        self,
        *,
        max_packet_bytes: int | None = CSP_V1_DEFAULT_MAX_PACKET_BYTES,
        timeout_us: int = CSP_V1_REASSEMBLY_TIMEOUT_US,
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

    def feed(self, frame: CspV1CanFrame, *, now: int) -> tuple[ReassemblyEvent, ...]:
        """Process one received frame; return what happened, in order."""

        if not frame.is_extended_id:
            return (
                self._dropped(
                    ReassemblyDropReason.MALFORMED,
                    now,
                    frame,
                    "CSP v1 over CAN requires an extended 29-bit CAN identifier",
                ),
            )

        can_fields = _unpack_can_id(frame.can_id)
        key: _ConnectionKey = (
            can_fields.source,
            can_fields.destination,
            can_fields.identification,
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
                        f"continuation frame (remaining={can_fields.remaining}) with no packet "
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
                del self._partials[key]
            if frame.dlc < CSP_V1_HEADER_BYTES + CSP_V1_LENGTH_BYTES:
                events.append(
                    self._dropped(
                        ReassemblyDropReason.MALFORMED,
                        now,
                        frame,
                        "a first frame must hold the 4-byte header and the 2-byte length, "
                        f"got {frame.dlc} data bytes",
                        can_fields,
                    )
                )
                return tuple(events)
            fields = unpack_header(frame.data)
            length = _declared_length(frame)
            if self.max_packet_bytes is not None and length > self.max_packet_bytes:
                events.append(
                    self._dropped(
                        ReassemblyDropReason.OVERFLOW,
                        now,
                        frame,
                        f"the packet announces {length} payload bytes, over "
                        f"max_packet_bytes={self.max_packet_bytes}",
                        can_fields,
                        limit=self.max_packet_bytes,
                    )
                )
                return tuple(events)
            partial = _PartialPacket(
                can_fields=can_fields,
                fields=fields,
                length=length,
                frames=[],
                remain=can_fields.remaining + 1,
                last_used=now,
            )
            self._partials[key] = partial
            chunk = frame.data[CSP_V1_HEADER_BYTES + CSP_V1_LENGTH_BYTES :]
        else:
            assert partial is not None
            chunk = frame.data

        # libcsp: the remaining-frames field must be one less than the frames still expected.
        # With none expected (remain 0) nothing can match, so a surplus frame counts as lost.
        if can_fields.remaining != partial.remain - 1:
            del self._partials[key]
            events.append(
                self._dropped(
                    ReassemblyDropReason.FRAME_LOST,
                    now,
                    frame,
                    f"expected remaining={partial.remain - 1}, got {can_fields.remaining} "
                    f"after {len(partial.frames)} frame(s)",
                    can_fields,
                )
            )
            return tuple(events)
        partial.remain -= 1

        if len(partial.payload) + len(chunk) > partial.length:
            del self._partials[key]
            events.append(
                self._dropped(
                    ReassemblyDropReason.MALFORMED,
                    now,
                    frame,
                    f"the frame would bring the payload to {len(partial.payload) + len(chunk)} "
                    f"bytes, over the {partial.length} the length field declared",
                    can_fields,
                )
            )
            return tuple(events)

        partial.frames.append(frame)
        partial.payload.extend(chunk)

        if len(partial.payload) != partial.length:
            events.append(
                ReassemblyEvent(
                    kind="pending",
                    time=now,
                    frame=frame,
                    sender=can_fields.source,
                    destination=can_fields.destination,
                )
            )
            return tuple(events)

        del self._partials[key]
        try:
            _validate_csp_fields(partial.fields)
        except CspV1CodecError as exc:
            events.append(
                self._dropped(ReassemblyDropReason.MALFORMED, now, frame, str(exc), can_fields)
            )
            return tuple(events)

        packet = DecodedCspV1Packet(
            fields=partial.fields,
            payload=bytes(partial.payload),
            frames=tuple(partial.frames),
            can_fields=partial.can_fields,
        )
        events.append(
            ReassemblyEvent(
                kind="complete",
                time=now,
                frame=frame,
                packet=packet,
                sender=can_fields.source,
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
        frame: CspV1CanFrame | None,
        detail: str,
        can_fields: CspV1CanIdFields | None = None,
        *,
        limit: int | None = None,
    ) -> ReassemblyEvent:
        return ReassemblyEvent(
            kind="dropped",
            time=time,
            frame=frame,
            reason=reason,
            detail=detail,
            sender=None if can_fields is None else can_fields.source,
            destination=None if can_fields is None else can_fields.destination,
            limit=limit,
        )


def _validate_max_packet_bytes(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CspV1RangeError(f"max_packet_bytes must be a positive integer, got {value!r}")
    return value


def _pack_can_id(fields: CspV1CanIdFields) -> int:
    return (
        ((fields.source & _CFP1_ADDRESS_MASK) << _CFP1_SRC_OFFSET)
        | ((fields.destination & _CFP1_ADDRESS_MASK) << _CFP1_DST_OFFSET)
        | ((0 if fields.begin else 1) << _CFP1_TYPE_OFFSET)
        | ((fields.remaining & _CFP1_REMAIN_MASK) << _CFP1_REMAIN_OFFSET)
        | (fields.identification & _CFP1_ID_MASK)
    )


def _unpack_can_id(can_id: int) -> CspV1CanIdFields:
    return CspV1CanIdFields(
        source=(can_id >> _CFP1_SRC_OFFSET) & _CFP1_ADDRESS_MASK,
        destination=(can_id >> _CFP1_DST_OFFSET) & _CFP1_ADDRESS_MASK,
        begin=((can_id >> _CFP1_TYPE_OFFSET) & _CFP1_TYPE_MASK) == 0,
        remaining=(can_id >> _CFP1_REMAIN_OFFSET) & _CFP1_REMAIN_MASK,
        identification=can_id & _CFP1_ID_MASK,
    )


def _unpack_frame_id(frame: CspV1CanFrame) -> CspV1CanIdFields:
    if not frame.is_extended_id:
        raise CspV1FrameError("CSP v1 over CAN requires an extended 29-bit CAN identifier")
    return _unpack_can_id(frame.can_id)


def _declared_length(frame: CspV1CanFrame) -> int:
    start = CSP_V1_HEADER_BYTES
    return int.from_bytes(frame.data[start : start + CSP_V1_LENGTH_BYTES], "big")


def _validate_csp_fields(fields: CspV1Fields) -> None:
    if fields.flags & CSP_FLAG_FRAGMENT:
        raise CspV1FrameError("CSP_FFRAG packets are not supported")


def _coerce_bytes(name: str, value: BytesLike) -> bytes:
    try:
        return bytes(value)
    except TypeError as exc:
        raise CspV1FrameError(f"{name} must be bytes-like") from exc


def _validate_uint(name: str, value: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CspV1RangeError(f"{name} must be an integer")
    if not 0 <= value <= maximum:
        raise CspV1RangeError(f"{name} must be in range 0..{maximum}, got {value}")
    return int(value)


def _validate_bool(name: str, value: bool) -> bool:
    if not isinstance(value, bool):
        raise CspV1RangeError(f"{name} must be a bool")
    return value


__all__ = [
    "CFP1_IDENTIFICATION_MAX",
    "CFP1_REMAINING_MAX",
    "CLASSIC_CAN_MAX_DATA_BYTES",
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
    "CSP_V1_ADDRESS_MAX",
    "CSP_V1_BROADCAST_ADDRESS",
    "CSP_V1_CONTINUATION_PAYLOAD_BYTES",
    "CSP_V1_DEFAULT_MAX_PACKET_BYTES",
    "CSP_V1_FIRST_FRAME_PAYLOAD_BYTES",
    "CSP_V1_FLAGS_MAX",
    "CSP_V1_HEADER_BYTES",
    "CSP_V1_LENGTH_BYTES",
    "CSP_V1_MAX_PAYLOAD_BYTES",
    "CSP_V1_PORT_MAX",
    "CSP_V1_PRIORITY_MAX",
    "CSP_V1_REASSEMBLY_TIMEOUT_US",
    "CSP_V1_SINGLE_FRAME_MAX_PAYLOAD_BYTES",
    "EXTENDED_CAN_ID_MAX",
    "STANDARD_CAN_ID_MAX",
    "BytesLike",
    "CspPriority",
    "CspV1CanFrame",
    "CspV1CanIdFields",
    "CspV1CodecError",
    "CspV1Fields",
    "CspV1Fragmenter",
    "CspV1FrameError",
    "CspV1RangeError",
    "CspV1Reassembler",
    "DecodedCspV1Packet",
    "ReassemblyDropReason",
    "ReassemblyEvent",
    "decode_can_id",
    "decode_first_frame_header",
    "decode_frame",
    "fragment_packet",
    "pack_header",
    "unpack_header",
]
