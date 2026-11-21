"""CSP v1 selected from the setup: config, adapters, the runner, traces and bus faults.

The codec itself is tested in ``test_csp_v1.py`` against the golden vectors; here the
question is that a setup with ``protocol_version = 1`` runs end to end on CFP1 frames, never
on CFP2 ones, and that v2 setups are untouched.
"""

from __future__ import annotations

import io
import json
import re
from collections import deque
from pathlib import Path

import can
import pytest
from pydantic import ValidationError

import cubesat_testbed.scenario.runner as runner_module
from cubesat_testbed.config import parse_scenario, parse_testbed_config
from cubesat_testbed.main import main
from cubesat_testbed.protocol.csp_v1 import (
    CspV1CanFrame,
    CspV1Fields,
    CspV1Reassembler,
    fragment_packet,
)
from cubesat_testbed.protocol.csp_v2 import CspCanFrame, CspFields
from cubesat_testbed.report import RunRecorder, render_report
from cubesat_testbed.scenario import run_scenario
from cubesat_testbed.transport import (
    FaultInjectingTransport,
    InMemoryBusAdapter,
    SocketCanAdapter,
    TracingTransportAdapter,
    TransportEnvelope,
    TransportError,
    build_transport_adapter,
)
from tests.golden_vector_loader import CSP_V1_CAN_VECTOR_DIR, load_golden_vectors

SETUP = """
{version_line}
[transport]
{transport}

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "simulated"
module_type = "generic_eps"
address = 2

[nodes.payload]
mode = "simulated"
module_type = "simple_payload"
address = 3
{payload_extra}

[nodes.obc.commands.payload_power_off]
target = "payload"
destination_port = 10
source_port = 10
payload_hex = "{command_payload}"

[nodes.eps.telemetry.battery_percent]
source_port = 20
destination_port = 20
units = "percent"
min = 0
max = 100
offset = 0
length = 4
type = "float"

[nodes.payload.telemetry.power_status]
source_port = 21
destination_port = 21
offset = 0
length = 1
type = "uint"

[nodes.payload.telemetry.power_status.enum]
0 = "offline"
1 = "online"

[nodes.obc.rules.low_battery_shed_payload]
signal = "eps.telemetry.battery_percent"
op = "<"
threshold = 30.0
for = "3s"

[[nodes.obc.rules.low_battery_shed_payload.actions]]
type = "send_command"
command = "payload_power_off"
"""

SCENARIO = """
name: Shed the payload
steps:
  - action: inject_fault
    type: state_override
    target: eps.model.battery_percent
    value: 25
    duration: 10s
  - action: wait
    virtual_time: 3s
  - action: assert
    signal: payload.telemetry.power_status
    op: "=="
    value: offline
    timeout: 2s
"""


def _setup_text(
    version: int | None = 1,
    *,
    transport: str = 'type = "in-memory"',
    payload_extra: str = "",
    command_payload: str = "00",
    extra: str = "",
) -> str:
    version_line = "" if version is None else f"protocol_version = {version}\n"
    return (
        SETUP.format(
            version_line=version_line,
            transport=transport,
            payload_extra=payload_extra,
            command_payload=command_payload,
        )
        + extra
    )


def _run(
    version: int | None = 1,
    scenario_text: str = SCENARIO,
    *,
    recorder: RunRecorder | None = None,
    **setup_kwargs: str,
) -> tuple[object, list[str]]:
    setup = parse_testbed_config(_setup_text(version, **setup_kwargs))
    scenario = parse_scenario(scenario_text, setup=setup)
    trace = io.StringIO()
    result = run_scenario(scenario, setup, output=io.StringIO(), trace=trace, recorder=recorder)
    return result, trace.getvalue().splitlines()


# --- the setup ------------------------------------------------------------------------------


def test_the_protocol_version_defaults_to_csp_v2() -> None:
    assert parse_testbed_config(_setup_text(None)).protocol_version == 2
    assert parse_testbed_config(_setup_text(1)).protocol_version == 1


def test_csp_v1_runs_over_can_links_only() -> None:
    parse_testbed_config(_setup_text(1, transport='type = "socketcan"\ninterface = "vcan0"'))

    with pytest.raises(ValidationError, match="carries CSP v2 only"):
        parse_testbed_config(_setup_text(1, transport='type = "kiss"\ndevice = "/dev/null"'))
    with pytest.raises(ValidationError, match="carries CSP v2 only"):
        parse_testbed_config(
            _setup_text(
                1,
                transport='type = "zmq"\npublish_endpoint = "tcp://127.0.0.1:6000"\n'
                'subscribe_endpoint = "tcp://127.0.0.1:7000"',
            )
        )


