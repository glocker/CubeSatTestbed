from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cubesat_testbed.config import (
    AssertStep,
    ConfigParseError,
    InjectFaultStep,
    NodeMode,
    ScenarioParseError,
    ScenarioReferenceError,
    SendCommandStep,
    SocketCanTransportConfig,
    WaitStep,
    load_scenario,
    load_testbed_config,
    parse_scenario,
    parse_testbed_config,
)
from cubesat_testbed.config import (
    TestbedConfig as SetupConfigModel,
)


def test_default_setup_and_low_battery_scenario_validate_together() -> None:
    setup = load_testbed_config(Path("configs/default_satellite.toml"))

    assert isinstance(setup, SetupConfigModel)
    assert setup.nodes["obc"].mode is NodeMode.SIMULATED
    assert setup.nodes["obc"].commands["payload_power_off"].payload_hex == b"\x00"
    assert setup.telemetry_signal_names() == frozenset(
        {"eps.telemetry.battery_percent", "payload.telemetry.power_status"}
    )

    scenario = load_scenario(Path("configs/scenarios/low_battery.yaml"), setup=setup)

    assert scenario.name == "EPS Low Battery Protection Test"
    assert len(scenario.steps) == 3
    assert isinstance(scenario.steps[0], InjectFaultStep)
    assert scenario.steps[0].duration == 5_000_000
    assert isinstance(scenario.steps[1], WaitStep)
    assert scenario.steps[1].virtual_time == 3_000_000
    assert isinstance(scenario.steps[2], AssertStep)
    assert scenario.steps[2].timeout == 1_000_000


def test_setup_rejects_simulated_node_without_module_type() -> None:
    with pytest.raises(ValidationError, match="simulated nodes must declare module_type"):
        parse_testbed_config(
            """
            [transport]
            type = "in-memory"

            [nodes.eps]
            mode = "simulated"
            address = 2
            """
        )


def test_setup_rejects_duplicate_csp_addresses() -> None:
    with pytest.raises(ValidationError, match="share CSP address 2"):
        parse_testbed_config(
            """
            [transport]
            type = "in-memory"

            [nodes.eps]
            mode = "simulated"
            module_type = "generic_eps"
            address = 2

            [nodes.payload]
            mode = "simulated"
            module_type = "simple_payload"
            address = 2
            """
        )


def test_setup_rejects_hardware_node_on_in_memory_transport() -> None:
    with pytest.raises(ValidationError, match="hardware nodes require transport.type='socketcan'"):
        parse_testbed_config(
            """
            [transport]
            type = "in-memory"

            [nodes.eps]
            mode = "hardware"
            address = 2
            """
        )


def test_setup_accepts_socketcan_hil_transport_for_hardware_node() -> None:
    setup = load_testbed_config(Path("configs/examples/socketcan_hil.toml"))

    assert isinstance(setup.transport, SocketCanTransportConfig)
    assert setup.transport.interface == "vcan0"
    assert setup.nodes["payload"].mode is NodeMode.HARDWARE


def test_setup_rejects_unknown_command_target() -> None:
    with pytest.raises(ValidationError, match="targets unknown node"):
        parse_testbed_config(
            """
            [transport]
            type = "in-memory"

            [nodes.obc]
            mode = "simulated"
            module_type = "obc_peer"
            address = 1

            [nodes.obc.commands.payload_power_off]
            target = "payload"
            destination_port = 10
            source_port = 10
            """
        )


def test_setup_rejects_telemetry_signal_mapped_to_wrong_node() -> None:
    with pytest.raises(ValidationError, match="expected it to start with eps.telemetry"):
        parse_testbed_config(
            """
            [transport]
            type = "in-memory"

            [nodes.eps]
            mode = "simulated"
            module_type = "generic_eps"
            address = 2

            [nodes.eps.telemetry.battery_percent]
            signal = "payload.telemetry.power_status"
            source_port = 20
            destination_port = 20
            offset = 0
            length = 4
            type = "float"
            """
        )


