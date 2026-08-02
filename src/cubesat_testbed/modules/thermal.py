"""Single-node RC thermal model with a commandable heater.

This module is deliberately the smallest subsystem model in the package that
is still worth running, because it doubles as the worked example in
``docs/writing-a-module.md``: one state variable, one first-order ODE, one
command pair, three named faults, and no dependency on any other module. A
thermal loop that actually closes -- telemetry crosses the bus, an OBC Peer
rule reads it, the resulting command comes back over the same bus and changes
the model -- is built in the ``thermal-heater`` packaged example, not wired
between modules in Python.

The model is a lumped first-order (RC) node::

    C dT/dt = P_internal + P_heater - k (T - T_ambient)

with ``C`` the node's heat capacity (J/K) and ``k`` its conductance to the
environment (W/K). Integration is explicit Euler at the module's own
``tick_seconds``, which keeps a tick a pure function of the previous state --
no wall-clock time, no accumulated drift, identical results on every run.
Explicit Euler is only stable while the step stays below the node's time
constant ``tau = C / k``, so the config rejects a step that exceeds it rather
than silently producing an oscillating temperature.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cubesat_testbed.engine import CommandPayload, DiscreteEventEngine, VirtualTime
from cubesat_testbed.fault_injection import FaultInjectionEngine
from cubesat_testbed.modules.base import (
    ModuleCommandError,
    ModuleError,
    SimulatedModule,
    TelemetrySample,
    _coerce_bool_value,
    _coerce_finite_float_value,
    _coerce_non_negative_float_value,
    _coerce_non_negative_int_value,
    _coerce_positive_float_value,
    _coerce_unit_interval_exclusive_zero,
    _validate_identifier,
    _validate_optional_endpoint,
    normalize_command,
)
from cubesat_testbed.transport.base import EndpointId

if TYPE_CHECKING:
    from cubesat_testbed.modules.registry import ModuleBuildContext

HeaterStatus = Literal["on", "off"]

THERMAL_HEATER_ON_COMMAND = "thermal_heater_on"
THERMAL_HEATER_OFF_COMMAND = "thermal_heater_off"

THERMAL_HEATER_STUCK_OFF_FAULT = "heater_stuck_off"
THERMAL_HEATER_STUCK_ON_FAULT = "heater_stuck_on"
THERMAL_RADIATOR_DEGRADED_FAULT = "radiator_degraded"

_ABSOLUTE_ZERO_C = -273.15


@dataclass(frozen=True, slots=True)
class ThermalRcConfig:
    """Configuration knobs for :class:`ThermalRcModule`.

    The defaults describe a small, deliberately fast node -- a time constant of
    a few virtual seconds -- so a scenario can watch it cool, heat and recover
    inside a handful of virtual seconds instead of the tens of minutes a real
    structural panel would take. Retune it for a real board through
    ``[nodes.<node>.params]``; nothing here is baked into the engine.
    """

    name: str = "thermal"
    endpoint: EndpointId | None = None
    initial_temperature_c: float = 20.0
    ambient_temperature_c: float = -30.0
    thermal_capacity_j_per_k: float = 10.0
    conductance_w_per_k: float = 2.0
    internal_heat_w: float = 20.0
    heater_power_w: float = 60.0
    heater_initially_on: bool = False
    tick_seconds: float = 1.0
    radiator_degraded_conductance_factor: float = 0.25

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_identifier("module name", self.name))
        object.__setattr__(
            self,
            "endpoint",
            _validate_optional_endpoint("module endpoint", self.endpoint),
        )
        object.__setattr__(
            self,
            "initial_temperature_c",
            _coerce_temperature_value("initial_temperature_c", self.initial_temperature_c),
        )
        object.__setattr__(
            self,
            "ambient_temperature_c",
            _coerce_temperature_value("ambient_temperature_c", self.ambient_temperature_c),
        )
        object.__setattr__(
            self,
            "thermal_capacity_j_per_k",
            _coerce_positive_float_value("thermal_capacity_j_per_k", self.thermal_capacity_j_per_k),
        )
        object.__setattr__(
            self,
            "conductance_w_per_k",
            _coerce_non_negative_float_value("conductance_w_per_k", self.conductance_w_per_k),
        )
        object.__setattr__(
            self,
            "internal_heat_w",
            _coerce_non_negative_float_value("internal_heat_w", self.internal_heat_w),
        )
        object.__setattr__(
            self,
            "heater_power_w",
            _coerce_non_negative_float_value("heater_power_w", self.heater_power_w),
        )
        object.__setattr__(
            self,
            "heater_initially_on",
            _coerce_bool_value("heater_initially_on", self.heater_initially_on),
        )
        object.__setattr__(
            self,
            "tick_seconds",
            _coerce_positive_float_value("tick_seconds", self.tick_seconds),
        )
        object.__setattr__(
            self,
            "radiator_degraded_conductance_factor",
            _coerce_unit_interval_exclusive_zero(
                "radiator_degraded_conductance_factor",
                self.radiator_degraded_conductance_factor,
            ),
        )
        self._validate_integration_stability()

    @property
    def time_constant_s(self) -> float:
        """Node time constant ``C / k`` in seconds, or ``inf`` when uncoupled."""

        if self.conductance_w_per_k == 0.0:
            return float("inf")
        return self.thermal_capacity_j_per_k / self.conductance_w_per_k

    def _validate_integration_stability(self) -> None:
        # Explicit Euler on this ODE decays by a factor (1 - dt/tau) per step:
        # above tau the sign flips and the model rings, above 2*tau it diverges.
        # A model that silently oscillates is worse than one that refuses to be
        # configured, so the boundary is a validation error, not a docs note.
        # The worst case is the degraded radiator, whose lower conductance only
        # lengthens tau, so checking the nominal value is sufficient.
        if self.tick_seconds > self.time_constant_s:
            raise ModuleError(
                f"tick_seconds must not exceed the node time constant "
                f"thermal_capacity_j_per_k / conductance_w_per_k "
                f"({self.time_constant_s:g}s); a larger step makes explicit Euler "
                "integration oscillate instead of settling"
            )


@dataclass(slots=True)
class ThermalRcState:
    """Mutable state owned by the RC thermal model."""

    temperature_c: float
    heater_commanded_on: bool


class ThermalRcModule(SimulatedModule):
    """Deterministic lumped-capacitance thermal node with a commandable heater."""

    def __init__(
        self,
        config: ThermalRcConfig | None = None,
        *,
        fault_engine: FaultInjectionEngine | None = None,
    ) -> None:
        self.config = ThermalRcConfig() if config is None else config
        self.state = ThermalRcState(
            temperature_c=self.config.initial_temperature_c,
            heater_commanded_on=self.config.heater_initially_on,
        )
        super().__init__(
            self.config.name,
            endpoint=self.config.endpoint,
            fault_engine=fault_engine,
        )

    @property
    def temperature_c(self) -> float:
        """Effective node temperature after model-state overrides."""

        resolved = self.resolve_model_value("temperature_c", self.state.temperature_c)
        return _coerce_temperature_value(self.model_path("temperature_c"), resolved)

    @property
    def heater_enabled(self) -> bool:
        """Effective heater state after stuck-heater faults and state overrides."""

        enabled = self.state.heater_commanded_on
        if self.is_fault_active(THERMAL_HEATER_STUCK_OFF_FAULT):
            enabled = False
        if self.is_fault_active(THERMAL_HEATER_STUCK_ON_FAULT):
            enabled = True
        resolved = self.resolve_model_value("heater_enabled", enabled)
        return _coerce_bool_value(self.model_path("heater_enabled"), resolved)

    @property
    def heater_status(self) -> HeaterStatus:
        """Human-readable heater state, the form carried on the wire."""

        return "on" if self.heater_enabled else "off"

    @property
    def heater_power_w(self) -> float:
        """Effective heater power drawn into the node."""

        computed = self.config.heater_power_w if self.heater_enabled else 0.0
        resolved = self.resolve_model_value("heater_power_w", computed)
        return _coerce_non_negative_float_value(self.model_path("heater_power_w"), resolved)

    @property
    def conductance_w_per_k(self) -> float:
        """Effective conductance to the environment after radiator faults."""

        computed = self.config.conductance_w_per_k
        if self.is_fault_active(THERMAL_RADIATOR_DEGRADED_FAULT):
            computed *= self.config.radiator_degraded_conductance_factor
        resolved = self.resolve_model_value("conductance_w_per_k", computed)
        return _coerce_non_negative_float_value(self.model_path("conductance_w_per_k"), resolved)

    @property
    def ambient_temperature_c(self) -> float:
        """Effective environment temperature the node exchanges heat with."""

        resolved = self.resolve_model_value(
            "ambient_temperature_c", self.config.ambient_temperature_c
        )
        return _coerce_temperature_value(self.model_path("ambient_temperature_c"), resolved)

    def is_fault_active(self, name: str) -> bool:
        """Return whether a thermal named fault flag is active."""

        if self.fault_engine is None:
            return False
        return self.fault_engine.is_module_fault_active(
            self.name, _validate_identifier("fault", name)
        )

    def active_named_faults(self) -> frozenset[str]:
        """Return active named fault flags for this thermal module."""

        if self.fault_engine is None:
            return frozenset()
        return self.fault_engine.active_named_faults(self.name)

    def set_heater_enabled(self, enabled: bool) -> None:
        """Command the heater on or off.

        A stuck-heater fault still overrides the commanded state; this records
        what the OBC asked for, not what the hardware did.
        """

        self.state.heater_commanded_on = _coerce_bool_value("heater enabled", enabled)

    def handle_command(
        self,
        command: CommandPayload | str,
        *,
        payload: object = None,
        source: EndpointId | None = None,
        target: EndpointId | None = None,
    ) -> bool:
        """Apply a thermal command.

        Returns ``False`` when a targeted command is for another module. Unknown
        commands that target this module fail loudly with ``ModuleCommandError``.
        """

        request = normalize_command(command, payload=payload, source=source, target=target)
        if not self.command_targets_module(request):
            return False

        if request.command == THERMAL_HEATER_ON_COMMAND:
            self.set_heater_enabled(True)
        elif request.command == THERMAL_HEATER_OFF_COMMAND:
            self.set_heater_enabled(False)
        else:
            raise ModuleCommandError(f"thermal command {request.command!r} is not supported")

        return True

    def tick(self, ticks: int = 1) -> dict[str, object]:
        """Integrate the RC node forward by ``ticks`` virtual ticks."""

        tick_count = _coerce_non_negative_int_value("thermal tick count", ticks)
        for _index in range(tick_count):
            net_power_w = (
                self.config.internal_heat_w
                + self.heater_power_w
                - self.conductance_w_per_k * (self.temperature_c - self.ambient_temperature_c)
            )
            delta_c = net_power_w * self.config.tick_seconds / self.config.thermal_capacity_j_per_k
            self.state.temperature_c = max(_ABSOLUTE_ZERO_C, self.temperature_c + delta_c)
        return self.telemetry()

    def telemetry(self, *, now: VirtualTime | None = None) -> dict[str, object]:
        """Return current decoded thermal telemetry values after signal overrides."""

        values: dict[str, object] = {
            "temperature_c": self.temperature_c,
            "heater_status": self.heater_status,
            "heater_enabled": self.heater_enabled,
            "heater_power_w": self.heater_power_w,
        }
        return {
            field: self.resolve_signal_value(field, value, now=now)
            for field, value in values.items()
        }

    def emit_telemetry(
        self,
        engine: DiscreteEventEngine,
        *,
        names: Iterable[str] | None = None,
        delay: VirtualTime = 0,
        source: EndpointId | None = None,
    ) -> tuple[TelemetrySample, ...]:
        """Schedule current decoded thermal telemetry samples on the DES engine."""

        telemetry = self.telemetry(now=engine.now)
        selected_names = tuple(telemetry) if names is None else tuple(names)
        samples: list[TelemetrySample] = []
        for name in selected_names:
            try:
                value = telemetry[name]
            except KeyError as exc:
                raise ModuleError(f"thermal telemetry field {name!r} is not supported") from exc
            samples.append(
                self.schedule_telemetry_sample(
                    engine,
                    name,
                    value,
                    delay=delay,
                    source=source,
                )
            )
        return tuple(samples)


def build_thermal_rc(context: ModuleBuildContext) -> ThermalRcModule:
    """Registry factory for the ``thermal_rc`` module type.

    Nothing here is specific to being a built-in: a third-party module's
    factory is this same three-line shape.
    """

    return ThermalRcModule(
        context.module_config(ThermalRcConfig),
        fault_engine=context.fault_engine,
    )


def _coerce_temperature_value(kind: str, value: object) -> float:
    number = _coerce_finite_float_value(kind, value)
    if number < _ABSOLUTE_ZERO_C:
        raise ModuleError(f"{kind} must not be below absolute zero ({_ABSOLUTE_ZERO_C} C)")
    return number


__all__ = [
    "THERMAL_HEATER_OFF_COMMAND",
    "THERMAL_HEATER_ON_COMMAND",
    "THERMAL_HEATER_STUCK_OFF_FAULT",
    "THERMAL_HEATER_STUCK_ON_FAULT",
    "THERMAL_RADIATOR_DEGRADED_FAULT",
    "HeaterStatus",
    "ThermalRcConfig",
    "ThermalRcModule",
    "ThermalRcState",
    "build_thermal_rc",
]