def test_an_unknown_protocol_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_testbed_config(_setup_text(3))
    with pytest.raises(ValidationError):
        parse_testbed_config(_setup_text(0))


@pytest.mark.parametrize(
    ("address", "message"),
    [(32, "5 bits wide"), (31, "broadcast"), (200, "5 bits wide")],
)
def test_csp_v1_addresses_are_five_bits_and_the_top_one_is_broadcast(
    address: int, message: str
) -> None:
    text = _setup_text(1).replace("address = 3\n", f"address = {address}\n", 1)

    with pytest.raises(ValidationError, match=message):
        parse_testbed_config(text)


def test_the_same_addresses_are_fine_on_csp_v2() -> None:
    text = _setup_text(2).replace("address = 3\n", "address = 31\n", 1)

    assert parse_testbed_config(text).nodes["payload"].address == 31


def test_the_highest_usable_v1_address_is_30() -> None:
    text = _setup_text(1).replace("address = 3\n", "address = 30\n", 1)

    assert parse_testbed_config(text).nodes["payload"].address == 30


# --- the adapters ------------------------------------------------------------------------------


def test_adapters_speak_csp_v2_unless_told_otherwise() -> None:
    assert InMemoryBusAdapter().can_protocol.version == 2
    assert InMemoryBusAdapter(protocol_version=1).can_protocol.version == 1
    assert SocketCanAdapter(bus=_FakeBus()).can_protocol.version == 2
    assert SocketCanAdapter(bus=_FakeBus(), protocol_version=1).can_protocol.version == 1


def test_an_unknown_adapter_protocol_version_is_refused() -> None:
    with pytest.raises(ValueError, match="1 or 2"):
        InMemoryBusAdapter(protocol_version=3)


def test_decorators_report_the_version_of_the_adapter_they_wrap() -> None:
    bus = InMemoryBusAdapter(protocol_version=1)
    faults = FaultInjectingTransport(bus, now=lambda: 0, schedule=lambda _d, _a: None)
    tracing = TracingTransportAdapter(faults)

    assert faults.can_protocol.version == 1
    assert tracing.can_protocol.version == 1


def test_the_factory_builds_a_v1_adapter_from_the_setup() -> None:
    setup = parse_testbed_config(_setup_text(1))

    adapter = build_transport_adapter(setup.transport, protocol_version=setup.protocol_version)

    assert adapter.can_protocol.version == 1


def test_a_v1_sender_produces_the_frames_libcsp_does() -> None:
    ping = next(v for v in load_golden_vectors(CSP_V1_CAN_VECTOR_DIR) if v.name == "ping")
    sender = InMemoryBusAdapter(protocol_version=1).new_sender(max_packet_bytes=256)
    fields = CspFields(priority=2, source=1, destination=2, destination_port=1, source_port=18)

    frames = sender.frames(fields, b"\x55")

    assert [(f.can_id, f.data) for f in frames] == [(f.can_id, f.payload) for f in ping.frames]
    assert all(isinstance(f, CspV1CanFrame) for f in frames)


def test_a_v1_sender_refuses_what_v1_cannot_carry() -> None:
    sender = InMemoryBusAdapter(protocol_version=1).new_sender(max_packet_bytes=256)
    fields = CspFields(priority=2, source=1, destination=40, destination_port=1, source_port=1)

    with pytest.raises(ValueError, match="cannot be carried by CSP v1"):
        sender.frames(fields, b"")


def test_a_v2_sender_still_produces_cfp2_frames() -> None:
    sender = InMemoryBusAdapter().new_sender(max_packet_bytes=256)
    fields = CspFields(priority=2, source=1, destination=2, destination_port=1, source_port=1)

    assert all(isinstance(f, CspCanFrame) for f in sender.frames(fields, b"0123456789"))


class _FakeBus:
    def __init__(self, received: list[can.Message] | None = None) -> None:
        self.received: deque[can.Message] = deque(received or [])
        self.sent: list[can.Message] = []

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self.sent.append(msg)

    def recv(self, timeout: float | None = None) -> can.Message | None:
        return self.received.popleft() if self.received else None

    def shutdown(self) -> None:
        pass


def _v1_frames(payload: bytes = b"\x01", *, source: int = 2, destination: int = 2, port: int = 20):
    return fragment_packet(
        CspV1Fields(2, source, destination, port, port), payload, identification=0
    )


