"""libcsp-compatible CSP v2 over ZeroMQ, pinned to libcsp ``v2.1``.

A message, as captured in ``tests/golden_vectors/zmq/``, is the 6-byte CSP v2 header
followed by the payload and nothing else: no framing or escaping, no CRC32 (so unlike
KISS the interface adds no trailer, and a packet with ``CSP_FLAG_CRC32`` carries only
that flag's own trailer), no topic or address prefix (that byte exists for CSP v1
only), no fragmentation. libcsp's ZeroMQ interface subscribes to everything, so there
is no filter to model.

Like :mod:`~cubesat_testbed.protocol.csp_v2_kiss`, this is a code path of its own
and shares nothing with the CAN codec beyond the header fields.
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
)
from cubesat_testbed.protocol.csp_v2_packet import (
    CSP_V2_HEADER_BYTES,
    CspPacketFrame,
    pack_header,
    unpack_header,
)

ZMQ_LINK = "zmq"


def encode_zmq_message(
    fields: CspFields,
    payload: BytesLike,
    *,
    max_packet_bytes: int = CSP_V2_DEFAULT_MAX_PACKET_BYTES,
) -> bytes:
    """Encode one CSP packet as the exact message libcsp ``v2.1`` would publish.

    ``payload`` is the CSP payload as libcsp holds it, including any
    ``CSP_FLAG_CRC32``/``CSP_FLAG_HMAC`` trailer; like the other codecs this does not
    compute one. ``max_packet_bytes`` is the sending node's packet buffer.
    """

    payload_bytes = bytes(payload)
    if len(payload_bytes) > max_packet_bytes:
        raise CspV2FrameError(
            f"CSP payload of {len(payload_bytes)} bytes exceeds the sender's "
            f"max_packet_bytes={max_packet_bytes}"
        )
    return pack_header(fields) + payload_bytes


class ZmqDropReason(str, Enum):
    """Why a received message was discarded."""

    TOO_SHORT = "too_short"
    """Shorter than a CSP header."""
    OVERFLOW = "overflow"
    """The payload is larger than the receiver's packet buffer."""
    MALFORMED = "malformed"
    """The header decoded to something outside the supported CSP v2 profile."""


@dataclass(frozen=True, slots=True)
class ZmqEvent:
    """The outcome of decoding one message: a packet, or why it was dropped."""

    packet: CspPacketFrame | None = None
    reason: ZmqDropReason | None = None
    detail: str = ""
    fields: CspFields | None = None
    limit: int | None = None


def decode_zmq_message(
    message: BytesLike,
    *,
    max_packet_bytes: int = CSP_V2_DEFAULT_MAX_PACKET_BYTES,
) -> ZmqEvent:
    """Decode one received message; never raises, a bad message is a dropped event."""

    data = bytes(message)
    if len(data) < CSP_V2_HEADER_BYTES:
        return ZmqEvent(
            reason=ZmqDropReason.TOO_SHORT,
            detail=f"{len(data)} bytes, a CSP v2 header alone is {CSP_V2_HEADER_BYTES}",
        )
    try:
        fields = unpack_header(data[:CSP_V2_HEADER_BYTES])
    except CspV2CodecError as exc:
        return ZmqEvent(reason=ZmqDropReason.MALFORMED, detail=str(exc))
    payload = data[CSP_V2_HEADER_BYTES:]
    if len(payload) > max_packet_bytes:
        return ZmqEvent(
            reason=ZmqDropReason.OVERFLOW,
            detail=f"payload of {len(payload)} bytes is over max_packet_bytes={max_packet_bytes}",
            fields=fields,
            limit=max_packet_bytes,
        )
    if fields.flags & 0x10:  # CSP_FLAG_FRAGMENT, unsupported as on the other links
        return ZmqEvent(
            reason=ZmqDropReason.MALFORMED,
            detail="CSP_FFRAG packets are not supported",
            fields=fields,
        )
    return ZmqEvent(packet=CspPacketFrame(fields=fields, payload=payload, wire=data, link=ZMQ_LINK))


__all__ = [
    "ZMQ_LINK",
    "ZmqDropReason",
    "ZmqEvent",
    "decode_zmq_message",
    "encode_zmq_message",
]
