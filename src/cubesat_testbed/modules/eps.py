"""Generic EPS module for product v1.

The EPS model is a reference subsystem, not a claim to match a specific real
board. It owns battery/load/power-mode state, emits EPS telemetry, accepts named
commands, and reacts to named faults or model-state overrides.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from cubesat_testbed.engine import CommandPayload, DiscreteEventEngine, VirtualTime
from cubesat_testbed.fault_injection import FaultInjectionEngine
from cubesat_testbed.modules.base import (
    ModuleCommandError,
    ModuleError,
    SimulatedModule,
    TelemetrySample,
    _clamp,
    _coerce_bool_value,
    _coerce_non_negative_float_value,
    _coerce_non_negative_int_value,
    _coerce_percentage_value,
    _coerce_positive_float_value,
    _coerce_unit_interval_exclusive_zero,
    _validate_identifier,
    _validate_optional_endpoint,
    normalize_command,
)
from cubesat_testbed.modules.payload import SimplePayloadModule
from cubesat_testbed.transport.base import EndpointId

if TYPE_CHECKING:
    from cubesat_testbed.modules.registry import ModuleBuildContext

EPS_PAYLOAD_RAIL_ON_COMMAND = "eps_payload_rail_on"
EPS_PAYLOAD_RAIL_OFF_COMMAND = "eps_payload_rail_off"
EPS_SET_POWER_MODE_COMMAND = "eps_set_power_mode"
EPS_POWER_MODE_NOMINAL_COMMAND = "eps_power_mode_nominal"
EPS_POWER_MODE_LOW_POWER_COMMAND = "eps_power_mode_low_power"
EPS_POWER_MODE_SAFE_COMMAND = "eps_power_mode_safe"
EPS_POWER_OFF_COMMAND = "eps_power_off"

EPS_BATTERY_CELL_DEAD_FAULT = "battery_cell_dead"
EPS_SOLAR_ARRAY_OFFLINE_FAULT = "solar_array_offline"
EPS_BUS_UNDERVOLTAGE_FAULT = "bus_undervoltage"
EPS_PAYLOAD_RAIL_STUCK_OFF_FAULT = "payload_rail_stuck_off"
EPS_PAYLOAD_RAIL_STUCK_ON_FAULT = "payload_rail_stuck_on"


class EpsPowerMode(str, Enum):
    """Reference EPS power modes exposed in telemetry."""

    NOMINAL = "nominal"
    LOW_POWER = "low_power"
    SAFE = "safe"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class GenericEpsConfig:
    """Configuration knobs for :class:`GenericEpsModule`."""

    name: str = "eps"
    endpoint: EndpointId | None = None
    initial_battery_percent: float = 80.0
    battery_capacity_wh: float = 20.0
    tick_seconds: float = 1.0
    base_load_w: float = 1.0
    low_power_load_w: float = 0.5
    safe_load_w: float = 0.2
    solar_input_w: float = 1.5
    empty_voltage_v: float = 6.0
    full_voltage_v: float = 8.4
    low_power_threshold_percent: float = 30.0
    recovery_threshold_percent: float = 35.0
    critical_threshold_percent: float = 10.0
    payload_rail_initially_enabled: bool = True
    shed_payload_in_low_power: bool = False
    shed_payload_in_safe_mode: bool = True
    battery_cell_dead_capacity_factor: float = 0.5
    battery_cell_dead_voltage_drop_v: float = 1.2
    undervoltage_fault_voltage_v: float = 5.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_identifier("module name", self.name))
        object.__setattr__(
            self,
            "endpoint",
            _validate_optional_endpoint("module endpoint", self.endpoint),
        )
        object.__setattr__(
            self,
            "initial_battery_percent",
            _coerce_percentage_value("initial_battery_percent", self.initial_battery_percent),
        )
        object.__setattr__(
            self,
            "battery_capacity_wh",
            _coerce_positive_float_value("battery_capacity_wh", self.battery_capacity_wh),
        )
        object.__setattr__(
            self,
            "tick_seconds",
            _coerce_positive_float_value("tick_seconds", self.tick_seconds),
        )
        object.__setattr__(
            self,
            "base_load_w",
            _coerce_non_negative_float_value("base_load_w", self.base_load_w),
        )
        object.__setattr__(
            self,
            "low_power_load_w",
            _coerce_non_negative_float_value("low_power_load_w", self.low_power_load_w),
        )
        object.__setattr__(
            self,
            "safe_load_w",
            _coerce_non_negative_float_value("safe_load_w", self.safe_load_w),
        )
        object.__setattr__(
            self,
            "solar_input_w",
            _coerce_non_negative_float_value("solar_input_w", self.solar_input_w),
        )
        object.__setattr__(
            self,
            "empty_voltage_v",
            _coerce_non_negative_float_value("empty_voltage_v", self.empty_voltage_v),
        )
        object.__setattr__(
            self,
            "full_voltage_v",
            _coerce_non_negative_float_value("full_voltage_v", self.full_voltage_v),
        )
        object.__setattr__(
            self,
            "low_power_threshold_percent",
            _coerce_percentage_value(
                "low_power_threshold_percent", self.low_power_threshold_percent
            ),
        )
        object.__setattr__(
            self,
            "recovery_threshold_percent",
            _coerce_percentage_value("recovery_threshold_percent", self.recovery_threshold_percent),
        )
        object.__setattr__(
            self,
            "critical_threshold_percent",
            _coerce_percentage_value("critical_threshold_percent", self.critical_threshold_percent),
        )
        object.__setattr__(
            self,
            "payload_rail_initially_enabled",
            _coerce_bool_value(
                "payload_rail_initially_enabled", self.payload_rail_initially_enabled
            ),
        )
        object.__setattr__(
            self,
            "shed_payload_in_low_power",
            _coerce_bool_value("shed_payload_in_low_power", self.shed_payload_in_low_power),
        )
        object.__setattr__(
            self,
            "shed_payload_in_safe_mode",
            _coerce_bool_value("shed_payload_in_safe_mode", self.shed_payload_in_safe_mode),
        )
        object.__setattr__(
            self,
            "battery_cell_dead_capacity_factor",
            _coerce_unit_interval_exclusive_zero(
                "battery_cell_dead_capacity_factor", self.battery_cell_dead_capacity_factor
            ),
        )
        object.__setattr__(
            self,
            "battery_cell_dead_voltage_drop_v",
            _coerce_non_negative_float_value(
                "battery_cell_dead_voltage_drop_v", self.battery_cell_dead_voltage_drop_v
            ),
        )
        object.__setattr__(
            self,
            "undervoltage_fault_voltage_v",
            _coerce_non_negative_float_value(
                "undervoltage_fault_voltage_v", self.undervoltage_fault_voltage_v
            ),
        )
        self._validate_threshold_order()
        self._validate_voltage_order()

    def _validate_threshold_order(self) -> None:
        if self.critical_threshold_percent > self.low_power_threshold_percent:
            raise ModuleError("critical_threshold_percent must be <= low_power_threshold_percent")
        if self.low_power_threshold_percent > self.recovery_threshold_percent:
            raise ModuleError("low_power_threshold_percent must be <= recovery_threshold_percent")

    def _validate_voltage_order(self) -> None:
        if self.empty_voltage_v > self.full_voltage_v:
            raise ModuleError("empty_voltage_v must be <= full_voltage_v")


@dataclass(slots=True)
class GenericEpsState:
    """Mutable state owned by the generic EPS model."""

    battery_percent: float
    commanded_power_mode: EpsPowerMode
    payload_rail_commanded_enabled: bool
    power_mode: EpsPowerMode = EpsPowerMode.NOMINAL
    battery_voltage_v: float = 0.0
    load_power_w: float = 0.0


class GenericEpsModule(SimulatedModule):
    """Deterministic v1 EPS model with battery, load and payload-rail behavior."""

    def __init__(
        self,
        config: GenericEpsConfig | None = None,
        *,
        payload: SimplePayloadModule | None = None,
        fault_engine: FaultInjectionEngine | None = None,
    ) -> None:
        self.config = GenericEpsConfig() if config is None else config
        self.payload = payload
        self.state = GenericEpsState(
            battery_percent=self.config.initial_battery_percent,
            commanded_power_mode=EpsPowerMode.NOMINAL,
            payload_rail_commanded_enabled=self.config.payload_rail_initially_enabled,
        )
        super().__init__(
            self.config.name,
            endpoint=self.config.endpoint,
            fault_engine=fault_engine,
        )
        self._refresh_derived_state()

    @property
    def battery_percent(self) -> float:
        """Effective battery state of charge after overrides and named faults."""

        resolved = self.resolve_model_value("battery_percent", self.state.battery_percent)
        percent = _coerce_percentage_value(self.model_path("battery_percent"), resolved)
        return min(percent, self._max_battery_percent())

    @property
    def battery_voltage_v(self) -> float:
        """Effective battery/bus voltage telemetry source."""

        resolved = self.resolve_model_value("battery_voltage_v", self.state.battery_voltage_v)
        return _coerce_non_negative_float_value(self.model_path("battery_voltage_v"), resolved)

    @property
    def solar_input_w(self) -> float:
        """Effective solar input power after named faults and state overrides."""

        computed = (
            0.0
            if self.is_fault_active(EPS_SOLAR_ARRAY_OFFLINE_FAULT)
            else self.config.solar_input_w
        )
        resolved = self.resolve_model_value("solar_input_w", computed)
        return _coerce_non_negative_float_value(self.model_path("solar_input_w"), resolved)

    @property
    def power_mode(self) -> EpsPowerMode:
        """Effective power mode after thresholds, commands and state overrides."""

        resolved = self.resolve_model_value("power_mode", self._automatic_power_mode().value)
        return _coerce_power_mode(resolved)

    @property
    def payload_rail_enabled(self) -> bool:
        """Effective payload rail state after mode logic, faults and overrides."""

        return self._effective_payload_rail_enabled(self.power_mode)

    @property
    def load_power_w(self) -> float:
        """Effective EPS load including attached payload draw when its rail is powered."""

        resolved = self.resolve_model_value("load_power_w", self.state.load_power_w)
        return _coerce_non_negative_float_value(self.model_path("load_power_w"), resolved)

    def attach_payload(self, payload: SimplePayloadModule | None) -> None:
        """Attach or detach the payload whose load and power rail are controlled by EPS."""

        self.payload = payload
        self._refresh_derived_state()

    def is_fault_active(self, name: str) -> bool:
        """Return whether an EPS named fault flag is active."""

        if self.fault_engine is None:
            return False
        return self.fault_engine.is_module_fault_active(
            self.name, _validate_identifier("fault", name)
        )

    def active_named_faults(self) -> frozenset[str]:
        """Return active named fault flags for this EPS module."""

        if self.fault_engine is None:
            return frozenset()
        return self.fault_engine.active_named_faults(self.name)

    def set_payload_rail_enabled(self, enabled: bool) -> None:
        """Command the payload rail on or off and propagate the effective result."""

        self.state.payload_rail_commanded_enabled = _coerce_bool_value(
            "payload rail enabled", enabled
        )
        self._refresh_derived_state()

    def set_power_mode(self, mode: EpsPowerMode | str) -> None:
        """Command a desired EPS power mode.

        Threshold logic may still force a safer effective mode when the battery is
        critical.
        """

        self.state.commanded_power_mode = _coerce_power_mode(mode)
        self._refresh_derived_state()

    def handle_command(
        self,
        command: CommandPayload | str,
        *,
        payload: object = None,
        source: EndpointId | None = None,
        target: EndpointId | None = None,
    ) -> bool:
        """Apply an EPS command.

        Returns ``False`` when a targeted command is for another module. Unknown
        commands that target this module fail loudly with ``ModuleCommandError``.
        """

        request = normalize_command(command, payload=payload, source=source, target=target)
        if not self.command_targets_module(request):
            return False

        if request.command == EPS_PAYLOAD_RAIL_ON_COMMAND:
            self.set_payload_rail_enabled(True)
        elif request.command == EPS_PAYLOAD_RAIL_OFF_COMMAND:
            self.set_payload_rail_enabled(False)
        elif request.command == EPS_SET_POWER_MODE_COMMAND:
            self.set_power_mode(_extract_power_mode(request.payload))
        elif request.command == EPS_POWER_MODE_NOMINAL_COMMAND:
            self.set_power_mode(EpsPowerMode.NOMINAL)
        elif request.command == EPS_POWER_MODE_LOW_POWER_COMMAND:
            self.set_power_mode(EpsPowerMode.LOW_POWER)
        elif request.command == EPS_POWER_MODE_SAFE_COMMAND:
            self.set_power_mode(EpsPowerMode.SAFE)
        elif request.command == EPS_POWER_OFF_COMMAND:
            self.set_power_mode(EpsPowerMode.OFF)
        else:
            raise ModuleCommandError(f"EPS command {request.command!r} is not supported")

        return True

    def tick(self, ticks: int = 1) -> dict[str, object]:
        """Advance the EPS battery/load model by ``ticks`` virtual ticks."""

        tick_count = _coerce_non_negative_int_value("EPS tick count", ticks)
        for _index in range(tick_count):
            mode = self.power_mode
            rail_enabled = self._effective_payload_rail_enabled(mode)
            self._sync_payload_power(rail_enabled)
            load_power_w = self._calculate_load_power_w(mode, rail_enabled)
            net_power_w = self.solar_input_w - load_power_w
            delta_percent = (
                (net_power_w * self.config.tick_seconds / 3600.0)
                / self._effective_battery_capacity_wh()
                * 100.0
            )
            self.state.battery_percent = _clamp(
                self.battery_percent + delta_percent,
                0.0,
                self._max_battery_percent(),
            )
            self._refresh_derived_state()
        if tick_count == 0:
            self._refresh_derived_state()
        return self.telemetry()

    def telemetry(self, *, now: VirtualTime | None = None) -> dict[str, object]:
        """Return current decoded EPS telemetry values after signal overrides."""

        self._refresh_derived_state()
        values: dict[str, object] = {
            "battery_percent": self.battery_percent,
            "battery_voltage_v": self.battery_voltage_v,
            "load_power_w": self.load_power_w,
            "solar_input_w": self.solar_input_w,
            "power_mode": self.power_mode.value,
            "payload_rail_enabled": self.payload_rail_enabled,
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
        """Schedule current decoded EPS telemetry samples on the DES engine."""

        telemetry = self.telemetry(now=engine.now)
        selected_names = tuple(telemetry) if names is None else tuple(names)
        samples: list[TelemetrySample] = []
        for name in selected_names:
            try:
                value = telemetry[name]
            except KeyError as exc:
                raise ModuleError(f"EPS telemetry field {name!r} is not supported") from exc
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

    def _refresh_derived_state(self) -> None:
        mode = self.power_mode
        rail_enabled = self._effective_payload_rail_enabled(mode)
        self._sync_payload_power(rail_enabled)
        self.state.power_mode = mode
        self.state.load_power_w = self._calculate_load_power_w(mode, rail_enabled)
        self.state.battery_voltage_v = self._calculate_battery_voltage_v()

    def _sync_payload_power(self, rail_enabled: bool) -> None:
        if self.payload is not None:
            self.payload.set_eps_power_available(rail_enabled)

    def _automatic_power_mode(self) -> EpsPowerMode:
        if self.state.commanded_power_mode is EpsPowerMode.OFF:
            return EpsPowerMode.OFF

        battery_percent = self.battery_percent
        if battery_percent <= 0.0:
            return EpsPowerMode.OFF
        if battery_percent <= self.config.critical_threshold_percent:
            return EpsPowerMode.SAFE
        if self.state.commanded_power_mode is EpsPowerMode.SAFE:
            return EpsPowerMode.SAFE
        if self.state.commanded_power_mode is EpsPowerMode.LOW_POWER:
            return EpsPowerMode.LOW_POWER
        if battery_percent <= self.config.low_power_threshold_percent:
            return EpsPowerMode.LOW_POWER
        if (
            self.state.power_mode is EpsPowerMode.LOW_POWER
            and battery_percent < self.config.recovery_threshold_percent
        ):
            return EpsPowerMode.LOW_POWER
        return EpsPowerMode.NOMINAL

    def _effective_payload_rail_enabled(self, mode: EpsPowerMode) -> bool:
        enabled = self.state.payload_rail_commanded_enabled
        if (
            mode is EpsPowerMode.OFF
            or mode is EpsPowerMode.SAFE
            and self.config.shed_payload_in_safe_mode
            or mode is EpsPowerMode.LOW_POWER
            and self.config.shed_payload_in_low_power
        ):
            enabled = False

        if self.is_fault_active(EPS_PAYLOAD_RAIL_STUCK_OFF_FAULT):
            enabled = False
        if self.is_fault_active(EPS_PAYLOAD_RAIL_STUCK_ON_FAULT):
            enabled = True

        resolved = self.resolve_model_value("payload_rail_enabled", enabled)
        return _coerce_bool_value(self.model_path("payload_rail_enabled"), resolved)

    def _calculate_load_power_w(self, mode: EpsPowerMode, rail_enabled: bool) -> float:
        if mode is EpsPowerMode.OFF:
            base_load_w = 0.0
        elif mode is EpsPowerMode.SAFE:
            base_load_w = self.config.safe_load_w
        elif mode is EpsPowerMode.LOW_POWER:
            base_load_w = self.config.low_power_load_w
        else:
            base_load_w = self.config.base_load_w

        payload_load_w = 0.0
        if rail_enabled and self.payload is not None:
            payload_load_w = self.payload.power_draw_w
        return base_load_w + payload_load_w

    def _calculate_battery_voltage_v(self) -> float:
        fraction = self.battery_percent / 100.0
        voltage = (
            self.config.empty_voltage_v
            + (self.config.full_voltage_v - self.config.empty_voltage_v) * fraction
        )
        if self.is_fault_active(EPS_BATTERY_CELL_DEAD_FAULT):
            voltage -= self.config.battery_cell_dead_voltage_drop_v
        if self.is_fault_active(EPS_BUS_UNDERVOLTAGE_FAULT):
            voltage = min(voltage, self.config.undervoltage_fault_voltage_v)
        return max(0.0, voltage)

    def _effective_battery_capacity_wh(self) -> float:
        capacity = self.config.battery_capacity_wh
        if self.is_fault_active(EPS_BATTERY_CELL_DEAD_FAULT):
            capacity *= self.config.battery_cell_dead_capacity_factor
        return capacity

    def _max_battery_percent(self) -> float:
        if self.is_fault_active(EPS_BATTERY_CELL_DEAD_FAULT):
            return 100.0 * self.config.battery_cell_dead_capacity_factor
        return 100.0


def build_generic_eps(context: ModuleBuildContext) -> GenericEpsModule:
    """Registry factory for the ``generic_eps`` module type.

    The EPS switches a payload's power rail, so it is registered one build
    order later than ``simple_payload`` and picks that module up here. Wiring
    by type rather than by a configured node name is a v1 simplification: a
    setup with two payloads attaches the first one.
    """

    return GenericEpsModule(
        context.module_config(GenericEpsConfig),
        payload=context.first_module_of_type(SimplePayloadModule),
        fault_engine=context.fault_engine,
    )


def _extract_power_mode(payload: object) -> EpsPowerMode:
    if isinstance(payload, Mapping):
        try:
            return _coerce_power_mode(payload["mode"])
        except KeyError as exc:
            raise ModuleCommandError("eps_set_power_mode payload mapping requires 'mode'") from exc
    return _coerce_power_mode(payload)


def _coerce_power_mode(value: object) -> EpsPowerMode:
    if isinstance(value, EpsPowerMode):
        return value
    if isinstance(value, str):
        try:
            return EpsPowerMode(value)
        except ValueError as exc:
            supported = ", ".join(mode.value for mode in EpsPowerMode)
            raise ModuleError(f"EPS power mode must be one of: {supported}") from exc
    raise ModuleError("EPS power mode must be a string or EpsPowerMode")


__all__ = [
    "EPS_BATTERY_CELL_DEAD_FAULT",
    "EPS_BUS_UNDERVOLTAGE_FAULT",
    "EPS_PAYLOAD_RAIL_OFF_COMMAND",
    "EPS_PAYLOAD_RAIL_ON_COMMAND",
    "EPS_PAYLOAD_RAIL_STUCK_OFF_FAULT",
    "EPS_PAYLOAD_RAIL_STUCK_ON_FAULT",
    "EPS_POWER_MODE_LOW_POWER_COMMAND",
    "EPS_POWER_MODE_NOMINAL_COMMAND",
    "EPS_POWER_MODE_SAFE_COMMAND",
    "EPS_POWER_OFF_COMMAND",
    "EPS_SET_POWER_MODE_COMMAND",
    "EPS_SOLAR_ARRAY_OFFLINE_FAULT",
    "EpsPowerMode",
    "GenericEpsConfig",
    "GenericEpsModule",
    "GenericEpsState",
    "build_generic_eps",
]
