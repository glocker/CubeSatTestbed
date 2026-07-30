from __future__ import annotations

import pytest

from cubesat_testbed.engine import CommandPayload, DiscreteEventEngine, EventKind, SimulationEvent
from cubesat_testbed.fault_injection import FaultInjectionEngine
from cubesat_testbed.modules import (
    EPS_BATTERY_CELL_DEAD_FAULT,
    EPS_BUS_UNDERVOLTAGE_FAULT,
    EPS_PAYLOAD_RAIL_OFF_COMMAND,
    EPS_PAYLOAD_RAIL_STUCK_OFF_FAULT,
    EPS_SOLAR_ARRAY_OFFLINE_FAULT,
    PAYLOAD_DOWNLINK_DATA_COMMAND,
    PAYLOAD_POWER_OFF_COMMAND,
    PAYLOAD_START_CAPTURE_COMMAND,
    EpsPowerMode,
    GenericEpsConfig,
    GenericEpsModule,
    SimplePayloadConfig,
    SimplePayloadModule,
)


def test_payload_commands_update_power_data_volume_and_power_draw() -> None:
    payload = SimplePayloadModule(
        SimplePayloadConfig(
            data_rate_bytes_per_tick=10,
            one_shot_capture_bytes=5,
            max_data_volume_bytes=25,
            standby_power_w=0.5,
            active_power_w=4.0,
        )
    )

    assert payload.power_status == "online"
    assert payload.power_draw_w == 0.5

    assert payload.handle_command(CommandPayload(PAYLOAD_START_CAPTURE_COMMAND, target="payload"))
    assert payload.tick(3) == 25
    assert payload.data_volume_bytes == 25
    assert payload.power_draw_w == 4.0

    assert payload.handle_command(
        CommandPayload(PAYLOAD_DOWNLINK_DATA_COMMAND, payload={"bytes": 7}, target="payload")
    )
    assert payload.data_volume_bytes == 18

    assert payload.handle_command(CommandPayload(PAYLOAD_POWER_OFF_COMMAND, target="payload"))
    assert payload.power_status == "offline"
    assert payload.power_draw_w == 0.0
    assert not payload.capture_enabled
    assert payload.tick() == 0


def test_payload_telemetry_uses_signal_overrides_and_engine_events() -> None:
    engine = DiscreteEventEngine()
    faults = FaultInjectionEngine()
    payload = SimplePayloadModule(fault_engine=faults)
    seen: list[tuple[str, object, object]] = []

    def record(event: SimulationEvent, _engine: DiscreteEventEngine) -> None:
        assert event.kind is EventKind.TELEMETRY
        telemetry = event.payload
        assert telemetry is not None
        seen.append((telemetry.signal, telemetry.value, telemetry.source))

    engine.add_handler(EventKind.TELEMETRY, record)
    faults.signal_override("payload.telemetry.power_status", "offline")

    samples = payload.emit_telemetry(engine, names=("power_status",))
    engine.run_until_idle()

    assert [(sample.signal, sample.value, sample.source) for sample in samples] == [
        ("payload.telemetry.power_status", "offline", "payload")
    ]
    assert seen == [("payload.telemetry.power_status", "offline", "payload")]


def test_eps_battery_model_includes_payload_load() -> None:
    payload = SimplePayloadModule(
        SimplePayloadConfig(
            standby_power_w=0.5,
            active_power_w=4.0,
            data_rate_bytes_per_tick=1,
        )
    )
    payload.handle_command(PAYLOAD_START_CAPTURE_COMMAND)
    eps = GenericEpsModule(
        GenericEpsConfig(
            initial_battery_percent=50.0,
            battery_capacity_wh=100.0,
            tick_seconds=3600.0,
            base_load_w=1.0,
            solar_input_w=0.0,
        ),
        payload=payload,
    )

    telemetry = eps.tick()

    assert telemetry["load_power_w"] == 5.0
    assert telemetry["battery_percent"] == pytest.approx(45.0)
    assert telemetry["power_mode"] == EpsPowerMode.NOMINAL.value


def test_eps_payload_rail_command_controls_payload_and_load() -> None:
    payload = SimplePayloadModule(SimplePayloadConfig(active_power_w=4.0))
    payload.handle_command(PAYLOAD_START_CAPTURE_COMMAND)
    eps = GenericEpsModule(GenericEpsConfig(base_load_w=1.0, solar_input_w=0.0), payload=payload)

    assert eps.load_power_w == 5.0
    assert payload.power_status == "online"

    assert eps.handle_command(CommandPayload(EPS_PAYLOAD_RAIL_OFF_COMMAND, target="eps"))

    assert not eps.payload_rail_enabled
    assert payload.power_status == "offline"
    assert eps.telemetry()["load_power_w"] == 1.0


def test_eps_named_fault_reactions_affect_telemetry_and_payload_rail() -> None:
    faults = FaultInjectionEngine()
    payload = SimplePayloadModule(SimplePayloadConfig(active_power_w=4.0), fault_engine=faults)
    payload.handle_command(PAYLOAD_START_CAPTURE_COMMAND)
    eps = GenericEpsModule(
        GenericEpsConfig(
            initial_battery_percent=80.0,
            base_load_w=1.0,
            solar_input_w=2.0,
            battery_cell_dead_capacity_factor=0.5,
            battery_cell_dead_voltage_drop_v=1.2,
            undervoltage_fault_voltage_v=4.8,
        ),
        payload=payload,
        fault_engine=faults,
    )

    faults.activate_named_fault(f"eps.{EPS_BATTERY_CELL_DEAD_FAULT}")
    faults.activate_named_fault(f"eps.{EPS_SOLAR_ARRAY_OFFLINE_FAULT}")
    faults.activate_named_fault(f"eps.{EPS_BUS_UNDERVOLTAGE_FAULT}")
    faults.activate_named_fault(f"eps.{EPS_PAYLOAD_RAIL_STUCK_OFF_FAULT}")

    telemetry = eps.tick(0)

    assert telemetry["battery_percent"] == 50.0
    assert telemetry["solar_input_w"] == 0.0
    assert telemetry["battery_voltage_v"] == 4.8
    assert not telemetry["payload_rail_enabled"]
    assert payload.power_status == "offline"
    assert eps.active_named_faults() == frozenset(
        {
            EPS_BATTERY_CELL_DEAD_FAULT,
            EPS_BUS_UNDERVOLTAGE_FAULT,
            EPS_PAYLOAD_RAIL_STUCK_OFF_FAULT,
            EPS_SOLAR_ARRAY_OFFLINE_FAULT,
        }
    )


def test_eps_state_and_signal_overrides_are_passive_inputs() -> None:
    engine = DiscreteEventEngine()
    faults = FaultInjectionEngine(engine)
    eps = GenericEpsModule(fault_engine=faults)
    seen: list[object] = []

    def record(event: SimulationEvent, _engine: DiscreteEventEngine) -> None:
        telemetry = event.payload
        assert telemetry is not None
        seen.append(telemetry.value)

    engine.add_handler(EventKind.TELEMETRY, record)
    engine.schedule_fault("state_override", "eps.model.battery_percent", value=25.0, duration=5)
    engine.schedule_fault("signal_override", "eps.telemetry.power_mode", value="forced")
    engine.run_until_idle()

    assert eps.telemetry()["battery_percent"] == 25.0
    assert eps.telemetry()["power_mode"] == "forced"

    eps.emit_telemetry(engine, names=("battery_percent", "power_mode"))
    engine.run_until_idle()

    assert seen == [25.0, "forced"]
