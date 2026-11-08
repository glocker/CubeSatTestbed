"""An in-process ZeroMQ hub for tests: the role libcsp's ``zmqproxy`` plays.

Clients publish to :attr:`publish_endpoint` (the hub's XSUB side, libcsp's port 6000)
and subscribe from :attr:`subscribe_endpoint` (its XPUB side, port 7000); everything
published is delivered to every subscriber, the sender included. Ports are chosen by
the OS so tests never collide with a real hub or each other.
"""

from __future__ import annotations

import threading
from typing import Self

import zmq


class InProcessHub:
    def __init__(self) -> None:
        self._context = zmq.Context()
        self._frontend = self._context.socket(zmq.XSUB)
        self._backend = self._context.socket(zmq.XPUB)
        front_port = self._frontend.bind_to_random_port("tcp://127.0.0.1")
        back_port = self._backend.bind_to_random_port("tcp://127.0.0.1")
        self.publish_endpoint = f"tcp://127.0.0.1:{front_port}"
        self.subscribe_endpoint = f"tcp://127.0.0.1:{back_port}"
        self._thread = threading.Thread(target=self._run, name="zmq-test-hub", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            zmq.proxy(self._frontend, self._backend)
        except zmq.ZMQError:
            pass  # the context was terminated by close()

    def close(self) -> None:
        self._frontend.close(linger=0)
        self._backend.close(linger=0)
        self._context.term()
        self._thread.join(timeout=2.0)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
