"""libcsp-compatible CSP v1 over ZeroMQ, pinned to libcsp ``v2.1`` run as CSP v1.

A message, as captured in ``tests/golden_vectors/csp_v1/zmq/``, is::

    <destination address (1)> <CSP v1 header (4)> <payload>

and nothing else: no framing or escaping and no checksum of the link's own (a packet with
``CSP_FLAG_CRC32`` carries only that flag's own trailer in the payload). The leading byte is
the destination address, which libcsp uses as the ZeroMQ topic and strips again on receipt
without comparing it with the header; the header is big-endian. libcsp's ZeroMQ interface
subscribes to everything, so there is no filter to model.

A separate code path from the CSP v2 message layout (:mod:`csp_v2_zmq`), which has no leading
byte and a 6-byte header.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cubesat_testbed.protocol.csp_v1 import (
    CSP_FLAG_FRAGMENT,
    CSP_V1_DEFAULT_MAX_PACKET_BYTES,
    CSP_V1_HEADER_BYTES,
    BytesLike,
    CspV1CodecError,
    CspV1Fields,
    CspV1FrameError,
    pack_header,
    unpack_header,
)
from cubesat_testbed.protocol.csp_v1_packet import CspV1PacketFrame

ZMQ_LINK = "zmq"
ZMQ_DESTINATION_BYTES = 1
ZMQ_PREFIX_AND_HEADER_BYTES = ZMQ_DESTINATION_BYTES + CSP_V1_HEADER_BYTES


def encode_zmq_message(
    fields: CspV1Fields,
    payload: BytesLike,
    *,
    max_packet_bytes: int = CSP_V1_DEFAULT_MAX_PACKET_BYTES,
) -> bytes:
    """Encode one CSP v1 packet as the exact message libcsp would publish.

    ``payload`` is the CSP payload as libcsp holds it, including any CRC32/HMAC trailer: this
    does not compute one. ``max_packet_bytes`` is the sending node's packet buffer.
    """

    payload_bytes = bytes(payload)
    if len(payload_bytes) > max_packet_bytes:
        raise CspV1FrameError(
            f"CSP payload of {len(payload_bytes)} bytes exceeds the sender's "
            f"max_packet_bytes={max_packet_bytes}"
        )
    return bytes((fields.destination,)) + pack_header(fields) + payload_bytes


class ZmqDropReason(str, Enum):
    """Why a received message was discarded."""

    TOO_SHORT = "too_short"
    """Shorter than the destination byte plus a CSP v1 header."""
    OVERFLOW = "overflow"
    """The payload is larger than the receiver's packet buffer."""
    MALFORMED = "malformed"
    """The header decoded to something outside the supported CSP v1 profile."""


@dataclass(frozen=True, slots=True)
class ZmqEvent:
    """The outcome of decoding one message: a packet, or why it was dropped."""

    packet: CspV1PacketFrame | None = None
    reason: ZmqDropReason | None = None
    detail: str = ""
    fields: CspV1Fields | None = None
    limit: int | None = None


def decode_zmq_message(
    message: BytesLike,
    *,
    max_packet_bytes: int = CSP_V1_DEFAULT_MAX_PACKET_BYTES,
) -> ZmqEvent:
    """Decode one received message; never raises, a bad message is a dropped event."""

    data = bytes(message)
    if len(data) < ZMQ_PREFIX_AND_HEADER_BYTES:
        return ZmqEvent(
            reason=ZmqDropReason.TOO_SHORT,
            detail=f"{len(data)} bytes, the destination byte and a CSP v1 header are "
            f"{ZMQ_PREFIX_AND_HEADER_BYTES}",
        )
    try:
        fields = unpack_header(data[ZMQ_DESTINATION_BYTES:ZMQ_PREFIX_AND_HEADER_BYTES])
    except CspV1CodecError as exc:
        return ZmqEvent(reason=ZmqDropReason.MALFORMED, detail=str(exc))
    payload = data[ZMQ_PREFIX_AND_HEADER_BYTES:]
    if len(payload) > max_packet_bytes:
        return ZmqEvent(
            reason=ZmqDropReason.OVERFLOW,
            detail=f"payload of {len(payload)} bytes is over max_packet_bytes={max_packet_bytes}",
            fields=fields,
            limit=max_packet_bytes,
        )
    if fields.flags & CSP_FLAG_FRAGMENT:
        return ZmqEvent(
            reason=ZmqDropReason.MALFORMED,
            detail="CSP_FFRAG packets are not supported",
            fields=fields,
        )
    return ZmqEvent(
        packet=CspV1PacketFrame(fields=fields, payload=payload, wire=data, link=ZMQ_LINK)
    )


__all__ = [
    "ZMQ_DESTINATION_BYTES",
    "ZMQ_LINK",
    "ZMQ_PREFIX_AND_HEADER_BYTES",
    "ZmqDropReason",
    "ZmqEvent",
    "decode_zmq_message",
    "encode_zmq_message",
]
