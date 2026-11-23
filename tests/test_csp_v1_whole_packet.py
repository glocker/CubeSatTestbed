"""CSP v1 over KISS and ZeroMQ: the codecs against the golden vectors, the adapters, and scenarios.

The vectors under ``tests/golden_vectors/csp_v1/{kiss,zmq}/`` are what libcsp ``v2.1``, run as CSP
v1, puts on a serial line and publishes to a hub; they are the oracle. The codecs are checked by
encoding the packet the generator asked for and expecting the captured bytes back, and by decoding
the captured bytes and expecting that packet.
"""

from __future__ import annotations

import ast
import io
import os
import pty
import struct
import tty
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

from cubesat_testbed.clock import RealTimeClock
from cubesat_testbed.config import parse_scenario, parse_testbed_config
from cubesat_testbed.protocol import csp_v1_kiss, csp_v1_packet, csp_v1_zmq
from cubesat_testbed.protocol.csp_v1 import CspV1CodecError, CspV1Fields
from cubesat_testbed.protocol.csp_v1_kiss import (
    KissDecoder,
    KissDropReason,
    crc32c,
    encode_kiss_frame,
)
from cubesat_testbed.protocol.csp_v1_packet import CspV1PacketFrame
from cubesat_testbed.protocol.csp_v1_zmq import (
    ZmqDropReason,
    decode_zmq_message,
    encode_zmq_message,
)
from cubesat_testbed.protocol.csp_v2 import CspFields
from cubesat_testbed.protocol.csp_v2_packet import CspPacketFrame
from cubesat_testbed.scenario import ScenarioRunner, build_runtime, run_scenario
from cubesat_testbed.transport import (
    KissAdapter,
    PyzmqMessageLink,
    TransportError,
    ZmqAdapter,
    build_transport_adapter,
)
from tests.golden_vector_loader import (
    CSP_V1_KISS_VECTOR_DIR,
    CSP_V1_ZMQ_VECTOR_DIR,
    load_kiss_vectors,
    load_zmq_vectors,
    parse_payload_hex,
)
from tests.zmq_hub import InProcessHub

KISS_VECTORS = load_kiss_vectors(CSP_V1_KISS_VECTOR_DIR)
ZMQ_VECTORS = load_zmq_vectors(CSP_V1_ZMQ_VECTOR_DIR)

PING = CspV1Fields(priority=2, source=1, destination=2, destination_port=1, source_port=18)


def _fields_of(metadata: dict[str, object], header_hex: str) -> CspV1Fields:
    from cubesat_testbed.protocol.csp_v1 import unpack_header

    return unpack_header(parse_payload_hex(header_hex))


# --- KISS codec vs the vectors -------------------------------------------------------------------


@pytest.mark.parametrize("vector", KISS_VECTORS, ids=[v.name for v in KISS_VECTORS])
def test_encoding_the_requested_packet_gives_the_captured_kiss_frames(vector) -> None:  # type: ignore[no-untyped-def]
    metadata = vector.metadata
    for index, frame in enumerate(vector.frames):
        fields = _fields_of(metadata, metadata["expected_csp_headers_hex"][index])
        payload = parse_payload_hex(metadata["expected_payloads_hex"][index])

        assert encode_kiss_frame(fields, payload) == frame


@pytest.mark.parametrize("vector", KISS_VECTORS, ids=[v.name for v in KISS_VECTORS])
@pytest.mark.parametrize("chunk", [1, 3, 4096])
def test_decoding_the_captured_frames_gives_the_requested_packets_in_any_chunking(
    vector,  # type: ignore[no-untyped-def]
    chunk: int,
) -> None:
    decoder = KissDecoder()
    wire = b"".join(vector.frames)

    events = []
    for offset in range(0, len(wire), chunk):
        events.extend(decoder.feed(wire[offset : offset + chunk]))

    assert [e.reason for e in events] == [None] * len(vector.frames)
    packets = [e.packet for e in events]
    assert all(isinstance(p, CspV1PacketFrame) for p in packets)
    metadata = vector.metadata
    for index, packet in enumerate(packets):
        assert packet is not None
        assert packet.wire == vector.frames[index]
        assert packet.payload == parse_payload_hex(metadata["expected_payloads_hex"][index])
        assert packet.fields == _fields_of(metadata, metadata["expected_csp_headers_hex"][index])
    assert not decoder.in_frame


