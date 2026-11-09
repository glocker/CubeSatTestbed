"""KISS transport: serial line, adapter routing, and scenarios over a real pty."""

from __future__ import annotations

import io
import json
import os
import pty
import struct
import time
import tty
from collections.abc import Iterator
from pathlib import Path

import pytest

from cubesat_testbed.config import KissTransportConfig, parse_scenario, parse_testbed_config
from cubesat_testbed.main import main
from cubesat_testbed.protocol.csp_v2 import CspFields, CspV2FrameError
from cubesat_testbed.protocol.csp_v2_kiss import KissDecoder, encode_kiss_frame
from cubesat_testbed.protocol.csp_v2_packet import CspPacketFrame
from cubesat_testbed.scenario import run_scenario
from cubesat_testbed.transport import (
    KissAdapter,
    TermiosSerialLine,
    TransportError,
    build_transport_adapter,
)

PING = CspFields(priority=2, source=1, destination=2, destination_port=1, source_port=18)


class LoopLine:
    """An in-process serial line: what the test pushes is read, what is written is kept."""

    def __init__(self) -> None:
        self.incoming = bytearray()
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    def read(self, timeout: float = 0.0) -> bytes:
        data = bytes(self.incoming)
        self.incoming.clear()
        return data

    def close(self) -> None:
        self.closed = True


class Pty:
    """A pty pair: ``path`` is what the testbed opens, ``master`` is the far end."""

    def __init__(self) -> None:
        self.master, self._slave = pty.openpty()
        # Raw before anything is pushed: a cooked pty would echo and translate the bytes.
        tty.setraw(self._slave)
        self.path = os.ttyname(self._slave)

    def push(self, data: bytes) -> None:
        os.write(self.master, data)

    def pull(self, expected: int, timeout: float = 2.0) -> bytes:
        deadline = time.monotonic() + timeout
        data = b""
        while len(data) < expected and time.monotonic() < deadline:
            try:
                data += os.read(self.master, 4096)
            except BlockingIOError:
                time.sleep(0.01)
        return data

    def close(self) -> None:
        os.close(self.master)
        os.close(self._slave)


@pytest.fixture
def serial_pty() -> Iterator[Pty]:
    line = Pty()
    os.set_blocking(line.master, False)
    yield line
    line.close()


def _packet(destination: int, payload: bytes = b"\x55") -> CspPacketFrame:
    fields = CspFields(
        priority=2, source=1, destination=destination, destination_port=1, source_port=18
    )
    return CspPacketFrame(fields=fields, payload=payload)


# --- the line -------------------------------------------------------------------------


def test_termios_line_moves_bytes_both_ways_over_a_pty(serial_pty: Pty) -> None:
    line = TermiosSerialLine.open(serial_pty.path, baudrate=115200)
    try:
        line.write(b"\xc0\x00hello\xc0")
        assert serial_pty.pull(8) == b"\xc0\x00hello\xc0"

        serial_pty.push(b"\x01\x02\x03")
        assert line.read(timeout=1.0) == b"\x01\x02\x03"
        assert line.read(timeout=0.0) == b""
    finally:
        line.close()
        line.close()  # safe to repeat


def test_termios_line_is_raw_so_control_bytes_pass_untouched(serial_pty: Pty) -> None:
    line = TermiosSerialLine.open(serial_pty.path)
    try:
        payload = bytes(range(256))
        serial_pty.push(payload)
        received = b""
        deadline = time.monotonic() + 2.0
        while len(received) < 256 and time.monotonic() < deadline:
            received += line.read(timeout=0.1)
        assert received == payload
    finally:
        line.close()


def test_termios_line_rejects_a_missing_device_and_an_unsupported_baudrate(
    serial_pty: Pty, tmp_path: Path
) -> None:
    with pytest.raises(TransportError, match="failed to open serial device"):
        TermiosSerialLine.open(str(tmp_path / "nope"))
    with pytest.raises(TransportError, match="unsupported baudrate 12345"):
        TermiosSerialLine.open(serial_pty.path, baudrate=12345)


# --- the adapter ---------------------------------------------------------------------


def test_packet_for_a_local_node_stays_inside_the_testbed() -> None:
    line = LoopLine()
    adapter = KissAdapter(line, local_addresses=[2, 3])

    envelope = adapter.send(_packet(2), source=1)

    assert line.written == b""
    assert adapter.receive(endpoint=2) is envelope
    assert adapter.receive(endpoint=3) is None
    assert adapter.receive() is envelope  # the monitor stream sees it too
    assert isinstance(envelope.frame, CspPacketFrame) and envelope.frame.wire == b""


