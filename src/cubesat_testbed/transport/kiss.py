"""Serial KISS transport adapter (CSP v2 over a UART, USB-serial or pty).

A serial line joins exactly two ends, unlike a CAN bus every node listens on, so
the adapter works the way libcsp's own routing does:

- a packet addressed to a *local* node (one the testbed simulates) is delivered
  inside the testbed, to that node's endpoint queue and the monitor stream, and
  never touches the line;
- a packet addressed to anything else goes out on the line as a KISS frame;
- every packet that arrives on the line is delivered like a bus frame: to the
  monitor stream and to the local endpoint it is addressed to.

As with SocketCAN, a frame sent out on the line is not echoed back.

The receive side validates frames itself (framing, CRC32, size) because that is
the link's job; what it rejects is reported through :meth:`take_drops`, never
swallowed. Packet-level routing is left to the caller, as on every transport.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from cubesat_testbed.protocol.csp_v2 import CSP_V2_DEFAULT_MAX_PACKET_BYTES, CspV2CodecError
from cubesat_testbed.protocol.csp_v2_kiss import KissDecoder, encode_kiss_frame
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
from cubesat_testbed.transport.serial_line import SerialLine, TermiosSerialLine


class KissAdapter(TransportAdapter):
    """CSP v2 over a KISS serial line, with local delivery for simulated nodes.

    ``local_addresses`` are the CSP addresses of the nodes the testbed simulates;
    each is also the endpoint id of its receive queue. ``max_packet_bytes`` is the
    receive buffer applied to frames from the line (the largest of the nodes'
    limits); each destination node's own limit is enforced again by the runner.
    """

    def __init__(
        self,
        line: SerialLine,
        *,
        local_addresses: Iterable[int] = (),
        max_packet_bytes: int = CSP_V2_DEFAULT_MAX_PACKET_BYTES,
    ) -> None:
        self._line = line
        self._local_addresses = frozenset(local_addresses)
        self._max_packet_bytes = max_packet_bytes
        self._monitor: deque[TransportEnvelope] = deque()
        self._endpoint_queues: dict[EndpointId, deque[TransportEnvelope]] = {
            address: deque() for address in sorted(self._local_addresses)
        }
        self._next_sequence = 0
        self._decoder = KissDecoder(max_packet_bytes=max_packet_bytes)
        self._drops: list[TransportDrop] = []
        self._closed = False

    @classmethod
    def open(
        cls,
        device: str,
        *,
        baudrate: int = 115200,
        local_addresses: Iterable[int] = (),
        max_packet_bytes: int = CSP_V2_DEFAULT_MAX_PACKET_BYTES,
    ) -> KissAdapter:
        """Open ``device`` and wrap it."""

        return cls(
            TermiosSerialLine.open(device, baudrate=baudrate),
            local_addresses=local_addresses,
            max_packet_bytes=max_packet_bytes,
        )

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
        """Deliver a packet locally, or write it to the line as a KISS frame."""

        self._ensure_open()
        if not isinstance(frame, CspPacketFrame):
            raise TransportError("the KISS transport carries whole CSP packets, not CAN frames")
        if frame.fields.destination in self._local_addresses:
            return self._deliver(frame, source=source)

        try:
            wire = encode_kiss_frame(
                frame.fields, frame.payload, max_packet_bytes=self._max_packet_bytes
            )
        except CspV2CodecError as exc:
            raise TransportError(f"packet cannot be sent as a KISS frame: {exc}") from exc
        self._line.write(wire)
        sent = CspPacketFrame(fields=frame.fields, payload=frame.payload, wire=wire)
        # A frame that goes out is not echoed back, so it is not queued locally; the
        # envelope still gets an adapter-local sequence number like any other.
        return self._envelope(sent, source=source)

    def receive(
        self,
        *,
        endpoint: EndpointId | None = None,
        timeout: float | None = None,
    ) -> TransportEnvelope | None:
        """Return the next packet for a stream, reading the line for more if needed."""

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
        self._line.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def _pump(self, timeout: float) -> None:
        """Move whatever the line has delivered into the receive queues."""

        data = self._line.read(timeout)
        if not data:
            return
        for event in self._decoder.feed(data):
            if event.packet is not None:
                self._deliver(event.packet, source=None)
                continue
            assert event.reason is not None
            self._drops.append(
                TransportDrop(
                    reason=event.reason.value,
                    detail=event.detail,
                    source=None if event.fields is None else event.fields.source,
                    destination=None if event.fields is None else event.fields.destination,
                    limit=event.limit,
                )
            )

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
            raise TransportError("KISS serial line is already closed")


__all__ = ["KissAdapter"]
