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

    ``receive`` defaults to non-blocking: it returns ``None`` immediately when
    no frame is available. Passing ``timeout`` lets a caller (the scenario
    runner, pacing a HIL run against a :class:`~cubesat_testbed.clock.Clock`)
    block up to ``timeout`` real seconds for a frame to arrive, returning as
    soon as one does instead of always waiting the full timeout. Adapters that
    cannot block (e.g. the in-memory bus, which is always instantaneous)
    ignore ``timeout``.
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
    def receive(
        self,
        *,
        endpoint: EndpointId | None = None,
        timeout: float | None = None,
    ) -> TransportEnvelope | None:
        """Receive the next available frame envelope, or ``None`` if empty.

        ``endpoint=None`` represents the adapter's default receive stream. An
        implementation may also expose endpoint-specific streams for simulated
        nodes. ``timeout`` is real seconds to block waiting for a frame before
        giving up (``None`` or ``0`` means a single non-blocking poll).
        """

    def drain(self, *, endpoint: EndpointId | None = None) -> tuple[TransportEnvelope, ...]:
        """Receive all currently available frames from a stream in order.

        Always a non-blocking poll: this drains what is already queued, it
        does not wait for more to arrive.
        """

        envelopes: list[TransportEnvelope] = []
        while True:
            envelope = self.receive(endpoint=endpoint)
            if envelope is None:
                return tuple(envelopes)
            envelopes.append(envelope)

    def close(self) -> None:
        """Release any operating-system resources this adapter holds.

        A no-op by default, since a purely in-process adapter holds none; a
        HIL adapter overrides it to hand its bus socket back. Must be safe to
        call more than once, so an owner can close unconditionally without
        checking whether someone else already did.
        """


__all__ = [
    "EndpointId",
    "TransportAdapter",
    "TransportEndpointError",
    "TransportEnvelope",
    "TransportError",
]
