"""Several telemetry signals in one packet (backlog item 17): config, sending, receiving."""

from __future__ import annotations

import io
import json
from collections import deque

import can
import pytest
from pydantic import ValidationError

import cubesat_testbed.scenario.runner as runner_module
from cubesat_testbed.config import (
    ScenarioReferenceError,
    parse_obc_rules_file,
    parse_scenario,
    parse_testbed_config,
)
from cubesat_testbed.examples import get_example
from cubesat_testbed.main import main
from cubesat_testbed.protocol.csp_v2 import CspFields, fragment_packet
from cubesat_testbed.report import RunRecorder, render_report
from cubesat_testbed.scenario import run_scenario
from cubesat_testbed.scenario.runner import ScenarioRuntimeError, build_obc_rules_from_file
from cubesat_testbed.transport import SocketCanAdapter

BASE = """
[transport]
type = "in-memory"

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "simulated"
module_type = "generic_eps"
address = 2
"""

PACKET = (
    BASE
    + """
[nodes.eps.packets.hk]
source_port = 20
destination_port = 20
to = "obc"

[nodes.eps.packets.hk.fields.battery_percent]
offset = 0
length = 4
type = "float"

[nodes.eps.packets.hk.fields.cell_voltages_v]
offset = 4
length = 4
count = 2
type = "float"

[nodes.eps.packets.hk.fields.power_mode]
offset = 12
bit_offset = 0
bit_length = 2
type = "uint"

[nodes.eps.packets.hk.fields.power_mode.enum]
0 = "nominal"
1 = "low_power"
2 = "safe"
3 = "off"

[nodes.eps.packets.hk.fields.payload_rail_enabled]
offset = 12
bit_offset = 2
bit_length = 1
type = "uint"
"""
)


def _run(setup_text: str, steps: str, **kwargs: object) -> tuple[object, str]:
    setup = parse_testbed_config(setup_text)
    scenario = parse_scenario(f"name: packets\nsteps:\n{steps}", setup=setup)
    trace = io.StringIO()
    result = run_scenario(scenario, setup, output=io.StringIO(), trace=trace, **kwargs)  # type: ignore[arg-type]
    return result, trace.getvalue()


# --- sending and receiving ---------------------------------------------------------------------


def test_a_packets_fields_travel_in_one_csp_packet() -> None:
    result, trace = _run(
        PACKET,
        """
  - {action: wait, virtual_time: 1s}
  - {action: assert, signal: eps.telemetry.battery_percent, op: ">", value: 79}
  - {action: assert, signal: eps.telemetry.power_mode, op: "==", value: nominal}
  - {action: assert, signal: eps.telemetry.payload_rail_enabled, op: "==", value: 1}
""",
    )

    assert result.passed  # type: ignore[attr-defined]
    tx = [line for line in trace.splitlines() if " TX " in line and "dport=20" in line]
    assert len(tx) == 1  # only a first CFP2 frame carries the ports: one packet per second
    packet_lines = [
        line for line in trace.splitlines() if " TX " in line and "telemetry eps." in line
    ]
    assert len(packet_lines) == 1
    assert "telemetry eps.packets.hk battery_percent=" in packet_lines[0]
    assert "power_mode='nominal' payload_rail_enabled=1" in packet_lines[0]
    assert "frames=3" in packet_lines[0]  # 13 bytes: 4 + 8 + 1


def test_an_array_is_asserted_whole_or_element_by_element() -> None:
    result, _trace = _run(
        PACKET,
        """
  - {action: wait, virtual_time: 1s}
  - {action: assert, signal: "eps.telemetry.cell_voltages_v[0]", op: ">", value: 3.5}
  - {action: assert, signal: "eps.telemetry.cell_voltages_v[1]", op: ">", value: 3.5}
  - {action: inject_fault, type: named_fault, target: eps.battery_cell_dead}
  - {action: assert, signal: "eps.telemetry.cell_voltages_v[1]", op: "<", value: 3.0}
  - {action: assert, signal: eps.telemetry.cell_voltages_v, op: "!=", value: [0.0, 0.0]}
""",
    )

    assert result.passed, [a.detail for a in result.assertions]  # type: ignore[attr-defined]


def test_an_obc_rule_watches_one_element_of_an_array() -> None:
    setup = (
        PACKET
        + """
[nodes.payload]
mode = "simulated"
module_type = "simple_payload"
address = 3

[nodes.payload.telemetry.power_status]
source_port = 21
destination_port = 21
offset = 0
length = 1
type = "uint"

[nodes.payload.telemetry.power_status.enum]
0 = "offline"
1 = "online"

[nodes.obc.commands.payload_power_off]
target = "payload"
destination_port = 10
source_port = 10
payload_hex = "00"

[nodes.obc.rules.weak_cell]
signal = "eps.telemetry.cell_voltages_v[1]"
op = "<"
threshold = 3.0

[[nodes.obc.rules.weak_cell.actions]]
type = "send_command"
command = "payload_power_off"
"""
    )
    steps = """
  - {action: wait, virtual_time: 2s}
  - {action: assert, signal: payload.telemetry.power_status, op: "==", value: online}
  - {action: inject_fault, type: named_fault, target: eps.battery_cell_dead}
  - {action: assert, signal: payload.telemetry.power_status, op: "==", value: offline, timeout: 3s}
"""
    result, _trace = _run(setup, steps)

    assert result.passed, [a.detail for a in result.assertions]  # type: ignore[attr-defined]


