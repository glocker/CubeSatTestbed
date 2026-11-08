"""ZeroMQ hub connection for the ZMQ transport, through the optional ``pyzmq`` extra.

:class:`MessageLink` is the small surface the ZMQ adapter needs: publish a message,
read the next one that arrived. :class:`PyzmqMessageLink` implements it the way
libcsp's ``csp_zmqhub_init_w_endpoints`` does: a PUB socket *connected* to the hub's
subscribe port and a SUB socket, subscribed to everything, connected to its publish
port. ``pyzmq`` is imported only when a link is opened, so the base install does
not need it.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from cubesat_testbed.transport.base import TransportError


class MessageLink(Protocol):
    """The message pipe a ZMQ adapter talks over."""

    def send(self, message: bytes) -> None:
        """Publish one message."""

    def receive(self, timeout: float = 0.0) -> bytes | None:
        """Return the next message, waiting up to ``timeout`` seconds, or ``None``."""

    def close(self) -> None:
        """Release the link; safe to call more than once."""


def _import_zmq() -> Any:
    try:
        import zmq
    except ImportError as exc:
        raise TransportError(
            "the 'zmq' transport needs pyzmq; install it with: pip install 'cubesat-testbed[zmq]'"
        ) from exc
    return zmq


class PyzmqMessageLink:
    """A PUB/SUB client of a libcsp ``zmqproxy``-style hub.

    ``publish_endpoint`` is where this client publishes (the hub's *subscribe* port,
    6000 by default in libcsp) and ``subscribe_endpoint`` where it listens (the hub's
    *publish* port, 7000). ``settle_seconds`` is the wall-clock time given after
    connecting for the hub to learn the new subscriber and for this client to learn
    the hub's: ZeroMQ PUB/SUB silently drops anything sent before that, which is why
    libcsp's own examples sleep after connecting too. It is link setup, outside
    virtual time.
    """

    def __init__(
        self,
        publish_endpoint: str,
        subscribe_endpoint: str,
        *,
        settle_seconds: float = 0.5,
    ) -> None:
        zmq = _import_zmq()
        self._zmq = zmq
        self._context = zmq.Context()
        self._publisher = self._context.socket(zmq.PUB)
        self._subscriber = self._context.socket(zmq.SUB)
        self._closed = False
        try:
            self._subscriber.setsockopt(zmq.SUBSCRIBE, b"")
            self._publisher.connect(publish_endpoint)
            self._subscriber.connect(subscribe_endpoint)
        except zmq.ZMQError as exc:
            self.close()
            raise TransportError(
                f"failed to connect to the ZMQ hub ({publish_endpoint!r}, "
                f"{subscribe_endpoint!r}): {exc}"
            ) from exc
        if settle_seconds > 0:
            time.sleep(settle_seconds)

    def send(self, message: bytes) -> None:
        try:
            self._publisher.send(message)
        except self._zmq.ZMQError as exc:
            raise TransportError(f"failed to publish to the ZMQ hub: {exc}") from exc

    def receive(self, timeout: float = 0.0) -> bytes | None:
        try:
            if self._subscriber.poll(int(max(timeout, 0.0) * 1000)) == 0:
                return None
            message: bytes = self._subscriber.recv(self._zmq.NOBLOCK)
            return message
        except self._zmq.Again:
            return None
        except self._zmq.ZMQError as exc:
            raise TransportError(f"failed to read from the ZMQ hub: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._publisher.close(linger=0)
        self._subscriber.close(linger=0)
        self._context.term()


__all__ = ["MessageLink", "PyzmqMessageLink"]
