"""End-to-end multi-frame CSP packets: send, trace, reassemble, drop visibly."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import cubesat_testbed.scenario.runner as runner_module
from cubesat_testbed.config import parse_scenario, parse_testbed_config
from cubesat_testbed.main import main
from cubesat_testbed.protocol.csp_v2 import CspCanFrame
from cubesat_testbed.scenario import run_scenario
from cubesat_testbed.transport import InMemoryBusAdapter, TransportAdapter, TransportEnvelope

SETUP_TEMPLATE = """
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

[nodes.payload]
mode = "simulated"
module_type = "simple_payload"
address = 3
{payload_extra}

[nodes.obc.commands.payload_power_off]
target = "payload"
destination_port = 10
source_port = 10
payload_hex = "00 01 02 03 04 05 06 07 08 09 0a 0b"

[nodes.eps.telemetry.battery_percent]
source_port = 20
destination_port = 20
units = "percent"
min = 0
max = 100
offset = 8
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
name: Multi-frame low battery
steps:
  - action: inject_fault
    type: state_override
    target: eps.model.battery_percent
    value: 25
    duration: 5s
  - action: wait
    virtual_time: 3s
  - action: assert
    signal: eps.telemetry.battery_percent
    op: "=="
    value: 25.0
    timeout: 1s
  - action: assert
    signal: payload.telemetry.power_status
    op: "=="
    value: offline
    timeout: 1s
"""


def _setup_text(payload_extra: str = "") -> str:
    return SETUP_TEMPLATE.format(payload_extra=payload_extra)


def _run(payload_extra: str = "", scenario_text: str = SCENARIO) -> tuple[object, list[str]]:
    setup = parse_testbed_config(_setup_text(payload_extra))
    scenario = parse_scenario(scenario_text, setup=setup)
    trace = io.StringIO()
    result = run_scenario(scenario, setup, output=io.StringIO(), trace=trace)
    return result, trace.getvalue().splitlines()


def test_telemetry_and_command_packets_longer_than_one_frame_cross_the_bus() -> None:
    result, lines = _run()

    assert result.passed  # type: ignore[attr-defined]
    assert result.dropped_packets == ()  # type: ignore[attr-defined]

    telemetry_end = [
        line
        for line in lines
        if " TX " in line
        and "cfp=end" in line
        and "telemetry eps.telemetry.battery_percent" in line
    ]
    assert telemetry_end
    assert all("frames=2" in line for line in telemetry_end)
    # 8 padding bytes before the float at offset 8, then 25.0 big-endian.
    assert "payload=000000000000000041c80000" in telemetry_end[0]

    first_frames = [line for line in lines if " TX " in line and "cfp=begin" in line]
    assert any("src=2" in line and "dst=2" in line and "dport=20" in line for line in first_frames)
    assert any("src=1" in line and "dst=3" in line and "dport=10" in line for line in first_frames)

    command_end = [
        line for line in lines if "cfp=end" in line and " command obc.payload_power_off" in line
    ]
    assert command_end
    assert "payload=000102030405060708090a0b" in command_end[0]


def test_consecutive_packets_from_one_node_advance_the_cfp2_packet_counter() -> None:
    _result, lines = _run()

    counters = {
        token
        for line in lines
        if " TX " in line and "cfp=begin" in line and "src=2" in line
        for token in line.split()
        if token.startswith("pc=")
    }

    assert {"pc=0", "pc=1", "pc=2", "pc=3"} <= counters


def test_a_target_with_a_smaller_packet_buffer_drops_the_command_visibly() -> None:
    result, lines = _run("max_packet_bytes = 8")

    assert not result.passed  # type: ignore[attr-defined]
    drops = result.dropped_packets  # type: ignore[attr-defined]
    # The OBC rule keeps firing while the battery stays low, so the command is
    # sent, and dropped, more than once.
    assert drops
    assert {(d.reason, d.destination, d.limit) for d in drops} == {("overflow", 3, 8)}
    assert "max_packet_bytes=8" in drops[0].detail

    drop_lines = [line for line in lines if " DROP " in line]
    assert len(drop_lines) == len(drops)
    assert drop_lines[0].startswith(f"trace DROP t={drops[0].time} node=3")
    assert "reason=overflow" in drop_lines[0] and "limit=8" in drop_lines[0]


class _FrameDroppingBus(InMemoryBusAdapter):
    """Loses every frame with ``frame_count == 1`` that is addressed to node 3."""

    def send(self, frame: CspCanFrame, *, source: object = None) -> TransportEnvelope:
        destination = (frame.can_id >> 13) & 0x3FFF
        frame_count = (frame.can_id >> 2) & 0x7
        if destination == 3 and frame_count == 1 and not frame.can_id & 0x2:
            return TransportEnvelope(frame=frame, sequence=-1, source=source)  # type: ignore[arg-type]
        return super().send(frame, source=source)  # type: ignore[arg-type]


def test_a_lost_frame_makes_the_packet_time_out_in_virtual_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build(*_args: object, **_kwargs: object) -> TransportAdapter:
        return _FrameDroppingBus(endpoints=(1, 2, 3))

    monkeypatch.setattr(runner_module, "build_transport_adapter", build)

    # Wait past the reassembly window so the stuck packet is reported.
    result, lines = _run(scenario_text=SCENARIO + "  - action: wait\n    virtual_time: 3s\n")

    assert not result.passed  # type: ignore[attr-defined]
    drops = result.dropped_packets  # type: ignore[attr-defined]
    assert drops
    assert {(d.reason, d.destination) for d in drops} == {("timeout", 3)}
    # Reported at the instant the one-second window closed, not when it was noticed.
    assert drops[0].time % 1_000_000 == 0
    assert any(" DROP " in line and "reason=timeout" in line for line in lines)


def test_multi_frame_runs_are_byte_identical_across_repeats() -> None:
    first = _run()
    second = _run()

    assert first == second


def test_cli_reports_a_dropped_packet_in_text_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup_path = tmp_path / "setup.toml"
    scenario_path = tmp_path / "scenario.yaml"
    setup_path.write_text(_setup_text("max_packet_bytes = 8"), encoding="utf-8")
    scenario_path.write_text(SCENARIO, encoding="utf-8")
    args = ["run", "--config", str(setup_path), "--scenario", str(scenario_path)]

    assert main(args) == 1
    text = capsys.readouterr().out
    assert any(
        line.startswith("DROP t=") and "reason=overflow" in line for line in text.splitlines()
    )
    assert "dropped_packets=" in text.splitlines()[-1]

    assert main([*args, "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["dropped_packets"][0]["reason"] == "overflow"
    assert payload["dropped_packets"][0]["node"] == 3
    assert payload["dropped_packets"][0]["limit"] == 8


def test_cli_run_without_drops_keeps_the_summary_line_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["run", "--example", "default"]) == 0

    assert "dropped_packets" not in capsys.readouterr().out