def test_packet_for_a_remote_node_goes_out_as_a_kiss_frame_and_is_not_echoed() -> None:
    line = LoopLine()
    adapter = KissAdapter(line, local_addresses=[2])

    envelope = adapter.send(_packet(9, b"\xc0\x01"), source=1)

    expected = encode_kiss_frame(_packet(9).fields, b"\xc0\x01")
    assert bytes(line.written) == expected
    assert isinstance(envelope.frame, CspPacketFrame) and envelope.frame.wire == expected
    assert adapter.receive() is None
    assert adapter.receive(endpoint=2) is None


def test_sequence_numbers_keep_counting_across_local_and_line_sends() -> None:
    adapter = KissAdapter(LoopLine(), local_addresses=[2])

    sequences = [adapter.send(_packet(d), source=1).sequence for d in (2, 9, 2)]

    assert sequences == [0, 1, 2]


def test_frames_arriving_on_the_line_reach_the_monitor_and_the_addressed_endpoint() -> None:
    line = LoopLine()
    adapter = KissAdapter(line, local_addresses=[2, 3])
    line.incoming += encode_kiss_frame(_packet(3, b"\x07").fields, b"\x07")

    monitor = adapter.receive()
    to_three = adapter.receive(endpoint=3)

    assert isinstance(monitor.frame, CspPacketFrame)  # type: ignore[union-attr]
    assert monitor.frame.payload == b"\x07"  # type: ignore[union-attr]
    assert to_three is not None and adapter.receive(endpoint=2) is None


def test_frames_split_across_reads_are_reassembled() -> None:
    line = LoopLine()
    adapter = KissAdapter(line, local_addresses=[3])
    wire = encode_kiss_frame(_packet(3).fields, b"\x07\x08")

    line.incoming += wire[:5]
    assert adapter.receive() is None
    line.incoming += wire[5:]

    assert adapter.receive() is not None


def test_rejected_frames_are_reported_by_take_drops_then_forgotten() -> None:
    line = LoopLine()
    adapter = KissAdapter(line, local_addresses=[3])
    bad = bytearray(encode_kiss_frame(_packet(3).fields, b"\x07"))
    bad[-3] ^= 0xFF
    line.incoming += bytes(bad)

    assert adapter.receive() is None
    (drop,) = adapter.take_drops()

    assert (drop.reason, drop.source, drop.destination) == ("crc_error", 1, 3)
    assert adapter.take_drops() == ()


def test_adapter_refuses_can_frames_and_oversized_packets_and_closes_once() -> None:
    from cubesat_testbed.protocol.csp_v2 import pack

    line = LoopLine()
    adapter = KissAdapter(line, local_addresses=[2])

    with pytest.raises(TransportError, match="not CAN frames"):
        adapter.send(pack(PING, b"\x01"))
    with pytest.raises(TransportError, match="max_packet_bytes=256"):
        adapter.send(_packet(9, bytes(253)))
    adapter.close()
    adapter.close()
    assert line.closed and adapter.closed
    with pytest.raises(TransportError, match="closed"):
        adapter.send(_packet(2))


def test_new_sender_builds_one_whole_packet_frame_and_enforces_the_limit() -> None:
    sender = KissAdapter(LoopLine()).new_sender(max_packet_bytes=12)

    (frame,) = sender.frames(PING, bytes(8))  # 12 bytes of buffer minus the 4-byte CRC32
    assert isinstance(frame, CspPacketFrame) and frame.payload == bytes(8)
    with pytest.raises(CspV2FrameError, match=r"limit of 8 bytes \(max_packet_bytes=12\)"):
        sender.frames(PING, bytes(9))


def test_kiss_payload_limit_leaves_room_for_the_crc32() -> None:
    adapter = KissAdapter(LoopLine())

    assert adapter.payload_limit(256) == 252
    assert adapter.payload_limit(4) == 0
    assert adapter.payload_limit(2) == 0  # never negative


def test_every_payload_the_sender_accepts_can_actually_be_put_on_the_line() -> None:
    """The sender's limit and the encoder's must agree, or a run dies mid-scenario."""

    line = LoopLine()
    adapter = KissAdapter(line, local_addresses=[2])
    sender = adapter.new_sender(max_packet_bytes=256)

    for length in (0, 1, 251, 252):
        (frame,) = sender.frames(PING, bytes(length))
        adapter.send(frame, source=1)  # does not raise
    for length in (253, 256):
        with pytest.raises(CspV2FrameError):  # refused up front, as a clear error
            sender.frames(PING, bytes(length))


