from __future__ import annotations

import os
from collections import deque

import can
import pytest

from cubesat_testbed.config import InMemoryTransportConfig, SocketCanTransportConfig
from cubesat_testbed.protocol.csp_v2 import CspCanFrame
from cubesat_testbed.transport import (
    InMemoryBusAdapter,
    SocketCanAdapter,
    TransportEndpointError,
    TransportError,
    build_transport_adapter,
)


class FakeCanBus:
    def __init__(
        self,
        received: list[can.Message] | None = None,
        *,
        fail_send: bool = False,
        fail_recv: bool = False,
    ) -> None:
        self.received: deque[can.Message] = deque(received or [])
        self.sent: list[tuple[can.Message, float | None]] = []
        self.recv_timeouts: list[float | None] = []
        self.shutdown_called = False
        self.fail_send = fail_send
        self.fail_recv = fail_recv

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        if self.fail_send:
            raise can.CanError("send failed")
        self.sent.append((msg, timeout))

    def recv(self, timeout: float | None = None) -> can.Message | None:
        if self.fail_recv:
            raise can.CanError("receive failed")
        self.recv_timeouts.append(timeout)
        if not self.received:
            return None
        return self.received.popleft()

    def shutdown(self) -> None:
        self.shutdown_called = True


def test_send_writes_extended_classic_frame_to_python_can_bus() -> None:
    bus = FakeCanBus()
    adapter = SocketCanAdapter(interface="vcan0", bus=bus, send_timeout=0.25)
    frame = _frame()

    envelope = adapter.send(frame, source=1)

    assert envelope.frame == frame
    assert envelope.sequence == 0
    assert envelope.source == 1
    assert len(bus.sent) == 1

    message, timeout = bus.sent[0]
    assert message.arbitration_id == frame.can_id
    assert bytes(message.data) == frame.data
    assert message.dlc == frame.dlc
    assert message.is_extended_id is True
    assert message.is_fd is False
    assert message.is_remote_frame is False
    assert message.is_error_frame is False
    assert message.is_rx is False
    assert timeout == 0.25


def test_receive_converts_python_can_message_to_transport_envelope_without_blocking() -> None:
    frame = _frame()
    bus = FakeCanBus([_message(frame)])
    adapter = SocketCanAdapter(interface="vcan0", bus=bus)

    envelope = adapter.receive()

    assert envelope is not None
    assert envelope.frame == frame
    assert envelope.sequence == 0
    assert envelope.source is None
    assert bus.recv_timeouts == [0.0]
    assert adapter.receive() is None
    assert bus.recv_timeouts == [0.0, 0.0]


def test_receive_passes_a_deadline_timeout_through_to_the_underlying_bus() -> None:
    bus = FakeCanBus()
    adapter = SocketCanAdapter(interface="vcan0", bus=bus)

    assert adapter.receive(timeout=1.5) is None

    assert bus.recv_timeouts == [1.5]


def test_send_and_receive_share_adapter_local_sequence_order() -> None:
    frame = _frame()
    bus = FakeCanBus([_message(frame)])
    adapter = SocketCanAdapter(interface="vcan0", bus=bus)

    sent = adapter.send(frame, source="obc")
    received = adapter.receive()

    assert sent.sequence == 0
    assert received is not None
    assert received.sequence == 1


def test_endpoint_specific_receive_is_not_available_on_socketcan() -> None:
    adapter = SocketCanAdapter(interface="vcan0", bus=FakeCanBus())

    with pytest.raises(TransportEndpointError, match="default receive stream"):
        adapter.receive(endpoint="eps")


