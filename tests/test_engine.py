from __future__ import annotations

import pytest

from cubesat_testbed.engine import (
    AssertionPayload,
    BusPayload,
    CommandPayload,
    DiscreteEventEngine,
    EngineScheduleError,
    EventKind,
    FaultPayload,
    SimulationEvent,
    TelemetryPayload,
    TimerPayload,
)
from cubesat_testbed.protocol.csp_v2 import CspCanFrame
from cubesat_testbed.transport import TransportEnvelope


def test_events_dispatch_by_virtual_time_then_schedule_order() -> None:
    engine = DiscreteEventEngine()
    seen: list[tuple[int, int, EventKind, object]] = []

    def record(event: SimulationEvent, running_engine: DiscreteEventEngine) -> None:
        seen.append((running_engine.now, event.sequence, event.kind, event.payload))

    engine.schedule_at(10, EventKind.COMMAND, "first-at-10", handler=record)
    engine.schedule_at(5, EventKind.TELEMETRY, "first-at-5", handler=record)
    engine.schedule_at(10, EventKind.FAULT, "second-at-10", handler=record)

    dispatched = engine.run_until_idle()

    assert [(event.time, event.sequence, event.kind) for event in dispatched] == [
        (5, 1, EventKind.TELEMETRY),
        (10, 0, EventKind.COMMAND),
        (10, 2, EventKind.FAULT),
    ]
    assert seen == [
        (5, 1, EventKind.TELEMETRY, "first-at-5"),
        (10, 0, EventKind.COMMAND, "first-at-10"),
        (10, 2, EventKind.FAULT, "second-at-10"),
    ]
    assert engine.now == 10


def test_run_next_jumps_to_next_scheduled_event_without_intermediate_ticks() -> None:
    engine = DiscreteEventEngine()
    seen_times: list[int] = []

    def record(event: SimulationEvent, running_engine: DiscreteEventEngine) -> None:
        assert event.kind is EventKind.TIMER
        seen_times.append(running_engine.now)

    event = engine.schedule_timer(100, name="wake", handler=record)

    assert engine.now == 0
    assert engine.next_event_time == 100
    assert engine.run_next() == event
    assert seen_times == [100]
    assert engine.now == 100
    assert engine.run_next() is None


def test_timer_delay_is_relative_to_current_virtual_time() -> None:
    engine = DiscreteEventEngine()
    fired: list[tuple[str, int]] = []

    def record_timer(event: SimulationEvent, running_engine: DiscreteEventEngine) -> None:
        payload = event.payload
        assert isinstance(payload, TimerPayload)
        assert payload.name is not None
        fired.append((payload.name, running_engine.now))
        if payload.name == "first":
            running_engine.schedule_timer(3, name="second", handler=record_timer)

    engine.schedule_timer(5, name="first", handler=record_timer)

    assert [event.time for event in engine.run_until_idle()] == [5, 8]
    assert fired == [("first", 5), ("second", 8)]
    assert engine.now == 8


def test_engine_supports_all_product_v1_event_payloads() -> None:
    engine = DiscreteEventEngine()
    envelope = TransportEnvelope(
        frame=CspCanFrame(can_id=0x10004083, data=bytes.fromhex("00 04 14 80 55")),
        sequence=7,
        source="obc",
    )

    scheduled = (
        engine.schedule_timer(0, name="tick"),
        engine.schedule_bus(envelope, endpoint="eps"),
        engine.schedule_telemetry("eps.telemetry.voltage", 7.4, source="eps"),
        engine.schedule_command("payload_power_off", target="payload"),
        engine.schedule_fault(
            "state_override",
            "eps.model.battery_percent",
            value=25,
            duration=5,
        ),
        engine.schedule_assertion("payload_offline", passed=None),
    )

    assert [event.kind for event in scheduled] == [
        EventKind.TIMER,
        EventKind.BUS,
        EventKind.TELEMETRY,
        EventKind.COMMAND,
        EventKind.FAULT,
        EventKind.ASSERTION,
    ]
    assert [event.kind for event in engine.run_until_idle()] == [
        EventKind.TIMER,
        EventKind.BUS,
        EventKind.TELEMETRY,
        EventKind.COMMAND,
        EventKind.FAULT,
        EventKind.ASSERTION,
    ]

    timer_payload = scheduled[0].payload
    bus_payload = scheduled[1].payload
    telemetry_payload = scheduled[2].payload
    command_payload = scheduled[3].payload
    fault_payload = scheduled[4].payload
    assertion_payload = scheduled[5].payload

    assert isinstance(timer_payload, TimerPayload)
    assert timer_payload.name == "tick"
    assert isinstance(bus_payload, BusPayload)
    assert bus_payload.envelope == envelope
    assert bus_payload.endpoint == "eps"
    assert isinstance(telemetry_payload, TelemetryPayload)
    assert telemetry_payload.signal == "eps.telemetry.voltage"
    assert telemetry_payload.value == 7.4
    assert isinstance(command_payload, CommandPayload)
    assert command_payload.command == "payload_power_off"
    assert command_payload.target == "payload"
    assert isinstance(fault_payload, FaultPayload)
    assert fault_payload.fault_type == "state_override"
    assert fault_payload.target == "eps.model.battery_percent"
    assert fault_payload.duration == 5
    assert isinstance(assertion_payload, AssertionPayload)
    assert assertion_payload.name == "payload_offline"


def test_registered_handlers_are_called_after_event_specific_handler() -> None:
    engine = DiscreteEventEngine()
    calls: list[str] = []

    def one_shot_handler(event: SimulationEvent, running_engine: DiscreteEventEngine) -> None:
        assert running_engine is engine
        assert event.kind is EventKind.COMMAND
        calls.append("one-shot")

    def registered_handler(event: SimulationEvent, running_engine: DiscreteEventEngine) -> None:
        assert running_engine is engine
        assert event.kind is EventKind.COMMAND
        calls.append("registered")

    engine.add_handler(EventKind.COMMAND, registered_handler)
    engine.schedule_command("deploy", handler=one_shot_handler)
    engine.run_until_idle()

    assert calls == ["one-shot", "registered"]


def test_schedule_rejects_events_in_the_past() -> None:
    engine = DiscreteEventEngine()
    engine.advance_to(10)

    with pytest.raises(EngineScheduleError, match="cannot schedule"):
        engine.schedule_at(9, EventKind.TIMER)


def test_advance_to_rejects_skipping_pending_events() -> None:
    engine = DiscreteEventEngine()
    engine.schedule_timer(5, name="due")

    with pytest.raises(EngineScheduleError, match="pending event"):
        engine.advance_to(6)

    assert engine.now == 0
    assert engine.next_event_time == 5