def test_the_kiss_crc_is_crc32c_and_accepted_over_header_plus_payload_too() -> None:
    body = b"\x01\x02"
    from cubesat_testbed.protocol.csp_v1 import pack_header

    header = pack_header(PING)
    over_both = bytes((0xC0, 0x00)) + header + body + crc32c(header + body).to_bytes(4, "big")
    over_both += b"\xc0"
    assert crc32c(b"123456789") == 0xE3069283  # the CRC-32C check value

    (event,) = KissDecoder().feed(over_both)

    assert event.packet is not None and event.packet.payload == body


def test_the_kiss_limit_is_the_buffer_plus_the_four_byte_header() -> None:
    # libcsp drops a frame once header plus data reach max_packet_bytes + 4 bytes, one byte
    # short of the buffer: a 12-byte buffer still takes 7 payload bytes plus the CRC32.
    fields = PING

    def received(payload_length: int) -> list[KissDropReason | None]:
        frame = encode_kiss_frame(fields, bytes(payload_length), max_packet_bytes=64)
        return [e.reason for e in KissDecoder(max_packet_bytes=12).feed(frame)]

    assert received(7) == [None]
    assert received(8) == [KissDropReason.OVERFLOW]


def test_a_frame_with_a_bad_crc_a_short_frame_and_a_fragment_flag_are_dropped_visibly() -> None:
    good = bytearray(encode_kiss_frame(PING, b"\x01\x02"))
    bad_crc = bytearray(good)
    bad_crc[-3] ^= 0xFF
    short = bytes((0xC0, 0x00, 0x01, 0x02, 0xC0))
    fragment = encode_kiss_frame(CspV1Fields(2, 1, 2, 1, 18, 0x10), b"")

    reasons = [
        [e.reason for e in KissDecoder().feed(bytes(frame))] for frame in (bad_crc, short, fragment)
    ]

    assert reasons == [
        [KissDropReason.CRC_ERROR],
        [KissDropReason.TOO_SHORT],
        [KissDropReason.MALFORMED],
    ]


def test_the_kiss_encoder_refuses_a_payload_that_does_not_fit_with_its_crc() -> None:
    with pytest.raises(CspV1CodecError, match="max_packet_bytes=8"):
        encode_kiss_frame(PING, bytes(5), max_packet_bytes=8)
    encode_kiss_frame(PING, bytes(4), max_packet_bytes=8)


# --- ZeroMQ codec vs the vectors -----------------------------------------------------------------


@pytest.mark.parametrize("vector", ZMQ_VECTORS, ids=[v.name for v in ZMQ_VECTORS])
def test_the_zmq_codec_reproduces_and_reads_the_captured_messages(vector) -> None:  # type: ignore[no-untyped-def]
    metadata = vector.metadata
    for index, message in enumerate(vector.messages):
        fields = _fields_of(metadata, metadata["expected_csp_headers_hex"][index])
        payload = parse_payload_hex(metadata["expected_payloads_hex"][index])

        assert encode_zmq_message(fields, payload) == message
        event = decode_zmq_message(message)
        assert event.packet is not None
        assert (event.packet.fields, event.packet.payload) == (fields, payload)
        assert event.packet.wire == message and event.packet.link == "zmq"


def test_the_zmq_destination_byte_is_stripped_not_compared() -> None:
    message = bytearray(encode_zmq_message(PING, b"\x55"))
    message[0] = 0x1F  # libcsp strips the topic byte without looking at it

    event = decode_zmq_message(bytes(message))

    assert event.packet is not None and event.packet.fields == PING


def test_bad_zmq_messages_are_dropped_visibly() -> None:
    fragment = encode_zmq_message(CspV1Fields(2, 1, 2, 1, 18, 0x10), b"")

    assert decode_zmq_message(b"\x02\x82\x20").reason is ZmqDropReason.TOO_SHORT
    assert decode_zmq_message(fragment).reason is ZmqDropReason.MALFORMED
    event = decode_zmq_message(encode_zmq_message(PING, bytes(9)), max_packet_bytes=8)
    assert (event.reason, event.limit) == (ZmqDropReason.OVERFLOW, 8)
    with pytest.raises(CspV1CodecError, match="max_packet_bytes=8"):
        encode_zmq_message(PING, bytes(9), max_packet_bytes=8)


