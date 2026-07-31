"""Transport adapter interfaces and implementations."""

from cubesat_testbed.transport.base import (
    EndpointId,
    TransportAdapter,
    TransportEndpointError,
    TransportEnvelope,
    TransportError,
)
from cubesat_testbed.transport.factory import build_transport_adapter
from cubesat_testbed.transport.in_memory import InMemoryBusAdapter
from cubesat_testbed.transport.socketcan import CanBusLike, SocketCanAdapter
from cubesat_testbed.transport.tracing import (
    FrameAnnotator,
    TracingTransportAdapter,
    format_trace_line,
)

__all__ = [
    "CanBusLike",
    "EndpointId",
    "FrameAnnotator",
    "InMemoryBusAdapter",
    "SocketCanAdapter",
    "TracingTransportAdapter",
    "TransportAdapter",
    "TransportEndpointError",
    "TransportEnvelope",
    "TransportError",
    "build_transport_adapter",
    "format_trace_line",
]
