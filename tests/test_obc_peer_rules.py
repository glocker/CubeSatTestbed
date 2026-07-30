from __future__ import annotations

import pytest

from cubesat_testbed.config import load_testbed_config
from cubesat_testbed.engine import DiscreteEventEngine
from cubesat_testbed.fault_injection import FaultInjectionEngine, FaultInjectionKind
from cubesat_testbed.modules import (
    ModuleError,
    ObcPeerCommandAction,
    ObcPeerConfig,
    ObcPeerFaultAction,
    ObcPeerModule,
    ObcPeerNamedCommand,
    ObcPeerRule,
    ObcPeerThresholdCondition,
    ThresholdOperator,
)
from cubesat_testbed.protocol.csp_v2 import decode_frame
from cubesat_testbed.transport import InMemoryBusAdapter


def test_obc_peer_for_duration_and_cooldown_gate_named_command_emission() -> None:
    bus = InMemoryBusAdapter(endpoints=(1, 3))
    obc = ObcPeerModule(
        ObcPeerConfig(
            endpoint=1,
            commands={
                "payload_power_off": ObcPeerNamedCommand(
                    source_address=1,
                    destination_address=3,
                    destination_port=10,
                    source_port=10,
                    payload=b"\x00",
                )
            },
            rules=(
                ObcPeerRule(
                    name="low_battery_shed_payload",
                    condition=ObcPeerThresholdCondition(
                        "eps.telemetry.battery_percent",
                        ThresholdOperator.LT,
                        30.0,
                    ),
                    for_duration=3,
                    cooldown=10,
                    actions=(ObcPeerCommandAction("payload_power_off"),),
                ),
            ),
        ),
        transport=bus,
    )

    assert obc.observe_telemetry("eps.telemetry.battery_percent", 25.0, now=0) == ()
    assert obc.observe_telemetry("eps.telemetry.battery_percent", 25.0, now=2) == ()
    first_results = obc.observe_telemetry("eps.telemetry.battery_percent", 25.0, now=3)

    assert [result.rule_name for result in first_results] == ["low_battery_shed_payload"]
    assert bus.pending_count() == 1
    envelope = bus.receive()
    assert envelope is not None
    assert envelope.source == 1

    decoded = decode_frame(envelope.frame)
    assert decoded.fields.source == 1
    assert decoded.fields.destination == 3
    assert decoded.fields.destination_port == 10
    assert decoded.fields.source_port == 10
    assert decoded.payload == b"\x00"

    assert obc.observe_telemetry("eps.telemetry.battery_percent", 25.0, now=4) == ()
    assert bus.receive() is None

    second_results = obc.observe_telemetry("eps.telemetry.battery_percent", 25.0, now=13)

    assert [result.rule_name for result in second_results] == ["low_battery_shed_payload"]
    assert bus.pending_count() == 1


def test_obc_peer_false_condition_resets_for_duration() -> None:
    bus = InMemoryBusAdapter()
    obc = ObcPeerModule(
        ObcPeerConfig(
            commands={
                "shed_payload": ObcPeerNamedCommand(
                    source_address=1,
                    destination_address=3,
                    destination_port=10,
                    source_port=10,
                )
            },
            rules=(
                ObcPeerRule(
                    name="low_battery",
                    condition=ObcPeerThresholdCondition(
                        "eps.telemetry.battery_percent",
                        "<",
                        30.0,
                    ),
                    for_duration=2,
                    actions=(ObcPeerCommandAction("shed_payload"),),
                ),
            ),
        ),
        transport=bus,
    )

    assert obc.observe_telemetry("eps.telemetry.battery_percent", 25.0, now=0) == ()
    assert obc.observe_telemetry("eps.telemetry.battery_percent", 31.0, now=1) == ()
    assert obc.observe_telemetry("eps.telemetry.battery_percent", 25.0, now=2) == ()
    assert obc.observe_telemetry("eps.telemetry.battery_percent", 25.0, now=3) == ()
    assert bus.receive() is None

    results = obc.observe_telemetry("eps.telemetry.battery_percent", 25.0, now=4)

    assert [result.rule_name for result in results] == ["low_battery"]
    assert bus.pending_count() == 1


