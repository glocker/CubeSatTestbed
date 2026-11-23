"""Which CSP protocol version a whole-packet link (KISS, ZeroMQ) speaks.

The counterpart of :mod:`~cubesat_testbed.transport.can_protocol` for the links that carry a
packet as one unit. The two versions have separate codecs that share nothing
(:mod:`~cubesat_testbed.protocol.csp_v2_kiss`, :mod:`~cubesat_testbed.protocol.csp_v2_zmq`
and their ``csp_v1_*`` siblings); this module is the one place that knows both, and hands the
adapters what differs: how a packet becomes frames for the runtime, how it is encoded and decoded
for each link, and the frame type that results.

As on CAN, the runtime describes a packet to send with
:class:`~cubesat_testbed.protocol.csp_v2.CspFields` whichever version carries it, and the v1 sender
turns it into a :class:`~cubesat_testbed.protocol.csp_v1.CspV1Fields` and refuses what v1 cannot
carry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, TypeAlias

from cubesat_testbed.protocol import csp_v1_kiss, csp_v1_zmq, csp_v2_kiss, csp_v2_zmq
from cubesat_testbed.protocol.csp_v1 import CspV1CodecError, CspV1Fields, CspV1FrameError
from cubesat_testbed.protocol.csp_v1_packet import CspV1PacketFrame
from cubesat_testbed.protocol.csp_v2 import CspFields, CspV2CodecError, CspV2FrameError
from cubesat_testbed.protocol.csp_v2_packet import CspPacketFrame

WholePacketFrame: TypeAlias = CspPacketFrame | CspV1PacketFrame
"""A whole CSP packet of either protocol version."""

WHOLE_PACKET_FRAMES = (CspPacketFrame, CspV1PacketFrame)
"""``isinstance`` check for :data:`WholePacketFrame`."""

CodecError = (CspV1CodecError, CspV2CodecError)
"""What either version's codec raises for a packet it cannot encode."""


class _Reason(Protocol):
    @property
    def value(self) -> str: ...


class _Fields(Protocol):
    @property
    def source(self) -> int: ...

    @property
    def destination(self) -> int: ...


class LinkEvent(Protocol):
    """What a decoded KISS frame or ZeroMQ message reports, whichever version."""

    @property
    def packet(self) -> WholePacketFrame | None: ...

    @property
    def reason(self) -> _Reason | None: ...

    @property
    def detail(self) -> str: ...

    @property
    def fields(self) -> _Fields | None: ...

    @property
    def limit(self) -> int | None: ...


class KissFrameDecoder(Protocol):
    """An incremental KISS receiver."""

    def feed(self, data: bytes) -> tuple[LinkEvent, ...]: ...


class WholePacketProtocol(ABC):
    """What differs between CSP versions on a whole-packet link."""

    version: int
    """The CSP protocol version, 1 or 2."""
    frame_class: type[WholePacketFrame]
    """The frame type a packet of this version is."""

    @abstractmethod
    def make_frame(
        self, fields: CspFields, payload: bytes, *, wire: bytes = b"", link: str = ""
    ) -> WholePacketFrame:
        """A packet as this version's frame type."""

    @abstractmethod
    def encode_kiss(self, frame: WholePacketFrame, *, max_packet_bytes: int) -> bytes: ...

    @abstractmethod
    def new_kiss_decoder(self, *, max_packet_bytes: int) -> KissFrameDecoder: ...

    @abstractmethod
    def encode_zmq(self, frame: WholePacketFrame, *, max_packet_bytes: int) -> bytes: ...

    @abstractmethod
    def decode_zmq(self, message: bytes, *, max_packet_bytes: int) -> LinkEvent: ...

    @abstractmethod
    def with_wire(self, frame: WholePacketFrame, wire: bytes, link: str) -> WholePacketFrame:
        """``frame`` with the bytes it crossed a link as."""


class WholePacketSender:
    """Links that carry a packet whole: one packet is exactly one frame.

    ``max_packet_bytes`` is the node's packet buffer. ``payload_limit`` is how much payload
    actually fits it on this link, which is less when the link puts its own bytes in the same
    buffer (the KISS CRC32).
    """

    def __init__(
        self, protocol: WholePacketProtocol, *, max_packet_bytes: int, payload_limit: int
    ) -> None:
        self._protocol = protocol
        self.max_packet_bytes = max_packet_bytes
        self.payload_limit = payload_limit

    def frames(self, fields: CspFields, payload: bytes) -> tuple[WholePacketFrame, ...]:
        if len(payload) > self.payload_limit:
            error = CspV1FrameError if self._protocol.version == 1 else CspV2FrameError
            raise error(
                f"CSP payload of {len(payload)} bytes exceeds the sender's limit of "
                f"{self.payload_limit} bytes (max_packet_bytes={self.max_packet_bytes})"
            )
        return (self._protocol.make_frame(fields, payload),)