def test_a_boards_packet_too_short_for_its_fields_is_a_reported_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real board whose firmware sends less than the setup describes: dropped, visibly."""

    frames = fragment_packet(CspFields(2, 2, 1, 20, 20), bytes(4))
    bus = _Bus(
        [can.Message(arbitration_id=f.can_id, data=f.data, is_extended_id=True) for f in frames]
    )
    monkeypatch.setattr(
        runner_module,
        "build_transport_adapter",
        lambda *_a, **_k: SocketCanAdapter(interface="vcan0", bus=bus),
    )
    setup = PACKET.replace('type = "in-memory"', 'type = "socketcan"\ninterface = "vcan0"').replace(
        'mode = "simulated"\nmodule_type = "generic_eps"', 'mode = "hardware"'
    )

    result, trace = _run(setup, "  - {action: wait, virtual_time: 1s}\n")

    (drop,) = result.dropped_packets  # type: ignore[attr-defined]
    assert drop.reason == "undecodable"
    assert drop.detail.startswith("eps.packets.hk: cannot decode telemetry")
    assert "too short" in drop.detail
    assert "reason=undecodable" in trace


class _Bus:
    def __init__(self, incoming: list[can.Message]) -> None:
        self.incoming: deque[can.Message] = deque(incoming)

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        pass

    def recv(self, timeout: float | None = None) -> can.Message | None:
        return self.incoming.popleft() if self.incoming else None

    def shutdown(self) -> None:
        pass


def test_a_module_missing_a_packet_field_names_the_field_and_the_packet() -> None:
    setup = PACKET.replace("fields.payload_rail_enabled]", "fields.no_such_field]")

    with pytest.raises(
        ScenarioRuntimeError, match=r"no telemetry 'no_such_field'.*eps\.packets\.hk"
    ):
        _run(setup, "  - {action: wait, virtual_time: 1s}\n")


def test_the_json_report_and_html_report_carry_array_elements(tmp_path: object) -> None:
    setup = parse_testbed_config(PACKET)
    scenario = parse_scenario(
        "name: packets\nsteps:\n  - {action: wait, virtual_time: 2s}\n"
        '  - {action: assert, signal: "eps.telemetry.cell_voltages_v[1]", op: ">", value: 3}\n',
        setup=setup,
    )
    recorder = RunRecorder()
    result = run_scenario(scenario, setup, output=io.StringIO(), recorder=recorder)

    assert result.passed
    assert "eps.telemetry.cell_voltages_v[1]" in recorder.signals
    assert all(
        isinstance(value, tuple) for _t, value in recorder.signals["eps.telemetry.cell_voltages_v"]
    )
    html = render_report(recorder, result)
    assert "eps.telemetry.cell_voltages_v[1]" in html
    assert recorder.assertions[0].evidence_frame is not None


def test_the_eps_housekeeping_example_passes_from_the_cli(
    capsys: pytest.CaptureFixture[str],
) -> None:
    example = get_example("eps-housekeeping")

    code = main(["run", "-c", str(example.setup_path), "-s", str(example.scenario_path), "--json"])

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is True


# --- config validation -------------------------------------------------------------------------


def test_the_single_signal_shorthand_and_a_packet_coexist() -> None:
    setup = parse_testbed_config(
        PACKET
        + """
[nodes.eps.telemetry.load_power_w]
source_port = 21
destination_port = 21
offset = 0
length = 4
type = "float"
"""
    )

    assert "eps.telemetry.load_power_w" in setup.telemetry_signal_names()
    assert setup.telemetry_signal_counts()["eps.telemetry.cell_voltages_v"] == 2
    labels = [d.label for d in setup.telemetry_packets()]
    assert labels == ["eps.load_power_w", "eps.packets.hk"]


def test_overlapping_fields_of_one_packet_are_rejected_when_the_config_loads() -> None:
    setup = PACKET.replace("bit_offset = 2\nbit_length = 1", "bit_offset = 1\nbit_length = 1")

    with pytest.raises(ValidationError, match=r"eps\.packets\.hk.*overlap at byte offset 12 bit 1"):
        parse_testbed_config(setup)


def test_a_packet_and_a_mapping_on_the_same_ports_cannot_be_told_apart() -> None:
    setup = (
        PACKET
        + """
