"""Bus fault injection: rules on the in-memory bus, deterministic and visible."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cubesat_testbed.config import (
    BusFaultRule,
    ScenarioReferenceError,
    parse_scenario,
    parse_testbed_config,
)
from cubesat_testbed.main import main
from cubesat_testbed.protocol.csp_v2 import (
    CspCanFrame,
    CspFields,
    decode_frame,
    fragment_packet,
)
from cubesat_testbed.report import RunRecorder, render_report
from cubesat_testbed.scenario import run_scenario
from cubesat_testbed.transport import (
    BusFaultEvent,
    FaultInjectingTransport,
    InMemoryBusAdapter,
    format_bus_fault,
)

# --- a hand-driven virtual clock for the transport on its own ---------------------------


class _Clock:
    """Virtual time and the callbacks scheduled on it, run by hand."""

    def __init__(self) -> None:
        self.now = 0
        self._pending: list[tuple[int, int, object]] = []
        self._counter = 0

    def schedule(self, delay: int, action: object) -> None:
        self._pending.append((self.now + delay, self._counter, action))
        self._counter += 1

    def advance(self, to: int) -> None:
        while True:
            due = sorted(item for item in self._pending if item[0] <= to)
            if not due:
                break
            item = due[0]
            self._pending.remove(item)
            self.now = item[0]
            item[2]()  # type: ignore[operator]
        self.now = to


def _bus(
    *rules: dict[str, object], seed: int = 0
) -> tuple[FaultInjectingTransport, _Clock, InMemoryBusAdapter]:
    clock = _Clock()
    inner = InMemoryBusAdapter()
    bus = FaultInjectingTransport(inner, now=lambda: clock.now, schedule=clock.schedule, seed=seed)
    for rule in rules:
        bus.add_rule(BusFaultRule.model_validate(rule))
    return bus, clock, inner


def _frames(
    port: int, payload: bytes = b"\x01", *, source: int = 1, count: int = 0
) -> tuple[CspCanFrame, ...]:
    fields = CspFields(
        priority=2,
        source=source,
        destination=2,
        destination_port=port,
        source_port=5,
    )
    return fragment_packet(fields, payload, packet_count=count)


def _send(bus: FaultInjectingTransport, port: int, payload: bytes = b"\x01") -> int:
    (frame,) = _frames(port, payload)
    return bus.send(frame, source=1).sequence


def _ports_received(bus: FaultInjectingTransport) -> list[int]:
    return [decode_frame(env.frame).fields.destination_port for env in bus.drain()]  # type: ignore[arg-type]


# --- the schema ---------------------------------------------------------------------------


def test_a_rule_needs_exactly_the_parameters_of_its_type() -> None:
    with pytest.raises(ValidationError, match="positive delay"):
        BusFaultRule(type="delay")
    with pytest.raises(ValidationError, match="only valid for a delay"):
        BusFaultRule(type="drop", delay="1s")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="only valid for a reorder"):
        BusFaultRule(type="drop", hold="1s")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="only valid for a corrupt"):
        BusFaultRule(type="drop", byte=1)
    with pytest.raises(ValidationError, match="either nth or probability"):
        BusFaultRule(type="drop", nth=1, probability=0.5)
    with pytest.raises(ValidationError):
        BusFaultRule(type="explode")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        BusFaultRule(type="drop", nth=0)
    with pytest.raises(ValidationError):
        BusFaultRule(type="drop", port=64)


def test_bus_faults_are_refused_on_a_transport_that_runs_on_wall_clock_time() -> None:
    text = """
[transport]
type = "kiss"
device = "/dev/null"

[bus_faults]
seed = 1

[nodes.a]
mode = "software"
address = 1
"""
    with pytest.raises(ValidationError, match="requires transport.type='in-memory'"):
        parse_testbed_config(text)


def test_an_inject_bus_fault_step_is_refused_on_a_real_transport() -> None:
    setup = parse_testbed_config(
        """
[transport]
type = "socketcan"
interface = "vcan0"

