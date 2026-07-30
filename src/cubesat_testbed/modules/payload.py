"""Simple Payload module for product v1.

The payload model covers basic command/data-volume behavior and power draw so
scenarios can exercise cross-module EPS/OBC/payload interactions.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

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
    _validate_identifier,
    _validate_optional_endpoint,
    normalize_command,
)
from cubesat_testbed.transport.base import EndpointId

PowerStatus = Literal["online", "offline"]

PAYLOAD_POWER_ON_COMMAND = "payload_power_on"
PAYLOAD_POWER_OFF_COMMAND = "payload_power_off"
PAYLOAD_START_CAPTURE_COMMAND = "payload_start_capture"
PAYLOAD_STOP_CAPTURE_COMMAND = "payload_stop_capture"
PAYLOAD_CAPTURE_ONCE_COMMAND = "payload_capture_once"
PAYLOAD_CLEAR_DATA_COMMAND = "payload_clear_data"
PAYLOAD_DOWNLINK_DATA_COMMAND = "payload_downlink_data"


@dataclass(frozen=True, slots=True)
class SimplePayloadConfig:
    """Configuration knobs for :class:`SimplePayloadModule`."""

    name: str = "payload"
    endpoint: EndpointId | None = None
    initially_powered: bool = True
    standby_power_w: float = 0.2
    active_power_w: float = 5.0
    data_rate_bytes_per_tick: int = 1024
    one_shot_capture_bytes: int = 1024
    max_data_volume_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_identifier("module name", self.name))
        object.__setattr__(
            self,
            "endpoint",
            _validate_optional_endpoint("module endpoint", self.endpoint),
        )
        object.__setattr__(
            self,
            "initially_powered",
            _coerce_bool_value("initially_powered", self.initially_powered),
        )
        object.__setattr__(
            self,
            "standby_power_w",
            _coerce_non_negative_float_value("standby_power_w", self.standby_power_w),
        )
        object.__setattr__(
            self,
            "active_power_w",
            _coerce_non_negative_float_value("active_power_w", self.active_power_w),
        )
        object.__setattr__(
            self,
            "data_rate_bytes_per_tick",
            _coerce_non_negative_int_value(
                "data_rate_bytes_per_tick", self.data_rate_bytes_per_tick
            ),
        )
        object.__setattr__(
            self,
            "one_shot_capture_bytes",
            _coerce_non_negative_int_value("one_shot_capture_bytes", self.one_shot_capture_bytes),
        )
        object.__setattr__(
            self,
            "max_data_volume_bytes",
            _coerce_non_negative_int_value("max_data_volume_bytes", self.max_data_volume_bytes),
        )


@dataclass(slots=True)
class SimplePayloadState:
    """Mutable state owned by the simple payload model."""

    commanded_on: bool
    eps_power_available: bool
    capture_enabled: bool = False
    data_volume_bytes: int = 0


class SimplePayloadModule(SimulatedModule):
    """Deterministic v1 payload model with command, data-volume and power state."""

    def __init__(
        self,
        config: SimplePayloadConfig | None = None,
        *,
        fault_engine: FaultInjectionEngine | None = None,
    ) -> None:
        self.config = SimplePayloadConfig() if config is None else config
        self.state = SimplePayloadState(
            commanded_on=self.config.initially_powered,
            eps_power_available=True,
        )
        super().__init__(
            self.config.name,
            endpoint=self.config.endpoint,
            fault_engine=fault_engine,
        )

    @property
    def powered(self) -> bool:
        """Effective payload power state after EPS availability and state overrides."""

        computed_power = self.state.commanded_on and self.state.eps_power_available
        resolved = self.resolve_model_value("powered", computed_power)
        return _coerce_bool_value(self.model_path("powered"), resolved)

    @property
    def power_status(self) -> PowerStatus:
        """Human-readable power status used by scenario assertions."""

        return "online" if self.powered else "offline"

    @property
    def capture_enabled(self) -> bool:
        """Whether the payload is currently collecting data."""

        resolved = self.resolve_model_value("capture_enabled", self.state.capture_enabled)
        return self.powered and _coerce_bool_value(self.model_path("capture_enabled"), resolved)

    @property
    def data_volume_bytes(self) -> int:
        """Effective onboard payload data volume."""

        resolved = self.resolve_model_value("data_volume_bytes", self.state.data_volume_bytes)
        volume = _coerce_non_negative_int_value(self.model_path("data_volume_bytes"), resolved)
        return min(volume, self.config.max_data_volume_bytes)

    @property
    def power_draw_w(self) -> float:
        """Effective payload load presented to EPS."""

        if not self.powered:
            computed_draw = 0.0
        elif self.capture_enabled:
            computed_draw = self.config.active_power_w
        else:
            computed_draw = self.config.standby_power_w
        resolved = self.resolve_model_value("power_draw_w", computed_draw)
        return _coerce_non_negative_float_value(self.model_path("power_draw_w"), resolved)

    def set_eps_power_available(self, available: bool) -> None:
        """Update the EPS-provided payload power rail state."""

        self.state.eps_power_available = _coerce_bool_value("eps power availability", available)
        if not self.state.eps_power_available:
            self.state.capture_enabled = False

    def handle_command(
        self,
        command: CommandPayload | str,
        *,
        payload: object = None,
        source: EndpointId | None = None,
        target: EndpointId | None = None,
    ) -> bool:
        """Apply a payload command.

        Returns ``False`` when a targeted command is for another module. Unknown
        commands that target this module fail loudly with ``ModuleCommandError``.
        """

        request = normalize_command(command, payload=payload, source=source, target=target)
        if not self.command_targets_module(request):
            return False

        if request.command == PAYLOAD_POWER_ON_COMMAND:
            self.state.commanded_on = True
        elif request.command == PAYLOAD_POWER_OFF_COMMAND:
            self.state.commanded_on = False
            self.state.capture_enabled = False
        elif request.command == PAYLOAD_START_CAPTURE_COMMAND:
            self.state.capture_enabled = True
        elif request.command == PAYLOAD_STOP_CAPTURE_COMMAND:
            self.state.capture_enabled = False
        elif request.command == PAYLOAD_CAPTURE_ONCE_COMMAND:
            self._increase_data_volume(
                _extract_byte_count(
                    request.payload,
                    default=self.config.one_shot_capture_bytes,
                    field_names=("bytes", "data_volume_bytes"),
                )
            )
        elif request.command == PAYLOAD_CLEAR_DATA_COMMAND:
            self.state.data_volume_bytes = 0
        elif request.command == PAYLOAD_DOWNLINK_DATA_COMMAND:
            self._decrease_data_volume(
                _extract_byte_count(
                    request.payload,
                    default=self.config.one_shot_capture_bytes,
                    field_names=("bytes", "data_volume_bytes"),
                )
            )
        else:
            raise ModuleCommandError(f"payload command {request.command!r} is not supported")

        return True

    def tick(self, ticks: int = 1) -> int:
        """Advance the data-production model by ``ticks`` virtual ticks.

        The module does not schedule itself or sleep; callers drive ticks through
        the deterministic event engine or tests.
        """

        tick_count = _coerce_non_negative_int_value("payload tick count", ticks)
        if tick_count == 0 or not self.capture_enabled:
            return 0
        generated = self.config.data_rate_bytes_per_tick * tick_count
        return self._increase_data_volume(generated)

    def telemetry(self, *, now: VirtualTime | None = None) -> dict[str, object]:
        """Return current decoded telemetry values after signal overrides."""

        values: dict[str, object] = {
            "power_status": self.power_status,
            "powered": self.powered,
            "capture_enabled": self.capture_enabled,
            "data_volume_bytes": self.data_volume_bytes,
            "power_draw_w": self.power_draw_w,
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
        """Schedule current decoded telemetry samples on the DES engine."""

        telemetry = self.telemetry(now=engine.now)
        selected_names = tuple(telemetry) if names is None else tuple(names)
        samples: list[TelemetrySample] = []
        for name in selected_names:
            try:
                value = telemetry[name]
            except KeyError as exc:
                raise ModuleError(f"payload telemetry field {name!r} is not supported") from exc
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

    def _increase_data_volume(self, byte_count: int) -> int:
        if not self.powered:
            return 0
        before = self.state.data_volume_bytes
        after = int(_clamp(before + byte_count, 0.0, float(self.config.max_data_volume_bytes)))
        self.state.data_volume_bytes = after
        return after - before

    def _decrease_data_volume(self, byte_count: int) -> int:
        before = self.state.data_volume_bytes
        after = max(0, before - byte_count)
        self.state.data_volume_bytes = after
        return before - after


def _extract_byte_count(
    payload: object,
    *,
    default: int,
    field_names: tuple[str, ...],
) -> int:
    if payload is None:
        return default
    if isinstance(payload, int) and not isinstance(payload, bool):
        return _coerce_non_negative_int_value("command byte count", payload)
    if isinstance(payload, Mapping):
        for field_name in field_names:
            if field_name in payload:
                return _coerce_non_negative_int_value(
                    f"command payload {field_name!r}", payload[field_name]
                )
    raise ModuleCommandError(
        "payload data-volume commands require an integer byte count or a mapping with "
        f"one of: {', '.join(field_names)}"
    )


__all__ = [
    "PAYLOAD_CAPTURE_ONCE_COMMAND",
    "PAYLOAD_CLEAR_DATA_COMMAND",
    "PAYLOAD_DOWNLINK_DATA_COMMAND",
    "PAYLOAD_POWER_OFF_COMMAND",
    "PAYLOAD_POWER_ON_COMMAND",
    "PAYLOAD_START_CAPTURE_COMMAND",
    "PAYLOAD_STOP_CAPTURE_COMMAND",
    "PowerStatus",
    "SimplePayloadConfig",
    "SimplePayloadModule",
    "SimplePayloadState",
]
