"""Deterministic discrete-event simulation engine.

The engine owns virtual time and dispatches scheduled events between modules,
transports, the scenario runner, the OBC Peer rule engine, and the fault
injection layer. Runtime model updates are event-driven, not wall-clock sleep
loops.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from heapq import heappop, heappush
from typing import TypeAlias

from cubesat_testbed.transport.base import EndpointId, TransportEnvelope

VirtualTime: TypeAlias = int
EventHandler: TypeAlias = Callable[["SimulationEvent", "DiscreteEventEngine"], None]


class EngineError(RuntimeError):
    """Base class for deterministic engine failures."""


class EngineScheduleError(EngineError, ValueError):
    """Raised when an event cannot be scheduled on the virtual timeline."""


class EventKind(str, Enum):
    """Supported product-v1 event classes."""

    TIMER = "timer"
    BUS = "bus"
    TELEMETRY = "telemetry"
    COMMAND = "command"
    FAULT = "fault"
    ASSERTION = "assertion"


@dataclass(frozen=True, slots=True)
class TimerPayload:
    """Payload for a scheduled virtual-time timer event."""

    name: str | None = None

    def __post_init__(self) -> None:
        if self.name is not None:
            object.__setattr__(self, "name", _validate_non_empty_str("timer name", self.name))


@dataclass(frozen=True, slots=True)
class BusPayload:
    """Payload for a bus-frame delivery event."""

    envelope: TransportEnvelope
    endpoint: EndpointId | None = None


@dataclass(frozen=True, slots=True)
class TelemetryPayload:
    """Payload for decoded or generated telemetry."""

    signal: str
    value: object
    source: EndpointId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal", _validate_non_empty_str("telemetry signal", self.signal))


@dataclass(frozen=True, slots=True)
class CommandPayload:
    """Payload for a command emitted by a scenario, peer rule, or module."""

    command: str
    payload: object = None
    source: EndpointId | None = None
    target: EndpointId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", _validate_non_empty_str("command", self.command))


@dataclass(frozen=True, slots=True)
class FaultPayload:
    """Payload for a passive fault-injection request."""

    fault_type: str
    target: str
    value: object = None
    duration: VirtualTime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "fault_type", _validate_non_empty_str("fault type", self.fault_type)
        )
        object.__setattr__(self, "target", _validate_non_empty_str("fault target", self.target))
        if self.duration is not None:
            object.__setattr__(
                self,
                "duration",
                _validate_virtual_time("fault duration", self.duration),
            )


@dataclass(frozen=True, slots=True)
class AssertionPayload:
    """Payload for a scenario assertion request or result."""

    name: str
    passed: bool | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_non_empty_str("assertion name", self.name))
        if self.passed is not None and not isinstance(self.passed, bool):
            raise EngineScheduleError("assertion passed state must be a bool or None")


@dataclass(frozen=True, slots=True)
class SimulationEvent:
    """One immutable event scheduled on the engine's virtual timeline.

    Events are ordered by ``time`` first and by engine-assigned ``sequence`` second.
    The sequence number makes same-tick dispatch deterministic without depending on
    dictionary order, object identity, wall-clock timestamps, or background loops.
    """

    time: VirtualTime
    sequence: int
    kind: EventKind
    payload: object = None


@dataclass(order=True, slots=True)
class _QueuedEvent:
    time: VirtualTime
    sequence: int
    event: SimulationEvent = field(compare=False)
    handler: EventHandler | None = field(default=None, compare=False)


class DiscreteEventEngine:
    """Deterministic virtual-time event dispatcher.

    The engine never sleeps or spins over empty ticks. A call to ``run_next`` pops
    the next scheduled event, jumps ``now`` directly to that event's virtual time,
    and dispatches the event's one-shot handler plus any registered handlers for
    the event kind.
    """

    def __init__(self, *, start_time: VirtualTime = 0) -> None:
        self._now = _validate_virtual_time("start_time", start_time)
        self._next_sequence = 0
        self._queue: list[_QueuedEvent] = []
        self._handlers: dict[EventKind, list[EventHandler]] = {}

    @property
    def now(self) -> VirtualTime:
        """Current virtual time owned by the engine."""

        return self._now

    @property
    def pending_count(self) -> int:
        """Number of events waiting on the virtual timeline."""

        return len(self._queue)

    @property
    def next_event_time(self) -> VirtualTime | None:
        """Virtual time of the next event, or ``None`` when the queue is empty."""

        if not self._queue:
            return None
        return self._queue[0].time

    def pending_events(self) -> tuple[SimulationEvent, ...]:
        """Return queued events in deterministic dispatch order without mutating state."""

        return tuple(item.event for item in sorted(self._queue))

    def add_handler(self, kind: EventKind | str, handler: EventHandler) -> None:
        """Register a deterministic handler for every event of ``kind``."""

        event_kind = _coerce_event_kind(kind)
        self._handlers.setdefault(event_kind, []).append(handler)

    def remove_handler(self, kind: EventKind | str, handler: EventHandler) -> None:
        """Remove a previously registered event-kind handler."""

        event_kind = _coerce_event_kind(kind)
        handlers = self._handlers.get(event_kind)
        if handlers is None or handler not in handlers:
            raise EngineScheduleError(f"handler is not registered for {event_kind.value!r} events")
        handlers.remove(handler)
        if not handlers:
            del self._handlers[event_kind]

    def schedule_at(
        self,
        time: VirtualTime,
        kind: EventKind | str,
        payload: object = None,
        *,
        handler: EventHandler | None = None,
    ) -> SimulationEvent:
        """Schedule one event at an absolute virtual time."""

        scheduled_time = _validate_virtual_time("event time", time)
        if scheduled_time < self._now:
            raise EngineScheduleError(
                f"cannot schedule event at {scheduled_time}; current virtual time is {self._now}"
            )

        sequence = self._next_sequence
        self._next_sequence += 1
        event = SimulationEvent(
            time=scheduled_time,
            sequence=sequence,
            kind=_coerce_event_kind(kind),
            payload=payload,
        )
        heappush(
            self._queue,
            _QueuedEvent(
                time=event.time,
                sequence=event.sequence,
                event=event,
                handler=handler,
            ),
        )
        return event

    def schedule_after(
        self,
        delay: VirtualTime,
        kind: EventKind | str,
        payload: object = None,
        *,
        handler: EventHandler | None = None,
    ) -> SimulationEvent:
        """Schedule one event after a non-negative virtual-time delay."""

        validated_delay = _validate_virtual_time("event delay", delay)
        return self.schedule_at(self._now + validated_delay, kind, payload, handler=handler)

    def schedule_timer(
        self,
        delay: VirtualTime,
        *,
        name: str | None = None,
        handler: EventHandler | None = None,
    ) -> SimulationEvent:
        """Schedule a timer event relative to the current virtual time."""

        return self.schedule_after(
            delay,
            EventKind.TIMER,
            TimerPayload(name=name),
            handler=handler,
        )

    def schedule_bus(
        self,
        envelope: TransportEnvelope,
        *,
        delay: VirtualTime = 0,
        endpoint: EndpointId | None = None,
        handler: EventHandler | None = None,
    ) -> SimulationEvent:
        """Schedule deterministic delivery of one bus envelope."""

        return self.schedule_after(
            delay,
            EventKind.BUS,
            BusPayload(envelope=envelope, endpoint=endpoint),
            handler=handler,
        )

    def schedule_telemetry(
        self,
        signal: str,
        value: object,
        *,
        delay: VirtualTime = 0,
        source: EndpointId | None = None,
        handler: EventHandler | None = None,
    ) -> SimulationEvent:
        """Schedule a telemetry event."""

        return self.schedule_after(
            delay,
            EventKind.TELEMETRY,
            TelemetryPayload(signal=signal, value=value, source=source),
            handler=handler,
        )

    def schedule_command(
        self,
        command: str,
        *,
        delay: VirtualTime = 0,
        payload: object = None,
        source: EndpointId | None = None,
        target: EndpointId | None = None,
        handler: EventHandler | None = None,
    ) -> SimulationEvent:
        """Schedule a command event."""

        return self.schedule_after(
            delay,
            EventKind.COMMAND,
            CommandPayload(command=command, payload=payload, source=source, target=target),
            handler=handler,
        )

    def schedule_fault(
        self,
        fault_type: str,
        target: str,
        *,
        delay: VirtualTime = 0,
        value: object = None,
        duration: VirtualTime | None = None,
        handler: EventHandler | None = None,
    ) -> SimulationEvent:
        """Schedule a passive fault-injection request event."""

        return self.schedule_after(
            delay,
            EventKind.FAULT,
            FaultPayload(fault_type=fault_type, target=target, value=value, duration=duration),
            handler=handler,
        )

    def schedule_assertion(
        self,
        name: str,
        *,
        delay: VirtualTime = 0,
        passed: bool | None = None,
        detail: str | None = None,
        handler: EventHandler | None = None,
    ) -> SimulationEvent:
        """Schedule a scenario assertion event."""

        return self.schedule_after(
            delay,
            EventKind.ASSERTION,
            AssertionPayload(name=name, passed=passed, detail=detail),
            handler=handler,
        )

    def run_next(self) -> SimulationEvent | None:
        """Dispatch the next scheduled event, jumping virtual time to it."""

        if not self._queue:
            return None

        item = heappop(self._queue)
        self._now = item.time
        self._dispatch(item)
        return item.event

    def run_until_idle(self, *, max_events: int | None = None) -> tuple[SimulationEvent, ...]:
        """Dispatch queued events until the timeline is empty or ``max_events`` is reached."""

        remaining = _validate_optional_count("max_events", max_events)
        dispatched: list[SimulationEvent] = []

        while self._queue:
            if remaining is not None:
                if remaining == 0:
                    break
                remaining -= 1

            event = self.run_next()
            if event is None:
                break
            dispatched.append(event)

        return tuple(dispatched)

    def run_until(
        self,
        time: VirtualTime,
        *,
        max_events: int | None = None,
    ) -> tuple[SimulationEvent, ...]:
        """Dispatch events due at or before ``time`` and then jump to that horizon."""

        horizon = _validate_virtual_time("run horizon", time)
        if horizon < self._now:
            raise EngineScheduleError(
                f"cannot run backward to {horizon}; current virtual time is {self._now}"
            )

        remaining = _validate_optional_count("max_events", max_events)
        dispatched: list[SimulationEvent] = []
        stopped_by_limit = False

        while self._queue and self._queue[0].time <= horizon:
            if remaining is not None:
                if remaining == 0:
                    stopped_by_limit = True
                    break
                remaining -= 1

            event = self.run_next()
            if event is None:
                break
            dispatched.append(event)

        if not stopped_by_limit:
            self._now = horizon

        return tuple(dispatched)

    def advance_to(self, time: VirtualTime) -> None:
        """Advance virtual time without dispatching events.

        This method may jump over empty virtual-time spans only. It refuses to
        skip pending events because doing so would make later dispatch move time
        backward.
        """

        target = _validate_virtual_time("advance target", time)
        if target < self._now:
            raise EngineScheduleError(
                f"cannot advance backward to {target}; current virtual time is {self._now}"
            )
        if self._queue and self._queue[0].time < target:
            raise EngineScheduleError(
                f"cannot advance past pending event at {self._queue[0].time} without dispatching it"
            )
        self._now = target

    def _dispatch(self, item: _QueuedEvent) -> None:
        event = item.event
        if item.handler is not None:
            item.handler(event, self)

        for handler in tuple(self._handlers.get(event.kind, ())):
            handler(event, self)


SimulationEngine = DiscreteEventEngine


def _coerce_event_kind(kind: EventKind | str) -> EventKind:
    if isinstance(kind, EventKind):
        return kind
    try:
        return EventKind(kind)
    except ValueError as exc:
        supported = ", ".join(event_kind.value for event_kind in EventKind)
        raise EngineScheduleError(
            f"unsupported event kind {kind!r}; expected one of: {supported}"
        ) from exc


def _validate_virtual_time(name: str, value: VirtualTime) -> VirtualTime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EngineScheduleError(f"{name} must be an integer virtual-time tick")
    if value < 0:
        raise EngineScheduleError(f"{name} must be non-negative, got {value}")
    return int(value)


def _validate_optional_count(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise EngineScheduleError(f"{name} must be an integer or None")
    if value < 0:
        raise EngineScheduleError(f"{name} must be non-negative, got {value}")
    return int(value)


def _validate_non_empty_str(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise EngineScheduleError(f"{name} must be a string")
    if not value.strip():
        raise EngineScheduleError(f"{name} must not be empty")
    return value


__all__ = [
    "AssertionPayload",
    "BusPayload",
    "CommandPayload",
    "DiscreteEventEngine",
    "EngineError",
    "EngineScheduleError",
    "EventHandler",
    "EventKind",
    "FaultPayload",
    "SimulationEngine",
    "SimulationEvent",
    "TelemetryPayload",
    "TimerPayload",
    "VirtualTime",
]
