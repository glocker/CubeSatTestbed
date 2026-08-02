from __future__ import annotations

import pytest

from cubesat_testbed.engine import CommandPayload, DiscreteEventEngine
from cubesat_testbed.fault_injection import FaultInjectionEngine
from cubesat_testbed.modules import (
    THERMAL_HEATER_OFF_COMMAND,
    THERMAL_HEATER_ON_COMMAND,
    THERMAL_HEATER_STUCK_OFF_FAULT,
    THERMAL_HEATER_STUCK_ON_FAULT,
    THERMAL_RADIATOR_DEGRADED_FAULT,
    ModuleCommandError,
    ModuleError,
    ThermalRcConfig,
    ThermalRcModule,
)


def _module(**params: object) -> ThermalRcModule:
    return ThermalRcModule(ThermalRcConfig(**params))  # type: ignore[arg-type]


def test_tick_integrates_toward_the_passive_equilibrium() -> None:
    """One Euler step must be exactly the closed form, not merely 'about right'.

    With C=10, k=2, ambient=-30 and 20 W of internal heat, a step of
    T <- T + (P - k(T - T_ambient)) * dt / C from 20 C is
    20 + (20 - 2*50) * 1 / 10 = 12, and the equilibrium it settles at is
    ambient + P/k = -20 C.
    """

    thermal = _module(initial_temperature_c=20.0)

    thermal.tick()

    assert thermal.temperature_c == pytest.approx(12.0)

    thermal.tick(200)

    assert thermal.temperature_c == pytest.approx(-20.0)


def test_heater_raises_the_equilibrium_it_settles_at() -> None:
    thermal = _module(initial_temperature_c=-20.0)

    assert thermal.handle_command(CommandPayload(THERMAL_HEATER_ON_COMMAND, target="thermal"))
    assert thermal.heater_status == "on"
    assert thermal.heater_power_w == 60.0

    thermal.tick(200)

    # ambient + (internal + heater) / k = -30 + 80 / 2
    assert thermal.temperature_c == pytest.approx(10.0)

    assert thermal.handle_command(CommandPayload(THERMAL_HEATER_OFF_COMMAND, target="thermal"))
    thermal.tick(200)

    assert thermal.temperature_c == pytest.approx(-20.0)


def test_tick_is_a_pure_function_of_state_so_runs_are_reproducible() -> None:
    first = _module()
    second = _module()

    first.tick(25)
    for _step in range(25):
        second.tick()

    assert first.temperature_c == second.temperature_c


def test_telemetry_reports_the_heater_as_a_wire_friendly_label() -> None:
    thermal = _module()

    telemetry = thermal.telemetry()

    assert telemetry["heater_status"] == "off"
    assert telemetry["heater_enabled"] is False
    assert telemetry["heater_power_w"] == 0.0
    assert telemetry["temperature_c"] == thermal.temperature_c


def test_emit_telemetry_schedules_every_signal_on_the_engine() -> None:
    engine = DiscreteEventEngine()
    thermal = _module()

    samples = thermal.emit_telemetry(engine)

    assert {sample.signal for sample in samples} == {
        "thermal.telemetry.temperature_c",
        "thermal.telemetry.heater_status",
        "thermal.telemetry.heater_enabled",
        "thermal.telemetry.heater_power_w",
    }
    assert all(sample.source == "thermal" for sample in samples)


def test_emit_telemetry_rejects_an_unknown_field() -> None:
    engine = DiscreteEventEngine()
    thermal = _module()

    with pytest.raises(ModuleError, match="telemetry field 'nope' is not supported"):
        thermal.emit_telemetry(engine, names=("nope",))


def test_unknown_command_targeting_this_module_fails_loudly() -> None:
    thermal = _module()

    with pytest.raises(ModuleCommandError, match="thermal command 'nope' is not supported"):
        thermal.handle_command(CommandPayload("nope", target="thermal"))


def test_command_for_another_module_is_declined_not_raised() -> None:
    thermal = _module()

    assert thermal.handle_command(CommandPayload(THERMAL_HEATER_ON_COMMAND, target="eps")) is False
    assert thermal.heater_status == "off"


