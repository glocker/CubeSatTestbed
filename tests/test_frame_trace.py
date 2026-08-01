"""Wire-level frame tracing: line format, transport wrapping and CLI plumbing."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import cubesat_testbed.scenario.runner as runner_module
from cubesat_testbed.config import load_scenario, load_testbed_config
from cubesat_testbed.main import main
from cubesat_testbed.protocol.csp_v2 import CspCanFrame, CspFields, pack
from cubesat_testbed.scenario import run_scenario
from cubesat_testbed.transport import (
    InMemoryBusAdapter,
    SocketCanAdapter,
    TracingTransportAdapter,
    TransportAdapter,
    TransportEnvelope,
    format_trace_line,
)
from tests.example_paths import DEFAULT_SCENARIO, DEFAULT_SETUP
from tests.test_socketcan_transport import FakeCanBus

DEFAULT_CONFIG = str(DEFAULT_SETUP)
LOW_BATTERY_SCENARIO = str(DEFAULT_SCENARIO)


def _envelope(frame: CspCanFrame, *, sequence: int = 0) -> TransportEnvelope:
    return TransportEnvelope(frame=frame, sequence=sequence)


def _telemetry_frame() -> CspCanFrame:
    """One `eps.telemetry.battery_percent` frame as the default setup emits it."""

    return pack(
        CspFields(priority=2, source=2, destination=2, destination_port=20, source_port=20),
        b"\x41\xc8\x00\x00",  # float32 25.0
    )


def _trace_lines(stream: io.StringIO) -> list[str]:
    return [line for line in stream.getvalue().splitlines() if line.startswith("trace ")]


def test_trace_line_reports_can_id_csp_header_and_both_byte_views() -> None:
    line = format_trace_line("TX", _envelope(_telemetry_frame()), now=1_000_000)

    assert line == (
        "trace t=1000000 TX can_id=0x10004103 dlc=8 pri=2 src=2 dst=2 dport=20 sport=20 "
        "flags=0x00 data=0009450041c80000 payload=41c80000"
    )


def test_trace_line_omits_timestamp_without_a_virtual_time_source() -> None:
    line = format_trace_line("RX", _envelope(_telemetry_frame()))

    assert line.startswith("trace RX can_id=")


def test_trace_line_marks_an_empty_csp_payload_rather_than_printing_nothing() -> None:
    frame = pack(
        CspFields(priority=1, source=1, destination=3, destination_port=10, source_port=10),
        b"",
    )

    assert " payload=- " in f"{format_trace_line('TX', _envelope(frame), now=0)} "


def test_trace_reports_an_undecodable_frame_instead_of_raising() -> None:
    # Too short to hold the 4-byte CSP v2 header extension: a real bus can carry
    # this, and a trace exists to show it rather than fail on it.
    line = format_trace_line("RX", _envelope(CspCanFrame(can_id=0x10004103, data=b"\x01\x02")))

    assert line.startswith("trace RX can_id=0x10004103 dlc=2 data=0102 undecodable=")
    assert "header extension" in line


def test_trace_annotation_is_appended_and_never_consulted_for_a_bad_frame() -> None:
    annotated = format_trace_line(
        "TX",
        _envelope(_telemetry_frame()),
        annotate=lambda packet: f"telemetry src={packet.fields.source}",
    )
    undecodable = format_trace_line(
        "RX",
        _envelope(CspCanFrame(can_id=0x10004103, data=b"\x01")),
        annotate=lambda _packet: pytest.fail("annotator must not see an undecodable frame"),
    )

    assert annotated.endswith(" telemetry src=2")
    assert "undecodable=" in undecodable


def test_tracing_adapter_traces_both_directions_and_passes_frames_through() -> None:
    stream = io.StringIO()
    inner = InMemoryBusAdapter()
    bus: TransportAdapter = TracingTransportAdapter(inner, stream=stream, now=lambda: 7)
    frame = _telemetry_frame()

    sent = bus.send(frame, source=2)
    received = bus.receive()

    assert received == sent
    assert inner.pending_count() == 0
    directions = [line.split()[2] for line in _trace_lines(stream)]
    assert directions == ["TX", "RX"]
    assert all(line.startswith("trace t=7 ") for line in _trace_lines(stream))


def test_tracing_adapter_does_not_trace_a_frame_the_bus_refused() -> None:
    class _RefusingAdapter(InMemoryBusAdapter):
        def send(self, frame: CspCanFrame, *, source: object = None) -> TransportEnvelope:
            raise RuntimeError("bus is down")

    stream = io.StringIO()
    bus = TracingTransportAdapter(_RefusingAdapter(), stream=stream)

    with pytest.raises(RuntimeError, match="bus is down"):
        bus.send(_telemetry_frame())

    assert _trace_lines(stream) == []


def test_tracing_adapter_shows_an_outgoing_socketcan_frame_the_bus_never_echoes() -> None:
    """The HIL case the transport boundary exists for.

    A SocketCAN adapter does not receive its own messages back, so an outgoing
    command is invisible to anything that only watches the delivery path. Here
    it is still traced as `TX` -- exactly what an engineer needs to answer "did
    my testbed actually put that command on the bus?".
    """

    stream = io.StringIO()
    bus = FakeCanBus()
    adapter = SocketCanAdapter(interface="vcan0", bus=bus)
    traced: TransportAdapter = TracingTransportAdapter(adapter, stream=stream, now=lambda: 0)

    traced.send(_telemetry_frame(), source=2)

    assert traced.receive() is None
    assert len(bus.sent) == 1
    assert [line.split()[2] for line in _trace_lines(stream)] == ["TX"]


def test_tracing_adapter_delegates_close_to_the_wrapped_adapter() -> None:
    closed: list[bool] = []

    class _ClosableAdapter(InMemoryBusAdapter):
        def close(self) -> None:
            closed.append(True)

    TracingTransportAdapter(_ClosableAdapter(), stream=io.StringIO()).close()

    assert closed == [True]


def test_scenario_trace_names_the_decoded_telemetry_signal_and_command_route() -> None:
    setup = load_testbed_config(DEFAULT_CONFIG)
    scenario = load_scenario(LOW_BATTERY_SCENARIO, setup=setup)
    trace = io.StringIO()

    result = run_scenario(scenario, setup, output=io.StringIO(), trace=trace)

    assert result.passed
    lines = _trace_lines(trace)
    # The same frame is traced on the way out and on the way back in: the
    # round trip through the bus is what an assertion actually observes.
    assert any(
        line.startswith("trace t=0 TX ")
        and line.endswith(" telemetry eps.telemetry.battery_percent=25.0")
        for line in lines
    )
    assert any(
        line.startswith("trace t=0 RX ")
        and line.endswith(" telemetry eps.telemetry.battery_percent=25.0")
        for line in lines
    )
    # The OBC rule's own command frame goes straight to the transport without
    # passing through the runner, and still lands in the trace.
    assert any(line.endswith(" command obc.payload_power_off->payload") for line in lines)
    assert any(
        line.endswith(" telemetry payload.telemetry.power_status='offline'") for line in lines
    )


def test_scenario_trace_marks_a_frame_matching_no_configured_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Traffic from an unconfigured peer is described, not swallowed.

    Injected by putting a frame on the bus as the runtime is built, which is
    the closest in-process stand-in for a stranger talking on a real CAN bus.
    """

    setup = load_testbed_config(DEFAULT_CONFIG)
    scenario = load_scenario(LOW_BATTERY_SCENARIO, setup=setup)
    trace = io.StringIO()
    stray = pack(
        CspFields(priority=3, source=9, destination=9, destination_port=63, source_port=63),
        b"\xde\xad",
    )
    build_adapter = runner_module.build_transport_adapter

    def _build_with_stray_frame(*args: object, **kwargs: object) -> TransportAdapter:
        adapter = build_adapter(*args, **kwargs)  # type: ignore[arg-type]
        adapter.send(stray)
        return adapter

    monkeypatch.setattr(runner_module, "build_transport_adapter", _build_with_stray_frame)

    with pytest.warns(RuntimeWarning, match="no configured route"):
        run_scenario(scenario, setup, output=io.StringIO(), trace=trace)

    assert any(line.endswith(" unrouted") for line in _trace_lines(trace))


