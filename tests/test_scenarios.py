from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

from cubesat_testbed.config import load_scenario, load_testbed_config, parse_scenario
from cubesat_testbed.protocol.csp_v2 import CspFields, pack
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

    assert result.passed
    assert result.finished_at == 3_000_000
    assert [
        (assertion.name, assertion.passed, assertion.actual) for assertion in result.assertions
    ] == [("assert_3", True, "offline")]
    assert output.getvalue() == (
        "PASS t=3000000 assert_3: payload.telemetry.power_status == 'offline'; actual='offline'\n"
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
