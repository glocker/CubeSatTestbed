"""In-memory CSP bus adapter for deterministic tests and CI.

This adapter does not require Linux SocketCAN, root privileges, background
threads, wall-clock sleeps, or polling loops. Delivery is synchronous: a call to
``send`` immediately appends the frame to deterministic in-process queues.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from cubesat_testbed.protocol.csp_v2 import CspCanFrame
from cubesat_testbed.transport.base import (
    EndpointId,
    TransportAdapter,
    TransportEndpointError,
    TransportEnvelope,
)


class InMemoryBusAdapter(TransportAdapter):
    """Deterministic in-process CAN-like broadcast bus.

    The default receive stream (``endpoint=None``) acts as a bus monitor and sees
    every sent frame in send order. Connected endpoints receive frames sent after
    they connect; a sender does not receive its own frame on its endpoint queue.
    """

    def __init__(self, *, endpoints: Iterable[EndpointId] = ()) -> None:
        self._monitor_queue: deque[TransportEnvelope] = deque()
        self._endpoint_queues: dict[EndpointId, deque[TransportEnvelope]] = {}
        self._next_sequence = 0

        for endpoint in endpoints:
            self.connect(endpoint)

    @property
    def endpoints(self) -> tuple[EndpointId, ...]:
        """Connected endpoint identifiers in deterministic connection order."""

        return tuple(self._endpoint_queues.keys())

    def connect(self, endpoint: EndpointId) -> None:
        """Create an endpoint-specific receive queue.

        Endpoint queues only receive frames sent after the endpoint is connected;
        historical bus-monitor backlog is not replayed.
        """

        endpoint = _validate_endpoint(endpoint)
        if endpoint in self._endpoint_queues:
            raise TransportEndpointError(f"endpoint {endpoint!r} is already connected")
        self._endpoint_queues[endpoint] = deque()

    def connect_endpoint(self, endpoint: EndpointId) -> None:
        """Alias for ``connect`` for call sites that prefer explicit naming."""

        self.connect(endpoint)

    def disconnect(self, endpoint: EndpointId) -> None:
        """Remove an endpoint-specific receive queue."""

        endpoint = _validate_endpoint(endpoint)
        if endpoint not in self._endpoint_queues:
            raise TransportEndpointError(f"endpoint {endpoint!r} is not connected")
        del self._endpoint_queues[endpoint]

    def disconnect_endpoint(self, endpoint: EndpointId) -> None:
        """Alias for ``disconnect`` for call sites that prefer explicit naming."""

        self.disconnect(endpoint)

    def send(
        self,
        frame: CspCanFrame,
        *,
        source: EndpointId | None = None,
    ) -> TransportEnvelope:
        """Synchronously broadcast one CSP-over-CAN frame to receive queues."""

        if source is not None:
            source = _validate_endpoint(source)

        envelope = TransportEnvelope(frame=frame, sequence=self._next_sequence, source=source)
        self._next_sequence += 1

        self._monitor_queue.append(envelope)
        for endpoint, queue in self._endpoint_queues.items():
            if endpoint != source:
                queue.append(envelope)

        return envelope

    def receive(self, *, endpoint: EndpointId | None = None) -> TransportEnvelope | None:
        """Return the next queued frame for a stream, or ``None`` when empty."""

        queue = self._queue_for(endpoint)
        if not queue:
            return None
        return queue.popleft()

    def pending_count(self, *, endpoint: EndpointId | None = None) -> int:
        """Return the number of frames currently queued for a stream."""

        return len(self._queue_for(endpoint))

    def clear(self, *, endpoint: EndpointId | None = None) -> None:
        """Drop currently queued frames from one stream without changing endpoints."""

        self._queue_for(endpoint).clear()

    def _queue_for(self, endpoint: EndpointId | None) -> deque[TransportEnvelope]:
        if endpoint is None:
            return self._monitor_queue

        endpoint = _validate_endpoint(endpoint)
        try:
            return self._endpoint_queues[endpoint]
        except KeyError as exc:
            raise TransportEndpointError(f"endpoint {endpoint!r} is not connected") from exc


def _validate_endpoint(endpoint: EndpointId) -> EndpointId:
    if isinstance(endpoint, bool):
        raise TransportEndpointError("endpoint id must be a non-bool string or integer")
    if isinstance(endpoint, str):
        if endpoint == "":
            raise TransportEndpointError("endpoint id must not be an empty string")
        return endpoint
    if isinstance(endpoint, int):
        if endpoint < 0:
            raise TransportEndpointError("integer endpoint id must be non-negative")
        return endpoint
    raise TransportEndpointError("endpoint id must be a string or integer")


__all__ = ["InMemoryBusAdapter"]