class _CspV2Whole(WholePacketProtocol):
    version = 2
    frame_class = CspPacketFrame

    def make_frame(
        self, fields: CspFields, payload: bytes, *, wire: bytes = b"", link: str = ""
    ) -> WholePacketFrame:
        return CspPacketFrame(fields=fields, payload=payload, wire=wire, link=link)

    def with_wire(self, frame: WholePacketFrame, wire: bytes, link: str) -> WholePacketFrame:
        assert isinstance(frame, CspPacketFrame)
        return CspPacketFrame(fields=frame.fields, payload=frame.payload, wire=wire, link=link)

    def encode_kiss(self, frame: WholePacketFrame, *, max_packet_bytes: int) -> bytes:
        assert isinstance(frame, CspPacketFrame)
        return csp_v2_kiss.encode_kiss_frame(
            frame.fields, frame.payload, max_packet_bytes=max_packet_bytes
        )

    def new_kiss_decoder(self, *, max_packet_bytes: int) -> KissFrameDecoder:
        return csp_v2_kiss.KissDecoder(max_packet_bytes=max_packet_bytes)

    def encode_zmq(self, frame: WholePacketFrame, *, max_packet_bytes: int) -> bytes:
        assert isinstance(frame, CspPacketFrame)
        return csp_v2_zmq.encode_zmq_message(
            frame.fields, frame.payload, max_packet_bytes=max_packet_bytes
        )

    def decode_zmq(self, message: bytes, *, max_packet_bytes: int) -> LinkEvent:
        return csp_v2_zmq.decode_zmq_message(message, max_packet_bytes=max_packet_bytes)


class _CspV1Whole(WholePacketProtocol):
    version = 1
    frame_class = CspV1PacketFrame

    def make_frame(
        self, fields: CspFields, payload: bytes, *, wire: bytes = b"", link: str = ""
    ) -> WholePacketFrame:
        try:
            v1_fields = CspV1Fields(
                priority=fields.priority,
                source=fields.source,
                destination=fields.destination,
                destination_port=fields.destination_port,
                source_port=fields.source_port,
                flags=fields.flags,
            )
        except CspV1CodecError as exc:
            raise CspV1CodecError(f"packet cannot be carried by CSP v1: {exc}") from exc
        return CspV1PacketFrame(fields=v1_fields, payload=payload, wire=wire, link=link)

    def with_wire(self, frame: WholePacketFrame, wire: bytes, link: str) -> WholePacketFrame:
        assert isinstance(frame, CspV1PacketFrame)
        return CspV1PacketFrame(fields=frame.fields, payload=frame.payload, wire=wire, link=link)

    def encode_kiss(self, frame: WholePacketFrame, *, max_packet_bytes: int) -> bytes:
        assert isinstance(frame, CspV1PacketFrame)
        return csp_v1_kiss.encode_kiss_frame(
            frame.fields, frame.payload, max_packet_bytes=max_packet_bytes
        )

    def new_kiss_decoder(self, *, max_packet_bytes: int) -> KissFrameDecoder:
        return csp_v1_kiss.KissDecoder(max_packet_bytes=max_packet_bytes)

    def encode_zmq(self, frame: WholePacketFrame, *, max_packet_bytes: int) -> bytes:
        assert isinstance(frame, CspV1PacketFrame)
        return csp_v1_zmq.encode_zmq_message(
            frame.fields, frame.payload, max_packet_bytes=max_packet_bytes
        )

    def decode_zmq(self, message: bytes, *, max_packet_bytes: int) -> LinkEvent:
        return csp_v1_zmq.decode_zmq_message(message, max_packet_bytes=max_packet_bytes)


CSP_V2_WHOLE: WholePacketProtocol = _CspV2Whole()
CSP_V1_WHOLE: WholePacketProtocol = _CspV1Whole()


def whole_packet_protocol_for(version: int) -> WholePacketProtocol:
    """The whole-packet protocol for ``version`` (1 or 2); anything else is an error."""

    if version == 2:
        return CSP_V2_WHOLE
    if version == 1:
        return CSP_V1_WHOLE
    raise ValueError(f"CSP protocol version must be 1 or 2, got {version!r}")


__all__ = [
    "CSP_V1_WHOLE",
    "CSP_V2_WHOLE",
    "WHOLE_PACKET_FRAMES",
    "CodecError",
    "KissFrameDecoder",
    "LinkEvent",
    "WholePacketFrame",
    "WholePacketProtocol",
    "WholePacketSender",
    "whole_packet_protocol_for",
]