def test_obc_peer_attach_listens_to_engine_telemetry_events() -> None:
    engine = DiscreteEventEngine()
    bus = InMemoryBusAdapter()
    obc = ObcPeerModule(
        ObcPeerConfig(
            commands={
                "payload_power_off": ObcPeerNamedCommand(
                    source_address=1,
                    destination_address=3,
                    destination_port=10,
                    source_port=10,
                    payload=b"\x00",
                )
            },
            rules=(
                ObcPeerRule(
                    name="low_battery",
                    condition=ObcPeerThresholdCondition(
                        "eps.telemetry.battery_percent",
                        ThresholdOperator.LTE,
                        25.0,
                    ),
                    actions=(ObcPeerCommandAction("payload_power_off"),),
                ),
            ),
        ),
        transport=bus,
    )
    obc.attach(engine)

    engine.schedule_telemetry("eps.telemetry.battery_percent", 25.0, delay=5, source="eps")
    engine.run_until_idle()

    assert obc.last_results[0].triggered_at == 5
    assert bus.pending_count() == 1


def test_obc_peer_fault_actions_call_passive_fault_engine() -> None:
    faults = FaultInjectionEngine()
    obc = ObcPeerModule(
        ObcPeerConfig(
            rules=(
                ObcPeerRule(
                    name="high_voltage_spoof",
                    condition=ObcPeerThresholdCondition(
                        "eps.telemetry.battery_voltage_v",
                        ">",
                        8.0,
                    ),
                    actions=(
                        ObcPeerFaultAction(
                            FaultInjectionKind.SIGNAL_OVERRIDE,
                            "eps.telemetry.battery_voltage_v",
                            value=8.0,
                            duration=5,
                        ),
                        ObcPeerFaultAction("named_fault", "eps.bus_undervoltage"),
                    ),
                ),
            ),
        ),
        fault_engine=faults,
    )

    results = obc.observe_telemetry("eps.telemetry.battery_voltage_v", 8.4, now=7)

    assert [result.rule_name for result in results] == ["high_voltage_spoof"]
    override = faults.get_signal_override("eps.telemetry.battery_voltage_v", now=7)
    assert override is not None
    assert override.value == 8.0
    assert override.expires_at == 12
    assert faults.is_named_fault_active("eps.bus_undervoltage")


def test_obc_peer_uses_setup_command_mappings_for_csp_payloads() -> None:
    setup = load_testbed_config("configs/default_satellite.toml")
    bus = InMemoryBusAdapter()
    obc = ObcPeerModule.from_testbed_config(
        setup,
        rules=(
            ObcPeerRule(
                name="low_battery",
                condition=ObcPeerThresholdCondition(
                    "eps.telemetry.battery_percent",
                    "<",
                    30.0,
                ),
                actions=(ObcPeerCommandAction("payload_power_off"),),
            ),
        ),
        transport=bus,
    )

    obc.observe_telemetry("eps.telemetry.battery_percent", 25.0, now=0)

    envelope = bus.receive()
    assert envelope is not None
    assert envelope.source == setup.nodes["obc"].address
    decoded = decode_frame(envelope.frame)
    assert decoded.fields.source == setup.nodes["obc"].address
    assert decoded.fields.destination == setup.nodes["payload"].address
    assert decoded.payload == setup.nodes["obc"].commands["payload_power_off"].payload_hex


def test_obc_peer_fails_loudly_when_required_runtime_dependency_is_missing() -> None:
    obc = ObcPeerModule(
        ObcPeerConfig(
            rules=(
                ObcPeerRule(
                    name="low_battery",
                    condition=ObcPeerThresholdCondition(
                        "eps.telemetry.battery_percent",
                        "<",
                        30.0,
                    ),
                    actions=(ObcPeerCommandAction("payload_power_off"),),
                ),
            ),
        )
    )

    with pytest.raises(ModuleError, match="transport adapter"):
        obc.observe_telemetry("eps.telemetry.battery_percent", 25.0, now=0)
