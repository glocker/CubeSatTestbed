"""Transport adapter interfaces and implementations."""

from cubesat_testbed.transport.base import (
    CanPacketSender,
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
from cubesat_testbed.transport.factory import build_transport_adapter
from cubesat_testbed.transport.in_memory import InMemoryBusAdapter
from cubesat_testbed.transport.kiss import KissAdapter
from cubesat_testbed.transport.serial_line import SerialLine, TermiosSerialLine
from cubesat_testbed.transport.socketcan import CanBusLike, SocketCanAdapter
from cubesat_testbed.transport.tracing import (
    FrameAnnotator,
    TracingTransportAdapter,
    format_trace_line,
)

__all__ = [
    "CanBusLike",
    "CanPacketSender",
    "EndpointId",
    "Frame",
    "FrameAnnotator",
    "InMemoryBusAdapter",
    "KissAdapter",
    "PacketSender",
    "SerialLine",
    "SocketCanAdapter",
    "TermiosSerialLine",
    "TracingTransportAdapter",
    "TransportAdapter",
    "TransportDrop",
    "TransportEndpointError",
    "TransportEnvelope",
    "TransportError",
    "WholePacketSender",
    "build_transport_adapter",
    "format_trace_line",
]
