"""Telemetry addressing (known bug 13): who a signal's frames are addressed to, and what matches.

A real OBC only receives frames carrying its own CSP address, so a simulated node's telemetry
has to be addressed to it (``to``). And a real board's housekeeping goes to the OBC, never to
the board itself, so a signal it sends must be matched without regard to the destination
unless the mapping names one.
"""

from __future__ import annotations

import io
from collections import deque

import can
import pytest
from pydantic import ValidationError

import cubesat_testbed.scenario.runner as runner_module
from cubesat_testbed.config import parse_scenario, parse_testbed_config
from cubesat_testbed.protocol import csp_v1
from cubesat_testbed.protocol.csp_v1 import CspV1Fields
from cubesat_testbed.protocol.csp_v2 import CspFields, fragment_packet
from cubesat_testbed.scenario import run_scenario
from cubesat_testbed.transport import SocketCanAdapter

BOARD_SETUP = """
{version_line}
[transport]
type = "socketcan"
interface = "vcan0"

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "hardware"
address = 2

[nodes.eps.telemetry.battery_percent]
source_port = 20
destination_port = 20
{to_line}
offset = 0
length = 4
type = "float"
"""

SIMULATED_SETUP = """
{version_line}
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

[nodes.eps.telemetry.battery_percent]
source_port = 20
destination_port = 20
{to_line}
offset = 0
length = 4
type = "float"
"""

WAIT_AND_ASSERT = """
name: board housekeeping
steps:
  - action: wait
    virtual_time: 1s
  - action: assert
    signal: eps.telemetry.battery_percent
    op: "=="
    value: 25.0
    timeout: 2s
"""

SIMULATED_SCENARIO = """
name: simulated housekeeping
steps:
  - action: wait
    virtual_time: 2s
  - action: assert
    signal: eps.telemetry.battery_percent
    op: ">="
    value: 0
    timeout: 1s
"""

TO_OBC = 'to = "obc"'


class _Bus:
    def __init__(self, incoming: list[can.Message] | None = None) -> None:
        self.incoming: deque[can.Message] = deque(incoming or [])
        self.sent: list[can.Message] = []

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self.sent.append(msg)

    def recv(self, timeout: float | None = None) -> can.Message | None:
        return self.incoming.popleft() if self.incoming else None

    def shutdown(self) -> None:
        pass


def _version_line(version: int) -> str:
    return "" if version == 2 else "protocol_version = 1\n"


def _board_frames(version: int, destination: int) -> list[can.Message]:
    payload = bytes.fromhex("41c80000")  # 25.0
    if version == 1:
        frames = csp_v1.fragment_packet(
            CspV1Fields(2, 2, destination, 20, 20), payload, identification=0
        )
    else:
        frames = fragment_packet(CspFields(2, 2, destination, 20, 20), payload)
    return [can.Message(arbitration_id=f.can_id, data=f.data, is_extended_id=True) for f in frames]


def _run_board(
    monkeypatch: pytest.MonkeyPatch, version: int, destination: int, to_line: str = ""
) -> object:
    bus = _Bus(_board_frames(version, destination))
    monkeypatch.setattr(
        runner_module,
        "build_transport_adapter",
        lambda *_a, **_k: SocketCanAdapter(interface="vcan0", bus=bus, protocol_version=version),
    )
    setup = parse_testbed_config(
        BOARD_SETUP.format(version_line=_version_line(version), to_line=to_line)
    )
    scenario = parse_scenario(WAIT_AND_ASSERT, setup=setup)
    return run_scenario(scenario, setup, output=io.StringIO())


# --- a real board's housekeeping, addressed to the OBC ---------------------------------------


