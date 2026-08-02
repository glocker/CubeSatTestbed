"""Base contract for simulated CubeSat subsystem modules.

Modules are isolated finite state machines. They react to events, scheduled
virtual-time timers, commands, and fault requests. They must not run independent
wall-clock polling loops.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from cubesat_testbed.engine import CommandPayload, DiscreteEventEngine, VirtualTime
from cubesat_testbed.fault_injection import FaultInjectionEngine
from cubesat_testbed.transport.base import EndpointId

_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class ModuleError(ValueError):
    """Base error raised by simulated subsystem modules."""


class ModuleCommandError(ModuleError):
    """Raised when a module cannot execute a targeted command."""


@dataclass(frozen=True, slots=True)
class ModuleCommandRequest:
    """Normalized command view accepted by simulated modules."""

    command: str
    payload: object = None
    source: EndpointId | None = None
    target: EndpointId | None = None


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    """One generated module telemetry sample on the decoded telemetry path."""

    signal: str
    value: object
    source: EndpointId | None = None


class SimulatedModule:
    """Common helpers for deterministic product-v1 simulated modules."""

    def __init__(
        self,
        name: str,
        *,
        endpoint: EndpointId | None = None,
        fault_engine: FaultInjectionEngine | None = None,
    ) -> None:
        self._name = _validate_identifier("module name", name)
        self._endpoint = _validate_optional_endpoint("module endpoint", endpoint)
        self._fault_engine = fault_engine

    @property
    def name(self) -> str:
        """Configured module/node name."""

        return self._name

    @property
    def endpoint(self) -> EndpointId | None:
        """Optional transport endpoint associated with this module."""

        return self._endpoint

    @property
    def default_source(self) -> EndpointId:
        """Endpoint used as telemetry source when a caller does not provide one."""

        if self._endpoint is not None:
            return self._endpoint
        return self._name

    @property
    def fault_engine(self) -> FaultInjectionEngine | None:
        """Passive fault-injection engine used to resolve overrides and flags."""

        return self._fault_engine

    def set_fault_engine(self, fault_engine: FaultInjectionEngine | None) -> None:
        """Replace the passive fault-injection engine reference."""

        self._fault_engine = fault_engine

    def model_path(self, field: str) -> str:
        """Return the canonical model-state path for ``field``."""

        return f"{self._name}.model.{_validate_identifier('model field', field)}"

    def telemetry_path(self, field: str) -> str:
        """Return the canonical telemetry path for ``field``."""

        return f"{self._name}.telemetry.{_validate_identifier('telemetry field', field)}"

    def command_targets_module(self, command: ModuleCommandRequest) -> bool:
        """Return whether ``command`` targets this module or is an untargeted command."""

        if command.target is None:
            return True
        return command.target == self._name or (
            self._endpoint is not None and command.target == self._endpoint
        )

    def resolve_model_value(
        self,
        field: str,
        current_value: object,
        *,
        now: VirtualTime | None = None,
    ) -> object:
        """Return a model value after applying any active state override."""

        if self._fault_engine is None:
            return current_value
        return self._fault_engine.resolve_state_value(
            self.model_path(field), current_value, now=now
        )

    def resolve_signal_value(
        self,
        field: str,
        current_value: object,
        *,
        now: VirtualTime | None = None,
    ) -> object:
        """Return a telemetry value after applying any active signal override."""

        if self._fault_engine is None:
            return current_value
        return self._fault_engine.resolve_signal_value(
            self.telemetry_path(field), current_value, now=now
        )

    def make_telemetry_sample(
        self,
        field: str,
        value: object,
        *,
        source: EndpointId | None = None,
    ) -> TelemetrySample:
        """Build a telemetry sample without scheduling it."""

        return TelemetrySample(
            signal=self.telemetry_path(field),
            value=value,
            source=self.default_source if source is None else source,
        )

    def schedule_telemetry_sample(
        self,
        engine: DiscreteEventEngine,
        field: str,
        value: object,
        *,
        delay: VirtualTime = 0,
        source: EndpointId | None = None,
    ) -> TelemetrySample:
        """Schedule one decoded telemetry sample on the deterministic engine."""

        sample = self.make_telemetry_sample(field, value, source=source)
        engine.schedule_telemetry(sample.signal, sample.value, delay=delay, source=sample.source)
        return sample


def normalize_command(
    command: CommandPayload | str,
    *,
    payload: object = None,
    source: EndpointId | None = None,
    target: EndpointId | None = None,
) -> ModuleCommandRequest:
    """Normalize a DES ``CommandPayload`` or direct command name for module handlers."""

    if isinstance(command, CommandPayload):
        return ModuleCommandRequest(
            command=command.command,
            payload=command.payload,
            source=command.source,
            target=command.target,
        )
    return ModuleCommandRequest(
        command=_validate_identifier("command", command),
        payload=payload,
        source=_validate_optional_endpoint("command source", source),
        target=_validate_optional_endpoint("command target", target),
    )


def _validate_identifier(kind: str, value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ModuleError(
            f"{kind} must start with a letter and contain only letters, digits, '_' or '-'"
        )
    return value


def _validate_optional_endpoint(kind: str, value: EndpointId | None) -> EndpointId | None:
    if value is None:
        return None
    return _validate_endpoint(kind, value)


def _validate_endpoint(kind: str, value: EndpointId) -> EndpointId:
    if isinstance(value, bool):
        raise ModuleError(f"{kind} must be a non-bool string or integer")
    if isinstance(value, str):
        if value == "":
            raise ModuleError(f"{kind} must not be an empty string")
        return value
    if isinstance(value, int):
        if value < 0:
            raise ModuleError(f"{kind} integer value must be non-negative")
        return value
    raise ModuleError(f"{kind} must be a string or integer")


def _coerce_bool_value(kind: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ModuleError(f"{kind} must be a bool")
    return value


def _coerce_non_negative_int_value(kind: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModuleError(f"{kind} must be a non-negative integer")
    if value < 0:
        raise ModuleError(f"{kind} must be non-negative")
    return int(value)


def _coerce_positive_int_value(kind: str, value: object) -> int:
    number = _coerce_non_negative_int_value(kind, value)
    if number == 0:
        raise ModuleError(f"{kind} must be positive")
    return number


def _coerce_finite_float_value(kind: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ModuleError(f"{kind} must be a finite integer or float")
    number = float(value)
    if not math.isfinite(number):
        raise ModuleError(f"{kind} must be finite")
    return number


def _coerce_non_negative_float_value(kind: str, value: object) -> float:
    number = _coerce_finite_float_value(kind, value)
    if number < 0.0:
        raise ModuleError(f"{kind} must be non-negative")
    return number


def _coerce_positive_float_value(kind: str, value: object) -> float:
    number = _coerce_non_negative_float_value(kind, value)
    if number == 0.0:
        raise ModuleError(f"{kind} must be positive")
    return number


def _coerce_unit_interval_exclusive_zero(kind: str, value: object) -> float:
    number = _coerce_finite_float_value(kind, value)
    if not 0.0 < number <= 1.0:
        raise ModuleError(f"{kind} must be in range (0, 1]")
    return number


def _coerce_percentage_value(kind: str, value: object) -> float:
    number = _coerce_finite_float_value(kind, value)
    if not 0.0 <= number <= 100.0:
        raise ModuleError(f"{kind} must be in range 0..100")
    return number


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


__all__ = [
    "ModuleCommandError",
    "ModuleCommandRequest",
    "ModuleError",
    "SimulatedModule",
    "TelemetrySample",
    "normalize_command",
]
