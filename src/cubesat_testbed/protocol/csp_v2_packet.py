"""CSP v2 packets that cross a link whole, with no CAN framing.

CAN carries a packet as one or several 8-byte frames (``csp_v2``). Serial KISS
and ZeroMQ carry it as one unit: the 6-byte CSP v2 header followed by the
payload. :class:`CspPacketFrame` is that unit as the transport boundary sees it,
and :func:`pack_header` / :func:`unpack_header` are the header layout those links
share. This module deliberately knows nothing about CAN identifiers, CFP2 or any
particular link's framing, so the CAN path and the whole-packet path stay apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from cubesat_testbed.protocol.csp_v2 import (
    CspFields,
    CspV2FrameError,
    CspV2RangeError,
)

CSP_V2_HEADER_BYTES = 6

_PRIO_OFFSET = 46
_DST_OFFSET = 32
_SRC_OFFSET = 18
_DPORT_OFFSET = 12
_SPORT_OFFSET = 6
_FLAGS_OFFSET = 0


class CspPacketView(Protocol):
    """What routing and tracing need from a received packet, on any link.

    Satisfied by :class:`~cubesat_testbed.protocol.csp_v2.DecodedCspPacket`
    (reassembled from CAN frames) and by :class:`CspPacketFrame` (carried whole).
    """

    @property
    def fields(self) -> CspFields: ...

    @property
    def payload(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class CspPacketFrame:
    """One whole CSP v2 packet, as a link without frames carries it.

    ``wire`` is the exact bytes the link put on the line for this packet (for
    KISS, the frame from ``FEND`` to ``FEND``), kept so a trace can show them. It
    is empty for a packet that never touched a line, such as one delivered between
    two simulated nodes.
    """

    fields: CspFields
    payload: bytes
    wire: bytes = b""

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "payload", bytes(self.payload))
            object.__setattr__(self, "wire", bytes(self.wire))
        except TypeError as exc:
            raise CspV2FrameError("payload and wire must be bytes-like") from exc


def pack_header(fields: CspFields) -> bytes:
    """Encode the 48-bit CSP v2 header, most significant byte first."""

    value = (
        (fields.priority << _PRIO_OFFSET)
        | (fields.destination << _DST_OFFSET)
        | (fields.source << _SRC_OFFSET)
        | (fields.destination_port << _DPORT_OFFSET)
        | (fields.source_port << _SPORT_OFFSET)
        | (fields.flags << _FLAGS_OFFSET)
    )
    return value.to_bytes(CSP_V2_HEADER_BYTES, "big")


def unpack_header(header: bytes) -> CspFields:
    """Decode a 48-bit CSP v2 header; raises for a header of the wrong length."""

    if len(header) != CSP_V2_HEADER_BYTES:
        raise CspV2RangeError(
            f"CSP v2 header must be {CSP_V2_HEADER_BYTES} bytes, got {len(header)}"
        )
    value = int.from_bytes(header, "big")
    return CspFields(
        priority=(value >> _PRIO_OFFSET) & 0x3,
        destination=(value >> _DST_OFFSET) & 0x3FFF,
        source=(value >> _SRC_OFFSET) & 0x3FFF,
        destination_port=(value >> _DPORT_OFFSET) & 0x3F,
        source_port=(value >> _SPORT_OFFSET) & 0x3F,
        flags=(value >> _FLAGS_OFFSET) & 0x3F,
    )


__all__ = [
    "CSP_V2_HEADER_BYTES",
    "CspPacketFrame",
    "CspPacketView",
    "pack_header",
    "unpack_header",
]
