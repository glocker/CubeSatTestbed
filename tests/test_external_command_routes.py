"""A command aimed at a software/hardware node is observed, never "delivered" to a model.

Regression tests for runs that used to abort with ``simulated module 'payload' is not
available`` when the testbed saw a configured command whose target a real peer owns.
"""

from __future__ import annotations

import io
import warnings

import pytest

from cubesat_testbed.config import parse_scenario, parse_testbed_config
from cubesat_testbed.protocol.csp_v2 import CspFields, pack
from cubesat_testbed.scenario import ScenarioRunner, build_runtime

SETUP = """
[transport]
type = "in-memory"

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "{eps_mode}"
address = 2

[nodes.payload]
mode = "{payload_mode}"
{payload_module}
address = 3

[nodes.eps.commands.{eps_command}]
target = "payload"
destination_port = 10
source_port = 11
payload_hex = "00"

[nodes.obc.commands.shed_from_obc]
target = "payload"
destination_port = 12
source_port = 13
payload_hex = "01"
"""

WAIT = "name: observe\nsteps:\n  - action: wait\n    virtual_time: 1s\n"
SEND = (
    "name: send\nsteps:\n  - action: send_command\n    command: shed_from_obc\n"
    "    source: obc\n    target: payload\n    payload: {reason: test}\n"
    "  - action: wait\n    virtual_time: 1s\n"
)
EPS_TO_PAYLOAD = CspFields(priority=2, source=2, destination=3, destination_port=10, source_port=11)


def _setup(*, eps_mode: str, payload_mode: str, eps_command: str = "shed"):  # type: ignore[no-untyped-def]
    module = 'module_type = "simple_payload"' if payload_mode == "simulated" else ""
    return parse_testbed_config(
        SETUP.format(
            eps_mode=eps_mode,
            payload_mode=payload_mode,
            payload_module=module,
            eps_command=eps_command,
        )
    )


def test_traffic_between_two_other_nodes_matching_a_command_route_is_only_observed() -> None:
    setup = _setup(eps_mode="software", payload_mode="software")
    runtime = build_runtime(setup)
    # A real eps and a real payload talking on a bus the testbed merely watches.
    runtime.transport.send(pack(EPS_TO_PAYLOAD, b"\x00"), source=2)
    runner = ScenarioRunner(runtime, output=io.StringIO())

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a routed command is not an "unrouted frame" either
        result = runner.run(parse_scenario(WAIT, setup=setup))

    assert result.passed and result.dropped_packets == ()


def test_send_command_to_an_external_node_on_the_in_memory_bus_runs_and_is_traced() -> None:
    setup = _setup(eps_mode="software", payload_mode="software")
    trace = io.StringIO()
    runtime = build_runtime(setup, trace=trace)
    runner = ScenarioRunner(runtime, output=io.StringIO())

    result = runner.run(parse_scenario(SEND, setup=setup))

    assert result.passed
    lines = trace.getvalue().splitlines()
    # The in-memory bus hands the testbed's own frame back, so it shows as TX and RX.
    assert any(" TX " in line and "command obc.shed_from_obc->payload" in line for line in lines)
    assert any(" RX " in line and "command obc.shed_from_obc->payload" in line for line in lines)


def test_no_semantic_payload_is_kept_for_a_command_nobody_will_deliver() -> None:
    setup = _setup(eps_mode="software", payload_mode="software")
    runner = ScenarioRunner(build_runtime(setup), output=io.StringIO())

    runner.run(parse_scenario(SEND, setup=setup))

    assert runner._semantic_payloads_by_envelope == {}


def test_a_command_to_a_simulated_target_is_still_delivered_with_its_payload() -> None:
    # simple_payload understands this command name, unlike the made-up "shed".
    setup = _setup(eps_mode="software", payload_mode="simulated", eps_command="payload_power_off")
    runtime = build_runtime(setup)
    runner = ScenarioRunner(runtime, output=io.StringIO())
    payload = runtime.module("payload")
    received: list[object] = []
    original = payload.handle_command  # type: ignore[attr-defined]

    def spy(command):  # type: ignore[no-untyped-def]
        received.append(command)
        return original(command)

    payload.handle_command = spy  # type: ignore[attr-defined,method-assign]
    runtime.transport.send(pack(EPS_TO_PAYLOAD, b"\x00"), source=2)

    runner.run(parse_scenario(WAIT, setup=setup))

    assert [c.command for c in received] == ["payload_power_off"]  # type: ignore[attr-defined]
    assert runner._semantic_payloads_by_envelope == {}


def test_an_ambiguous_route_is_still_a_hard_error() -> None:
    """Observing is for routes whose target is external; a config bug stays loud."""

    text = SETUP.format(
        eps_mode="software", payload_mode="software", payload_module="", eps_command="shed"
    )
    text += (
        '\n[nodes.eps.commands.shed_twin]\ntarget = "payload"\ndestination_port = 10\n'
        'source_port = 11\npayload_hex = "00"\n'
    )
    setup = parse_testbed_config(text)
    runtime = build_runtime(setup)
    runtime.transport.send(pack(EPS_TO_PAYLOAD, b"\x00"), source=2)

    with pytest.raises(Exception, match="multiple configured routes"):
        ScenarioRunner(runtime, output=io.StringIO()).run(parse_scenario(WAIT, setup=setup))