def test_the_v1_link_codecs_share_no_code_with_the_v2_ones() -> None:
    for module in (csp_v1_kiss, csp_v1_zmq, csp_v1_packet):
        source = Path(module.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
        imported = {
            alias.name if isinstance(node, ast.Import) else node.module or ""
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import | ast.ImportFrom)
            for alias in (node.names if isinstance(node, ast.Import) else [None])
        }
        assert not any("csp_v2" in name for name in imported), module.__name__


# --- the adapters --------------------------------------------------------------------------------


class _LoopLine:
    def __init__(self) -> None:
        self.incoming = bytearray()
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written += data

    def read(self, timeout: float = 0.0) -> bytes:
        data = bytes(self.incoming)
        self.incoming.clear()
        return data

    def close(self) -> None:
        pass


class _FakeLink:
    def __init__(self) -> None:
        self.incoming: list[bytes] = []
        self.sent: list[bytes] = []

    def send(self, message: bytes) -> None:
        self.sent.append(message)

    def receive(self, timeout: float = 0.0) -> bytes | None:
        return self.incoming.pop(0) if self.incoming else None

    def close(self) -> None:
        pass


def _packet(destination: int, payload: bytes = b"\x55") -> CspV1PacketFrame:
    fields = CspV1Fields(2, 1, destination, 1, 18)
    return CspV1PacketFrame(fields=fields, payload=payload)


def test_a_v1_kiss_adapter_keeps_local_packets_inside_and_puts_the_rest_on_the_line() -> None:
    line = _LoopLine()
    adapter = KissAdapter(line, local_addresses=[2], protocol_version=1)

    local = adapter.send(_packet(2), source=1)
    out = adapter.send(_packet(9, b"\xc0\x01"), source=1)

    assert adapter.receive(endpoint=2) is local
    expected = encode_kiss_frame(_packet(9).fields, b"\xc0\x01")
    assert bytes(line.written) == expected
    assert isinstance(out.frame, CspV1PacketFrame) and out.frame.wire == expected
    assert out.frame.link == "serial"


def test_a_v1_kiss_adapter_reads_frames_and_reports_what_it_rejects() -> None:
    line = _LoopLine()
    adapter = KissAdapter(line, local_addresses=[3], protocol_version=1)
    bad = bytearray(encode_kiss_frame(_packet(3).fields, b"\x07"))
    bad[-3] ^= 0xFF
    line.incoming += encode_kiss_frame(_packet(3, b"\x07").fields, b"\x07") + bytes(bad)

    envelope = adapter.receive()

    assert envelope is not None and isinstance(envelope.frame, CspV1PacketFrame)
    assert envelope.frame.payload == b"\x07" and envelope.frame.link == "serial"
    assert [d.reason for d in adapter.take_drops()] == ["crc_error"]


def test_a_v1_zmq_adapter_publishes_and_receives_v1_messages() -> None:
    link = _FakeLink()
    adapter = ZmqAdapter(link, local_addresses=[2], protocol_version=1)

    adapter.send(_packet(9), source=1)
    link.incoming.append(encode_zmq_message(CspV1Fields(2, 5, 2, 1, 18), b"\x07"))
    echo = encode_zmq_message(CspV1Fields(2, 2, 7, 1, 18), b"\x08")  # our own address: the echo
    link.incoming.append(echo)
    envelope = adapter.receive()

    assert link.sent == [encode_zmq_message(_packet(9).fields, b"\x55")]
    assert envelope is not None and isinstance(envelope.frame, CspV1PacketFrame)
    assert envelope.frame.payload == b"\x07" and envelope.frame.link == "zmq"
    assert adapter.receive() is None  # the echo was ignored


