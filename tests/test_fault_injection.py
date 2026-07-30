from __future__ import annotations

import pytest

from cubesat_testbed.engine import DiscreteEventEngine
from cubesat_testbed.fault_injection import (
    FaultInjectionEngine,
    FaultInjectionError,
    FaultInjectionKind,
)


def test_state_override_lifecycle_expires_by_virtual_time() -> None:
    engine = DiscreteEventEngine()
    faults = FaultInjectionEngine(engine)

    engine.schedule_fault(
        "state_override",
        "eps.model.battery_percent",
        value=25,
        duration=5,
    )

    engine.run_next()

    override = faults.get_state_override("eps.model.battery_percent")
    assert override is not None
    assert override.kind is FaultInjectionKind.STATE_OVERRIDE
    assert override.value == 25
    assert override.activated_at == 0
    assert override.expires_at == 5
    assert faults.resolve_state_value("eps.model.battery_percent", 80) == 25

    engine.run_until(4)
    assert faults.has_state_override("eps.model.battery_percent")

    engine.run_until(5)
    assert not faults.has_state_override("eps.model.battery_percent")
    assert faults.resolve_state_value("eps.model.battery_percent", 80) == 80


def test_signal_override_lifecycle_expires_by_cycles() -> None:
    faults = FaultInjectionEngine()

    override = faults.signal_override("eps.telemetry.voltage_mv", 4500, cycles=2)

    assert override.expires_after_cycle == 2
    assert faults.resolve_signal_value("eps.telemetry.voltage_mv", 7400) == 4500
    assert faults.advance_cycles() == ()
    assert faults.resolve_signal_value("eps.telemetry.voltage_mv", 7400) == 4500

    assert faults.advance_cycles() == (override,)
    assert not faults.has_signal_override("eps.telemetry.voltage_mv")
    assert faults.resolve_signal_value("eps.telemetry.voltage_mv", 7400) == 7400


def test_override_expires_when_first_time_or_cycle_boundary_is_reached() -> None:
    engine = DiscreteEventEngine()
    faults = FaultInjectionEngine(engine)

    faults.state_override("eps.model.temperature_c", 95.0, duration=10, cycles=1)
    engine.run_until(5)

    expired = faults.advance_cycles()

    assert [override.target for override in expired] == ["eps.model.temperature_c"]
    assert not faults.has_state_override("eps.model.temperature_c")


def test_named_fault_activation_and_explicit_clearing() -> None:
    faults = FaultInjectionEngine()

    flag = faults.activate_named_fault("eps.battery_cell_dead")

    assert flag.module == "eps"
    assert flag.name == "battery_cell_dead"
    assert flag.target == "eps.battery_cell_dead"
    assert faults.is_named_fault_active("eps.battery_cell_dead")
    assert faults.is_module_fault_active("eps", "battery_cell_dead")
    assert faults.active_named_faults() == frozenset({"eps.battery_cell_dead"})
    assert faults.active_named_faults("eps") == frozenset({"battery_cell_dead"})

    assert faults.clear_named_fault("eps.battery_cell_dead") == flag
    assert not faults.is_named_fault_active("eps.battery_cell_dead")
    assert faults.active_named_faults("eps") == frozenset()


def test_fault_event_can_clear_named_fault_with_false_value() -> None:
    engine = DiscreteEventEngine()
    faults = FaultInjectionEngine(engine)

    engine.schedule_fault("named_fault", "eps.battery_cell_dead")
    engine.schedule_fault("named_fault", "eps.battery_cell_dead", value=False, delay=1)

    engine.run_next()
    assert faults.is_named_fault_active("eps.battery_cell_dead")

    engine.run_next()
    assert not faults.is_named_fault_active("eps.battery_cell_dead")


def test_direct_override_replacement_and_manual_clear() -> None:
    faults = FaultInjectionEngine()

    first = faults.state_override("eps.model.temperature_c", 80.0, duration=10)
    second = faults.state_override("eps.model.temperature_c", 95.0, duration=3)

    assert first != second
    assert faults.resolve_state_value("eps.model.temperature_c", 20.0) == 95.0
    assert faults.clear_state_override("eps.model.temperature_c") == second
    assert faults.resolve_state_value("eps.model.temperature_c", 20.0) == 20.0


def test_fault_engine_rejects_wrong_target_kinds() -> None:
    faults = FaultInjectionEngine()

    with pytest.raises(FaultInjectionError, match=r"\.model\."):
        faults.state_override("eps.telemetry.battery_percent", 25)

    with pytest.raises(FaultInjectionError, match=r"\.telemetry\."):
        faults.signal_override("eps.model.voltage", 4500)

    with pytest.raises(FaultInjectionError, match="module fault flag"):
        faults.activate_named_fault("eps.model.battery_percent")


def test_named_faults_do_not_support_expiration() -> None:
    faults = FaultInjectionEngine()

    with pytest.raises(FaultInjectionError, match="do not support duration or cycles"):
        faults.apply("named_fault", "eps.battery_cell_dead", duration=5)