@pytest.mark.parametrize("version", [1, 2])
def test_a_boards_telemetry_addressed_to_the_obc_is_its_signal(
    monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    result = _run_board(monkeypatch, version, destination=1)

    assert result.passed  # type: ignore[attr-defined]


@pytest.mark.parametrize("version", [1, 2])
def test_a_boards_telemetry_addressed_to_itself_still_matches(
    monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    result = _run_board(monkeypatch, version, destination=2)

    assert result.passed  # type: ignore[attr-defined]


@pytest.mark.parametrize("version", [1, 2])
def test_a_mapping_that_names_its_destination_matches_only_that_destination(
    monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    right = _run_board(monkeypatch, version, destination=1, to_line=TO_OBC)
    wrong = _run_board(monkeypatch, version, destination=2, to_line=TO_OBC)

    assert right.passed  # type: ignore[attr-defined]
    assert not wrong.passed  # type: ignore[attr-defined]
    assert "never observed" in wrong.assertions[0].detail  # type: ignore[attr-defined]


# --- a simulated node's telemetry, addressed to the OBC ----------------------------------------


def _telemetry_destinations(version: int, to_line: str) -> tuple[object, set[int]]:
    setup = parse_testbed_config(
        SIMULATED_SETUP.format(version_line=_version_line(version), to_line=to_line)
    )
    scenario = parse_scenario(SIMULATED_SCENARIO, setup=setup)
    trace = io.StringIO()
    result = run_scenario(scenario, setup, output=io.StringIO(), trace=trace)
    destinations = {
        int(token.split("=")[1])
        for line in trace.getvalue().splitlines()
        if " TX " in line and "dport=20" in line
        for token in line.split()
        if token.startswith("dst=")
    }
    return result, destinations


@pytest.mark.parametrize("version", [1, 2])
def test_a_simulated_nodes_telemetry_is_addressed_to_the_node_it_names(version: int) -> None:
    result, destinations = _telemetry_destinations(version, TO_OBC)

    assert result.passed  # type: ignore[attr-defined]
    assert destinations == {1}


@pytest.mark.parametrize("version", [1, 2])
def test_without_to_telemetry_is_still_addressed_to_its_own_sender(version: int) -> None:
    result, destinations = _telemetry_destinations(version, "")

    assert result.passed  # type: ignore[attr-defined]
    assert destinations == {2}


def test_the_obc_rule_still_fires_on_telemetry_addressed_to_the_obc() -> None:
    setup = parse_testbed_config(
        SIMULATED_SETUP.format(version_line="", to_line=TO_OBC)
        + """
[nodes.payload]
mode = "simulated"
module_type = "simple_payload"
address = 3

[nodes.obc.commands.payload_power_off]
target = "payload"
destination_port = 10
source_port = 10
payload_hex = "00"

[nodes.obc.rules.low]
signal = "eps.telemetry.battery_percent"
op = "<"
threshold = 30.0
for = "1s"

[[nodes.obc.rules.low.actions]]
type = "send_command"
command = "payload_power_off"
"""
    )
    scenario = parse_scenario(
        """
name: low battery
steps:
  - action: inject_fault
    type: state_override
    target: eps.model.battery_percent
    value: 25
    duration: 10s
  - action: wait
    virtual_time: 3s
  - action: assert
    signal: eps.telemetry.battery_percent
    op: "<"
    value: 30
    timeout: 1s
""",
        setup=setup,
    )
    trace = io.StringIO()

    result = run_scenario(scenario, setup, output=io.StringIO(), trace=trace)

    assert result.passed
    assert "command obc.payload_power_off->payload" in trace.getvalue()


def test_a_traced_telemetry_frame_is_named_whoever_it_is_addressed_to() -> None:
    _result, _destinations = _telemetry_destinations(2, TO_OBC)
    setup = parse_testbed_config(SIMULATED_SETUP.format(version_line="", to_line=TO_OBC))
    scenario = parse_scenario(SIMULATED_SCENARIO, setup=setup)
    trace = io.StringIO()

    run_scenario(scenario, setup, output=io.StringIO(), trace=trace)

    assert "telemetry eps.telemetry.battery_percent=" in trace.getvalue()
    assert "unrouted" not in trace.getvalue()


# --- the setup ---------------------------------------------------------------------------------


def test_to_must_name_a_configured_node() -> None:
    with pytest.raises(ValidationError, match="unknown node 'ground'"):
        parse_testbed_config(SIMULATED_SETUP.format(version_line="", to_line='to = "ground"'))


def test_to_defaults_to_none() -> None:
    setup = parse_testbed_config(SIMULATED_SETUP.format(version_line="", to_line=""))

    assert setup.nodes["eps"].telemetry["battery_percent"].to is None


def test_to_may_name_the_node_itself() -> None:
    setup = parse_testbed_config(SIMULATED_SETUP.format(version_line="", to_line='to = "eps"'))

    assert setup.nodes["eps"].telemetry["battery_percent"].to == "eps"
