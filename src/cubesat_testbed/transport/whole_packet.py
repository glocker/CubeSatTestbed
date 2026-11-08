"""Shared machinery for links that carry a CSP packet whole (serial KISS, ZeroMQ).

Both links hand the testbed one finished packet at a time, have no node-to-node
addressing of their own, and reach only one far end: a serial line has two ends, a
ZeroMQ hub is a single client connection. So they route the same way libcsp does:

- a packet addressed to a *local* node (one the testbed simulates) is delivered
  inside the testbed, to that node's endpoint queue and the monitor stream, and
  never touches the link;
- a packet addressed to anything else goes out on the link;
- every packet that arrives on the link is delivered like a bus frame: to the
  monitor stream and to the local endpoint it is addressed to.

A packet sent out is not echoed back to its sender. What differs per link, how a
packet is encoded for the wire and how bytes are read and validated, is left to
the subclass; what a link rejects is reported through :meth:`take_drops`, never
swallowed.
"""

from __future__ import annotations

from abc import abstractmethod
from collections import deque
from collections.abc import Iterable

from cubesat_testbed.protocol.csp_v2 import CSP_V2_DEFAULT_MAX_PACKET_BYTES
from cubesat_testbed.protocol.csp_v2_packet import CspPacketFrame
from cubesat_testbed.transport.base import (
    EndpointId,
    Frame,
    PacketSender,
    TransportAdapter,
    TransportDrop,
    TransportEndpointError,
    TransportEnvelope,
    TransportError,
    WholePacketSender,
)


class WholePacketAdapter(TransportAdapter):
    """A transport adapter for a link that carries whole CSP packets.

    ``local_addresses`` are the CSP addresses of the nodes the testbed simulates;
    each is also the endpoint id of its receive queue. ``max_packet_bytes`` is the
    receive buffer applied to what arrives on the link (the largest of the nodes'
    limits); each destination node's own limit is enforced again by the runner.
    """

    link_name: str = ""
    """Name of the link in error messages."""

    def __init__(
        self,
        *,
        local_addresses: Iterable[int] = (),
        max_packet_bytes: int = CSP_V2_DEFAULT_MAX_PACKET_BYTES,
    ) -> None:
        self._local_addresses = frozenset(local_addresses)
        self._max_packet_bytes = max_packet_bytes
        self._monitor: deque[TransportEnvelope] = deque()
        self._endpoint_queues: dict[EndpointId, deque[TransportEnvelope]] = {
            address: deque() for address in sorted(self._local_addresses)
        }
        self._next_sequence = 0
        self._drops: list[TransportDrop] = []
        self._closed = False

    # --- what a link provides -------------------------------------------------------

    @abstractmethod
    def _emit(self, packet: CspPacketFrame) -> CspPacketFrame:
        """Put a packet on the link; return it with ``wire`` and ``link`` filled in."""

    @abstractmethod
    def _poll(self, timeout: float) -> Iterable[CspPacketFrame | TransportDrop]:
        """Read what the link has delivered, waiting up to ``timeout`` seconds for some.

        Returns the packets that arrived and the frames the link rejected, in order.
        """

    @abstractmethod
    def _close_link(self) -> None:
        """Release the link's resources."""

    # --- TransportAdapter -----------------------------------------------------------

    def new_sender(
        self, *, max_packet_bytes: int = CSP_V2_DEFAULT_MAX_PACKET_BYTES
    ) -> PacketSender:
        return WholePacketSender(max_packet_bytes=max_packet_bytes)

    def take_drops(self) -> tuple[TransportDrop, ...]:
        drops = tuple(self._drops)
        self._drops.clear()
        return drops

    def send(
        self,
        frame: Frame,
        *,
        source: EndpointId | None = None,
    ) -> TransportEnvelope:
        """Deliver a packet locally, or put it on the link."""

        self._ensure_open()
        if not isinstance(frame, CspPacketFrame):
            raise TransportError(
                f"the {self.link_name} transport carries whole CSP packets, not CAN frames"
            )
        if frame.fields.destination in self._local_addresses:
            return self._deliver(frame, source=source)
        # A packet that goes out is not echoed back, so it is not queued locally; the
        # envelope still gets an adapter-local sequence number like any other.
        return self._envelope(self._emit(frame), source=source)

    def receive(
        self,
        *,
        endpoint: EndpointId | None = None,
        timeout: float | None = None,
    ) -> TransportEnvelope | None:
        """Return the next packet for a stream, polling the link for more if needed."""

        self._ensure_open()
        queue = self._queue_for(endpoint)
        self._pump(0.0)
        if not queue and timeout is not None and timeout > 0:
            self._pump(timeout)
        return queue.popleft() if queue else None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_link()

    @property
    def closed(self) -> bool:
        return self._closed

    # --- internals ------------------------------------------------------------------

    def _pump(self, timeout: float) -> None:
        for item in self._poll(timeout):
            if isinstance(item, TransportDrop):
                self._drops.append(item)
            else:
                self._deliver(item, source=None)

    def _envelope(self, frame: CspPacketFrame, *, source: EndpointId | None) -> TransportEnvelope:
        envelope = TransportEnvelope(frame=frame, sequence=self._next_sequence, source=source)
        self._next_sequence += 1
        return envelope

    def _deliver(self, frame: CspPacketFrame, *, source: EndpointId | None) -> TransportEnvelope:
        """Queue a packet for the monitor and for the local node it is addressed to."""

        envelope = self._envelope(frame, source=source)
        self._monitor.append(envelope)
        destination = frame.fields.destination
        if destination in self._endpoint_queues and destination != source:
            self._endpoint_queues[destination].append(envelope)
        return envelope

    def _queue_for(self, endpoint: EndpointId | None) -> deque[TransportEnvelope]:
        if endpoint is None:
            return self._monitor
        try:
            return self._endpoint_queues[endpoint]
        except KeyError as exc:
            raise TransportEndpointError(f"endpoint {endpoint!r} is not connected") from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise TransportError(f"{self.link_name} link is already closed")


__all__ = ["WholePacketAdapter"]
