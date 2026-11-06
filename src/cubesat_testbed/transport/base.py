"""Transport adapter boundary.

Adapters carry CSP v2 traffic through the deterministic in-memory bus, Linux
SocketCAN or a serial KISS line. What crosses the boundary is a :data:`Frame`:
a :class:`~cubesat_testbed.protocol.csp_v2.CspCanFrame` on CAN links, where one
CSP packet may span several frames, or a
:class:`~cubesat_testbed.protocol.csp_v2_packet.CspPacketFrame` on links that
carry a packet whole. Higher layers depend on this boundary rather than on a
concrete transport; the one thing that differs per link, how a packet becomes
frames, is asked of the adapter through :meth:`TransportAdapter.new_sender`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol, TypeAlias

from cubesat_testbed.protocol.csp_v2 import (
    CSP_V2_DEFAULT_MAX_PACKET_BYTES,
    CspCanFrame,
    CspFields,
    CspFragmenter,
    CspV2FrameError,
)
from cubesat_testbed.protocol.csp_v2_packet import CspPacketFrame

EndpointId = str | int
Frame: TypeAlias = CspCanFrame | CspPacketFrame


class TransportError(RuntimeError):
    """Base class for transport-boundary failures."""


class TransportEndpointError(TransportError, ValueError):
    """Raised when an endpoint identifier is invalid or not connected."""


@dataclass(frozen=True, slots=True)
class TransportEnvelope:
    """A frame plus deterministic adapter metadata.

    ``sequence`` is assigned by the adapter and is local to that adapter instance.
    It provides a reproducible ordering key without depending on wall-clock time.
    """

    frame: Frame
    sequence: int
    source: EndpointId | None = None

    @property
    def can_id(self) -> int:
        """Convenience access to a CAN frame's identifier (CAN links only)."""

        if not isinstance(self.frame, CspCanFrame):
            raise TransportError(
                "this envelope carries a whole packet, which has no CAN identifier"
            )
        return self.frame.can_id

    @property
    def data(self) -> bytes:
        """Convenience access to a CAN frame's raw data bytes (CAN links only)."""

        if not isinstance(self.frame, CspCanFrame):
            raise TransportError(
                "this envelope carries a whole packet, which has no CAN data field"
            )
        return self.frame.data


class PacketSender(Protocol):
    """Turns one CSP packet into the frames a link carries it as.

    One sender models one node's transmit side: it holds whatever per-node state
    the link needs (on CAN, the CFP2 packet counter) and the node's packet size
    limit. Obtain one from :meth:`TransportAdapter.new_sender`.
    """

    def frames(self, fields: CspFields, payload: bytes) -> tuple[Frame, ...]:
        """Frames to send, in bus order; raises if the packet cannot be sent."""
        ...


class CanPacketSender:
    """CAN: one packet becomes one or several CFP2 frames."""

    def __init__(self, *, max_packet_bytes: int = CSP_V2_DEFAULT_MAX_PACKET_BYTES) -> None:
        self._fragmenter = CspFragmenter(max_packet_bytes=max_packet_bytes)

    def frames(self, fields: CspFields, payload: bytes) -> tuple[Frame, ...]:
        return self._fragmenter.fragment(fields, payload)


class WholePacketSender:
    """Links that carry a packet whole: one packet is exactly one frame."""

    def __init__(self, *, max_packet_bytes: int = CSP_V2_DEFAULT_MAX_PACKET_BYTES) -> None:
        self.max_packet_bytes = max_packet_bytes

    def frames(self, fields: CspFields, payload: bytes) -> tuple[Frame, ...]:
        if len(payload) > self.max_packet_bytes:
            raise CspV2FrameError(
                f"CSP payload of {len(payload)} bytes exceeds the sender's "
                f"max_packet_bytes={self.max_packet_bytes}"
            )
        return (CspPacketFrame(fields=fields, payload=payload),)


@dataclass(frozen=True, slots=True)
class TransportDrop:
    """A frame or packet an adapter discarded below the scenario runner.

    ``reason`` is a short slug (``crc_error``, ``overflow``, ...). The adapter has
    no notion of virtual time, so the runner stamps the time when it collects the
    drop with :meth:`TransportAdapter.take_drops`.
    """

    reason: str
    detail: str
    source: int | None = None
    destination: int | None = None
    limit: int | None = None


class TransportAdapter(ABC):
    """Synchronous transport boundary used by simulated and HIL bus adapters.

    ``receive`` defaults to non-blocking: it returns ``None`` immediately when
    no frame is available. Passing ``timeout`` lets a caller (the scenario
    runner, pacing a HIL run against a :class:`~cubesat_testbed.clock.Clock`)
    block up to ``timeout`` real seconds for a frame to arrive, returning as
    soon as one does instead of always waiting the full timeout. Adapters that
    cannot block (e.g. the in-memory bus, which is always instantaneous)
    ignore ``timeout``.
    """

    def new_sender(
        self, *, max_packet_bytes: int = CSP_V2_DEFAULT_MAX_PACKET_BYTES
    ) -> PacketSender:
        """Create the transmit side of one node on this link.

        The default is CAN: packets are fragmented into CFP2 frames. Adapters for
        links that carry a packet whole override this.
        """

        return CanPacketSender(max_packet_bytes=max_packet_bytes)

    def take_drops(self) -> tuple[TransportDrop, ...]:
        """Return, and forget, what the adapter discarded since the last call.

        Empty by default: the in-memory bus and SocketCAN hand every frame up and
        leave judging it to the protocol layer. A link that validates frames itself
        (a serial line checking CRC32) reports the rejected ones here so a lost
        packet is never silent.
        """

        return ()

    @abstractmethod
    def send(
        self,
        frame: Frame,
        *,
        source: EndpointId | None = None,
    ) -> TransportEnvelope:
        """Send one already encoded frame.

        Implementations return the envelope that represents the accepted frame.
        The optional ``source`` identifies the logical sender when the adapter
        can model per-endpoint delivery.
        """

    @abstractmethod
    def receive(
        self,
        *,
        endpoint: EndpointId | None = None,
        timeout: float | None = None,
    ) -> TransportEnvelope | None:
        """Receive the next available frame envelope, or ``None`` if empty.

        ``endpoint=None`` represents the adapter's default receive stream. An
        implementation may also expose endpoint-specific streams for simulated
        nodes. ``timeout`` is real seconds to block waiting for a frame before
        giving up (``None`` or ``0`` means a single non-blocking poll).
        """

    def drain(self, *, endpoint: EndpointId | None = None) -> tuple[TransportEnvelope, ...]:
        """Receive all currently available frames from a stream in order.

        Always a non-blocking poll: this drains what is already queued, it
        does not wait for more to arrive.
        """

        envelopes: list[TransportEnvelope] = []
        while True:
            envelope = self.receive(endpoint=endpoint)
            if envelope is None:
                return tuple(envelopes)
            envelopes.append(envelope)

    def close(self) -> None:
        """Release any operating-system resources this adapter holds.

        A no-op by default, since a purely in-process adapter holds none; a
        HIL adapter overrides it to hand its bus socket back. Must be safe to
        call more than once, so an owner can close unconditionally without
        checking whether someone else already did.
        """


__all__ = [
    "CanPacketSender",
    "EndpointId",
    "Frame",
    "PacketSender",
    "TransportAdapter",
    "TransportDrop",
    "TransportEndpointError",
    "TransportEnvelope",
    "TransportError",
    "WholePacketSender",
]
