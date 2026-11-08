"""ZeroMQ transport adapter: the testbed as a client of a libcsp ``zmqproxy`` hub.

libcsp applications on a Linux host are normally joined through a ZeroMQ hub rather
than a real bus; this adapter makes the testbed one more client of it, so an
unmodified libcsp process can talk to a simulated node (and the other way round)
with no CAN adapter or kernel module.

Routing follows :class:`~cubesat_testbed.transport.whole_packet.WholePacketAdapter`:
a packet for a simulated node is delivered inside the testbed and never reaches the
hub; a packet for any other address is published to it; every message the hub
delivers is received. One thing is specific to a hub: it broadcasts, so what the
testbed publishes comes back to its own subscriber. A message whose source is one of
the testbed's own simulated addresses can only be that echo, since nothing else on
the hub may use those addresses, and is ignored rather than delivered twice.

The receive side validates messages itself (header, size) and reports what it
rejects through :meth:`take_drops`, never swallowing it.
"""

from __future__ import annotations

from collections.abc import Iterable

from cubesat_testbed.protocol.csp_v2 import CSP_V2_DEFAULT_MAX_PACKET_BYTES, CspV2CodecError
from cubesat_testbed.protocol.csp_v2_packet import CspPacketFrame
from cubesat_testbed.protocol.csp_v2_zmq import (
    ZMQ_LINK,
    decode_zmq_message,
    encode_zmq_message,
)
from cubesat_testbed.transport.base import TransportDrop, TransportError
from cubesat_testbed.transport.whole_packet import WholePacketAdapter
from cubesat_testbed.transport.zmq_link import MessageLink, PyzmqMessageLink


class ZmqAdapter(WholePacketAdapter):
    """CSP v2 through a ZeroMQ hub, with local delivery for simulated nodes."""

    link_name = "ZMQ"

    def __init__(
        self,
        link: MessageLink,
        *,
        local_addresses: Iterable[int] = (),
        max_packet_bytes: int = CSP_V2_DEFAULT_MAX_PACKET_BYTES,
    ) -> None:
        super().__init__(local_addresses=local_addresses, max_packet_bytes=max_packet_bytes)
        self._link = link

    @classmethod
    def open(
        cls,
        publish_endpoint: str = "tcp://127.0.0.1:6000",
        subscribe_endpoint: str = "tcp://127.0.0.1:7000",
        *,
        settle_seconds: float = 0.5,
        local_addresses: Iterable[int] = (),
        max_packet_bytes: int = CSP_V2_DEFAULT_MAX_PACKET_BYTES,
    ) -> ZmqAdapter:
        """Connect to a hub and wrap the connection."""

        return cls(
            PyzmqMessageLink(publish_endpoint, subscribe_endpoint, settle_seconds=settle_seconds),
            local_addresses=local_addresses,
            max_packet_bytes=max_packet_bytes,
        )

    def _emit(self, packet: CspPacketFrame) -> CspPacketFrame:
        try:
            message = encode_zmq_message(
                packet.fields, packet.payload, max_packet_bytes=self._max_packet_bytes
            )
        except CspV2CodecError as exc:
            raise TransportError(f"packet cannot be sent as a ZMQ message: {exc}") from exc
        self._link.send(message)
        return CspPacketFrame(
            fields=packet.fields, payload=packet.payload, wire=message, link=ZMQ_LINK
        )

    def _poll(self, timeout: float) -> Iterable[CspPacketFrame | TransportDrop]:
        items: list[CspPacketFrame | TransportDrop] = []
        wait = timeout
        while (message := self._link.receive(wait)) is not None:
            wait = 0.0  # having waited for the first, take only what is already there
            event = decode_zmq_message(message, max_packet_bytes=self._max_packet_bytes)
            if event.packet is not None:
                if event.packet.fields.source in self._local_addresses:
                    continue  # the hub echoing our own publication back
                items.append(event.packet)
                continue
            assert event.reason is not None
            items.append(
                TransportDrop(
                    reason=event.reason.value,
                    detail=event.detail,
                    source=None if event.fields is None else event.fields.source,
                    destination=None if event.fields is None else event.fields.destination,
                    limit=event.limit,
                )
            )
        return items

    def _close_link(self) -> None:
        self._link.close()


__all__ = ["ZmqAdapter"]