[nodes.a]
mode = "software"
address = 1
"""
    )
    scenario = parse_scenario(
        "name: s\nsteps:\n  - action: inject_bus_fault\n    type: drop\n    port: 3\n"
    )
    from cubesat_testbed.config import validate_scenario_references

    with pytest.raises(ScenarioReferenceError, match="in-memory"):
        validate_scenario_references(scenario, setup)


# --- drop, by the Nth frame on a port --------------------------------------------------------


def test_drop_the_nth_frame_on_a_port_and_only_that_one() -> None:
    bus, _clock, _inner = _bus({"type": "drop", "port": 7, "nth": 2})

    for port in (7, 9, 7, 9, 7):
        _send(bus, port)

    # Frames to port 9 are not counted, the second frame to port 7 is lost.
    assert _ports_received(bus) == [7, 9, 9, 7]
    (event,) = bus.events
    assert (event.type, event.direction, event.match, event.destination_port) == (
        "drop",
        "TX",
        2,
        7,
    )


def test_nth_with_count_drops_consecutive_frames() -> None:
    bus, _clock, _inner = _bus({"type": "drop", "port": 7, "nth": 2, "count": 2})

    for _ in range(5):
        _send(bus, 7)

    assert len(_ports_received(bus)) == 3
    assert [event.match for event in bus.events] == [2, 3]


def test_a_rule_without_nth_fires_on_every_matching_frame_up_to_its_count() -> None:
    bus, _clock, _inner = _bus({"type": "drop", "port": 7, "count": 2})

    for _ in range(4):
        _send(bus, 7)

    assert len(_ports_received(bus)) == 2
    assert len(bus.events) == 2


def test_selectors_match_source_destination_and_source_port() -> None:
    bus, _clock, _inner = _bus(
        {"type": "drop", "source": 9},
        {"type": "drop", "destination": 4},
        {"type": "drop", "source_port": 6},
    )
    other = CspFields(priority=2, source=1, destination=2, destination_port=3, source_port=5)
    from_nine = CspFields(priority=2, source=9, destination=2, destination_port=3, source_port=5)
    to_four = CspFields(priority=2, source=1, destination=4, destination_port=3, source_port=5)
    sport_six = CspFields(priority=2, source=1, destination=2, destination_port=3, source_port=6)

    for fields in (other, from_nine, to_four, sport_six):
        (frame,) = fragment_packet(fields, b"\x01")
        bus.send(frame, source=1)

    delivered = [decode_frame(env.frame).fields for env in bus.drain()]  # type: ignore[arg-type]
    assert delivered == [other]
    assert [event.rule for event in bus.events] == ["drop-1", "drop-2", "drop-3"]


def test_a_port_rule_reaches_every_fragment_of_a_multi_frame_packet() -> None:
    bus, _clock, _inner = _bus({"type": "drop", "port": 7, "nth": 2})
    packet = _frames(7, bytes(range(20)))  # first frame + two continuation frames
    assert len(packet) == 3

    for frame in packet:
        bus.send(frame, source=1)

    # The second frame on port 7 is the first continuation fragment, which carries no
    # port of its own: it is matched by the header of the packet it belongs to.
    assert len(list(bus.drain())) == 2
    (event,) = bus.events
    assert event.destination_port == 7
    assert event.match == 2


def test_the_first_matching_rule_wins_and_later_rules_count_only_what_reaches_them() -> None:
    bus, _clock, _inner = _bus(
        {"type": "drop", "port": 7, "nth": 1},
        {"type": "duplicate", "port": 7, "nth": 1},
    )

    for _ in range(3):
        _send(bus, 7)

    # Frame 1 is dropped by rule one. Rule two counts frames 2 and 3 as its 1st and 2nd,
    # so it duplicates frame 2.
    assert _ports_received(bus) == [7, 7, 7]
    assert [(e.rule, e.match) for e in bus.events] == [("drop-1", 1), ("duplicate-2", 1)]


# --- delay, in virtual time ------------------------------------------------------------------


def test_delay_holds_a_frame_for_a_virtual_duration() -> None:
    bus, clock, _inner = _bus({"type": "delay", "port": 7, "nth": 1, "delay": "200ms"})
    released: list[int] = []
    bus.set_release_hook(lambda: released.append(clock.now))

    _send(bus, 7)
    _send(bus, 9)

    assert _ports_received(bus) == [9]  # the frame to port 7 is still in flight
    assert bus.events[0].detail == "until=200000"

    clock.advance(199_999)
    assert _ports_received(bus) == []
    clock.advance(200_000)
    assert _ports_received(bus) == [7]
    assert released == [200_000]


def test_a_delayed_frame_keeps_the_sequence_its_sender_was_given() -> None:
    bus, clock, _inner = _bus({"type": "delay", "nth": 1, "delay": "1s"})

    delayed = _send(bus, 7)
    later = _send(bus, 9)
    clock.advance(1_000_000)

    # The sender was handed `delayed` before the frame reached the bus; the same number
    # comes back when it arrives, behind the frame that was sent after it.
    assert [env.sequence for env in bus.drain()] == [later, delayed]


# --- corrupt ---------------------------------------------------------------------------------


def test_corrupt_flips_the_named_byte() -> None:
    bus, _clock, _inner = _bus({"type": "corrupt", "nth": 1, "byte": 4, "mask": 0x0F})
    (frame,) = _frames(7, b"\xa0")

    bus.send(frame, source=1)

    (envelope,) = bus.drain()
    assert envelope.frame.data[:4] == frame.data[:4]  # type: ignore[union-attr]
    assert envelope.frame.data[4] == 0xAF  # type: ignore[union-attr]
    assert bus.events[0].detail == "byte=4 mask=0x0F a0->af"
    assert bus.events[0].data == frame.data  # the event keeps the frame as it was sent


def test_corrupt_without_a_byte_picks_one_from_the_seed() -> None:
    def corrupted_byte(seed: int) -> int:
        bus, _clock, _inner = _bus({"type": "corrupt"}, seed=seed)
        (frame,) = _frames(7, b"\x01\x02\x03\x04")
        bus.send(frame, source=1)
        (envelope,) = bus.drain()
        return next(
            index
            for index, (a, b) in enumerate(zip(frame.data, envelope.frame.data, strict=True))  # type: ignore[union-attr]
            if a != b
        )

    assert corrupted_byte(1) == corrupted_byte(1)
    assert len({corrupted_byte(seed) for seed in range(20)}) > 1


def test_corrupting_a_byte_a_frame_does_not_have_is_reported_not_raised() -> None:
    bus, _clock, _inner = _bus({"type": "corrupt", "nth": 1, "byte": 7})
    (frame,) = _frames(7, b"\x01")

    bus.send(frame, source=1)

    (envelope,) = bus.drain()
    assert envelope.frame == frame
    assert "skipped" in bus.events[0].detail


# --- duplicate and reorder -------------------------------------------------------------------


def test_duplicate_delivers_the_frame_twice_under_one_sequence() -> None:
    bus, _clock, _inner = _bus({"type": "duplicate", "port": 7, "nth": 1})

    sequence = _send(bus, 7)
    _send(bus, 9)

    envelopes = list(bus.drain())
    assert [decode_frame(e.frame).fields.destination_port for e in envelopes] == [7, 7, 9]  # type: ignore[arg-type]
    assert envelopes[0].sequence == envelopes[1].sequence == sequence


def test_reorder_delivers_the_frame_after_the_next_one() -> None:
    bus, _clock, _inner = _bus({"type": "reorder", "port": 7, "nth": 1})

    _send(bus, 7)
    assert _ports_received(bus) == []  # held back
    _send(bus, 9)
    _send(bus, 8)

    assert _ports_received(bus) == [9, 7, 8]


def test_a_reordered_frame_is_released_after_its_hold_if_nothing_follows() -> None:
    bus, clock, _inner = _bus({"type": "reorder", "nth": 1, "hold": "500ms"})

    _send(bus, 7)
    clock.advance(499_999)
    assert _ports_received(bus) == []
    clock.advance(500_000)
    assert _ports_received(bus) == [7]


# --- the receive side ------------------------------------------------------------------------


def test_rx_drop_loses_a_frame_that_was_on_the_bus() -> None:
    bus, _clock, inner = _bus({"type": "drop", "direction": "rx", "port": 7, "nth": 1})

    _send(bus, 7)
    _send(bus, 7)

    assert inner.pending_count() == 2  # both frames crossed the bus
    assert len(list(bus.drain())) == 1
    assert bus.events[0].direction == "RX"


def test_rx_delay_duplicate_corrupt_and_reorder() -> None:
    bus, clock, _inner = _bus(
        {"type": "delay", "direction": "rx", "port": 1, "nth": 1, "delay": "1s"},
        {"type": "duplicate", "direction": "rx", "port": 2, "nth": 1},
        {"type": "corrupt", "direction": "rx", "port": 3, "nth": 1, "byte": 4, "mask": 0x10},
        {"type": "reorder", "direction": "rx", "port": 4, "nth": 1},
    )
    released: list[int] = []
    bus.set_release_hook(lambda: released.append(clock.now))

    for port in (1, 2, 3, 4, 5):
        _send(bus, port, bytes([port]))

    received = list(bus.drain())
    # Port 1 is delayed, 2 comes twice, 3 comes with a flipped byte, 4 is held back
    # until 5 has gone by.
    assert [e.frame.data[4] for e in received] == [2, 2, 3 ^ 0x10, 5, 4]  # type: ignore[union-attr]

    clock.advance(1_000_000)
    (late,) = bus.drain()
    assert decode_frame(late.frame).fields.destination_port == 1  # type: ignore[arg-type]
    assert released == [1_000_000]


def test_endpoint_streams_pass_through_untouched() -> None:
    clock = _Clock()
    inner = InMemoryBusAdapter(endpoints=(3,))
    bus = FaultInjectingTransport(inner, now=lambda: clock.now, schedule=clock.schedule)
    bus.add_rule(BusFaultRule(type="drop", direction="rx"))

    _send(bus, 7)

    assert bus.pending_count(endpoint=3) == 1
    assert bus.receive(endpoint=3) is not None
    assert bus.events == ()


# --- determinism -------------------------------------------------------------------------------


def _probabilistic_run(seed: int) -> tuple[list[int], list[BusFaultEvent]]:
    bus, _clock, _inner = _bus({"type": "drop", "probability": 0.5}, seed=seed)
    for port in range(40):
        _send(bus, port % 64)
    return _ports_received(bus), list(bus.events)


def test_the_same_seed_gives_the_same_faults_and_another_seed_does_not() -> None:
    first = _probabilistic_run(11)

    assert _probabilistic_run(11) == first
    assert _probabilistic_run(12) != first
    delivered, events = first
    assert 0 < len(events) < 40
    assert len(delivered) + len(events) == 40


def test_adding_a_rule_does_not_change_the_draws_of_another() -> None:
    def drops(extra: bool) -> list[int]:
        rules = [{"type": "drop", "probability": 0.5, "port": 1}]
        if extra:
            rules.append({"type": "duplicate", "probability": 0.5, "port": 2})
        bus, _clock, _inner = _bus(*rules, seed=3)
        for _ in range(30):
            _send(bus, 1)
            _send(bus, 2)
        return [e.match for e in bus.events if e.type == "drop"]

    assert drops(extra=False) == drops(extra=True)


def test_take_lost_sequences_reports_dropped_frames_once() -> None:
    bus, _clock, _inner = _bus({"type": "drop", "nth": 1})

    lost = _send(bus, 7)
    _send(bus, 7)

    assert bus.take_lost_sequences() == (lost,)
    assert bus.take_lost_sequences() == ()


def test_without_rules_the_bus_is_a_plain_pass_through() -> None:
    bus, _clock, inner = _bus()

    sequences = [_send(bus, 7), _send(bus, 8)]

    assert sequences == [0, 1]
    assert [e.sequence for e in bus.drain()] == [0, 1]
    assert inner.pending_count() == 0
    assert not bus.has_rules


def test_format_bus_fault_is_a_single_greppable_line() -> None:
    bus, _clock, _inner = _bus({"type": "drop", "port": 7, "nth": 1, "name": "lose-first"})
    _send(bus, 7)

    line = format_bus_fault(bus.events[0])

    assert line.startswith("FAULT t=0 rule=lose-first action=drop dir=TX match=1 can_id=0x")
    assert " dport=7 " in line and "\n" not in line


# --- end to end through the runner -----------------------------------------------------------

SETUP = """
[transport]
type = "in-memory"

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "simulated"
module_type = "generic_eps"
address = 2