def test_an_adapter_refuses_a_packet_of_the_other_version() -> None:
    v2_packet = CspPacketFrame(
        fields=CspFields(priority=2, source=1, destination=9, destination_port=1, source_port=18),
        payload=b"\x55",
    )

    with pytest.raises(TransportError, match="speaks CSP v1"):
        KissAdapter(_LoopLine(), protocol_version=1).send(v2_packet)
    with pytest.raises(TransportError, match="speaks CSP v2"):
        ZmqAdapter(_FakeLink()).send(_packet(9))


def test_a_v1_sender_builds_one_v1_frame_and_refuses_what_v1_cannot_carry() -> None:
    adapter = KissAdapter(_LoopLine(), protocol_version=1)
    sender = adapter.new_sender(max_packet_bytes=12)
    fine = CspFields(priority=2, source=1, destination=2, destination_port=1, source_port=1)

    (frame,) = sender.frames(fine, b"\x01")

    assert isinstance(frame, CspV1PacketFrame)
    with pytest.raises(ValueError, match="cannot be carried by CSP v1"):
        sender.frames(
            CspFields(priority=2, source=1, destination=40, destination_port=1, source_port=1),
            b"",
        )
    with pytest.raises(ValueError, match="limit of 8 bytes"):
        sender.frames(fine, bytes(9))  # 12 - the 4-byte KISS CRC32


def test_the_factory_builds_v1_adapters_from_the_setup() -> None:
    setup = parse_testbed_config(
        "protocol_version = 1\n"
        '[transport]\ntype = "zmq"\npublish_endpoint = "tcp://127.0.0.1:6000"\n'
        'subscribe_endpoint = "tcp://127.0.0.1:7000"\nsettle_seconds = 0.0\n'
        '[nodes.a]\nmode = "software"\naddress = 1\n'
    )

    adapter = build_transport_adapter(setup.transport, protocol_version=setup.protocol_version)
    try:
        assert isinstance(adapter, ZmqAdapter)
        assert (
            adapter.new_sender()
            .frames(  # type: ignore[attr-defined]
                CspFields(2, 1, 2, 1, 1), b""
            )[0]
            .__class__
            is CspV1PacketFrame
        )
    finally:
        adapter.close()


# --- scenarios -----------------------------------------------------------------------------------

KISS_SETUP = """
protocol_version = 1

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

ZMQ_SETUP = KISS_SETUP.replace(
    '[transport]\ntype = "kiss"\ndevice = "{device}"',
    '[transport]\ntype = "zmq"\npublish_endpoint = "{publish}"\n'
    'subscribe_endpoint = "{subscribe}"\nsettle_seconds = 0.3',
)

SCENARIO = """
name: CSP v1 round trip
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

EPS_FIELDS = CspV1Fields(priority=2, source=2, destination=2, destination_port=20, source_port=20)
COMMAND_FIELDS = CspV1Fields(
    priority=2, source=1, destination=3, destination_port=10, source_port=10
)


class _Pty:
    def __init__(self) -> None:
        self.master, self._slave = pty.openpty()
        tty.setraw(self._slave)
        self.path = os.ttyname(self._slave)
        os.set_blocking(self.master, False)

    def pull(self, expected: int, timeout: float = 2.0) -> bytes:
        import time

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
def serial_pty() -> Iterator[_Pty]:
    line = _Pty()
    yield line
    line.close()


def test_a_v1_scenario_runs_over_a_kiss_line(serial_pty: _Pty) -> None:
    os.write(serial_pty.master, encode_kiss_frame(EPS_FIELDS, struct.pack(">f", 42.0)))
    setup = parse_testbed_config(KISS_SETUP.format(device=serial_pty.path))
    scenario = parse_scenario(SCENARIO, setup=setup)
    trace = io.StringIO()

    result = run_scenario(scenario, setup, output=io.StringIO(), trace=trace)

    assert result.passed and result.dropped_packets == ()
    expected = encode_kiss_frame(COMMAND_FIELDS, bytes(range(10)))
    assert serial_pty.pull(len(expected)) == expected
    lines = trace.getvalue().splitlines()
    assert any(
        " TX " in line
        and "link=serial" in line
        and "dst=3" in line
        and "command obc.payload_power_off" in line
        for line in lines
    )
    assert any(
        " RX " in line and "telemetry eps.telemetry.battery_percent=42.0" in line for line in lines
    )