def test_setup_rejects_invalid_telemetry_wire_layout() -> None:
    with pytest.raises(ValidationError, match="invalid telemetry wire layout"):
        parse_testbed_config(
            """
            [transport]
            type = "in-memory"

            [nodes.eps]
            mode = "simulated"
            module_type = "generic_eps"
            address = 2

            [nodes.eps.telemetry.battery_percent]
            source_port = 20
            destination_port = 20
            offset = 0
            length = 3
            type = "float"
            """
        )


def test_setup_rejects_enum_on_a_float_telemetry_field() -> None:
    with pytest.raises(ValidationError, match="enum is only valid for 'uint'/'int'"):
        parse_testbed_config(
            """
            [transport]
            type = "in-memory"

            [nodes.eps]
            mode = "simulated"
            module_type = "generic_eps"
            address = 2

            [nodes.eps.telemetry.battery_percent]
            source_port = 20
            destination_port = 20
            offset = 0
            length = 4
            type = "float"

            [nodes.eps.telemetry.battery_percent.enum]
            0 = "empty"
            """
        )


def test_setup_rejects_duplicate_enum_labels() -> None:
    with pytest.raises(ValidationError, match="mapped by more than one raw value"):
        parse_testbed_config(
            """
            [transport]
            type = "in-memory"

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
            1 = "offline"
            """
        )


def test_setup_rejects_non_integer_enum_key() -> None:
    with pytest.raises(ValidationError, match="must be an integer raw value"):
        parse_testbed_config(
            """
            [transport]
            type = "in-memory"

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
            offline = "offline"
            """
        )


def test_setup_accepts_an_obc_rule_with_a_command_action() -> None:
    setup = parse_testbed_config(
        """
        [transport]
        type = "in-memory"

        [nodes.obc]
        mode = "simulated"
        module_type = "obc_peer"
        address = 1

        [nodes.payload]
        mode = "simulated"
        module_type = "simple_payload"
        address = 3

        [nodes.obc.commands.payload_power_off]
        target = "payload"
        destination_port = 10
        source_port = 10

        [nodes.obc.rules.low_battery_shed_payload]
        signal = "eps.telemetry.battery_percent"
        op = "<"
        threshold = 30.0
        for = "3s"
        cooldown = "10s"

        [[nodes.obc.rules.low_battery_shed_payload.actions]]
        type = "send_command"
        command = "payload_power_off"
        """
    )

    rule = setup.nodes["obc"].rules["low_battery_shed_payload"]
    assert rule.signal == "eps.telemetry.battery_percent"
    assert rule.op == "<"
    assert rule.threshold == 30.0
    assert rule.for_duration == 3_000_000
    assert rule.cooldown == 10_000_000
    assert len(rule.actions) == 1


def test_setup_accepts_an_obc_rule_with_a_fault_action() -> None:
    setup = parse_testbed_config(
        """
        [transport]
        type = "in-memory"

        [nodes.obc]
        mode = "simulated"
        module_type = "obc_peer"
        address = 1

        [nodes.obc.rules.overheat_degrade_battery]
        signal = "eps.telemetry.temperature_c"
        op = ">"
        threshold = 55.0
        for = "2s"

        [[nodes.obc.rules.overheat_degrade_battery.actions]]
        type = "inject_fault"
        fault_type = "named_fault"
        target = "eps.battery_cell_dead"
        """
    )

    action = setup.nodes["obc"].rules["overheat_degrade_battery"].actions[0]
    assert action.type == "inject_fault"
    assert action.target == "eps.battery_cell_dead"


def test_setup_rejects_rules_for_non_obc_peer_module_type() -> None:
    with pytest.raises(ValidationError, match="rules is only valid for module_type = 'obc_peer'"):
        parse_testbed_config(
            """
            [transport]
            type = "in-memory"

            [nodes.eps]
            mode = "simulated"
            module_type = "generic_eps"
            address = 2

            [nodes.eps.rules.bogus]
            signal = "eps.telemetry.battery_percent"
            op = "<"
            threshold = 30.0

            [[nodes.eps.rules.bogus.actions]]
            type = "send_command"
            command = "whatever"
            """
        )


def test_setup_rejects_rule_with_no_actions() -> None:
    with pytest.raises(ValidationError):
        parse_testbed_config(
            """
            [transport]
            type = "in-memory"

            [nodes.obc]
            mode = "simulated"
            module_type = "obc_peer"
            address = 1

            [nodes.obc.rules.empty]
            signal = "eps.telemetry.battery_percent"
            op = "<"
            threshold = 30.0
            actions = []
            """
        )


