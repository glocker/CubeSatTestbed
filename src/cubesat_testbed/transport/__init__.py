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

__all__ = [
    "CanBusLike",
    "EndpointId",
    "InMemoryBusAdapter",
    "SocketCanAdapter",
    "TransportAdapter",
    "TransportEndpointError",
    "TransportEnvelope",
    "TransportError",
    "build_transport_adapter",
]
