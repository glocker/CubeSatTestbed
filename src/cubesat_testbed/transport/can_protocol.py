"""Which CSP protocol version a CAN link speaks, as one object the runtime asks.

A setup selects exactly one CSP version (``protocol_version``). The two codecs,
:mod:`~cubesat_testbed.protocol.csp_v2` (CFP2) and :mod:`~cubesat_testbed.protocol.csp_v1`
(CFP1), stay apart and know nothing of each other. This module is the single place that
knows both: it hands the rest of the runtime what differs per version -- how a packet becomes
frames, how frames become a packet again, which CAN-identifier field names the receiving node,
how a frame read off SocketCAN is typed -- so the scenario runner, the OBC Peer and the
adapters never branch on a version number themselves.

Packet fields travel through the runtime as :class:`~cubesat_testbed.protocol.csp_v2.CspFields`
whichever version carries them: its ranges contain CSP v1's, and the v1 sender turns it into a
:class:`~cubesat_testbed.protocol.csp_v1.CspV1Fields` and refuses what v1 cannot carry (an
address above 30, say). A received packet is read through :class:`PacketView`, which both
decoded packet types satisfy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol, TypeAlias

from cubesat_testbed.protocol import csp_v1
from cubesat_testbed.protocol.csp_v1 import (
    CspV1CanFrame,
    CspV1CodecError,
    CspV1Fields,
    CspV1Fragmenter,
    CspV1Reassembler,
)
from cubesat_testbed.protocol.csp_v2 import (
    CspCanFrame,
    CspFields,
    CspFragmenter,
    CspReassembler,
    decode_can_id,
)

CanFrame: TypeAlias = CspCanFrame | CspV1CanFrame
"""A classic CAN frame of either protocol version."""

SUPPORTED_VERSIONS = (1, 2)


class PacketFields(Protocol):
    """The header fields routing and tracing read, whichever CSP version a packet is."""

    @property
    def priority(self) -> int: ...

    @property
    def source(self) -> int: ...

    @property
    def destination(self) -> int: ...

    @property
    def destination_port(self) -> int: ...

    @property
    def source_port(self) -> int: ...

    @property
    def flags(self) -> int: ...


class PacketView(Protocol):
    """What routing and tracing need from a received packet, on any link and version."""

    @property
    def fields(self) -> PacketFields: ...

    @property
    def payload(self) -> bytes: ...


class CanPacketSenderLike(Protocol):
    """Turns one CSP packet into the CAN frames its version carries it as."""

    def frames(self, fields: CspFields, payload: bytes) -> tuple[CanFrame, ...]: ...


@dataclass(frozen=True, slots=True)
class PacketEvent:
    """Outcome of feeding one frame, or an expiry, to a :class:`PacketReassembler`.

    The same shape for both versions: ``kind`` is ``"complete"`` (``packet`` is set),
    ``"pending"`` or ``"dropped"`` (``reason`` is a short slug such as ``frame_lost`` or
    ``overflow``, with ``detail``). ``time`` is the virtual time it happened.
    """

    kind: str
    time: int
    frame: CanFrame | None = None
    packet: PacketView | None = None
    reason: str | None = None
    detail: str = ""
    sender: int | None = None
    destination: int | None = None
    limit: int | None = None


class PacketReassembler(Protocol):
    """One receiving node's CAN reassembly, in virtual time."""

    @property
    def pending_count(self) -> int: ...

    def feed(self, frame: CanFrame, *, now: int) -> tuple[PacketEvent, ...]: ...

    def expire(self, now: int) -> tuple[PacketEvent, ...]: ...


class CanPacketSender:
    """CSP v2 on CAN: one packet becomes one or several CFP2 frames."""

    def __init__(self, *, max_packet_bytes: int) -> None:
        self._fragmenter = CspFragmenter(max_packet_bytes=max_packet_bytes)

    def frames(self, fields: CspFields, payload: bytes) -> tuple[CanFrame, ...]:
        return self._fragmenter.fragment(fields, payload)


class CanV1PacketSender:
    """CSP v1 on CAN: one packet becomes one or several CFP1 frames."""

    def __init__(self, *, max_packet_bytes: int) -> None:
        self._fragmenter = CspV1Fragmenter(max_packet_bytes=max_packet_bytes)

    def frames(self, fields: CspFields, payload: bytes) -> tuple[CanFrame, ...]:
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
        return self._fragmenter.fragment(v1_fields, payload)