[nodes.payload]
mode = "simulated"
module_type = "simple_payload"
address = 3

[nodes.obc.commands.payload_power_off]
target = "payload"
destination_port = 10
source_port = 10
payload_hex = "{command_payload}"

[nodes.eps.telemetry.battery_percent]
source_port = 20
destination_port = 20
units = "percent"
min = 0
max = 100
offset = 0
length = 4
type = "float"

[nodes.payload.telemetry.power_status]
source_port = 21
destination_port = 21
offset = 0
length = 1
type = "uint"

[nodes.payload.telemetry.power_status.enum]
0 = "offline"
1 = "online"

[nodes.obc.rules.low_battery_shed_payload]
signal = "eps.telemetry.battery_percent"
op = "<"
threshold = 30.0
for = "3s"

[[nodes.obc.rules.low_battery_shed_payload.actions]]
type = "send_command"
command = "payload_power_off"
"""

SCENARIO = """
name: Shed the payload
steps:
  - action: inject_fault
    type: state_override
    target: eps.model.battery_percent
    value: 25
    duration: 10s
  - action: wait
    virtual_time: 3s
  - action: assert
    signal: payload.telemetry.power_status
    op: "=="
    value: offline
    timeout: 1s
  - action: wait
    virtual_time: 3s
"""

LOSE_THE_COMMAND = """
[bus_faults]
seed = 42

