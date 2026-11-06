"""libcsp-compatible CSP v2 over KISS (serial), pinned to libcsp ``v2.1``.

Layout, as captured in ``tests/golden_vectors/kiss/``::

    FEND 0x00 <escaped: header(6) payload crc32(4)> FEND

- ``FEND`` is ``0xC0``; inside a frame ``0xC0`` is sent as ``FESC TFEND`` (``DB DC``)
  and ``0xDB`` as ``FESC TFESC`` (``DB DD``). The byte after the opening ``FEND`` is
  the TNC command byte, ``0x00`` for data.
- The header is the 48-bit CSP v2 header, network byte order, full 14-bit addresses.
- The CRC32 is **always** present, whether or not ``CSP_FLAG_CRC32`` is set (a packet
  carrying that flag has its own trailer in the payload and gets a second CRC32).
  It is CRC-32C (Castagnoli, polynomial ``0x82F63B78``, initial value and final XOR
  ``0xFFFFFFFF``), sent big-endian. libcsp ``v2.1`` computes it over the payload only
  when sending, but accepts it over header plus payload as well when receiving, and
  so does :class:`KissDecoder`.
- There is no fragmentation: one frame carries one whole packet.

The decoder follows ``csp_kiss_rx`` byte for byte, including the places where it is
lenient (the TNC byte is skipped without being checked, an unknown escape sequence
is dropped) so it accepts what a real libcsp peer accepts and refuses what it
refuses. This module is a separate code path from the CAN codec on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cubesat_testbed.protocol.csp_v2 import (
    CSP_V2_DEFAULT_MAX_PACKET_BYTES,
    BytesLike,
    CspFields,
    CspV2CodecError,
    CspV2FrameError,
    CspV2RangeError,
)
from cubesat_testbed.protocol.csp_v2_packet import (
    CSP_V2_HEADER_BYTES,
    CspPacketFrame,
    pack_header,
    unpack_header,
)

KISS_FEND = 0xC0
KISS_FESC = 0xDB
KISS_TFEND = 0xDC
KISS_TFESC = 0xDD
KISS_TNC_DATA = 0x00
KISS_CRC32_BYTES = 4


def _crc32c_table() -> tuple[int, ...]:
    table = []
    for index in range(256):
        value = index
        for _ in range(8):
            value = (value >> 1) ^ 0x82F63B78 if value & 1 else value >> 1
        table.append(value)
    return tuple(table)


_CRC32C_TABLE = _crc32c_table()


def crc32c(data: BytesLike) -> int:
    """CRC-32C (Castagnoli) as libcsp's ``csp_crc32_memory`` computes it."""

    value = 0xFFFFFFFF
    for byte in bytes(data):
        value = _CRC32C_TABLE[(value ^ byte) & 0xFF] ^ (value >> 8)
    return value ^ 0xFFFFFFFF


def encode_kiss_frame(
    fields: CspFields,
    payload: BytesLike,
    *,
    max_packet_bytes: int = CSP_V2_DEFAULT_MAX_PACKET_BYTES,
) -> bytes:
    """Encode one CSP packet as the exact KISS frame libcsp ``v2.1`` would send.

    ``payload`` is the CSP payload as libcsp holds it, including any
    ``CSP_FLAG_CRC32``/``CSP_FLAG_HMAC`` trailer: like the CAN codec, this does not
    compute one. The KISS CRC32 is added here. ``max_packet_bytes`` is the sending
    node's packet buffer: libcsp refuses to send when payload plus the 4-byte CRC32
    would not fit it.
    """

    payload_bytes = bytes(payload)
    if len(payload_bytes) + KISS_CRC32_BYTES > max_packet_bytes:
        raise CspV2FrameError(
            f"KISS frame needs {len(payload_bytes)} payload bytes plus a "
            f"{KISS_CRC32_BYTES}-byte CRC32, over the sender's max_packet_bytes={max_packet_bytes}"
        )
    packet = (
        pack_header(fields)
        + payload_bytes
        + crc32c(payload_bytes).to_bytes(KISS_CRC32_BYTES, "big")
    )
    out = bytearray((KISS_FEND, KISS_TNC_DATA))
    for byte in packet:
        if byte == KISS_FEND:
            out += bytes((KISS_FESC, KISS_TFEND))
        elif byte == KISS_FESC:
            out += bytes((KISS_FESC, KISS_TFESC))
        else:
            out.append(byte)
    out.append(KISS_FEND)
    return bytes(out)


class KissDropReason(str, Enum):
    """Why a received KISS frame was discarded."""

    OVERFLOW = "overflow"
    """The frame outgrew the receiver's packet buffer."""
    TOO_SHORT = "too_short"
    """Shorter than a CSP header plus a CRC32."""
    CRC_ERROR = "crc_error"
    """The CRC32 matched neither the payload nor header plus payload."""
    MALFORMED = "malformed"
    """The header decoded to something outside the supported CSP v2 profile."""


@dataclass(frozen=True, slots=True)
class KissEvent:
    """One decoded packet, or one discarded frame, from :meth:`KissDecoder.feed`."""

    packet: CspPacketFrame | None = None
    reason: KissDropReason | None = None
    detail: str = ""
    wire: bytes = b""
    fields: CspFields | None = None
    """The header, when the dropped frame was long enough to have one."""
    limit: int | None = None

    @property
    def dropped(self) -> bool:
        return self.reason is not None