def test_factory_builds_a_kiss_adapter_from_config(serial_pty: Pty) -> None:
    config = KissTransportConfig(type="kiss", device=serial_pty.path, baudrate=9600)

    adapter = build_transport_adapter(config, local_addresses=[2])
    try:
        assert isinstance(adapter, KissAdapter)
    finally:
        adapter.close()


# --- config --------------------------------------------------------------------------


def test_setup_parses_a_kiss_transport_with_a_default_baudrate() -> None:
    setup = parse_testbed_config(
        """
        [transport]
        type = "kiss"
        device = "/dev/ttyUSB0"

        [nodes.obc]
        mode = "simulated"
        module_type = "obc_peer"
        address = 1

        [nodes.eps]
        mode = "hardware"
        address = 2
        """
    )

    assert setup.transport == KissTransportConfig(
        type="kiss", device="/dev/ttyUSB0", baudrate=115200
    )


def _command_setup(transport: str, target_mode: str, payload_len: int) -> str:
    return f"""
[transport]
{transport}

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.payload]
mode = "{target_mode}"
address = 3
{'module_type = "simple_payload"' if target_mode == "simulated" else ""}

[nodes.obc.commands.big]
target = "payload"
destination_port = 10
source_port = 10
payload_hex = "{"00" * payload_len}"
"""


@pytest.mark.parametrize(("length", "accepted"), [(252, True), (253, False), (256, False)])
def test_config_applies_the_crc32_overhead_to_commands_that_leave_on_the_line(
    length: int, accepted: bool
) -> None:
    text = _command_setup('type = "kiss"\ndevice = "/dev/ttyUSB0"', "software", length)

    if accepted:
        parse_testbed_config(text)
    else:
        with pytest.raises(ValueError, match=r"leaving 252 for the payload"):
            parse_testbed_config(text)


def test_config_does_not_charge_the_crc32_to_a_command_that_stays_inside_the_testbed() -> None:
    parse_testbed_config(_command_setup('type = "kiss"\ndevice = "/dev/ttyUSB0"', "simulated", 256))


def test_the_crc32_overhead_is_a_kiss_matter_not_a_general_one() -> None:
    parse_testbed_config(_command_setup('type = "in-memory"', "software", 256))


# --- scenarios over a pty ------------------------------------------------------------

SETUP = """
[transport]
type = "kiss"
device = "{device}"

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "software"
address = 2

[nodes.payload]
mode = "software"
address = 3
max_packet_bytes = 256

[nodes.obc.commands.payload_power_off]
target = "payload"
destination_port = 10
source_port = 10
payload_hex = "00 01 02 03 04 05 06 07 08 09"

[nodes.eps.telemetry.battery_percent]
source_port = 20
destination_port = 20
offset = 0
length = 4
type = "float"
"""

SCENARIO = """
name: KISS round trip
steps:
  - action: send_command
    command: payload_power_off
    source: obc
    target: payload
  - action: wait
    virtual_time: 1s
  - action: assert
    signal: eps.telemetry.battery_percent
    op: "=="
    value: 42.0
    timeout: 1s
"""


def _eps_telemetry_frame(value: float = 42.0) -> bytes:
    fields = CspFields(priority=2, source=2, destination=2, destination_port=20, source_port=20)
    return encode_kiss_frame(fields, struct.pack(">f", value))


def _run(device: str, *, trace: io.StringIO | None = None):  # type: ignore[no-untyped-def]
    setup = parse_testbed_config(SETUP.format(device=device))
    scenario = parse_scenario(SCENARIO, setup=setup)
    return run_scenario(scenario, setup, output=io.StringIO(), trace=trace)


def test_a_scenario_sends_a_long_command_and_reads_telemetry_over_the_line(
    serial_pty: Pty,
) -> None:
    serial_pty.push(_eps_telemetry_frame(42.0))
    trace = io.StringIO()

    result = _run(serial_pty.path, trace=trace)

    assert result.passed and result.dropped_packets == ()
    # The command left on the wire as one KISS frame, decoded back by the reference decoder.
    expected = encode_kiss_frame(
        CspFields(priority=2, source=1, destination=3, destination_port=10, source_port=10),
        bytes(range(10)),
    )
    assert serial_pty.pull(len(expected)) == expected

    lines = trace.getvalue().splitlines()
    tx = [line for line in lines if " TX " in line]
    assert any(
        "link=serial" in line and "dst=3" in line and "command obc.payload_power_off" in line
        for line in tx
    )
    rx = [
        line
        for line in lines
        if " RX " in line and "telemetry eps.telemetry.battery_percent=42.0" in line
    ]
    assert rx and "link=serial" in rx[0]