[[bus_faults.rules]]
type = "drop"
port = 10
nth = 1
"""


def _run(
    setup_extra: str = "",
    scenario_text: str = SCENARIO,
    *,
    command_payload: str = "00",
    recorder: RunRecorder | None = None,
) -> tuple[object, str]:
    setup = parse_testbed_config(SETUP.format(command_payload=command_payload) + setup_extra)
    scenario = parse_scenario(scenario_text, setup=setup)
    trace = io.StringIO()
    result = run_scenario(scenario, setup, output=io.StringIO(), trace=trace, recorder=recorder)
    return result, trace.getvalue()


def test_a_run_without_bus_faults_reports_none() -> None:
    result, trace = _run()

    assert result.passed  # type: ignore[attr-defined]
    assert result.bus_faults == ()  # type: ignore[attr-defined]
    assert result.bus_fault_seed is None  # type: ignore[attr-defined]
    assert "FAULT" not in trace


def test_a_dropped_command_is_visible_in_the_trace_and_the_result() -> None:
    result, trace = _run(LOSE_THE_COMMAND)

    assert result.bus_fault_seed == 42  # type: ignore[attr-defined]
    (fault,) = result.bus_faults  # type: ignore[attr-defined]
    assert (fault.type, fault.direction, fault.destination_port, fault.match) == (
        "drop",
        "TX",
        10,
        1,
    )

    lines = trace.splitlines()
    fault_index = next(i for i, line in enumerate(lines) if " FAULT " in line)
    # The frame the OBC tried to send comes first, what the bus did to it right after,
    # and that frame never shows up as received.
    assert " TX " in lines[fault_index - 1] and "dport=10" in lines[fault_index - 1]
    assert lines[fault_index].startswith(f"trace FAULT t={fault.time} rule=drop-1 action=drop")
    assert not any(
        " RX " in line and f"t={fault.time} " in line and "dport=10" in line for line in lines
    )


def test_the_rule_in_setup_and_the_same_rule_as_a_scenario_step_do_the_same_thing() -> None:
    step = """
  - action: inject_bus_fault
    type: drop
    port: 10
    nth: 1