def test_setup_rejects_named_fault_rule_action_with_duration() -> None:
    with pytest.raises(
        ValidationError, match="named_fault requests do not support duration or cycles"
    ):
        parse_testbed_config(
            """
            [transport]
            type = "in-memory"

            [nodes.obc]
            mode = "simulated"
            module_type = "obc_peer"
            address = 1

            [nodes.obc.rules.bad]
            signal = "eps.telemetry.battery_percent"
            op = "<"
            threshold = 30.0

            [[nodes.obc.rules.bad.actions]]
            type = "inject_fault"
            fault_type = "named_fault"
            target = "eps.battery_cell_dead"
            duration = "5s"
            """
        )


def test_setup_rejects_oversize_single_frame_command_payload() -> None:
    with pytest.raises(ValidationError, match="single-frame CSP payload"):
        parse_testbed_config(
            """
            [transport]
            type = "in-memory"

            [nodes.obc]
            mode = "simulated"
            module_type = "obc_peer"
            address = 1

            [nodes.payload]
            mode = "simulated"
            module_type = "simple_payload"
            address = 3

            [nodes.obc.commands.too_large]
            target = "payload"
            destination_port = 10
            source_port = 10
            payload_hex = "00 01 02 03 04"
            """
        )


def test_scenario_parsing_is_independent_from_execution() -> None:
    scenario = parse_scenario(
        """
        name: Command smoke test
        steps:
          - action: send_command
            command: payload_power_off
            source: obc
            target: payload
            payload:
              reason: low_battery
        """
    )

    assert isinstance(scenario.steps[0], SendCommandStep)
    assert scenario.steps[0].payload == {"reason": "low_battery"}


def test_scenario_rejects_invalid_fault_target_combination() -> None:
    with pytest.raises(ValidationError, match="state_override targets must include '.model.'"):
        parse_scenario(
            """
            name: Invalid fault
            steps:
              - action: inject_fault
                type: state_override
                target: eps.telemetry.voltage
                value: 1
            """
        )


def test_scenario_parses_fault_cycle_expiration() -> None:
    scenario = parse_scenario(
        """
        name: Cycle-expiring telemetry spoof
        steps:
          - action: inject_fault
            type: signal_override
            target: eps.telemetry.voltage_mv
            value: 4500
            cycles: "2 cycles"
        """
    )

    step = scenario.steps[0]
    assert isinstance(step, InjectFaultStep)
    assert step.duration is None
    assert step.cycles == 2


def test_scenario_rejects_named_fault_expiration() -> None:
    with pytest.raises(
        ValidationError, match="named_fault requests do not support duration or cycles"
    ):
        parse_scenario(
            """
            name: Invalid named fault expiration
            steps:
              - action: inject_fault
                type: named_fault
                target: eps.battery_cell_dead
                duration: "5s"
            """
        )


def test_scenario_reference_validation_rejects_unknown_command() -> None:
    setup = load_testbed_config(Path("configs/default_satellite.toml"))

    with pytest.raises(ScenarioReferenceError, match="command 'missing_command' is not configured"):
        parse_scenario(
            """
            name: Unknown command
            steps:
              - action: send_command
                command: missing_command
            """,
            setup=setup,
        )


def test_scenario_reference_validation_rejects_unmapped_assertion_signal() -> None:
    setup = load_testbed_config(Path("configs/default_satellite.toml"))

    with pytest.raises(ScenarioReferenceError, match="assertion signal 'eps.telemetry.unknown'"):
        parse_scenario(
            """
            name: Unknown signal
            steps:
              - action: assert
                signal: eps.telemetry.unknown
                op: ==
                value: 1
            """,
            setup=setup,
        )


def test_parse_errors_are_explicit_before_schema_validation() -> None:
    with pytest.raises(ConfigParseError, match="invalid setup TOML"):
        parse_testbed_config("[transport\n")

    with pytest.raises(ScenarioParseError, match="scenario YAML must contain a mapping"):
        parse_scenario("")
