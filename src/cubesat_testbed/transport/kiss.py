"""Serial KISS transport adapter (CSP over a UART, USB-serial or pty).

A serial line joins exactly two ends, unlike a CAN bus every node listens on, so the
adapter routes the way libcsp does (see
:class:`~cubesat_testbed.transport.whole_packet.WholePacketAdapter`): packets for
simulated nodes stay inside the testbed, everything else goes out on the line as a
KISS frame, and every frame arriving on the line is delivered. As with SocketCAN, a
frame sent out on the line is not echoed back.

The receive side validates frames itself (framing, CRC32, size) because that is the
link's job; what it rejects is reported through :meth:`take_drops`, never swallowed.
"""

from __future__ import annotations

from collections.abc import Iterable

from cubesat_testbed.protocol.csp_v2 import CSP_V2_DEFAULT_MAX_PACKET_BYTES
from cubesat_testbed.protocol.csp_v2_kiss import KISS_CRC32_BYTES
from cubesat_testbed.transport.base import TransportDrop, TransportError
from cubesat_testbed.transport.serial_line import SerialLine, TermiosSerialLine
from cubesat_testbed.transport.whole_packet import WholePacketAdapter
from cubesat_testbed.transport.whole_packet_protocol import CodecError, WholePacketFrame


class KissAdapter(WholePacketAdapter):
    """CSP over a KISS serial line, with local delivery for simulated nodes."""

    link_name = "KISS serial"

    def __init__(
        self,
        line: SerialLine,
        *,
        local_addresses: Iterable[int] = (),
        max_packet_bytes: int = CSP_V2_DEFAULT_MAX_PACKET_BYTES,
        protocol_version: int = 2,
    ) -> None:
        super().__init__(
            local_addresses=local_addresses,
            max_packet_bytes=max_packet_bytes,
            protocol_version=protocol_version,
        )
        self._line = line
        self._decoder = self._protocol.new_kiss_decoder(max_packet_bytes=max_packet_bytes)

    @classmethod
    def open(
        cls,
        device: str,
        *,
        baudrate: int = 115200,
        local_addresses: Iterable[int] = (),
        max_packet_bytes: int = CSP_V2_DEFAULT_MAX_PACKET_BYTES,
        protocol_version: int = 2,
    ) -> KissAdapter:
        """Open ``device`` and wrap it."""

        return cls(
            TermiosSerialLine.open(device, baudrate=baudrate),
            local_addresses=local_addresses,
            max_packet_bytes=max_packet_bytes,
            protocol_version=protocol_version,
        )

    def payload_limit(self, max_packet_bytes: int) -> int:
        """The KISS CRC32 shares libcsp's packet buffer with the payload, so it costs 4 bytes."""

        return max(0, max_packet_bytes - KISS_CRC32_BYTES)

    def _emit(self, packet: WholePacketFrame) -> WholePacketFrame:
        try:
            wire = self._protocol.encode_kiss(packet, max_packet_bytes=self._max_packet_bytes)
        except CodecError as exc:
            raise TransportError(f"packet cannot be sent as a KISS frame: {exc}") from exc
        self._line.write(wire)
        return self._protocol.with_wire(packet, wire, "serial")

    def _poll(self, timeout: float) -> Iterable[WholePacketFrame | TransportDrop]:
        data = self._line.read(timeout)
        if not data:
            return ()
        items: list[WholePacketFrame | TransportDrop] = []
        for event in self._decoder.feed(data):
            if event.packet is not None:
                packet = event.packet
                items.append(self._protocol.with_wire(packet, packet.wire, "serial"))
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
        self._line.close()


__all__ = ["KissAdapter"]
