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
)
from cubesat_testbed.transport.factory import build_transport_adapter
from cubesat_testbed.transport.faults import (
    BusFaultEvent,
    FaultInjectingTransport,
    format_bus_fault,
    format_bus_fault_trace_line,
)
from cubesat_testbed.transport.in_memory import InMemoryBusAdapter
from cubesat_testbed.transport.kiss import KissAdapter
from cubesat_testbed.transport.serial_line import SerialLine, TermiosSerialLine
from cubesat_testbed.transport.socketcan import CanBusLike, SocketCanAdapter
from cubesat_testbed.transport.tracing import (
    FrameAnnotator,
    TracingTransportAdapter,
    format_trace_line,
)
from cubesat_testbed.transport.whole_packet import WholePacketAdapter
from cubesat_testbed.transport.whole_packet_protocol import WholePacketSender
from cubesat_testbed.transport.zmq import ZmqAdapter
from cubesat_testbed.transport.zmq_link import MessageLink, PyzmqMessageLink

__all__ = [
    "BusFaultEvent",
    "CanBusLike",
    "CanPacketSender",
    "EndpointId",
    "FaultInjectingTransport",
    "Frame",
    "FrameAnnotator",
    "InMemoryBusAdapter",
    "KissAdapter",
    "MessageLink",
    "PacketSender",
    "PyzmqMessageLink",
    "SerialLine",
    "SocketCanAdapter",
    "TermiosSerialLine",
    "TracingTransportAdapter",
    "TransportAdapter",
    "TransportDrop",
    "TransportEndpointError",
    "TransportEnvelope",
    "TransportError",
    "WholePacketAdapter",
    "WholePacketSender",
    "ZmqAdapter",
    "build_transport_adapter",
    "format_bus_fault",
    "format_bus_fault_trace_line",
    "format_trace_line",
]
