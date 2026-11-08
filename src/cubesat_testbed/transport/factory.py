"""Transport adapter factory for validated testbed setup configs."""

from __future__ import annotations

from collections.abc import Iterable

from cubesat_testbed.config import (
    InMemoryTransportConfig,
    KissTransportConfig,
    SocketCanTransportConfig,
    TransportConfig,
    ZmqTransportConfig,
)
from cubesat_testbed.transport.base import EndpointId, TransportAdapter
from cubesat_testbed.transport.in_memory import InMemoryBusAdapter
from cubesat_testbed.transport.kiss import KissAdapter
from cubesat_testbed.transport.socketcan import CanBusLike, SocketCanAdapter
from cubesat_testbed.transport.zmq import ZmqAdapter


def build_transport_adapter(
    config: TransportConfig,
    *,
    endpoints: Iterable[EndpointId] = (),
    socketcan_bus: CanBusLike | None = None,
    socketcan_send_timeout: float | None = 0.0,
    local_addresses: Iterable[int] = (),
    max_packet_bytes: int = 256,
) -> TransportAdapter:
    """Build the concrete transport adapter described by ``config``.

    ``endpoints`` applies only to ``in-memory`` transports. For ``socketcan``
    configs, ``interface`` and ``receive_own_messages`` are taken from the
    validated config; ``socketcan_bus`` exists for tests or advanced callers that
    already own a ``python-can`` bus object.

    ``local_addresses`` and ``max_packet_bytes`` apply only to the whole-packet links
    (``kiss``, ``zmq``): the CSP addresses of the nodes the testbed simulates
    (delivered locally, never put on the link) and the receive buffer applied to
    what is read from it.
    """

    if isinstance(config, InMemoryTransportConfig):
        return InMemoryBusAdapter(endpoints=endpoints)
    if isinstance(config, SocketCanTransportConfig):
        return SocketCanAdapter(
            interface=config.interface,
            receive_own_messages=config.receive_own_messages,
            send_timeout=socketcan_send_timeout,
            bus=socketcan_bus,
        )
    if isinstance(config, KissTransportConfig):
        return KissAdapter.open(
            config.device,
            baudrate=config.baudrate,
            local_addresses=local_addresses,
            max_packet_bytes=max_packet_bytes,
        )
    if isinstance(config, ZmqTransportConfig):
        return ZmqAdapter.open(
            config.publish_endpoint,
            config.subscribe_endpoint,
            settle_seconds=config.settle_seconds,
            local_addresses=local_addresses,
            max_packet_bytes=max_packet_bytes,
        )
    raise TypeError(f"unsupported transport config type {type(config).__name__}")


__all__ = ["build_transport_adapter"]
