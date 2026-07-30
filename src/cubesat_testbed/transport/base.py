"""Transport adapter boundary.

v1 adapters carry CSP-over-CAN frames through either the deterministic in-memory
bus or Linux SocketCAN. Higher layers should depend on this boundary rather than
on a concrete transport implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from cubesat_testbed.protocol.csp_v2 import CspCanFrame

EndpointId = str | int


class TransportError(RuntimeError):
    """Base class for transport-boundary failures."""


class TransportEndpointError(TransportError, ValueError):
    """Raised when an endpoint identifier is invalid or not connected."""


@dataclass(frozen=True, slots=True)
class TransportEnvelope:
    """A CAN frame plus deterministic adapter metadata.

    ``sequence`` is assigned by the adapter and is local to that adapter instance.
    It provides a reproducible ordering key without depending on wall-clock time.
    """

    frame: CspCanFrame
    sequence: int
    source: EndpointId | None = None

    @property
    def can_id(self) -> int:
        """Convenience access to the wrapped frame's CAN identifier."""

        return self.frame.can_id

    @property
    def data(self) -> bytes:
        """Convenience access to the wrapped frame's raw CAN payload bytes."""

        return self.frame.data


class TransportAdapter(ABC):
    """Synchronous transport boundary used by simulated and HIL bus adapters.

    The boundary is deliberately non-blocking: ``receive`` returns ``None`` when
    no frame is currently available instead of sleeping or polling wall-clock
    time. Future event-engine integration can schedule calls into this interface
    using virtual time.
    """

    @abstractmethod
    def send(
        self,
        frame: CspCanFrame,
        *,
        source: EndpointId | None = None,
    ) -> TransportEnvelope:
        """Send one already encoded CSP-over-CAN frame.

        Implementations return the envelope that represents the accepted frame.
        The optional ``source`` identifies the logical sender when the adapter
        can model per-endpoint delivery.
        """

    @abstractmethod
    def receive(self, *, endpoint: EndpointId | None = None) -> TransportEnvelope | None:
        """Receive the next available frame envelope, or ``None`` if empty.

        ``endpoint=None`` represents the adapter's default receive stream. An
        implementation may also expose endpoint-specific streams for simulated
        nodes.
        """

    def drain(self, *, endpoint: EndpointId | None = None) -> tuple[TransportEnvelope, ...]:
        """Receive all currently available frames from a stream in order."""

        envelopes: list[TransportEnvelope] = []
        while True:
            envelope = self.receive(endpoint=endpoint)
            if envelope is None:
                return tuple(envelopes)
            envelopes.append(envelope)


__all__ = [
    "EndpointId",
    "TransportAdapter",
    "TransportEndpointError",
    "TransportEnvelope",
    "TransportError",
]
