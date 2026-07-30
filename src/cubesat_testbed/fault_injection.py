"""Passive fault injection executor.

The fault engine applies explicit external requests only:

- ``state_override`` for internal model paths such as ``eps.model.temperature``;
- ``signal_override`` for outgoing telemetry paths such as
  ``eps.telemetry.voltage``;
- named module fault flags such as ``eps.battery_cell_dead``.

Threshold evaluation belongs to the OBC Peer rule engine, not here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from cubesat_testbed.engine import (
    DiscreteEventEngine,
    EventHandler,
    EventKind,
    FaultPayload,
    SimulationEvent,
    VirtualTime,
)

CycleCount: TypeAlias = int

_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_PATH_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*)+$")


class FaultInjectionError(ValueError):
    """Raised when a passive fault request cannot be applied."""


class FaultInjectionKind(str, Enum):
    """Supported product-v1 passive fault request kinds."""

    STATE_OVERRIDE = "state_override"
    SIGNAL_OVERRIDE = "signal_override"
    NAMED_FAULT = "named_fault"


@dataclass(frozen=True, slots=True)
class ActiveOverride:
    """One active direct override and its optional expiration boundaries."""

    kind: FaultInjectionKind
    target: str
    value: object
    activated_at: VirtualTime
    activated_cycle: CycleCount
    expires_at: VirtualTime | None = None
    expires_after_cycle: CycleCount | None = None

    def is_expired(self, *, now: VirtualTime, cycle: CycleCount) -> bool:
        """Return whether this override is no longer active at ``now``/``cycle``."""

        return (self.expires_at is not None and now >= self.expires_at) or (
            self.expires_after_cycle is not None and cycle >= self.expires_after_cycle
        )


@dataclass(frozen=True, slots=True)
class NamedFaultFlag:
    """One active named fault flag owned by a module."""

    module: str
    name: str
    activated_at: VirtualTime
    activated_cycle: CycleCount

    @property
    def target(self) -> str:
        """Full scenario target path for this named fault flag."""

        return f"{self.module}.{self.name}"


class FaultInjectionEngine:
    """Passive state/signal override and named-fault registry.

    The engine does not evaluate thresholds or inspect telemetry. It only records
    explicit fault requests and exposes deterministic lookup methods that modules
    or the scenario runner can call while processing virtual-time events.
    """

    def __init__(self, engine: DiscreteEventEngine | None = None) -> None:
        self._engine: DiscreteEventEngine | None = None
        self._event_handler: EventHandler = self.handle_event
        self._cycle: CycleCount = 0
        self._state_overrides: dict[str, ActiveOverride] = {}
        self._signal_overrides: dict[str, ActiveOverride] = {}
        self._named_faults: dict[str, NamedFaultFlag] = {}

        if engine is not None:
            self.attach(engine)

    @property
    def cycle(self) -> CycleCount:
        """Current external/module cycle counter used for cycle-based expiration."""

        return self._cycle

    @property
    def attached_engine(self) -> DiscreteEventEngine | None:
        """DES engine whose ``FAULT`` events are handled by this instance."""

        return self._engine

    def attach(self, engine: DiscreteEventEngine) -> None:
        """Register this fault engine as a handler for DES ``FAULT`` events."""

        if self._engine is engine:
            return
        if self._engine is not None:
            self.detach()
        engine.add_handler(EventKind.FAULT, self._event_handler)
        self._engine = engine

    def detach(self) -> None:
        """Unregister this fault engine from its attached DES engine, if any."""

        if self._engine is None:
            return
        self._engine.remove_handler(EventKind.FAULT, self._event_handler)
        self._engine = None

    def handle_event(self, event: SimulationEvent, engine: DiscreteEventEngine) -> None:
        """Apply a ``FaultPayload`` dispatched by the deterministic event engine."""

        if event.kind is not EventKind.FAULT:
            raise FaultInjectionError(f"cannot handle non-fault event kind {event.kind.value!r}")
        if not isinstance(event.payload, FaultPayload):
            raise FaultInjectionError("fault events must carry a FaultPayload")
        self.apply_payload(event.payload, now=engine.now)

    def apply_payload(
        self,
        payload: FaultPayload,
        *,
        now: VirtualTime | None = None,
    ) -> ActiveOverride | NamedFaultFlag | None:
        """Apply a DES fault payload and return the resulting active object, if any."""

        return self.apply(
            payload.fault_type,
            payload.target,
            value=payload.value,
            duration=payload.duration,
            cycles=payload.cycles,
            now=now,
        )

    def apply(
        self,
        fault_type: FaultInjectionKind | str,
        target: str,
        *,
        value: object = None,
        duration: VirtualTime | None = None,
        cycles: CycleCount | None = None,
        now: VirtualTime | None = None,
    ) -> ActiveOverride | NamedFaultFlag | None:
        """Apply one passive fault request by kind and target path.

        ``duration`` and ``cycles`` are valid for direct overrides only. If both
        are provided, the override expires at whichever boundary is reached first.
        Named fault payloads are activations by default; pass ``value=False`` to
        clear an already-active named fault flag through the event path.
        """

        kind = _coerce_kind(fault_type)
        if kind is FaultInjectionKind.STATE_OVERRIDE:
            return self.state_override(target, value, duration=duration, cycles=cycles, now=now)
        if kind is FaultInjectionKind.SIGNAL_OVERRIDE:
            return self.signal_override(target, value, duration=duration, cycles=cycles, now=now)

        if duration is not None or cycles is not None:
            raise FaultInjectionError("named_fault requests do not support duration or cycles")
        if _named_fault_value_is_active(value):
            return self.activate_named_fault(target, now=now)
        return self.clear_named_fault(target)

    def state_override(
        self,
        target: str,
        value: object,
        *,
        duration: VirtualTime | None = None,
        cycles: CycleCount | None = None,
        now: VirtualTime | None = None,
    ) -> ActiveOverride:
        """Activate or replace a state override for a ``.model.`` target path."""

        validated_target = _validate_state_target(target)
        override = self._make_override(
            FaultInjectionKind.STATE_OVERRIDE,
            validated_target,
            value,
            duration=duration,
            cycles=cycles,
            now=now,
        )
        self._state_overrides[validated_target] = override
        return override

    def signal_override(
        self,
        target: str,
        value: object,
        *,
        duration: VirtualTime | None = None,
        cycles: CycleCount | None = None,
        now: VirtualTime | None = None,
    ) -> ActiveOverride:
        """Activate or replace a telemetry/signal override for a ``.telemetry.`` target path."""

        validated_target = _validate_signal_target(target)
        override = self._make_override(
            FaultInjectionKind.SIGNAL_OVERRIDE,
            validated_target,
            value,
            duration=duration,
            cycles=cycles,
            now=now,
        )
        self._signal_overrides[validated_target] = override
        return override

    def get_state_override(
        self,
        target: str,
        *,
        now: VirtualTime | None = None,
    ) -> ActiveOverride | None:
        """Return the active state override for ``target``, if one exists."""

        validated_target = _validate_state_target(target)
        self.clear_expired(now=now)
        return self._state_overrides.get(validated_target)

    def get_signal_override(
        self,
        target: str,
        *,
        now: VirtualTime | None = None,
    ) -> ActiveOverride | None:
        """Return the active signal override for ``target``, if one exists."""

        validated_target = _validate_signal_target(target)
        self.clear_expired(now=now)
        return self._signal_overrides.get(validated_target)

    def has_state_override(self, target: str, *, now: VirtualTime | None = None) -> bool:
        """Return whether ``target`` currently has an active state override."""

        return self.get_state_override(target, now=now) is not None

    def has_signal_override(self, target: str, *, now: VirtualTime | None = None) -> bool:
        """Return whether ``target`` currently has an active signal override."""

        return self.get_signal_override(target, now=now) is not None

    def resolve_state_value(
        self,
        target: str,
        current_value: object,
        *,
        now: VirtualTime | None = None,
    ) -> object:
        """Return the overridden model value for ``target`` or ``current_value``."""

        override = self.get_state_override(target, now=now)
        if override is None:
            return current_value
        return override.value

    def resolve_signal_value(
        self,
        target: str,
        current_value: object,
        *,
        now: VirtualTime | None = None,
    ) -> object:
        """Return the overridden telemetry value for ``target`` or ``current_value``."""

        override = self.get_signal_override(target, now=now)
        if override is None:
            return current_value
        return override.value

    def active_state_overrides(
        self,
        *,
        now: VirtualTime | None = None,
    ) -> dict[str, ActiveOverride]:
        """Return active state overrides keyed by target path."""

        self.clear_expired(now=now)
        return dict(self._state_overrides)

    def active_signal_overrides(
        self,
        *,
        now: VirtualTime | None = None,
    ) -> dict[str, ActiveOverride]:
        """Return active signal overrides keyed by target path."""

        self.clear_expired(now=now)
        return dict(self._signal_overrides)

    def clear_state_override(self, target: str) -> ActiveOverride | None:
        """Clear a state override explicitly and return the removed override, if any."""

        return self._state_overrides.pop(_validate_state_target(target), None)

    def clear_signal_override(self, target: str) -> ActiveOverride | None:
        """Clear a signal override explicitly and return the removed override, if any."""

        return self._signal_overrides.pop(_validate_signal_target(target), None)

    def activate_named_fault(
        self,
        target: str,
        *,
        now: VirtualTime | None = None,
    ) -> NamedFaultFlag:
        """Activate or replace a named module fault flag."""

        module, name = _parse_named_fault_target(target)
        flag = NamedFaultFlag(
            module=module,
            name=name,
            activated_at=self._current_time(now),
            activated_cycle=self._cycle,
        )
        self._named_faults[flag.target] = flag
        return flag

    def clear_named_fault(self, target: str) -> NamedFaultFlag | None:
        """Clear a named module fault flag explicitly."""

        module, name = _parse_named_fault_target(target)
        return self._named_faults.pop(f"{module}.{name}", None)

    def clear_module_faults(self, module: str) -> tuple[NamedFaultFlag, ...]:
        """Clear every active named fault flag owned by ``module``."""

        validated_module = _validate_identifier("module", module)
        removed: list[NamedFaultFlag] = []
        for target, flag in list(self._named_faults.items()):
            if flag.module == validated_module:
                removed.append(flag)
                del self._named_faults[target]
        return tuple(removed)

    def is_named_fault_active(self, target: str) -> bool:
        """Return whether a named fault target is active."""

        module, name = _parse_named_fault_target(target)
        return f"{module}.{name}" in self._named_faults

    def is_module_fault_active(self, module: str, name: str) -> bool:
        """Return whether ``module`` has active named fault flag ``name``."""

        return self.is_named_fault_active(f"{module}.{name}")

    def active_named_faults(self, module: str | None = None) -> frozenset[str]:
        """Return active named fault flags.

        Without ``module``, the returned set contains full ``module.flag`` target
        paths. With ``module``, it contains only fault flag names for that module.
        """

        if module is None:
            return frozenset(self._named_faults)

        validated_module = _validate_identifier("module", module)
        return frozenset(
            flag.name for flag in self._named_faults.values() if flag.module == validated_module
        )

    def advance_cycles(
        self,
        count: CycleCount = 1,
        *,
        now: VirtualTime | None = None,
    ) -> tuple[ActiveOverride, ...]:
        """Advance the external cycle counter and clear newly expired overrides."""

        self._cycle += _validate_non_negative_int("cycle advance", count)
        return self.clear_expired(now=now)

    def clear_expired(self, *, now: VirtualTime | None = None) -> tuple[ActiveOverride, ...]:
        """Clear direct overrides whose time or cycle expiration has been reached."""

        current_time = self._current_time(now)
        expired: list[ActiveOverride] = []
        for overrides in (self._state_overrides, self._signal_overrides):
            for target, override in list(overrides.items()):
                if override.is_expired(now=current_time, cycle=self._cycle):
                    expired.append(override)
                    del overrides[target]
        return tuple(expired)

    def clear_all(self) -> None:
        """Clear all active direct overrides and named fault flags."""

        self._state_overrides.clear()
        self._signal_overrides.clear()
        self._named_faults.clear()

    def _make_override(
        self,
        kind: FaultInjectionKind,
        target: str,
        value: object,
        *,
        duration: VirtualTime | None,
        cycles: CycleCount | None,
        now: VirtualTime | None,
    ) -> ActiveOverride:
        start_time = self._current_time(now)
        valid_duration = _validate_optional_non_negative_int("duration", duration)
        valid_cycles = _validate_optional_non_negative_int("cycles", cycles)
        return ActiveOverride(
            kind=kind,
            target=target,
            value=value,
            activated_at=start_time,
            activated_cycle=self._cycle,
            expires_at=None if valid_duration is None else start_time + valid_duration,
            expires_after_cycle=None if valid_cycles is None else self._cycle + valid_cycles,
        )

    def _current_time(self, now: VirtualTime | None) -> VirtualTime:
        if now is not None:
            return _validate_non_negative_int("virtual time", now)
        if self._engine is not None:
            return self._engine.now
        return 0


def _coerce_kind(value: FaultInjectionKind | str) -> FaultInjectionKind:
    try:
        return FaultInjectionKind(value)
    except ValueError as exc:
        supported = ", ".join(kind.value for kind in FaultInjectionKind)
        raise FaultInjectionError(
            f"unsupported fault type {value!r}; expected one of: {supported}"
        ) from exc


def _named_fault_value_is_active(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        active = value.get("active")
        if isinstance(active, bool):
            return active
    raise FaultInjectionError(
        "named_fault value must be omitted, a bool, or a mapping with boolean 'active'"
    )


def _validate_state_target(value: str) -> str:
    target = _validate_path("state_override target", value, min_segments=3)
    if ".model." not in target:
        raise FaultInjectionError("state_override targets must include '.model.'")
    return target


def _validate_signal_target(value: str) -> str:
    target = _validate_path("signal_override target", value, min_segments=3)
    if ".telemetry." not in target:
        raise FaultInjectionError("signal_override targets must include '.telemetry.'")
    return target


def _parse_named_fault_target(value: str) -> tuple[str, str]:
    target = _validate_path("named_fault target", value, min_segments=2)
    if ".model." in target or ".telemetry." in target:
        raise FaultInjectionError("named_fault targets must name a module fault flag")
    module, name = target.split(".", maxsplit=1)
    return module, name


def _validate_path(kind: str, value: str, *, min_segments: int) -> str:
    if not isinstance(value, str) or not _PATH_RE.fullmatch(value):
        raise FaultInjectionError(
            f"{kind} must be a dot-separated path of identifiers, for example "
            "'eps.telemetry.voltage'"
        )
    if len(value.split(".")) < min_segments:
        raise FaultInjectionError(f"{kind} must contain at least {min_segments} path segments")
    return value


def _validate_identifier(kind: str, value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise FaultInjectionError(
            f"{kind} must start with a letter and contain only letters, digits, '_' or '-'"
        )
    return value


def _validate_optional_non_negative_int(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    return _validate_non_negative_int(name, value)


def _validate_non_negative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FaultInjectionError(f"{name} must be a non-negative integer")
    if value < 0:
        raise FaultInjectionError(f"{name} must be non-negative, got {value}")
    return int(value)


__all__ = [
    "ActiveOverride",
    "CycleCount",
    "FaultInjectionEngine",
    "FaultInjectionError",
    "FaultInjectionKind",
    "NamedFaultFlag",
]