def test_a_bad_frame_on_the_line_is_reported_as_a_dropped_packet(serial_pty: Pty) -> None:
    bad = bytearray(_eps_telemetry_frame(42.0))
    bad[-3] ^= 0xFF
    serial_pty.push(bytes(bad))
    trace = io.StringIO()

    result = _run(serial_pty.path, trace=trace)

    assert not result.passed  # the telemetry never arrived
    assert [(d.reason, d.sender, d.destination) for d in result.dropped_packets] == [
        ("crc_error", 2, 2)
    ]
    assert any(
        line.startswith("trace DROP") and "reason=crc_error" in line
        for line in trace.getvalue().splitlines()
    )


def test_a_packet_over_the_receiving_nodes_limit_is_dropped_not_delivered(
    serial_pty: Pty,
) -> None:
    text = SETUP.format(device=serial_pty.path).replace(
        "address = 3\nmax_packet_bytes = 256", "address = 3\nmax_packet_bytes = 3"
    )
    setup = parse_testbed_config(text)
    scenario = parse_scenario(SCENARIO, setup=setup)
    to_payload = CspFields(priority=2, source=2, destination=3, destination_port=9, source_port=9)
    serial_pty.push(encode_kiss_frame(to_payload, bytes(5)))
    serial_pty.push(_eps_telemetry_frame(42.0))

    result = run_scenario(scenario, setup, output=io.StringIO())

    assert result.passed  # the telemetry for eps still arrived
    assert [(d.reason, d.sender, d.destination, d.limit) for d in result.dropped_packets] == [
        ("overflow", 2, 3, 3)
    ]


def test_a_scenario_sends_the_largest_command_the_link_allows(serial_pty: Pty) -> None:
    setup = parse_testbed_config(
        _command_setup(f'type = "kiss"\ndevice = "{serial_pty.path}"', "software", 252)
    )
    scenario = parse_scenario(
        "name: big\nsteps:\n  - action: send_command\n    command: big\n"
        "    source: obc\n    target: payload\n  - action: wait\n    virtual_time: 1s\n",
        setup=setup,
    )

    result = run_scenario(scenario, setup, output=io.StringIO())

    wire = serial_pty.pull(252 + 6 + 4 + 3)
    # Decoded with a roomier buffer: libcsp's own receive side drops this very frame
    # (see the next test), an asymmetry in libcsp itself that the testbed reproduces.
    (event,) = KissDecoder(max_packet_bytes=1000).feed(wire)
    assert result.dropped_packets == ()
    assert event.packet is not None and event.packet.payload == bytes(252)


def test_the_receive_side_drops_a_frame_at_the_same_boundary_a_libcsp_peer_does(
    serial_pty: Pty,
) -> None:
    to_payload = CspFields(priority=2, source=2, destination=3, destination_port=9, source_port=9)
    setup = parse_testbed_config(
        _command_setup(f'type = "kiss"\ndevice = "{serial_pty.path}"', "simulated", 1)
    )
    scenario = parse_scenario(
        "name: rx\nsteps:\n  - action: wait\n    virtual_time: 1s\n", setup=setup
    )
    for length in (251, 252):
        serial_pty.push(encode_kiss_frame(to_payload, bytes(length), max_packet_bytes=1000))

    result = run_scenario(scenario, setup, output=io.StringIO())

    assert [(d.reason, d.limit) for d in result.dropped_packets] == [("overflow", 256)]


def test_kiss_runs_are_identical_across_repeats() -> None:
    results = []
    for _ in range(2):
        line = Pty()
        os.set_blocking(line.master, False)
        try:
            line.push(_eps_telemetry_frame(42.0))
            trace = io.StringIO()
            results.append((_run(line.path, trace=trace), trace.getvalue()))
        finally:
            line.close()

    assert results[0] == results[1]


def test_cli_runs_over_a_pty_traces_the_serial_frames_and_warns_without_realtime(
    serial_pty: Pty, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup_path = tmp_path / "setup.toml"
    scenario_path = tmp_path / "scenario.yaml"
    setup_path.write_text(SETUP.format(device=serial_pty.path), encoding="utf-8")
    scenario_path.write_text(SCENARIO, encoding="utf-8")
    serial_pty.push(_eps_telemetry_frame(42.0))

    code = main(
        ["run", "--config", str(setup_path), "--scenario", str(scenario_path), "--trace", "--json"]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["passed"] is True
    assert "link=serial" in captured.err
    assert "transport.type='kiss'" in captured.err and "without --realtime" in captured.err
    assert KissDecoder  # imported for the reference decoder used above