def test_a_bad_v1_kiss_frame_is_reported_as_a_dropped_packet(serial_pty: _Pty) -> None:
    bad = bytearray(encode_kiss_frame(EPS_FIELDS, struct.pack(">f", 42.0)))
    bad[-3] ^= 0xFF
    os.write(serial_pty.master, bytes(bad))
    setup = parse_testbed_config(KISS_SETUP.format(device=serial_pty.path))
    scenario = parse_scenario(SCENARIO, setup=setup)

    result = run_scenario(scenario, setup, output=io.StringIO())

    assert not result.passed
    assert [(d.reason, d.sender, d.destination) for d in result.dropped_packets] == [
        ("crc_error", 2, 2)
    ]


def test_a_v1_packet_over_the_receiving_nodes_limit_is_dropped_on_kiss(serial_pty: _Pty) -> None:
    text = KISS_SETUP.format(device=serial_pty.path).replace(
        "address = 3\n", "address = 3\nmax_packet_bytes = 3\n", 1
    )
    setup = parse_testbed_config(text)
    scenario = parse_scenario(SCENARIO, setup=setup)
    to_payload = CspV1Fields(priority=2, source=2, destination=3, destination_port=9, source_port=9)
    os.write(serial_pty.master, encode_kiss_frame(to_payload, bytes(5)))
    os.write(serial_pty.master, encode_kiss_frame(EPS_FIELDS, struct.pack(">f", 42.0)))

    result = run_scenario(scenario, setup, output=io.StringIO())

    assert result.passed
    assert [(d.reason, d.sender, d.destination, d.limit) for d in result.dropped_packets] == [
        ("overflow", 2, 3, 3)
    ]


def test_a_v1_scenario_runs_through_a_zmq_hub() -> None:
    trace = io.StringIO()
    with InProcessHub() as hub:
        text = ZMQ_SETUP.format(publish=hub.publish_endpoint, subscribe=hub.subscribe_endpoint)
        peer = PyzmqMessageLink(hub.publish_endpoint, hub.subscribe_endpoint, settle_seconds=0.3)
        try:
            setup = parse_testbed_config(text)
            scenario = parse_scenario(SCENARIO, setup=setup)
            runtime = build_runtime(setup, clock=RealTimeClock(), trace=trace)
            try:
                peer.send(encode_zmq_message(EPS_FIELDS, struct.pack(">f", 42.0)))
                with warnings.catch_warnings():
                    warnings.simplefilter("error")  # the hub's echo must not look unrouted
                    result = ScenarioRunner(runtime, output=io.StringIO()).run(scenario)
            finally:
                runtime.transport.close()
            heard = []
            while (message := peer.receive(timeout=1.0)) is not None:
                heard.append(message)
        finally:
            peer.close()

    assert result.passed and result.dropped_packets == ()
    assert encode_zmq_message(COMMAND_FIELDS, bytes(range(10))) in heard
    lines = trace.getvalue().splitlines()
    assert any(
        " TX " in line and "link=zmq" in line and "command obc.payload_power_off" in line
        for line in lines
    )
    assert any(
        " RX " in line
        and "link=zmq" in line
        and "telemetry eps.telemetry.battery_percent=42.0" in line
        for line in lines
    )


def test_a_v1_zmq_message_that_is_too_short_is_reported_not_swallowed() -> None:
    with InProcessHub() as hub:
        text = ZMQ_SETUP.format(publish=hub.publish_endpoint, subscribe=hub.subscribe_endpoint)
        peer = PyzmqMessageLink(hub.publish_endpoint, hub.subscribe_endpoint, settle_seconds=0.3)
        try:
            setup = parse_testbed_config(text)
            scenario = parse_scenario(SCENARIO, setup=setup)
            runtime = build_runtime(setup, clock=RealTimeClock())
            try:
                peer.send(b"\x02\x82")
                peer.send(encode_zmq_message(EPS_FIELDS, struct.pack(">f", 42.0)))
                result = ScenarioRunner(runtime, output=io.StringIO()).run(scenario)
            finally:
                runtime.transport.close()
        finally:
            peer.close()

    assert result.passed
    assert [d.reason for d in result.dropped_packets] == ["too_short"]