def test_stuck_heater_faults_override_the_commanded_state() -> None:
    engine = DiscreteEventEngine()
    fault_engine = FaultInjectionEngine(engine)
    thermal = ThermalRcModule(ThermalRcConfig(), fault_engine=fault_engine)

    fault_engine.activate_named_fault("thermal.heater_stuck_on")

    assert thermal.is_fault_active(THERMAL_HEATER_STUCK_ON_FAULT)
    assert thermal.heater_status == "on"

    # The commanded state is still recorded: the fault models hardware that
    # ignores the command, not a command that was never sent.
    thermal.handle_command(CommandPayload(THERMAL_HEATER_OFF_COMMAND, target="thermal"))

    assert thermal.state.heater_commanded_on is False
    assert thermal.heater_status == "on"

    fault_engine.clear_named_fault("thermal.heater_stuck_on")
    fault_engine.activate_named_fault("thermal.heater_stuck_off")
    thermal.handle_command(CommandPayload(THERMAL_HEATER_ON_COMMAND, target="thermal"))

    assert thermal.is_fault_active(THERMAL_HEATER_STUCK_OFF_FAULT)
    assert thermal.heater_status == "off"
    assert thermal.heater_power_w == 0.0


def test_degraded_radiator_fault_reduces_conductance_and_raises_equilibrium() -> None:
    engine = DiscreteEventEngine()
    fault_engine = FaultInjectionEngine(engine)
    thermal = ThermalRcModule(
        ThermalRcConfig(radiator_degraded_conductance_factor=0.5),
        fault_engine=fault_engine,
    )

    fault_engine.activate_named_fault(f"thermal.{THERMAL_RADIATOR_DEGRADED_FAULT}")

    assert thermal.conductance_w_per_k == 1.0

    thermal.tick(400)

    # Half the conductance means twice the temperature rise over ambient:
    # -30 + 20 / 1.0 instead of -30 + 20 / 2.0.
    assert thermal.temperature_c == pytest.approx(-10.0)


def test_state_override_replaces_the_temperature_the_model_advances_from() -> None:
    engine = DiscreteEventEngine()
    fault_engine = FaultInjectionEngine(engine)
    thermal = ThermalRcModule(ThermalRcConfig(), fault_engine=fault_engine)

    fault_engine.state_override("thermal.model.temperature_c", 60.0)

    assert thermal.temperature_c == 60.0


def test_signal_override_spoofs_telemetry_without_touching_the_model() -> None:
    engine = DiscreteEventEngine()
    fault_engine = FaultInjectionEngine(engine)
    thermal = ThermalRcModule(
        ThermalRcConfig(initial_temperature_c=20.0), fault_engine=fault_engine
    )

    fault_engine.signal_override("thermal.telemetry.temperature_c", -50.0)

    assert thermal.telemetry()["temperature_c"] == -50.0
    assert thermal.state.temperature_c == 20.0


def test_config_rejects_an_integration_step_above_the_time_constant() -> None:
    """Explicit Euler rings above tau, so the config refuses to be built.

    A model that silently oscillates would look like physics and be believed.
    """

    with pytest.raises(ModuleError, match="tick_seconds must not exceed the node time constant"):
        ThermalRcConfig(thermal_capacity_j_per_k=10.0, conductance_w_per_k=2.0, tick_seconds=6.0)

    # Exactly at the boundary is still accepted, and so is any step when the
    # node has no conductive path at all (tau is infinite).
    assert ThermalRcConfig(
        thermal_capacity_j_per_k=10.0, conductance_w_per_k=2.0, tick_seconds=5.0
    ).time_constant_s == pytest.approx(5.0)
    assert ThermalRcConfig(conductance_w_per_k=0.0, tick_seconds=3600.0).time_constant_s == float(
        "inf"
    )


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"thermal_capacity_j_per_k": 0.0}, "thermal_capacity_j_per_k must be positive"),
        ({"conductance_w_per_k": -1.0}, "conductance_w_per_k must be non-negative"),
        ({"heater_power_w": -1.0}, "heater_power_w must be non-negative"),
        ({"initial_temperature_c": -300.0}, "must not be below absolute zero"),
        (
            {"radiator_degraded_conductance_factor": 0.0},
            "radiator_degraded_conductance_factor must be in range",
        ),
        ({"heater_initially_on": "yes"}, "heater_initially_on must be a bool"),
    ],
)
def test_config_rejects_physically_meaningless_values(
    params: dict[str, object], message: str
) -> None:
    with pytest.raises(ModuleError, match=message):
        ThermalRcConfig(**params)  # type: ignore[arg-type]
