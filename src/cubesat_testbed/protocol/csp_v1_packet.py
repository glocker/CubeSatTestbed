"""CSP v1 packets that cross a link whole, with no CAN framing.

CAN carries a CSP v1 packet as one or several CFP1 frames (:mod:`csp_v1`). Serial KISS and
ZeroMQ carry it as one unit built around the 4-byte CSP v1 header.
:class:`CspV1PacketFrame` is that unit as the transport boundary sees it. This module
knows nothing about CAN identifiers or any link's framing, and nothing about CSP v2: the v2
whole-packet frame (:mod:`csp_v2_packet`) is a different type and the two are never mixed.
"""

from __future__ import annotations

from dataclasses import dataclass

from cubesat_testbed.protocol.csp_v1 import CspV1Fields, CspV1FrameError


@dataclass(frozen=True, slots=True)
class CspV1PacketFrame:
    """One whole CSP v1 packet, as a link without frames carries it.

    ``wire`` is the exact bytes the link put on the line for this packet (for KISS, the frame
    from ``FEND`` to ``FEND``; for ZeroMQ, the message), kept so a trace can show them. It is
    empty for a packet that never touched a line, such as one delivered between two simulated
    nodes. ``link`` names the link the bytes crossed (``serial``, ``zmq``) and is empty exactly
    when ``wire`` is.
    """

    fields: CspV1Fields
    payload: bytes
    wire: bytes = b""
    link: str = ""

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "payload", bytes(self.payload))
            object.__setattr__(self, "wire", bytes(self.wire))
        except TypeError as exc:
            raise CspV1FrameError("payload and wire must be bytes-like") from exc


__all__ = ["CspV1PacketFrame"]
