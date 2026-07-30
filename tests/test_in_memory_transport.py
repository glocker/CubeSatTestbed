from __future__ import annotations

import pytest

from cubesat_testbed.protocol.csp_v2 import CspCanFrame
from cubesat_testbed.transport import (
    InMemoryBusAdapter,
    TransportAdapter,
    TransportEndpointError,
    TransportEnvelope,
)


def test_default_receive_stream_returns_sent_frames_in_order() -> None:
    bus: TransportAdapter = InMemoryBusAdapter()
    first = _frame(0x10004083, b"first")
    second = _frame(0x10004087, b"second")

    first_envelope = bus.send(first, source="obc")
    second_envelope = bus.send(second, source="eps")

    assert first_envelope.sequence == 0
    assert second_envelope.sequence == 1
    assert bus.receive() == first_envelope
    assert bus.receive() == second_envelope
    assert bus.receive() is None


def test_endpoint_receive_streams_preserve_deterministic_broadcast_order() -> None:
    bus = InMemoryBusAdapter(endpoints=("obc", "eps", "payload"))
    from_obc = _frame(0x10004083, b"obc")
    from_eps = _frame(0x10004087, b"eps")
    from_external = _frame(0x1000408B, b"ext")

    bus.send(from_obc, source="obc")
    bus.send(from_eps, source="eps")
    bus.send(from_external)

    assert _frames(bus.drain(endpoint="payload")) == [from_obc, from_eps, from_external]
    assert _frames(bus.drain(endpoint="obc")) == [from_eps, from_external]
    assert _frames(bus.drain(endpoint="eps")) == [from_obc, from_external]
    assert _frames(bus.drain()) == [from_obc, from_eps, from_external]


def test_integer_endpoint_ids_are_supported_for_csp_addresses() -> None:
    bus = InMemoryBusAdapter(endpoints=(1, 2))
    frame = _frame(0x10004083, b"ping")

    bus.send(frame, source=1)

    assert bus.receive(endpoint=1) is None
    envelope = bus.receive(endpoint=2)
    assert envelope is not None
    assert envelope.frame == frame
    assert envelope.source == 1


def test_connecting_endpoint_does_not_replay_existing_monitor_backlog() -> None:
    bus = InMemoryBusAdapter()
    old_frame = _frame(0x10004083, b"old")
    new_frame = _frame(0x10004087, b"new")

    bus.send(old_frame)
    bus.connect("eps")
    bus.send(new_frame)

    assert _frames(bus.drain(endpoint="eps")) == [new_frame]
    assert _frames(bus.drain()) == [old_frame, new_frame]


def test_receive_from_unknown_endpoint_fails_loudly() -> None:
    bus = InMemoryBusAdapter()

    with pytest.raises(TransportEndpointError, match="not connected"):
        bus.receive(endpoint="missing")


def test_empty_receive_is_non_blocking_and_returns_none() -> None:
    bus = InMemoryBusAdapter(endpoints=("eps",))

    assert bus.receive() is None
    assert bus.receive(endpoint="eps") is None


def _frame(can_id: int, payload: bytes) -> CspCanFrame:
    return CspCanFrame(can_id=can_id, data=payload)


def _frames(envelopes: tuple[TransportEnvelope, ...]) -> list[CspCanFrame]:
    return [envelope.frame for envelope in envelopes]