"""
    in_setup, _ = _run(LOSE_THE_COMMAND)
    in_scenario, _ = _run(scenario_text=SCENARIO.replace("steps:\n", "steps:" + step, 1))

    def shape(result: object) -> list[tuple[int, str, int]]:
        return [(f.time, f.type, f.match) for f in result.bus_faults]  # type: ignore[attr-defined]

    assert shape(in_scenario) == shape(in_setup)
    assert in_scenario.bus_fault_seed == 0  # type: ignore[attr-defined]


def test_two_runs_with_one_seed_have_byte_identical_traces() -> None:
    probabilistic = """
[bus_faults]
seed = 5

[[bus_faults.rules]]
type = "drop"
probability = 0.4
port = 20

[[bus_faults.rules]]
type = "corrupt"
probability = 0.3
"""
    first_result, first_trace = _run(probabilistic)
    second_result, second_trace = _run(probabilistic)

    assert first_trace == second_trace
    assert first_result.bus_faults == second_result.bus_faults  # type: ignore[attr-defined]
    assert first_result.bus_faults  # type: ignore[attr-defined]

    _, other_trace = _run(probabilistic.replace("seed = 5", "seed = 6"))
    assert other_trace != first_trace


def test_a_dropped_fragment_makes_the_packet_time_out_visibly() -> None:
    long_payload = "00 01 02 03 04 05 06 07 08 09 0a 0b"
    result, trace = _run(
        """