[nodes.eps.telemetry.load_power_w]
source_port = 20
destination_port = 20
offset = 0
length = 4
type = "float"
"""
    )

    with pytest.raises(
        ValidationError, match=r"eps\.load_power_w and eps\.packets\.hk cannot be told apart"
    ):
        parse_testbed_config(setup)


def test_two_shorthand_mappings_on_one_port_pair_point_at_packets() -> None:
    setup = (
        BASE
        + """
[nodes.eps.telemetry.battery_percent]
source_port = 20
destination_port = 20
offset = 0
length = 4
type = "float"

[nodes.eps.telemetry.battery_voltage_v]
source_port = 20
destination_port = 20
offset = 0
length = 4
type = "float"
"""
    )

    with pytest.raises(ValidationError, match=r"\[nodes\.eps\.packets\.<name>\]"):
        parse_testbed_config(setup)


def test_a_signal_in_a_packet_and_in_a_mapping_is_mapped_twice() -> None:
    setup = (
        PACKET
        + """
[nodes.eps.telemetry.battery_percent]
source_port = 22
destination_port = 22
offset = 0
length = 4
type = "float"
"""
    )

    with pytest.raises(ValidationError, match="mapped by both"):
        parse_testbed_config(setup)


def test_a_packet_longer_than_the_nodes_buffer_is_rejected() -> None:
    setup = PACKET.replace("address = 2\n", "address = 2\nmax_packet_bytes = 12\n", 1)

    with pytest.raises(ValidationError, match=r"eps\.packets\.hk needs a 13-byte payload"):
        parse_testbed_config(setup)


def test_a_bit_field_with_endian_is_rejected() -> None:
    setup = PACKET.replace("bit_length = 1\ntype", 'bit_length = 1\nendian = "little"\ntype')

    with pytest.raises(ValidationError, match="remove endian"):
        parse_testbed_config(setup)


def test_a_field_without_length_or_bits_is_rejected() -> None:
    setup = PACKET.replace("offset = 0\nlength = 4\ntype", "offset = 0\ntype", 1)

    with pytest.raises(ValidationError, match="give length"):
        parse_testbed_config(setup)


def test_a_packet_needs_at_least_one_field() -> None:
    setup = (
        BASE + "\n[nodes.eps.packets.hk]\nsource_port = 20\ndestination_port = 20\nfields = {}\n"
    )

    with pytest.raises(ValidationError, match="at least 1"):
        parse_testbed_config(setup)


@pytest.mark.parametrize(
    ("signal", "message"),
    [
        ("eps.telemetry.cell_voltages_v[2]", "out of range.*2 elements"),
        ("eps.telemetry.battery_percent[0]", "not an array"),
        ("eps.telemetry.nothing[0]", "not mapped"),
    ],
)
def test_a_scenario_index_is_checked_against_the_array(signal: str, message: str) -> None:
    setup = parse_testbed_config(PACKET)

    with pytest.raises(ScenarioReferenceError, match=message):
        parse_scenario(
            f'name: x\nsteps:\n  - {{action: assert, signal: "{signal}", op: ">", value: 0}}\n',
            setup=setup,
        )


def test_an_inline_rule_on_a_whole_array_is_rejected() -> None:
    setup = (
        PACKET
        + """
[nodes.obc.rules.weak]
signal = "eps.telemetry.cell_voltages_v"
op = "<"
threshold = 3.0

[[nodes.obc.rules.weak.actions]]
type = "inject_fault"
fault_type = "named_fault"
target = "eps.solar_array_offline"
"""
    )

    with pytest.raises(ValidationError, match=r"array of 2.*cell_voltages_v\[0\]"):
        parse_testbed_config(setup)


def test_a_rules_file_rule_on_a_whole_array_is_rejected_before_the_run(tmp_path: object) -> None:
    from pathlib import Path

    rules = Path(str(tmp_path)) / "rules.toml"
    rules.write_text(
        """
[obc.weak]
signal = "eps.telemetry.cell_voltages_v"
op = "<"
threshold = 3.0

[[obc.weak.actions]]
type = "inject_fault"
fault_type = "named_fault"
target = "eps.solar_array_offline"
""",
        encoding="utf-8",
    )
    parse_obc_rules_file(rules.read_text(encoding="utf-8"))

    with pytest.raises(ScenarioRuntimeError, match="array of 2"):
        _run(
            PACKET,
            "  - {action: wait, virtual_time: 1s}\n",
            obc_rules=build_obc_rules_from_file(rules),
        )


def test_a_signal_override_replaces_a_whole_array() -> None:
    result, _trace = _run(
        PACKET,
        """
  - {action: inject_fault, type: signal_override, target: eps.telemetry.cell_voltages_v, value: [4.0, 1.5]}
  - {action: wait, virtual_time: 1s}
  - {action: assert, signal: "eps.telemetry.cell_voltages_v[1]", op: "==", value: 1.5}
  - {action: assert, signal: eps.telemetry.cell_voltages_v, op: "==", value: [4.0, 1.5]}
""",
    )

    assert result.passed, [a.detail for a in result.assertions]  # type: ignore[attr-defined]