def test_socketcan_v1_sends_and_receives_v1_frames() -> None:
    frames = _v1_frames(bytes(range(12)))
    bus = _FakeBus(
        [can.Message(arbitration_id=f.can_id, data=f.data, is_extended_id=True) for f in frames]
    )
    adapter = SocketCanAdapter(bus=bus, protocol_version=1)

    adapter.send(frames[0], source=2)
    received = [adapter.receive() for _ in frames]

    assert bus.sent[0].arbitration_id == frames[0].can_id
    assert [e.frame for e in received if e is not None] == list(frames)
    assert all(isinstance(e.frame, CspV1CanFrame) for e in received if e is not None)


def test_socketcan_refuses_a_frame_of_the_other_protocol_version() -> None:
    v2_frame = CspCanFrame(can_id=0x10004083, data=b"\x00\x04\x14\x80\x55")
    v1_frame = _v1_frames()[0]

    with pytest.raises(TransportError, match="speaks CSP v1"):
        SocketCanAdapter(bus=_FakeBus(), protocol_version=1).send(v2_frame)
    with pytest.raises(TransportError, match="speaks CSP v2"):
        SocketCanAdapter(bus=_FakeBus()).send(v1_frame)


def test_envelope_exposes_can_id_and_data_of_a_v1_frame() -> None:
    frame = _v1_frames()[0]
    envelope = TransportEnvelope(frame=frame, sequence=0)

    assert envelope.can_id == frame.can_id
    assert envelope.data == frame.data


# --- the runner ----------------------------------------------------------------------------------


def test_a_v1_setup_runs_the_default_scenario_and_passes() -> None:
    result, lines = _run(1)

    assert result.passed  # type: ignore[attr-defined]
    assert result.dropped_packets == ()  # type: ignore[attr-defined]
    can_lines = [line for line in lines if "can_id=" in line]
    assert can_lines
    # Every frame is CFP1: it names the remaining-frames counter and the identification,
    # never the CFP2 packet and frame counters.
    assert all("rem=" in line or "dlc=" in line for line in can_lines)
    assert not any(" pc=" in line or " fc=" in line for line in can_lines)


def test_v1_and_v2_runs_reach_the_same_assertion_results() -> None:
    v1, _ = _run(1)
    v2, _ = _run(2)

    assert [a.passed for a in v1.assertions] == [a.passed for a in v2.assertions]  # type: ignore[attr-defined]
    assert [a.actual for a in v1.assertions] == [a.actual for a in v2.assertions]  # type: ignore[attr-defined]


def test_a_v1_trace_shows_the_cfp1_fields_and_the_reassembled_packet() -> None:
    _result, lines = _run(1)

    first = next(
        line for line in lines if " TX " in line and "cfp=begin" in line and "dport=20" in line
    )
    last = next(
        line for line in lines if " TX " in line and "cfp=end" in line and "frames=2" in line
    )
    assert "rem=1" in first and "id=0" in first
    assert "src=2 dst=2 dport=20 sport=20" in first
    assert "rem=0" in last and "payload=41c80000" in last
    assert "telemetry eps.telemetry.battery_percent=25.0" in last
    # Even a one-byte payload is a frame of its own with the header and the length in it.
    single = next(line for line in lines if " TX " in line and "dport=21" in line)
    assert "cfp=" not in single and "payload=01" in single


def test_a_v1_packet_identification_counts_per_sender() -> None:
    _result, lines = _run(1)

    ids = [
        re.search(r" id=(\d+)", line).group(1)  # type: ignore[union-attr]
        for line in lines
        if " TX " in line and "cfp=begin" in line and "src=2" in line
    ]
    assert ids[:4] == ["0", "1", "2", "3"]


def test_a_command_longer_than_one_frame_crosses_a_v1_bus_intact() -> None:
    long_payload = "00 01 02 03 04 05 06 07 08 09 0a 0b"
    result, lines = _run(1, command_payload=long_payload)

    assert result.passed  # type: ignore[attr-defined]
    ends = [line for line in lines if "cfp=end" in line and "command obc.payload_power_off" in line]
    assert ends
    assert "frames=3" in ends[0] and "payload=000102030405060708090a0b" in ends[0]