_NOT_STARTED, _STARTED, _ESCAPED = range(3)


class KissDecoder:
    """Incremental KISS receiver mirroring libcsp ``csp_kiss_rx``.

    Feed it whatever bytes arrive from the line, in any chunking; it returns the
    packets and dropped frames completed by them. ``max_packet_bytes`` is the
    receiver's packet buffer size: libcsp keeps the header in front of a
    ``max_packet_bytes`` data area, so a frame is dropped once header plus data would
    reach ``max_packet_bytes + 6`` bytes, which is one byte less than the buffer
    holds -- a quirk kept on purpose, since a real peer behaves the same way.
    After an overflow the rest of the frame is skipped.
    """

    def __init__(self, *, max_packet_bytes: int = CSP_V2_DEFAULT_MAX_PACKET_BYTES) -> None:
        if isinstance(max_packet_bytes, bool) or max_packet_bytes < 1:
            raise CspV2RangeError(
                f"max_packet_bytes must be a positive integer, got {max_packet_bytes!r}"
            )
        self.max_packet_bytes = max_packet_bytes
        self._mode = _NOT_STARTED
        self._first = False
        self._buffer = bytearray()
        self._wire = bytearray()

    @property
    def in_frame(self) -> bool:
        """Whether a frame is partly received."""

        return self._mode != _NOT_STARTED

    def feed(self, data: BytesLike) -> tuple[KissEvent, ...]:
        events: list[KissEvent] = []
        limit = self.max_packet_bytes + CSP_V2_HEADER_BYTES
        for byte in bytes(data):
            if self._mode != _NOT_STARTED and len(self._buffer) >= limit:
                events.append(
                    KissEvent(
                        reason=KissDropReason.OVERFLOW,
                        detail=f"frame reached {len(self._buffer)} bytes, over "
                        f"max_packet_bytes={self.max_packet_bytes} plus the {CSP_V2_HEADER_BYTES}"
                        "-byte header",
                        wire=bytes(self._wire),
                        limit=self.max_packet_bytes,
                    )
                )
                self._reset()

            if self._mode == _NOT_STARTED:
                if byte == KISS_FEND:
                    self._mode = _STARTED
                    self._first = True
                    self._buffer = bytearray()
                    self._wire = bytearray((KISS_FEND,))
                continue

            self._wire.append(byte)
            if self._mode == _STARTED:
                if byte == KISS_FESC:
                    self._mode = _ESCAPED
                elif byte == KISS_FEND:
                    if self._buffer:
                        events.append(self._complete())
                        self._reset()
                elif self._first:
                    self._first = False  # the TNC command byte, skipped unchecked
                else:
                    self._buffer.append(byte)
            else:  # _ESCAPED
                if byte == KISS_TFESC:
                    self._buffer.append(KISS_FESC)
                elif byte == KISS_TFEND:
                    self._buffer.append(KISS_FEND)
                self._mode = _STARTED
        return tuple(events)

    def _reset(self) -> None:
        self._mode = _NOT_STARTED
        self._buffer = bytearray()
        self._wire = bytearray()

    def _complete(self) -> KissEvent:
        packet = bytes(self._buffer)
        wire = bytes(self._wire)
        if len(packet) < CSP_V2_HEADER_BYTES + KISS_CRC32_BYTES:
            return KissEvent(
                reason=KissDropReason.TOO_SHORT,
                detail=f"{len(packet)} bytes, need at least "
                f"{CSP_V2_HEADER_BYTES + KISS_CRC32_BYTES}",
                wire=wire,
            )
        header, body, trailer = (
            packet[:CSP_V2_HEADER_BYTES],
            packet[CSP_V2_HEADER_BYTES:-KISS_CRC32_BYTES],
            packet[-KISS_CRC32_BYTES:],
        )
        try:
            fields = unpack_header(header)
        except CspV2CodecError as exc:
            return KissEvent(reason=KissDropReason.MALFORMED, detail=str(exc), wire=wire)
        received = int.from_bytes(trailer, "big")
        if received not in (crc32c(header + body), crc32c(body)):
            return KissEvent(
                reason=KissDropReason.CRC_ERROR,
                detail=f"CRC32 0x{received:08X} matches neither the payload "
                f"(0x{crc32c(body):08X}) nor header plus payload (0x{crc32c(header + body):08X})",
                wire=wire,
                fields=fields,
            )
        if fields.flags & 0x10:  # CSP_FLAG_FRAGMENT, unsupported as on CAN
            return KissEvent(
                reason=KissDropReason.MALFORMED,
                detail="CSP_FFRAG packets are not supported",
                wire=wire,
                fields=fields,
            )
        return KissEvent(packet=CspPacketFrame(fields=fields, payload=body, wire=wire))


__all__ = [
    "KISS_CRC32_BYTES",
    "KISS_FEND",
    "KISS_FESC",
    "KISS_TFEND",
    "KISS_TFESC",
    "KISS_TNC_DATA",
    "KissDecoder",
    "KissDropReason",
    "KissEvent",
    "crc32c",
    "encode_kiss_frame",
]