@pytest.mark.parametrize(
    ("message", "match"),
    [
        (
            can.Message(
                arbitration_id=0x123,
                is_extended_id=False,
                data=b"\x00",
                is_fd=False,
                check=True,
            ),
            "extended 29-bit",
        ),
        (
            can.Message(
                arbitration_id=0x10004083,
                is_extended_id=True,
                is_remote_frame=True,
                dlc=0,
                check=True,
            ),
            "remote frames",
        ),
        (
            can.Message(
                arbitration_id=0x10004083,
                is_extended_id=True,
                is_error_frame=True,
                dlc=0,
                check=True,
            ),
            "error frames",
        ),
        (
            can.Message(
                arbitration_id=0x10004083,
                is_extended_id=True,
                is_fd=True,
                data=bytes(range(9)),
                check=True,
            ),
            "CAN FD",
        ),
    ],
)
def test_receive_rejects_frames_outside_the_v1_socketcan_profile(
    message: can.Message,
    match: str,
) -> None:
    adapter = SocketCanAdapter(interface="vcan0", bus=FakeCanBus([message]))

    with pytest.raises(TransportError, match=match):
        adapter.receive()


def test_send_rejects_standard_can_ids_for_csp_v2_path() -> None:
    adapter = SocketCanAdapter(interface="vcan0", bus=FakeCanBus())
    frame = CspCanFrame(can_id=0x123, data=b"\x00", is_extended_id=False)

    with pytest.raises(TransportError, match="extended 29-bit"):
        adapter.send(frame)


def test_python_can_send_and_receive_errors_are_wrapped() -> None:
    send_adapter = SocketCanAdapter(interface="vcan0", bus=FakeCanBus(fail_send=True))
    recv_adapter = SocketCanAdapter(interface="vcan0", bus=FakeCanBus(fail_recv=True))

    with pytest.raises(TransportError, match="failed to send"):
        send_adapter.send(_frame())
    with pytest.raises(TransportError, match="failed to receive"):
        recv_adapter.receive()


def test_close_shuts_down_bus_and_prevents_reuse() -> None:
    bus = FakeCanBus()
    adapter = SocketCanAdapter(interface="vcan0", bus=bus)

    adapter.close()
    adapter.close()

    assert bus.shutdown_called is True
    assert adapter.closed is True
    with pytest.raises(TransportError, match="already closed"):
        adapter.send(_frame())


def test_transport_factory_builds_in_memory_adapter_from_config() -> None:
    adapter = build_transport_adapter(InMemoryTransportConfig(type="in-memory"), endpoints=(1, 2))

    assert isinstance(adapter, InMemoryBusAdapter)
    assert adapter.endpoints == (1, 2)


def test_transport_factory_builds_socketcan_adapter_from_config_with_injected_bus() -> None:
    bus = FakeCanBus()
    adapter = build_transport_adapter(
        SocketCanTransportConfig(
            type="socketcan",
            interface="vcan0",
            receive_own_messages=True,
        ),
        socketcan_bus=bus,
        socketcan_send_timeout=0.1,
    )

    assert isinstance(adapter, SocketCanAdapter)
    adapter.send(_frame())
    assert len(bus.sent) == 1
    assert bus.sent[0][1] == 0.1


@pytest.mark.socketcan
def test_socketcan_loopback_on_explicitly_configured_interface() -> None:
    interface = os.environ.get("CUBESAT_TESTBED_SOCKETCAN_INTERFACE")
    if interface is None:
        pytest.skip("set CUBESAT_TESTBED_SOCKETCAN_INTERFACE to run SocketCAN HIL smoke tests")

    frame = _frame()
    with SocketCanAdapter(
        interface=interface,
        receive_own_messages=True,
        send_timeout=0.1,
    ) as adapter:
        for _ in range(16):
            if adapter.receive() is None:
                break

        adapter.send(frame)
        received = None
        for _ in range(128):
            envelope = adapter.receive()
            if envelope is not None and envelope.frame == frame:
                received = envelope
                break

    assert received is not None


def _frame() -> CspCanFrame:
    return CspCanFrame(can_id=0x10004083, data=bytes.fromhex("00 04 14 80 55"))


def _message(frame: CspCanFrame) -> can.Message:
    return can.Message(
        arbitration_id=frame.can_id,
        is_extended_id=frame.is_extended_id,
        data=frame.data,
        is_fd=False,
        check=True,
    )
