from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

from cubesat_testbed.config import load_scenario, load_testbed_config, parse_scenario
from cubesat_testbed.protocol.csp_v2 import CspFields, decode_frame, pack
from cubesat_testbed.protocol.telemetry_codec import decode_telemetry_value
from cubesat_testbed.scenario import (
    ScenarioRunner,
    ScenarioRuntimeError,
    build_in_memory_runtime,
    run_scenario,
)


def test_default_low_battery_scenario_runs_over_virtual_time_with_pass_output() -> None:
    setup = load_testbed_config(Path("configs/default_satellite.toml"))
    scenario = load_scenario(Path("configs/scenarios/low_battery.yaml"), setup=setup)
    output = StringIO()
    runtime = build_in_memory_runtime(setup)

    result = ScenarioRunner(runtime, output=output).run(scenario)

    # The OBC reacts to the t=3s low-battery telemetry by commanding the
    # payload off; the payload's own next telemetry beacon (t=4s, one
    # physical step later) is the first honest opportunity for a bus listener
    # to observe "offline" -- this one-step propagation delay is real, not a
    # bug, now that assertions read decoded wire telemetry instead of the
    # simulation's live Python state.
    assert result.passed
    assert result.finished_at == 4_000_000
    assert [
        (assertion.name, assertion.passed, assertion.actual) for assertion in result.assertions
    ] == [("assert_3", True, "offline")]
    assert output.getvalue() == (
        "PASS t=4000000 assert_3: payload.telemetry.power_status == 'offline'; actual='offline'\n"
    )
    assert runtime.module("payload").telemetry()["power_status"] == "offline"


def test_send_command_step_updates_simulated_module_through_in_memory_bus() -> None:
    setup = load_testbed_config(Path("configs/default_satellite.toml"))
    scenario = parse_scenario(
        """
        name: Payload command smoke
        steps:
          - action: send_command
            command: payload_power_off
            source: obc
            target: payload
          - action: assert
            name: payload_off
            signal: payload.telemetry.power_status
            op: ==
            value: offline
        """,
        setup=setup,
    )
    output = StringIO()

    result = run_scenario(
        scenario,
        setup,
        output=output,
        install_default_obc_rules=False,
    )

    assert result.passed
    assert result.finished_at == 0
    assert output.getvalue() == (
        "PASS t=0 payload_off: payload.telemetry.power_status == 'offline'; actual='offline'\n"
    )


def test_assert_step_waits_until_timeout_and_reports_fail_deterministically() -> None:
    setup = load_testbed_config(Path("configs/default_satellite.toml"))
    scenario = parse_scenario(
        """
        name: Battery should not be critical
        steps:
          - action: assert
            name: critical_battery
            signal: eps.telemetry.battery_percent
            op: <
            value: 10
            timeout: "2s"
        """,
        setup=setup,
    )
    output = StringIO()

    result = run_scenario(
        scenario,
        setup,
        output=output,
        install_default_obc_rules=False,
    )

    assert not result.passed
    assert result.finished_at == 2_000_000
    assert len(result.assertions) == 1
    assertion = result.assertions[0]
    assert assertion.name == "critical_battery"
    assert not assertion.passed
    assert assertion.evaluated_at == 2_000_000
    assert output.getvalue().startswith(
        "FAIL t=2000000 critical_battery: eps.telemetry.battery_percent < 10; actual="
    )


def test_unrouted_bus_frame_warns_and_is_ignored_instead_of_aborting() -> None:
    setup = load_testbed_config(Path("configs/default_satellite.toml"))
    runtime = build_in_memory_runtime(setup)
    runner = ScenarioRunner(runtime)

    # No node is configured with these ports, so no command route matches.
    frame = pack(
        CspFields(priority=2, source=1, destination=2, destination_port=63, source_port=63),
        b"",
    )
    runtime.transport.send(frame, source=1)

    # A frame with no route is dropped, not a scenario-ending error: real CAN
    # buses routinely carry traffic this testbed does not own. Reaching this
    # point without an exception, after only a RuntimeWarning, is the point.
    with pytest.warns(RuntimeWarning, match="no configured route"):
        runner.wait(0)


def test_wait_raises_instead_of_silently_returning_early_when_budget_exhausted() -> None:
    setup = load_testbed_config(Path("configs/default_satellite.toml"))
    runtime = build_in_memory_runtime(setup)
    runner = ScenarioRunner(runtime, max_settle_events=2)

    with pytest.raises(ScenarioRuntimeError, match="max_settle_events=2"):
        runner.wait(3_600 * 1_000_000)


def test_assert_step_fails_gracefully_when_signal_never_observed_within_timeout() -> None:
    setup = load_testbed_config(Path("configs/default_satellite.toml"))
    scenario = parse_scenario(
        """
        name: Never-emitted signal
        steps:
          - action: assert
            name: unheard
            signal: eps.telemetry.battery_percent
            op: "=="
            value: 50
            timeout: "500us"
        """,
        setup=setup,
    )
    output = StringIO()
    runtime = build_in_memory_runtime(setup, install_default_obc_rules=False)
    # Detach the physical-step timer's telemetry source by using a runtime with
    # no EPS module wired: the config only maps the signal, nothing ever
    # transmits it, so it can never be observed within the short timeout.
    del runtime.modules["eps"]

    result = ScenarioRunner(runtime, output=output).run(scenario)

    assert not result.passed
    assertion = result.assertions[0]
    assert not assertion.passed
    assert assertion.actual is None
    assert assertion.detail == "eps.telemetry.battery_percent was never observed on the bus"


def test_telemetry_round_trips_through_a_real_csp_frame_on_the_bus() -> None:
    setup = load_testbed_config(Path("configs/default_satellite.toml"))
    runtime = build_in_memory_runtime(setup, install_default_obc_rules=False)
    # An independent sniffer endpoint, connected before anything is sent,
    # proves the EPS module's telemetry actually left as a real CSP-over-CAN
    # frame on the bus, not just a Python value pushed into the runner's
    # observable cache.
    runtime.transport.connect("sniffer")
    runner = ScenarioRunner(runtime)

    runner.wait(1_000_000)  # let the first physical step fire

    sniffed = [
        decode_frame(envelope.frame) for envelope in runtime.transport.drain(endpoint="sniffer")
    ]
    battery_frames = [packet for packet in sniffed if packet.fields.destination_port == 20]
    assert battery_frames, "expected an eps.telemetry.battery_percent CSP frame on the bus"

    mapping = setup.nodes["eps"].telemetry["battery_percent"]
    decoded_value = decode_telemetry_value("battery_percent", mapping, battery_frames[-1].payload)
    assert decoded_value == pytest.approx(
        runtime.module("eps").telemetry()["battery_percent"], rel=1e-4
    )