def test_a_packet_over_the_receivers_buffer_is_dropped_visibly_on_v1() -> None:
    result, lines = _run(
        1,
        command_payload="00 01 02 03 04 05 06 07 08 09 0a 0b",
        payload_extra="max_packet_bytes = 8",
    )

    drops = result.dropped_packets  # type: ignore[attr-defined]
    assert drops
    assert {(d.reason, d.destination, d.limit) for d in drops} == {("overflow", 3, 8)}
    assert any(" DROP " in line and "reason=overflow" in line for line in lines)
    assert not result.passed  # type: ignore[attr-defined]


def test_a_lost_fragment_is_reported_on_v1_as_the_receiver_sees_it() -> None:
    result, lines = _run(
        1,
        extra="""
[[bus_faults.rules]]
type = "drop"
port = 20
nth = 2
""",
    )

    (fault,) = result.bus_faults  # type: ignore[attr-defined]
    # The second frame on port 20 is the continuation of the first telemetry packet: it
    # carries no port itself and is matched by the header of the packet it belongs to.
    assert (fault.type, fault.destination_port, fault.match) == ("drop", 20, 2)
    reasons = {d.reason for d in result.dropped_packets}  # type: ignore[attr-defined]
    assert reasons & {"frame_lost", "timeout", "orphan_fragment"}
    assert any(" FAULT " in line and "dport=20" in line for line in lines)


def test_bus_faults_select_whole_v1_packets_by_port() -> None:
    result, _lines = _run(
        1,
        extra="""
[bus_faults]
seed = 4

[[bus_faults.rules]]
type = "duplicate"
direction = "tx"
port = 21
nth = 1
""",
    )

    (fault,) = result.bus_faults  # type: ignore[attr-defined]
    assert (fault.type, fault.destination_port) == ("duplicate", 21)
    assert result.bus_fault_seed == 4  # type: ignore[attr-defined]


def test_corrupting_a_v1_header_byte_is_visible_not_fatal() -> None:
    result, lines = _run(
        1,
        extra="""
[[bus_faults.rules]]
type = "corrupt"
port = 21
nth = 1
byte = 3
""",
    )

    assert len(result.bus_faults) == 1  # type: ignore[attr-defined]
    assert any(" FAULT " in line and "action=corrupt" in line for line in lines)


def test_a_v1_run_over_socketcan_reads_a_peers_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real EPS board's housekeeping, as CFP1 frames off the bus, satisfies an assertion."""

    setup_text = _setup_text(1, transport='type = "socketcan"\ninterface = "vcan0"').replace(
        'mode = "simulated"\nmodule_type = "generic_eps"\naddress = 2',
        'mode = "hardware"\naddress = 2',
    )
    bus = _FakeBus(
        [
            can.Message(arbitration_id=f.can_id, data=f.data, is_extended_id=True)
            for f in _v1_frames(bytes.fromhex("41c80000"))
        ]
    )
    monkeypatch.setattr(
        runner_module,
        "build_transport_adapter",
        lambda *_a, **_k: SocketCanAdapter(interface="vcan0", bus=bus, protocol_version=1),
    )
    setup = parse_testbed_config(setup_text)
    scenario = parse_scenario(
        "name: peer\nsteps:\n  - action: wait\n    virtual_time: 1s\n  - action: assert\n"
        "    signal: eps.telemetry.battery_percent\n    op: '=='\n    value: 25.0\n    timeout: 2s\n",
        setup=setup,
    )

    result = run_scenario(scenario, setup, output=io.StringIO())

    assert result.passed
    # The testbed's own frames went out as CFP1 too: the simulated payload node's telemetry
    # reassembles from them as CSP v1 packets.
    reassembler = CspV1Reassembler()
    sent_packets = [
        event.packet
        for message in bus.sent
        for event in reassembler.feed(
            CspV1CanFrame(message.arbitration_id, bytes(message.data)), now=0
        )
        if event.kind == "complete"
    ]
    assert sent_packets
    assert {packet.fields.source for packet in sent_packets if packet} == {3}


def test_v1_frames_appear_in_the_html_report() -> None:
    recorder = RunRecorder()
    result, _lines = _run(1, recorder=recorder)

    html = render_report(recorder, result)  # type: ignore[arg-type]

    assert "cfp=begin rem=1 id=0" in html
    assert "cfp=end rem=0 id=0" in html


def test_cli_runs_a_v1_setup_and_reports_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup_path = tmp_path / "setup.toml"
    scenario_path = tmp_path / "scenario.yaml"
    setup_path.write_text(_setup_text(1))
    scenario_path.write_text(SCENARIO)

    code = main(["run", "-c", str(setup_path), "-s", str(scenario_path), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
    assert payload["dropped_packets"] == []