[[bus_faults.rules]]
type = "drop"
port = 10
nth = 2
""",
        command_payload=long_payload,
    )

    # The second frame on port 10 is the first continuation fragment of the command.
    reasons = {d.reason for d in result.dropped_packets}  # type: ignore[attr-defined]
    assert reasons & {"timeout", "frame_lost", "orphan_fragment"}
    assert any(" FAULT " in line and "dport=10" in line for line in trace.splitlines())
    assert any(" DROP " in line for line in trace.splitlines())


def test_a_delayed_command_arrives_late_with_its_payload_intact() -> None:
    delayed = """
[[bus_faults.rules]]
type = "delay"
port = 10
nth = 1
delay = "700ms"
"""
    result, trace = _run(delayed)

    (fault,) = result.bus_faults  # type: ignore[attr-defined]
    assert fault.detail == f"until={fault.time + 700_000}"
    received = [
        line
        for line in trace.splitlines()
        if " RX " in line and "dport=10" in line and f"t={fault.time + 700_000} " in line
    ]
    # Released in the very instant its delay ran out, not at the next physical step.
    assert received
    assert result.passed  # type: ignore[attr-defined]


def test_a_dropped_command_does_not_leave_its_payload_behind() -> None:
    from cubesat_testbed.scenario.runner import ScenarioRunner, build_runtime

    setup = parse_testbed_config(SETUP.format(command_payload="00") + LOSE_THE_COMMAND)
    scenario = parse_scenario(
        SCENARIO.replace(
            "  - action: wait\n    virtual_time: 3s\n  - action: assert",
            "  - action: send_command\n    command: payload_power_off\n    payload: 1\n"
            "  - action: wait\n    virtual_time: 3s\n  - action: assert",
            1,
        ),
        setup=setup,
    )
    runtime = build_runtime(setup)
    runner = ScenarioRunner(runtime, output=io.StringIO())

    runner.run(scenario)

    assert runtime.bus_faults is not None
    assert runner._semantic_payloads_by_envelope == {}


def test_cli_reports_bus_faults_in_text_json_and_html(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup_path = tmp_path / "setup.toml"
    scenario_path = tmp_path / "scenario.yaml"
    html_path = tmp_path / "report.html"
    setup_path.write_text(SETUP.format(command_payload="00") + LOSE_THE_COMMAND)
    scenario_path.write_text(SCENARIO)
    base = ["run", "-c", str(setup_path), "-s", str(scenario_path)]

    assert main([*base, "--html", str(html_path)]) == 1
    text = capsys.readouterr().out
    assert "FAULT t=" in text and "action=drop" in text
    assert "bus_faults=1 bus_fault_seed=42" in text
    assert "bus faults" in html_path.read_text() and 'class="fault"' in html_path.read_text()

    assert main([*base, "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["bus_fault_seed"] == 42
    assert [(f["type"], f["direction"], f["destination_port"]) for f in payload["bus_faults"]] == [
        ("drop", "TX", 10)
    ]


def test_the_html_report_lists_each_fault_and_the_seed() -> None:
    recorder = RunRecorder()
    result, _trace = _run(LOSE_THE_COMMAND, recorder=recorder)

    html = render_report(recorder, result)  # type: ignore[arg-type]

    assert html.count('data-kind="fault"') == 1
    assert "seed <strong>42</strong>" in html


def test_a_corrupted_telemetry_payload_is_a_reported_drop_not_an_aborted_run() -> None:
    corrupt_status = """
[[bus_faults.rules]]
type = "corrupt"
port = 21
nth = 1
byte = 4
"""
    result, trace = _run(corrupt_status)

    drops = result.dropped_packets  # type: ignore[attr-defined]
    assert [(d.reason, d.destination, d.sender) for d in drops] == [("undecodable", 3, 3)]
    assert "payload.telemetry.power_status" in drops[0].detail
    assert any(" DROP " in line and "reason=undecodable" in line for line in trace.splitlines())
    assert len(result.bus_faults) == 1  # type: ignore[attr-defined]