def test_tracing_does_not_change_scenario_results() -> None:
    setup = load_testbed_config(DEFAULT_CONFIG)
    scenario = load_scenario(LOW_BATTERY_SCENARIO, setup=setup)

    untraced = run_scenario(scenario, setup, output=io.StringIO())
    traced = run_scenario(scenario, setup, output=io.StringIO(), trace=io.StringIO())

    assert traced == untraced


def test_cli_trace_goes_to_stderr_and_leaves_the_report_on_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["run", "-c", DEFAULT_CONFIG, "-s", LOW_BATTERY_SCENARIO, "--trace"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "SUMMARY scenario=" in captured.out
    assert "trace " not in captured.out
    assert "trace t=0 TX can_id=" in captured.err


def test_cli_trace_composes_with_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["run", "-c", DEFAULT_CONFIG, "-s", LOW_BATTERY_SCENARIO, "--trace", "--json"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["passed"] is True
    assert "command obc.payload_power_off->payload" in captured.err


def test_cli_trace_composes_with_quiet_output(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        ["run", "-c", DEFAULT_CONFIG, "-s", LOW_BATTERY_SCENARIO, "--trace", "--quiet"]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "trace t=0 TX can_id=" in captured.err


def test_cli_trace_explains_a_failing_assertion_from_the_bus_alone(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The point of the flag: a FAIL is diagnosable without dropping into Python.

    Without the scenario's fault injection the battery only discharges normally
    and never crosses the rule's 30% threshold, so the OBC never sheds the
    payload. The trace says exactly that: every battery frame stays above the
    threshold, the payload reports `online`, and no command frame is ever sent.
    """

    scenario_path = tmp_path / "never_sheds.yaml"
    scenario_path.write_text(
        """
name: "payload never shed"
steps:
  - action: "wait"
    virtual_time: "3s"
  - action: "assert"
    signal: "payload.telemetry.power_status"
    op: "=="
    value: "offline"
    timeout: "1s"
""".lstrip(),
        encoding="utf-8",
    )

    exit_code = main(["run", "-c", DEFAULT_CONFIG, "-s", str(scenario_path), "--trace"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "FAIL" in captured.out

    prefix = "telemetry eps.telemetry.battery_percent="
    battery_values = [
        float(line.split(prefix, 1)[1]) for line in captured.err.splitlines() if prefix in line
    ]
    assert battery_values
    assert all(value > 30.0 for value in battery_values)
    assert "telemetry payload.telemetry.power_status='online'" in captured.err
    assert "command obc.payload_power_off" not in captured.err