class _V2Reassembler:
    def __init__(self, max_packet_bytes: int | None) -> None:
        self._inner = CspReassembler(max_packet_bytes=max_packet_bytes)

    @property
    def pending_count(self) -> int:
        return self._inner.pending_count

    def feed(self, frame: CanFrame, *, now: int) -> tuple[PacketEvent, ...]:
        assert isinstance(frame, CspCanFrame)
        return tuple(
            PacketEvent(
                kind=event.kind,
                time=event.time,
                frame=event.frame,
                packet=event.packet,
                reason=None if event.reason is None else event.reason.value,
                detail=event.detail,
                sender=event.sender,
                destination=event.destination,
                limit=event.limit,
            )
            for event in self._inner.feed(frame, now=now)
        )

    def expire(self, now: int) -> tuple[PacketEvent, ...]:
        return tuple(
            PacketEvent(
                kind=event.kind,
                time=event.time,
                frame=event.frame,
                reason=None if event.reason is None else event.reason.value,
                detail=event.detail,
                sender=event.sender,
                destination=event.destination,
                limit=event.limit,
            )
            for event in self._inner.expire(now)
        )


class _V1Reassembler:
    def __init__(self, max_packet_bytes: int | None) -> None:
        self._inner = CspV1Reassembler(max_packet_bytes=max_packet_bytes)

    @property
    def pending_count(self) -> int:
        return self._inner.pending_count

    def feed(self, frame: CanFrame, *, now: int) -> tuple[PacketEvent, ...]:
        assert isinstance(frame, CspV1CanFrame)
        return tuple(_v1_event(event) for event in self._inner.feed(frame, now=now))

    def expire(self, now: int) -> tuple[PacketEvent, ...]:
        return tuple(_v1_event(event) for event in self._inner.expire(now))


def _v1_event(event: csp_v1.ReassemblyEvent) -> PacketEvent:
    return PacketEvent(
        kind=event.kind,
        time=event.time,
        frame=event.frame,
        packet=event.packet,
        reason=None if event.reason is None else event.reason.value,
        detail=event.detail,
        sender=event.sender,
        destination=event.destination,
        limit=event.limit,
    )


class CanProtocol(ABC):
    """What differs between CSP versions on a CAN link."""

    version: int
    """The CSP protocol version, 1 or 2."""
    address_max: int
    """The highest address the version can carry (a broadcast address included)."""
    frame_class: type[CanFrame]
    """The frame type the version's codec produces."""

    @abstractmethod
    def new_sender(self, *, max_packet_bytes: int) -> CanPacketSenderLike:
        """The transmit side of one node."""

    @abstractmethod
    def new_reassembler(self, *, max_packet_bytes: int | None) -> PacketReassembler:
        """The receive side of one node (``None`` removes the size limit)."""

    @abstractmethod
    def frame_destination(self, frame: CanFrame) -> int | None:
        """The node a frame is addressed to, from its CAN identifier (``None`` if unknown)."""

    def frame_from_can(self, can_id: int, data: bytes) -> CanFrame:
        """A frame read off a CAN interface, as this version's frame type."""

        return self.frame_class(can_id=can_id, data=data, is_extended_id=True)


class _CspV2Can(CanProtocol):
    version = 2
    address_max = 0x3FFF
    frame_class = CspCanFrame

    def new_sender(self, *, max_packet_bytes: int) -> CanPacketSenderLike:
        return CanPacketSender(max_packet_bytes=max_packet_bytes)

    def new_reassembler(self, *, max_packet_bytes: int | None) -> PacketReassembler:
        return _V2Reassembler(max_packet_bytes)

    def frame_destination(self, frame: CanFrame) -> int | None:
        if not isinstance(frame, CspCanFrame) or not frame.is_extended_id:
            return None
        return decode_can_id(frame.can_id).destination


class _CspV1Can(CanProtocol):
    version = 1
    address_max = csp_v1.CSP_V1_ADDRESS_MAX
    frame_class = CspV1CanFrame

    def new_sender(self, *, max_packet_bytes: int) -> CanPacketSenderLike:
        return CanV1PacketSender(max_packet_bytes=max_packet_bytes)

    def new_reassembler(self, *, max_packet_bytes: int | None) -> PacketReassembler:
        return _V1Reassembler(max_packet_bytes)

    def frame_destination(self, frame: CanFrame) -> int | None:
        if not isinstance(frame, CspV1CanFrame) or not frame.is_extended_id:
            return None
        return csp_v1.decode_can_id(frame.can_id).destination


CSP_V2_CAN: CanProtocol = _CspV2Can()
CSP_V1_CAN: CanProtocol = _CspV1Can()


def can_protocol_for(version: int) -> CanProtocol:
    """The CAN protocol for ``version`` (1 or 2); anything else is an error."""

    if version == 2:
        return CSP_V2_CAN
    if version == 1:
        return CSP_V1_CAN
    raise ValueError(f"CSP protocol version must be 1 or 2, got {version!r}")


__all__ = [
    "CSP_V1_CAN",
    "CSP_V2_CAN",
    "SUPPORTED_VERSIONS",
    "CanFrame",
    "CanPacketSender",
    "CanPacketSenderLike",
    "CanProtocol",
    "CanV1PacketSender",
    "PacketEvent",
    "PacketFields",
    "PacketReassembler",
    "PacketView",
    "can_protocol_for",
]
