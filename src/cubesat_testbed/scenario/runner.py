"""Scenario runner over deterministic virtual time.

A scenario is an ordered YAML script containing actions such as fault injection,
virtual waits, command sends, and assertions. The runner schedules these actions
on the DES engine and collects PASS/FAIL results.
"""

from __future__ import annotations

import sys
import warnings
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TextIO, runtime_checkable
from xml.etree.ElementTree import Element, SubElement, tostring

from cubesat_testbed.clock import Clock, VirtualClock
from cubesat_testbed.config import (
    CSP_EPHEMERAL_PORT_MIN,
    CSP_SERVICE_PORTS,
    SERVICE_REPLY_FIELDS,
    AssertionOperator,
    AssertStep,
    FaultType,
    InjectBusFaultStep,
    InjectFaultStep,
    InMemoryTransportConfig,
    NodeMode,
    RequestStep,
    ScenarioScript,
    SendCommandStep,
    TestbedConfig,
    WaitStep,
    load_obc_rules_file,
    load_scenario,
    load_testbed_config,
    rule_signal_problem,
    split_signal_reference,
)
from cubesat_testbed.dut.manager import NodeParticipant, resolve_participants
from cubesat_testbed.engine import (
    CommandPayload,
    DiscreteEventEngine,
    EventKind,
    SimulationEvent,
    TelemetryPayload,
    VirtualTime,
)
from cubesat_testbed.fault_injection import FaultInjectionEngine
from cubesat_testbed.modules import (
    ModuleBuildContext,
    ModuleRegistration,
    ModuleRegistryError,
    ObcPeerRule,
    SimulatedModule,
    module_registration,
    obc_peer_rule_from_config,
)
from cubesat_testbed.modules.base import ModuleRequest
from cubesat_testbed.protocol.csp_services import (
    CSP_SERVICE_FLAG_CRC32,
    ServiceRequestError,
    append_crc32,
    decode_uint32_reply,
    service_reply_data,
    strip_crc32,
)
from cubesat_testbed.protocol.csp_v2 import (
    CSP_V2_DEFAULT_MAX_PACKET_BYTES,
    CspFields,
)
from cubesat_testbed.protocol.telemetry_codec import (
    TelemetryCodecError,
    TelemetryPacketCodec,
)
from cubesat_testbed.report.recorder import RunRecorder
from cubesat_testbed.scenario.assertions import (
    AssertionResult,
    TelemetryAssertion,
    compare_values,
    format_assertion_result,
)
from cubesat_testbed.transport import (
    EndpointId,
    FrameAnnotator,
    InMemoryBusAdapter,
    PacketSender,
    TracingTransportAdapter,
    TransportAdapter,
    TransportEnvelope,
    build_transport_adapter,
)
from cubesat_testbed.transport.can_protocol import (
    PacketEvent,
    PacketFields,
    PacketReassembler,
    PacketView,
)
from cubesat_testbed.transport.faults import (
    BusFaultEvent,
    FaultInjectingTransport,
    format_bus_fault_trace_line,
)
from cubesat_testbed.transport.tracing import TRACE_PREFIX
from cubesat_testbed.transport.whole_packet_protocol import WHOLE_PACKET_FRAMES, WholePacketFrame

_PHYSICAL_STEP_US: VirtualTime = 1_000_000
"""Fixed cadence, in microseconds, of the runner's tick/telemetry/cycle step.

Product v1 ticks every simulated module, advances the fault-injection cycle
counter, and emits configured telemetry once per virtual second. This matches
the cadence product v1 has always used; it is not yet configurable per node.
"""


class ScenarioRunnerError(RuntimeError):
    """Base class for scenario execution failures."""


class ScenarioRuntimeError(ScenarioRunnerError, ValueError):
    """Raised when a scenario cannot be executed against the runtime."""


@runtime_checkable
class _CommandHandlingModule(Protocol):
    def handle_command(
        self,
        command: CommandPayload | str,
        *,
        payload: object = None,
        source: EndpointId | None = None,
        target: EndpointId | None = None,
    ) -> bool: ...


@runtime_checkable
class _TelemetryReportingModule(Protocol):
    """A module whose current values the runner can encode into frames.

    Deliberately only ``telemetry``: that is all the runner calls, and a
    protocol demanding more would silently exclude an otherwise correct
    module from publishing anything. ``emit_telemetry``, which schedules
    samples straight onto the engine, is for Python-API callers.
    """

    def telemetry(self, *, now: VirtualTime | None = None) -> dict[str, object]: ...


@runtime_checkable
class _TickableModule(Protocol):
    def tick(self, ticks: int = 1) -> object: ...


@dataclass(frozen=True, slots=True)
class _CommandRoute:
    command: str
    source_node: str
    target_node: str
    source_address: int
    destination_address: int
    destination_port: int
    source_port: int
    priority: int
    flags: int
    payload_hex: bytes


@dataclass(frozen=True, slots=True)
class _TelemetryRoute:
    """CSP routing plus wire codec for one configured telemetry packet.

    A packet carries one or more signals: a ``[nodes.<node>.telemetry.<name>]`` mapping
    is a packet with one, a ``[nodes.<node>.packets.<name>]`` one with several. The
    sender encodes them all into one payload and the receiver decodes them all from it.

    A frame the testbed sends for a simulated node is addressed to the node the packet
    names with ``to`` (an OBC, whose own CAN filter then accepts it) and, when it names
    none, to its own sender: the in-memory bus's monitor queue sees every frame whatever
    its destination, like a promiscuous bus analyzer, but a real node on a real bus does
    not. A frame the testbed *receives* is matched on the sender's address and the ports,
    and on the destination only when the packet named one: a real EPS sends its
    housekeeping to the OBC, never to itself, and that must still be the EPS's packet.
    """

    label: str
    node_name: str
    signals: tuple[str, ...]
    field_names: tuple[str, ...]
    """The keys of each signal in the module's ``telemetry()`` dict, in ``signals`` order."""
    codec: TelemetryPacketCodec
    source_address: int
    destination_address: int
    destination_named: bool
    destination_port: int
    source_port: int
    priority: int
    flags: int

    @property
    def name(self) -> str:
        """How the trace and drop reports name this packet: its signal, if it has one."""

        return self.signals[0] if len(self.signals) == 1 else self.label

    def matches(self, fields: PacketFields) -> bool:
        """Whether a received packet is this one."""

        return (
            self.source_address == fields.source
            and self.destination_port == fields.destination_port
            and self.source_port == fields.source_port
            and (not self.destination_named or self.destination_address == fields.destination)
        )

    def encode(self, values: Mapping[str, object]) -> bytes:
        """Encode the module's telemetry values (keyed by field name) into one payload."""

        return self.codec.encode(values)

    def decode(self, payload: bytes) -> dict[str, object]:
        """Decode a received payload into each signal's value, keyed by signal path."""

        decoded = self.codec.decode(payload)
        return {
            signal: decoded[field_name]
            for signal, field_name in zip(self.signals, self.field_names, strict=True)
        }


@runtime_checkable
class _RequestAnsweringModule(Protocol):
    def answer_request(
        self, request: ModuleRequest, *, now: VirtualTime
    ) -> Mapping[str, object] | None: ...


_SERVICE_NAMES_BY_PORT = {port: name for name, port in CSP_SERVICE_PORTS.items()}

_EPHEMERAL_PORT_MAX = 63
"""The highest CSP port (6 bits, in v1 and v2): the top of libcsp's outgoing-port range."""

_UPTIME_DIVISOR_US = 1_000_000


@dataclass(frozen=True, slots=True)
class _ServiceEndpoint:
    """The standard services one simulated node answers (``[nodes.<node>.services]``)."""

    node_name: str
    address: int
    services_by_port: dict[int, str]
    values: dict[str, int]
    """``memfree``/``buf_free``: the number the reply reports."""


@dataclass(frozen=True, slots=True)
class _RequestRoute:
    """One configured request of a node, and the layout of its reply."""

    node_name: str
    request: str
    address: int
    port: int
    payload_hex: bytes
    reply_label: str
    field_names: tuple[str, ...]
    codec: TelemetryPacketCodec
    simulated: bool

    @property
    def name(self) -> str:
        return f"{self.node_name}.requests.{self.request}"


@dataclass(slots=True)
class _PendingReply:
    """A scenario request waiting for its reply: the first matching packet is kept."""

    sent_at: VirtualTime
    payload: bytes | None = None
    flags: int = 0
    received_at: VirtualTime | None = None
    envelope: TransportEnvelope | None = None


@dataclass(slots=True)
class ScenarioRuntime:
    """Runtime objects required to execute a scenario.

    ``transport`` is any :class:`TransportAdapter` -- the in-memory bus for
    CI/local simulation, or :class:`~cubesat_testbed.transport.SocketCanAdapter`
    for a HIL run against ``software``/``hardware`` DUT nodes; the runner does
    not care which. ``participants`` is the DUT manager's mapping of every
    configured node to its role; ``modules`` only ever holds entries for
    ``participant.is_simulated`` nodes. ``clock`` paces ``wait()`` against
    wall-clock time when set to a :class:`~cubesat_testbed.clock.RealTimeClock`
    (the default :class:`~cubesat_testbed.clock.VirtualClock` never waits).
    """

    setup: TestbedConfig
    engine: DiscreteEventEngine
    fault_engine: FaultInjectionEngine
    transport: TransportAdapter
    participants: dict[str, NodeParticipant]
    modules: dict[str, SimulatedModule]
    command_routes: tuple[_CommandRoute, ...]
    telemetry_routes: tuple[_TelemetryRoute, ...]
    telemetry_routes_by_node: dict[str, tuple[_TelemetryRoute, ...]]
    telemetry_order: tuple[str, ...]
    tick_order: tuple[str, ...]
    service_endpoints: dict[int, _ServiceEndpoint] = field(default_factory=dict)
    """Simulated nodes answering libcsp services, by CSP address."""
    request_routes: tuple[_RequestRoute, ...] = ()
    clock: Clock = field(default_factory=VirtualClock)
    trace: TextIO | None = None
    """Stream ``DROP`` lines are written to when ``--trace`` is on."""
    recorder: RunRecorder | None = None
    """Observer for the HTML report; never read back by the run."""
    bus_faults: FaultInjectingTransport | None = None
    """The bus fault injector, present on the in-memory bus (and only there)."""

    def module(self, name: str) -> SimulatedModule:
        """Return a configured simulated module by node name."""

        try:
            return self.modules[name]
        except KeyError as exc:
            raise ScenarioRuntimeError(f"simulated module {name!r} is not available") from exc


@dataclass(frozen=True, slots=True)
class DroppedPacket:
    """A CSP packet a receiving node discarded instead of delivering.

    ``reason`` is a :class:`~cubesat_testbed.protocol.csp_v2.ReassemblyDropReason`
    value (``overflow``, ``frame_lost``, ``timeout``, ...). ``destination`` is the
    CSP address of the node that dropped it. It is part of the run result, so a
    packet lost on the bus never goes unreported.
    """

    time: VirtualTime
    destination: int | None
    sender: int | None
    reason: str
    detail: str
    limit: int | None = None


@dataclass(frozen=True, slots=True)
class ScenarioRunResult:
    """Summary of one deterministic scenario run."""

    scenario_name: str
    started_at: VirtualTime
    finished_at: VirtualTime
    assertions: tuple[AssertionResult, ...]
    dropped_packets: tuple[DroppedPacket, ...] = ()
    bus_faults: tuple[BusFaultEvent, ...] = ()
    """Every fault the bus injected, in order; the bus never fails silently."""
    bus_fault_seed: int | None = None
    """The seed the injected faults derive from; ``None`` when no bus fault rule existed."""

    @property
    def passed(self) -> bool:
        """Return whether every scenario assertion passed."""

        return all(result.passed for result in self.assertions)


def format_junit_xml(result: ScenarioRunResult, *, wall_time_seconds: float = 0.0) -> str:
    """Format one scenario result as a JUnit XML report, one testcase per assertion.

    ``time`` attributes report real wall-clock seconds the run took to
    execute, not virtual time: the scenario's own virtual-time timestamps are
    already in each testcase's failure detail, and JUnit consumers (CI
    dashboards, flake trackers) expect ``time`` to mean elapsed real time.
    """

    failed = sum(1 for assertion in result.assertions if not assertion.passed)
    testsuites = Element("testsuites")
    testsuite = SubElement(
        testsuites,
        "testsuite",
        {
            "name": result.scenario_name,
            "tests": str(len(result.assertions)),
            "failures": str(failed),
            "errors": "0",
            "skipped": "0",
            "time": f"{wall_time_seconds:.6f}",
        },
    )
    for assertion in result.assertions:
        testcase = SubElement(
            testsuite,
            "testcase",
            {"name": assertion.name, "classname": result.scenario_name, "time": "0"},
        )
        if not assertion.passed:
            failure = SubElement(testcase, "failure", {"message": assertion.detail})
            failure.text = assertion.detail
    return _serialize_junit_xml(testsuites)


def format_junit_error_xml(
    message: str,
    *,
    kind: str,
    wall_time_seconds: float = 0.0,
) -> str:
    """Format an execution error (no scenario result) as a JUnit XML report.

    CI tooling that expects a JUnit file at a fixed path should still get one
    when the scenario never ran at all (bad config, missing file, and so on),
    rather than a missing-file failure on top of the real error.
    """

    testsuites = Element("testsuites")
    testsuite = SubElement(
        testsuites,
        "testsuite",
        {
            "name": kind,
            "tests": "1",
            "failures": "0",
            "errors": "1",
            "skipped": "0",
            "time": f"{wall_time_seconds:.6f}",
        },
    )
    testcase = SubElement(testsuite, "testcase", {"name": kind, "classname": kind, "time": "0"})
    error = SubElement(testcase, "error", {"message": message})
    error.text = message
    return _serialize_junit_xml(testsuites)


def _serialize_junit_xml(testsuites: Element) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(testsuites, encoding="unicode")


class ScenarioRunner:
    """Execute parsed scenario scripts over an in-memory deterministic runtime."""

    def __init__(
        self,
        runtime: ScenarioRuntime,
        *,
        output: TextIO | None = None,
        max_settle_events: int = 10_000,
    ) -> None:
        self.runtime = runtime
        self._output = sys.stdout if output is None else output
        self._max_settle_events = _validate_positive_int("max_settle_events", max_settle_events)
        self._latest_telemetry: dict[str, object] = {}
        self._assertion_results: list[AssertionResult] = []
        self._semantic_payloads_by_envelope: dict[int, object] = {}
        self._limits_by_address = {
            node.address: node.max_packet_bytes for node in runtime.setup.nodes.values()
        }
        self._reassemblers: dict[int, PacketReassembler] = {}
        self._senders: dict[int, PacketSender] = {}
        self._dropped_packets: list[DroppedPacket] = []
        self._signal_evidence: dict[str, int | None] = {}
        self._pending_replies: dict[tuple[int, int, int, int], _PendingReply] = {}
        self._next_ephemeral_port: dict[int, int] = {}
        self.runtime.engine.add_handler(EventKind.TELEMETRY, self._record_telemetry)
        if runtime.bus_faults is not None:
            # A frame the bus held back is delivered in the instant it is released.
            runtime.bus_faults.set_release_hook(self._run_current_events)
        self._arm_physical_step()

    @property
    def latest_telemetry(self) -> Mapping[str, object]:
        """Latest decoded telemetry values observed by this runner."""

        return dict(self._latest_telemetry)

    def run(self, scenario: ScenarioScript) -> ScenarioRunResult:
        """Execute ``scenario`` deterministically and return assertion results."""

        started_at = self.runtime.engine.now
        self._assertion_results = []
        self._dropped_packets = []
        bus_faults = self.runtime.bus_faults
        first_bus_fault = 0 if bus_faults is None else len(bus_faults.events)
        recorder = self.runtime.recorder
        if recorder is not None:
            recorder.begin(self.runtime.setup, scenario)

        for index, step in enumerate(scenario.steps, start=1):
            if recorder is not None:
                recorder.step(self.runtime.engine.now, index, step)
            if isinstance(step, WaitStep):
                self.wait(step.virtual_time)
            elif isinstance(step, InjectFaultStep):
                self.inject_fault(step)
            elif isinstance(step, InjectBusFaultStep):
                self.inject_bus_fault(step)
            elif isinstance(step, SendCommandStep):
                self.send_command(step)
            elif isinstance(step, AssertStep):
                self.assert_step(step, index=index)
            elif isinstance(step, RequestStep):
                self.request_step(step, index=index)
            else:
                raise ScenarioRuntimeError(f"unsupported scenario step at index {index}: {step!r}")

        self._run_current_events()
        self._collect_transport_drops()
        self._expire_partial_packets()
        return ScenarioRunResult(
            scenario_name=scenario.name,
            started_at=started_at,
            finished_at=self.runtime.engine.now,
            assertions=tuple(self._assertion_results),
            dropped_packets=tuple(self._dropped_packets),
            bus_faults=() if bus_faults is None else bus_faults.events[first_bus_fault:],
            bus_fault_seed=(
                bus_faults.seed if bus_faults is not None and bus_faults.has_rules else None
            ),
        )

    def wait(self, virtual_time: VirtualTime) -> None:
        """Advance virtual time and drive configured in-memory modules.

        The DES engine's own recurring physical-step timer (armed once per
        runner instance, see ``_arm_physical_step``) ticks modules, advances the
        fault-injection cycle counter, and emits telemetry as this call jumps
        the engine forward; this method itself does not loop tick-by-tick.

        With the default :class:`~cubesat_testbed.clock.VirtualClock`, the
        engine jumps straight to ``target`` (pure simulation). With a
        :class:`~cubesat_testbed.clock.RealTimeClock`, this instead paces the
        jump against wall-clock time, giving a real ``software``/``hardware``
        peer a realistic window to respond and delivering its frames as soon
        as they arrive rather than only once ``target`` is reached.
        """

        delay = _validate_virtual_time("wait virtual_time", virtual_time)
        if delay == 0:
            self._run_current_events()
            self._emit_configured_telemetry()
            return

        target = self.runtime.engine.now + delay
        if isinstance(self.runtime.clock, VirtualClock):
            self.runtime.engine.run_until(target, max_events=self._max_settle_events)
            if self.runtime.engine.now < target:
                raise ScenarioRuntimeError(
                    f"wait exceeded max_settle_events={self._max_settle_events} before reaching "
                    f"virtual time {target}; pass a larger max_settle_events to ScenarioRunner "
                    "for long waits"
                )
        else:
            self._wait_paced(target)
        self._run_current_events()

    def _wait_paced(self, target: VirtualTime) -> None:
        """Advance to ``target``, blocking on the transport for early frames.

        Each iteration either delivers one arrived frame (virtual time does
        not advance -- it has not yet been decided that no earlier response
        was coming) or, once the transport's blocking receive times out,
        jumps to the next due virtual instant (safe, since wall-clock time
        has by then actually caught up to it) and repeats.
        """

        for _round in range(self._max_settle_events):
            if self.runtime.engine.now >= target:
                return
            next_event_time = self.runtime.engine.next_event_time
            next_stop = target if next_event_time is None else min(next_event_time, target)
            remaining = self.runtime.clock.seconds_until(next_stop)
            envelope = self.runtime.transport.receive(timeout=remaining)
            self._collect_transport_drops()
            if envelope is not None:
                self._deliver_transport_envelope(envelope)
                continue
            self.runtime.engine.run_until(next_stop, max_events=self._max_settle_events)
        raise ScenarioRuntimeError(
            f"real-time wait did not reach virtual time {target} within "
            f"max_settle_events={self._max_settle_events} rounds; check for a peer "
            "flooding the bus faster than the wait can advance"
        )

    def inject_fault(self, step: InjectFaultStep) -> None:
        """Apply one scenario fault request through the DES fault path."""

        fault_type = _fault_type_value(step.type)
        self.runtime.engine.schedule_fault(
            fault_type,
            step.target,
            value=step.value,
            duration=step.duration,
            cycles=step.cycles,
        )
        self._run_current_events()
        self._emit_configured_telemetry()

    def inject_bus_fault(self, step: InjectBusFaultStep) -> None:
        """Put one bus fault rule in force on the in-memory bus, from this instant on."""

        if self.runtime.bus_faults is None:
            raise ScenarioRuntimeError(
                "inject_bus_fault needs the in-memory bus; this runtime has no bus fault injector"
            )
        self.runtime.bus_faults.add_rule(step)

    def send_command(self, step: SendCommandStep) -> None:
        """Send one configured named command through the in-memory CSP path."""

        route = self._resolve_command_route(step)

        def emit_command(event: SimulationEvent, _engine: DiscreteEventEngine) -> None:
            payload = event.payload
            if not isinstance(payload, CommandPayload):
                raise ScenarioRuntimeError("scenario command events must carry a CommandPayload")
            self._send_route(route, payload=payload.payload)

        self.runtime.engine.schedule_command(
            route.command,
            payload=step.payload,
            source=route.source_node,
            target=route.target_node,
            handler=emit_command,
        )
        self._run_current_events()
        self._emit_configured_telemetry()

    def assert_step(self, step: AssertStep, *, index: int) -> AssertionResult:
        """Evaluate a scenario assertion, optionally waiting until its timeout.

        A signal that has not been observed on the bus yet is not an error: it
        is treated like any other not-yet-matching value and re-checked as the
        engine advances, exactly like a real bus listener that simply hasn't
        seen a frame yet. Only if the signal is still unobserved once the
        deadline is reached does the assertion fail, with a detail message
        that says so explicitly rather than comparing against a made-up value.
        """

        assertion = TelemetryAssertion(
            _assertion_name(step, index=index),
            step.signal,
            step.op,
            step.value,
            timeout=step.timeout,
        )
        deadline = self.runtime.engine.now + (assertion.timeout or 0)

        while True:
            observed, actual = self._try_read_signal(assertion.signal)
            if observed:
                result = assertion.evaluate(actual, now=self.runtime.engine.now)
                if result.passed or self.runtime.engine.now >= deadline:
                    break
            elif self.runtime.engine.now >= deadline:
                result = AssertionResult(
                    name=assertion.name,
                    signal=assertion.signal,
                    operator=AssertionOperator(assertion.operator),
                    expected=assertion.expected,
                    actual=None,
                    passed=False,
                    evaluated_at=self.runtime.engine.now,
                    detail=f"{assertion.signal} was never observed on the bus",
                )
                break
            # Jump straight to whichever comes first: the next scheduled event
            # (e.g. the next physical-step tick) or the assertion's own
            # deadline, instead of re-evaluating one microsecond at a time.
            next_event_time = self.runtime.engine.next_event_time
            next_stop = deadline if next_event_time is None else min(next_event_time, deadline)
            self.wait(next_stop - self.runtime.engine.now)

        self._assertion_results.append(result)
        if self.runtime.recorder is not None:
            self.runtime.recorder.assertion(result, self._signal_evidence.get(assertion.signal))
        print(format_assertion_result(result), file=self._output)
        self.runtime.engine.schedule_assertion(
            result.name, passed=result.passed, detail=result.detail
        )
        self._run_current_events()
        return result

    def request_step(self, step: RequestStep, *, index: int) -> tuple[AssertionResult, ...]:
        """Send a request from a simulated node and judge the reply read off the bus.

        The request leaves from an ephemeral source port, as libcsp's ``csp_connect`` gives
        one, and the reply is the first packet back from the target to the requester with
        the ports swapped. The step's first result says whether a reply came in time (and,
        for ``ping``, whether it echoed the request); each ``expect`` entry is one more
        result on a decoded reply field. Nothing reads the target module's state.
        """

        setup = self.runtime.setup
        source = setup.nodes[step.source]
        target = setup.nodes[step.target]
        if step.service is not None:
            port = CSP_SERVICE_PORTS[step.service]
            default_payload = bytes(range(4)) if step.service == "ping" else b""
            what = f"{step.target}.services.{step.service}"
            route = None
        else:
            assert step.request is not None  # the model requires one of the two
            request = target.requests[step.request]
            port = request.port
            default_payload = request.payload_hex
            what = f"{step.target}.requests.{step.request}"
            route = next(
                candidate
                for candidate in self.runtime.request_routes
                if candidate.node_name == step.target and candidate.request == step.request
            )
        payload = default_payload if step.payload_hex is None else step.payload_hex
        ephemeral = self._allocate_ephemeral_port(source.address, target.address)
        key = (target.address, source.address, port, ephemeral)
        pending = _PendingReply(sent_at=self.runtime.engine.now)
        self._pending_replies[key] = pending
        try:
            self._send_packet(
                CspFields(
                    priority=2,
                    source=source.address,
                    destination=target.address,
                    destination_port=port,
                    source_port=ephemeral,
                    flags=0,
                ),
                payload,
            )
            self._run_current_events()
            deadline = pending.sent_at + step.timeout
            while pending.payload is None and self.runtime.engine.now < deadline:
                next_event_time = self.runtime.engine.next_event_time
                next_stop = deadline if next_event_time is None else min(next_event_time, deadline)
                self.wait(next_stop - self.runtime.engine.now)
        finally:
            del self._pending_replies[key]

        results = self._judge_reply(step, index, what, payload, pending, route)
        evidence = (
            None
            if pending.envelope is None or self.runtime.recorder is None
            else self.runtime.recorder.rx_frame_id(pending.envelope)
        )
        for result in results:
            self._assertion_results.append(result)
            if self.runtime.recorder is not None:
                self.runtime.recorder.assertion(result, evidence)
            print(format_assertion_result(result), file=self._output)
            self.runtime.engine.schedule_assertion(
                result.name, passed=result.passed, detail=result.detail
            )
        self._run_current_events()
        return results

    def _judge_reply(
        self,
        step: RequestStep,
        index: int,
        what: str,
        payload: bytes,
        pending: _PendingReply,
        route: _RequestRoute | None,
    ) -> tuple[AssertionResult, ...]:
        name = step.name or f"request_{index}"
        now = self.runtime.engine.now

        def result(
            result_name: str,
            signal: str,
            expected: object,
            actual: object,
            passed: bool,
            detail: str,
        ) -> AssertionResult:
            return AssertionResult(
                name=result_name,
                signal=signal,
                operator=AssertionOperator.EQ,
                expected=expected,
                actual=actual,
                passed=passed,
                evaluated_at=now,
                detail=detail,
            )

        expected_reply = payload.hex() if step.service == "ping" else "reply"
        fields: dict[str, object] | None = None
        if pending.payload is None:
            main = result(
                name,
                what,
                expected_reply,
                None,
                False,
                f"{what}: no reply from {step.target} within {_seconds(step.timeout)}",
            )
        else:
            assert pending.received_at is not None
            after = _seconds(pending.received_at - pending.sent_at)
            try:
                data = strip_crc32(pending.payload, pending.flags)
                if step.service == "ping":
                    passed = data == payload
                    detail = (
                        f"{what}: {step.target} echoed {data.hex() or '(empty)'} after {after}"
                        if passed
                        else f"{what}: {step.target} answered {data.hex() or '(empty)'} after "
                        f"{after}, expected the echo {payload.hex() or '(empty)'}"
                    )
                    main = result(name, what, expected_reply, data.hex(), passed, detail)
                elif step.service is not None:
                    value = decode_uint32_reply(data)
                    fields = {SERVICE_REPLY_FIELDS[step.service]: value}
                    main = result(
                        name,
                        what,
                        "reply",
                        value,
                        True,
                        f"{what}: {step.target} answered {SERVICE_REPLY_FIELDS[step.service]}="
                        f"{value} after {after}",
                    )
                else:
                    assert route is not None
                    decoded = route.codec.decode(data)
                    fields = dict(decoded)
                    shown = " ".join(f"{key}={value!r}" for key, value in decoded.items())
                    main = result(
                        name,
                        what,
                        "reply",
                        data.hex(),
                        True,
                        f"{what}: {step.target} answered after {after}: {shown}",
                    )
            except (ServiceRequestError, TelemetryCodecError) as exc:
                main = result(
                    name,
                    what,
                    expected_reply,
                    pending.payload.hex(),
                    False,
                    f"{what}: {step.target} answered {pending.payload.hex()}, which does not "
                    f"decode: {exc}",
                )

        results = [main]
        for expectation in step.expect:
            signal = f"{what}.{expectation.field}"
            expectation_name = f"{name}.{expectation.field}"
            if fields is None:
                results.append(
                    result(
                        expectation_name,
                        signal,
                        expectation.value,
                        None,
                        False,
                        f"{signal} {expectation.op.value} {expectation.value!r}; no decoded reply",
                    )
                )
                continue
            field_name, element = split_signal_reference(f"reply.{expectation.field}")
            actual = fields[field_name.removeprefix("reply.")]
            if element is not None:
                assert isinstance(actual, tuple)
                actual = actual[element]
            passed, detail = compare_values(
                actual=actual, operator=expectation.op, expected=expectation.value, signal=signal
            )
            results.append(
                AssertionResult(
                    name=expectation_name,
                    signal=signal,
                    operator=AssertionOperator(expectation.op),
                    expected=expectation.value,
                    actual=actual,
                    passed=passed,
                    evaluated_at=now,
                    detail=detail,
                )
            )
        return tuple(results)

    def _allocate_ephemeral_port(self, source: int, target: int) -> int:
        """The next free source port of ``source``, cycling 17-63 like libcsp's connections."""

        span = _EPHEMERAL_PORT_MAX - CSP_EPHEMERAL_PORT_MIN + 1
        start = self._next_ephemeral_port.get(source, CSP_EPHEMERAL_PORT_MIN)
        for offset in range(span):
            port = CSP_EPHEMERAL_PORT_MIN + (start - CSP_EPHEMERAL_PORT_MIN + offset) % span
            if not any(key[1] == source and key[3] == port for key in self._pending_replies):
                self._next_ephemeral_port[source] = (
                    CSP_EPHEMERAL_PORT_MIN + (port - CSP_EPHEMERAL_PORT_MIN + 1) % span
                )
                return port
        raise ScenarioRuntimeError(f"node at address {source} has no free source port left")

    def _take_awaited_reply(self, packet: PacketView, envelope: TransportEnvelope) -> bool:
        fields = packet.fields
        pending = self._pending_replies.get(
            (fields.source, fields.destination, fields.source_port, fields.destination_port)
        )
        if pending is None or pending.payload is not None:
            return False
        pending.payload = packet.payload
        pending.flags = fields.flags
        pending.received_at = self.runtime.engine.now
        pending.envelope = envelope
        return True

    def _answer_request(self, packet: PacketView) -> bool:
        """Answer a service or configured request addressed to a simulated node.

        Returns whether the packet was such a request (answered, declined by the module or
        dropped as unanswerable), so the caller does not route it further.
        """

        fields = packet.fields
        endpoint = self.runtime.service_endpoints.get(fields.destination)
        if endpoint is not None and fields.destination_port in endpoint.services_by_port:
            service = endpoint.services_by_port[fields.destination_port]
            try:
                self._check_reply_flags(fields.flags)
                data = service_reply_data(
                    service,
                    strip_crc32(packet.payload, fields.flags),
                    uptime_s=self.runtime.engine.now // _UPTIME_DIVISOR_US,
                    values=endpoint.values,
                )
            except ServiceRequestError as exc:
                self._drop_request(packet, f"{endpoint.node_name}.services.{service}: {exc}")
                return True
            self._send_reply(fields, data)
            return True

        route = self._request_route_for(fields.destination, fields.destination_port, packet.payload)
        if route is None or not route.simulated:
            return False
        module = self.runtime.module(route.node_name)
        now = self.runtime.engine.now
        try:
            self._check_reply_flags(fields.flags)
        except ServiceRequestError as exc:
            self._drop_request(packet, f"{route.name}: {exc}")
            return True
        source_names = {node.address: name for name, node in self.runtime.setup.nodes.items()}
        values: Mapping[str, object] | None
        if isinstance(module, _RequestAnsweringModule):
            values = module.answer_request(
                ModuleRequest(
                    request=route.request,
                    payload=packet.payload,
                    source=source_names.get(fields.source),
                    source_address=fields.source,
                ),
                now=now,
            )
            if values is None:
                return True  # the module chose not to answer; the requester times out
        elif isinstance(module, _TelemetryReportingModule):
            values = module.telemetry(now=now)
        else:
            raise ScenarioRuntimeError(
                f"module {route.node_name!r} has neither answer_request nor telemetry, so it "
                f"cannot answer {route.name}"
            )
        missing = [name for name in route.field_names if name not in values]
        if missing:
            raise ScenarioRuntimeError(
                f"module {route.node_name!r} gave no value for "
                f"{', '.join(repr(name) for name in missing)} in its reply to {route.name} "
                f"({route.reply_label})"
            )
        data = route.codec.encode({name: values[name] for name in route.field_names})
        self._send_reply(fields, data)
        return True

    def _request_route_for(self, address: int, port: int, payload: bytes) -> _RequestRoute | None:
        candidates = [
            route
            for route in self.runtime.request_routes
            if route.address == address and route.port == port
        ]
        exact = [route for route in candidates if route.payload_hex == payload]
        if len(exact) == 1:
            return exact[0]
        if len(candidates) == 1:
            return candidates[0]
        return None

    @staticmethod
    def _check_reply_flags(flags: int) -> None:
        unsupported = flags & ~CSP_SERVICE_FLAG_CRC32
        if unsupported:
            raise ServiceRequestError(
                f"request flags 0x{flags:02x} ask for HMAC/XTEA/RDP, which a simulated node "
                "does not implement; only CRC32 is answered"
            )

    def _send_reply(self, request: PacketFields, data: bytes) -> None:
        """Send ``data`` back to the requester, as ``csp_sendto_reply(..., CSP_O_SAME)``."""

        self._send_packet(
            CspFields(
                priority=request.priority,
                source=request.destination,
                destination=request.source,
                destination_port=request.source_port,
                source_port=request.destination_port,
                flags=request.flags,
            ),
            append_crc32(data, request.flags),
        )

    def _drop_request(self, packet: PacketView, detail: str) -> None:
        self._append_drop(
            DroppedPacket(
                time=self.runtime.engine.now,
                destination=packet.fields.destination,
                sender=packet.fields.source,
                reason="unanswerable",
                detail=detail,
            )
        )

    def _is_request_traffic(self, fields: PacketFields) -> bool:
        """A request to, or a reply from, a node the testbed is not answering for.

        Such packets are observed (``--trace`` names them), not warned about: a real OBC
        pinging a real board, or the in-memory bus handing the testbed its own reply back.
        Anything else that matches no route is still warned about (see
        :func:`_classify_request`).
        """

        return _classify_request(fields, self.runtime.request_routes) is not None

    def _record_telemetry(self, event: SimulationEvent, _engine: DiscreteEventEngine) -> None:
        if not isinstance(event.payload, TelemetryPayload):
            raise ScenarioRuntimeError("telemetry events must carry a TelemetryPayload")
        self._latest_telemetry[event.payload.signal] = event.payload.value
        if self.runtime.recorder is not None:
            self.runtime.recorder.signal(
                self.runtime.engine.now, event.payload.signal, event.payload.value
            )

    def _arm_physical_step(self) -> None:
        """Schedule the recurring engine timer that drives ticks/cycles/telemetry.

        This re-arms itself on every fire, so a single ``wait()``/``run_until``
        call spanning many virtual seconds dispatches it as an ordinary DES
        event at each due instant instead of the runner looping in Python.
        """

        def _on_step(_event: SimulationEvent, engine: DiscreteEventEngine) -> None:
            self._run_current_events()
            self.runtime.fault_engine.advance_cycles(1, now=engine.now)
            self._tick_modules(1)
            self._emit_configured_telemetry()
            engine.schedule_timer(_PHYSICAL_STEP_US, handler=_on_step)

        self.runtime.engine.schedule_timer(_PHYSICAL_STEP_US, handler=_on_step)

    def _tick_modules(self, ticks: int) -> None:
        for node_name in self.runtime.tick_order:
            module = self.runtime.module(node_name)
            if isinstance(module, _TickableModule):
                module.tick(ticks)

    def _emit_configured_telemetry(self) -> None:
        """Encode and send each configured node's current telemetry as a real
        CSP frame, then let it round-trip back through the same decode path a
        bus listener (or hardware) would use, rather than pushing Python
        values straight into the observable telemetry cache.
        """

        for node_name in self.runtime.telemetry_order:
            if node_name not in self.runtime.modules:
                continue  # hardware/software node telemetry is observed from the bus, not self-generated
            module = self.runtime.module(node_name)
            if not isinstance(module, _TelemetryReportingModule):
                continue
            values = module.telemetry(now=self.runtime.engine.now)
            for route in self.runtime.telemetry_routes_by_node[node_name]:
                missing = [name for name in route.field_names if name not in values]
                if missing:
                    raise ScenarioRuntimeError(
                        f"module {node_name!r} reports no telemetry "
                        f"{', '.join(repr(name) for name in missing)}, which {route.label} "
                        "is configured to send"
                    )
                self._send_telemetry_route(route, values)
        self._run_current_events()

    def _run_current_events(self) -> None:
        """Dispatch same-tick engine events and drain the bus to a fixed point.

        Draining the bus can schedule new same-tick engine events (e.g.
        decoded telemetry), and dispatching those can put new envelopes on the
        bus (e.g. an OBC rule reacting to telemetry by sending a command); this
        alternates both steps until neither produces anything new.
        """

        for _round in range(self._max_settle_events):
            dispatched = self.runtime.engine.run_until(
                self.runtime.engine.now,
                max_events=self._max_settle_events,
            )
            drained_any = self._drain_transport_frames()
            if not dispatched and not drained_any:
                return
        raise ScenarioRuntimeError(
            f"same-tick event/bus activity did not settle within "
            f"max_settle_events={self._max_settle_events} rounds; check for a feedback loop"
        )

    def _send_route(self, route: _CommandRoute, *, payload: object) -> TransportEnvelope:
        envelope = self._send_packet(
            CspFields(
                priority=route.priority,
                source=route.source_address,
                destination=route.destination_address,
                destination_port=route.destination_port,
                source_port=route.source_port,
                flags=route.flags,
            ),
            route.payload_hex,
        )
        # Only a simulated target ever receives the semantic payload back; for an external
        # node nothing would pop the entry, so do not keep one.
        if payload is not None and route.target_node in self.runtime.modules:
            self._semantic_payloads_by_envelope[envelope.sequence] = payload
        return envelope

    def _send_telemetry_route(
        self, route: _TelemetryRoute, values: Mapping[str, object]
    ) -> TransportEnvelope:
        payload = route.encode(values)
        return self._send_packet(
            CspFields(
                priority=route.priority,
                source=route.source_address,
                destination=route.destination_address,
                destination_port=route.destination_port,
                source_port=route.source_port,
                flags=route.flags,
            ),
            payload,
        )

    def _send_packet(self, fields: CspFields, payload: bytes) -> TransportEnvelope:
        """Send one CSP packet as however many frames the link needs for it.

        How a packet becomes frames is the transport's business: several CFP2
        frames on CAN, one whole-packet frame on a serial line. Returns the
        envelope of the last frame, the one that completes the packet at the
        receiver.
        """

        sender = self._senders.get(fields.source)
        if sender is None:
            sender = self.runtime.transport.new_sender(
                max_packet_bytes=self._limits_by_address.get(
                    fields.source, CSP_V2_DEFAULT_MAX_PACKET_BYTES
                )
            )
            self._senders[fields.source] = sender
        envelope: TransportEnvelope | None = None
        for frame in sender.frames(fields, payload):
            envelope = self.runtime.transport.send(frame, source=fields.source)
        assert envelope is not None  # a packet always yields at least one frame
        return envelope

    def _drain_transport_frames(self) -> bool:
        """Deliver every currently queued bus frame; return whether any were."""

        delivered_any = False
        for envelope in self.runtime.transport.drain():
            self._deliver_transport_envelope(envelope)
            delivered_any = True
        self._collect_transport_drops()
        return delivered_any

    def _deliver_transport_envelope(self, envelope: TransportEnvelope) -> None:
        """Feed one bus frame to the receiving node's reassembler.

        A packet reaches the routing below only once its last frame has arrived
        and it fit the receiving node's ``max_packet_bytes``; whatever else
        happened to the frames is recorded as a dropped packet.
        """

        frame = envelope.frame
        if isinstance(frame, WHOLE_PACKET_FRAMES):
            self._deliver_whole_packet(frame, envelope)
            return
        destination = self.runtime.transport.can_protocol.frame_destination(frame)
        events = self._reassembler_for(destination).feed(frame, now=self.runtime.engine.now)
        for event in events:
            if event.kind == "dropped":
                self._record_drop(event, destination)
            elif event.kind == "complete" and event.packet is not None:
                self._route_packet(event.packet, envelope)

    def _deliver_whole_packet(self, packet: WholePacketFrame, envelope: TransportEnvelope) -> None:
        """Route a packet a link delivered whole, if it fits the receiving node."""

        destination = packet.fields.destination
        limit = self._limits_by_address.get(destination, CSP_V2_DEFAULT_MAX_PACKET_BYTES)
        payload_limit = self.runtime.transport.payload_limit(limit)
        if len(packet.payload) > payload_limit:
            self._append_drop(
                DroppedPacket(
                    time=self.runtime.engine.now,
                    destination=destination,
                    sender=packet.fields.source,
                    reason="overflow",
                    detail=f"payload of {len(packet.payload)} bytes is over the node's "
                    f"limit of {payload_limit} bytes (max_packet_bytes={limit})",
                    limit=limit,
                )
            )
            return
        self._route_packet(packet, envelope)

    def _collect_transport_drops(self) -> None:
        """Record what the transport itself rejected (a serial link's bad frames)."""

        if self.runtime.bus_faults is not None:
            # A frame a bus fault dropped will never be delivered back, so whatever was
            # kept for it (a command's semantic payload) would otherwise live forever.
            for sequence in self.runtime.bus_faults.take_lost_sequences():
                self._semantic_payloads_by_envelope.pop(sequence, None)

        for drop in self.runtime.transport.take_drops():
            self._append_drop(
                DroppedPacket(
                    time=self.runtime.engine.now,
                    destination=drop.destination,
                    sender=drop.source,
                    reason=drop.reason,
                    detail=drop.detail,
                    limit=drop.limit,
                )
            )

    def _reassembler_for(self, destination: int | None) -> PacketReassembler:
        key = -1 if destination is None else destination
        reassembler = self._reassemblers.get(key)
        if reassembler is None:
            reassembler = self.runtime.transport.can_protocol.new_reassembler(
                max_packet_bytes=self._limits_by_address.get(key, CSP_V2_DEFAULT_MAX_PACKET_BYTES)
            )
            self._reassemblers[key] = reassembler
        return reassembler

    def _expire_partial_packets(self) -> None:
        """Report packets still incomplete past the reassembly timeout."""

        now = self.runtime.engine.now
        for key, reassembler in self._reassemblers.items():
            for event in reassembler.expire(now):
                self._record_drop(event, None if key == -1 else key)

    def _record_drop(self, event: PacketEvent, destination: int | None) -> None:
        reason = "" if event.reason is None else event.reason
        self._append_drop(
            DroppedPacket(
                time=event.time,
                destination=destination if destination is not None else event.destination,
                sender=event.sender,
                reason=reason,
                detail=event.detail,
                limit=event.limit,
            )
        )

    def _append_drop(self, dropped: DroppedPacket) -> None:
        self._dropped_packets.append(dropped)
        if self.runtime.recorder is not None:
            self.runtime.recorder.drop(dropped)
        if self.runtime.trace is not None:
            self.runtime.trace.write(format_drop_trace_line(dropped) + "\n")
            self.runtime.trace.flush()

    def _route_packet(self, packet: PacketView, envelope: TransportEnvelope) -> None:
        if self._take_awaited_reply(packet, envelope):
            return
        if self._answer_request(packet):
            return
        command_route = self._command_route_for_packet(packet)
        if command_route is not None:
            self._deliver_command(command_route, packet, envelope)
            return

        telemetry_route = self._telemetry_route_for_packet(packet)
        if telemetry_route is not None:
            self._deliver_telemetry(telemetry_route, packet, envelope)
            return

        fields = packet.fields
        if self._is_request_traffic(fields):
            return
        warnings.warn(
            "received CSP frame with no configured route, ignoring it: "
            f"src={fields.source} dst={fields.destination} "
            f"dport={fields.destination_port} sport={fields.source_port}",
            RuntimeWarning,
            stacklevel=2,
        )

    def _deliver_command(
        self,
        route: _CommandRoute,
        packet: PacketView,
        envelope: TransportEnvelope,
    ) -> None:
        if route.target_node not in self.runtime.modules:
            # The target is a software/hardware node: a real peer handles this command,
            # not a model of ours. Seeing it on the bus is observation -- the trace already
            # names the route -- not a request to run something that does not exist, which
            # is what happens when the testbed merely overhears two other nodes talking,
            # or when the in-memory bus hands the testbed's own command back to it.
            self._semantic_payloads_by_envelope.pop(envelope.sequence, None)
            return

        module = self.runtime.module(route.target_node)
        if not isinstance(module, _CommandHandlingModule):
            raise ScenarioRuntimeError(f"module {route.target_node!r} cannot handle commands")

        semantic_payload = self._semantic_payloads_by_envelope.pop(envelope.sequence, None)
        if semantic_payload is None:
            semantic_payload = packet.payload if packet.payload else None

        handled = module.handle_command(
            CommandPayload(
                route.command,
                payload=semantic_payload,
                source=route.source_node,
                target=route.target_node,
            )
        )
        if not handled:
            raise ScenarioRuntimeError(
                f"command {route.source_node}.{route.command} was not handled by {route.target_node}"
            )

    def _deliver_telemetry(
        self, route: _TelemetryRoute, packet: PacketView, envelope: TransportEnvelope
    ) -> None:
        try:
            values = route.decode(packet.payload)
        except TelemetryCodecError as exc:
            # A payload the signal's codec cannot read (a value with no enum label, a
            # corrupted byte) is a packet the receiver discards, reported like any other
            # drop; it does not end the run, whose assertions then judge what the bus
            # actually delivered.
            self._append_drop(
                DroppedPacket(
                    time=self.runtime.engine.now,
                    destination=packet.fields.destination,
                    sender=packet.fields.source,
                    reason="undecodable",
                    detail=f"{route.name}: {exc}",
                )
            )
            return
        # The frame these values were decoded from, for the report to link an assertion
        # to; it only describes the run, nothing reads it back.
        frame_id = (
            None if self.runtime.recorder is None else self.runtime.recorder.rx_frame_id(envelope)
        )
        for signal, value in values.items():
            for sample_signal, sample_value in _signal_samples(signal, value):
                if frame_id is not None:
                    self._signal_evidence[sample_signal] = frame_id
                self.runtime.engine.schedule_telemetry(
                    sample_signal, sample_value, source=route.node_name
                )

    def _resolve_command_route(self, step: SendCommandStep) -> _CommandRoute:
        candidates = [
            route for route in self.runtime.command_routes if route.command == step.command
        ]
        if step.source is not None:
            candidates = [route for route in candidates if route.source_node == step.source]
        if step.target is not None:
            candidates = [route for route in candidates if route.target_node == step.target]

        if not candidates:
            source_detail = "" if step.source is None else f" from {step.source!r}"
            target_detail = "" if step.target is None else f" to {step.target!r}"
            raise ScenarioRuntimeError(
                f"command {step.command!r}{source_detail}{target_detail} is not configured"
            )
        if len(candidates) > 1:
            sources = ", ".join(route.source_node for route in candidates)
            raise ScenarioRuntimeError(
                f"command {step.command!r} is ambiguous; specify source ({sources})"
            )
        return candidates[0]

    def _command_route_for_packet(self, packet: PacketView) -> _CommandRoute | None:
        """Resolve the configured command route for an inbound CSP frame, if any.

        Returns ``None`` rather than raising when nothing matches: the caller
        also tries telemetry routes before deciding a frame is truly
        unroutable. An ambiguous match (multiple configured routes) is a
        configuration bug and remains a hard error.
        """

        fields = packet.fields
        candidates = [
            route
            for route in self.runtime.command_routes
            if route.source_address == fields.source
            and route.destination_address == fields.destination
            and route.destination_port == fields.destination_port
            and route.source_port == fields.source_port
        ]
        exact_payload = [route for route in candidates if route.payload_hex == packet.payload]
        if len(exact_payload) == 1:
            return exact_payload[0]
        if not exact_payload and len(candidates) == 1:
            return candidates[0]
        if not candidates:
            return None
        raise ScenarioRuntimeError("received CSP command frame matches multiple configured routes")

    def _telemetry_route_for_packet(self, packet: PacketView) -> _TelemetryRoute | None:
        """Resolve the configured telemetry route for an inbound CSP frame, if any."""

        fields = packet.fields
        candidates = [route for route in self.runtime.telemetry_routes if route.matches(fields)]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            return None
        raise ScenarioRuntimeError(
            "received CSP telemetry frame matches multiple configured routes"
        )

    def _try_read_signal(self, signal: str) -> tuple[bool, object]:
        """Return ``(True, value)`` if ``signal`` has been decoded off the bus.

        This deliberately does not fall back to reading the source module's
        Python state directly: an assertion must observe the same bytes a
        real bus listener would decode, not the simulation checked against
        itself. Returns ``(False, None)`` rather than raising when unobserved,
        since "no frame yet" is a normal, retryable condition within an
        assertion's timeout window, not a scenario error.
        """

        _split_telemetry_signal(signal)  # validates '<node>.telemetry.<field>' form
        if signal in self._latest_telemetry:
            return True, self._latest_telemetry[signal]
        return False, None


def build_runtime(
    setup: TestbedConfig,
    *,
    obc_rules: Mapping[str, Iterable[ObcPeerRule]] | None = None,
    clock: Clock | None = None,
    trace: TextIO | None = None,
    recorder: RunRecorder | None = None,
) -> ScenarioRuntime:
    """Build a runtime from a validated setup config.

    Works with either ``transport.type``: the in-memory bus for CI/local
    simulation, or SocketCAN for a HIL run against ``software``/``hardware``
    DUT nodes -- switching which node is real is a config change
    (``mode``/``transport.type`` in setup TOML), not a code change. Which
    nodes get a locally simulated module is decided once by
    :func:`cubesat_testbed.dut.manager.resolve_participants`, not re-derived
    here.

    Each OBC Peer node's rules come from its own ``[nodes.<node>.rules.*]``
    setup config by default. Pass ``obc_rules`` to override the rules for a
    given OBC node instead, for example with a set loaded from a standalone
    rules file (see ``load_obc_rules_file``).

    ``clock`` defaults to :class:`~cubesat_testbed.clock.VirtualClock`
    (pure simulation, ``wait()`` never blocks). Pass a
    :class:`~cubesat_testbed.clock.RealTimeClock` to pace ``wait()`` against
    wall-clock time for a HIL run, so a real ``software``/``hardware`` peer
    gets a realistic window to respond instead of the run blasting through
    virtual time instantly.

    Pass ``trace`` (a text stream, typically ``sys.stderr``) to wrap the
    transport in a :class:`~cubesat_testbed.transport.TracingTransportAdapter`
    and get one decoded line per frame crossing the bus. Purely observational:
    it changes neither scenario semantics nor determinism.

    Pass ``recorder`` (a :class:`~cubesat_testbed.report.RunRecorder`) to record the
    run for the HTML report. It rides on the same transport wrapper as ``trace`` and
    is equally passive; either, both or neither may be given.
    """

    engine = DiscreteEventEngine()
    fault_engine = FaultInjectionEngine(engine)
    command_routes = _build_command_routes(setup)
    telemetry_routes = _build_telemetry_routes(setup)
    participants = resolve_participants(setup)
    request_routes = _build_request_routes(setup)
    service_endpoints = _build_service_endpoints(setup)
    transport = build_transport_adapter(
        setup.transport,
        endpoints=(node.address for node in setup.nodes.values()),
        local_addresses=(
            participant.address for participant in participants.values() if participant.is_simulated
        ),
        max_packet_bytes=max(node.max_packet_bytes for node in setup.nodes.values()),
        protocol_version=setup.protocol_version,
    )
    bus_faults: FaultInjectingTransport | None = None
    if isinstance(transport, InMemoryBusAdapter):
        # Always present on the in-memory bus, and a plain pass-through until a rule is
        # added, so a scenario can add one mid-run without the runtime knowing in advance.
        bus_faults = FaultInjectingTransport(
            transport,
            now=lambda: engine.now,
            schedule=_scheduler_for(engine),
            seed=0 if setup.bus_faults is None else setup.bus_faults.seed,
        )
        if setup.bus_faults is not None:
            for fault_rule in setup.bus_faults.rules:
                bus_faults.add_rule(fault_rule)
        transport = bus_faults
    if trace is not None or recorder is not None:
        # Wrapping here, rather than tracing inside the runner, is what makes
        # frames an OBC Peer module sends straight to the transport visible too.
        tracing = TracingTransportAdapter(
            transport,
            stream=trace,
            now=lambda: engine.now,
            annotate=_build_frame_annotator(
                command_routes, telemetry_routes, request_routes, setup
            ),
            observer=None if recorder is None else recorder.observe_frame,
        )
        transport = tracing
        if bus_faults is not None:
            bus_faults.set_event_handler(_bus_fault_reporter(tracing, trace, recorder))

    # Both the construction order and the per-tick order come from the module
    # registry, so a module type this package has never heard of takes part in
    # exactly the same way a built-in does.
    build_order = _build_order(setup, participants)
    normalized_obc_rules = (
        None if obc_rules is None else {node: tuple(rules) for node, rules in obc_rules.items()}
    )
    if normalized_obc_rules is not None:
        _check_rule_signals(setup, normalized_obc_rules)

    modules: dict[str, SimulatedModule] = {}
    for node_name, registration in build_order:
        module = registration.factory(
            ModuleBuildContext(
                setup=setup,
                node_name=node_name,
                node=setup.nodes[node_name],
                engine=engine,
                fault_engine=fault_engine,
                transport=transport,
                modules=modules,
                obc_rules=normalized_obc_rules,
            )
        )
        if module.fault_engine is None:
            # A factory that forgot `fault_engine=context.fault_engine` would
            # otherwise produce a module on which every state override, signal
            # override and named fault silently does nothing -- a scenario that
            # passes or fails for the wrong reason, which is worse than one that
            # errors. Attaching it here makes fault injection work for every
            # module in a runtime, including third-party ones.
            module.set_fault_engine(fault_engine)
        modules[node_name] = module

    for node_name, participant in participants.items():
        if participant.is_simulated and node_name not in modules:
            raise ScenarioRuntimeError(f"simulated node {node_name!r} has no runtime module")

    telemetry_routes_by_node, telemetry_order = _telemetry_plan_from_routes(telemetry_routes)
    tick_order = tuple(node_name for node_name, _registration in build_order)

    return ScenarioRuntime(
        setup=setup,
        engine=engine,
        fault_engine=fault_engine,
        transport=transport,
        participants=participants,
        modules=modules,
        command_routes=command_routes,
        telemetry_routes=telemetry_routes,
        telemetry_routes_by_node=telemetry_routes_by_node,
        service_endpoints=service_endpoints,
        request_routes=request_routes,
        telemetry_order=telemetry_order,
        tick_order=tick_order,
        clock=VirtualClock() if clock is None else clock,
        trace=trace,
        recorder=recorder,
        bus_faults=bus_faults,
    )


def _check_rule_signals(
    setup: TestbedConfig, obc_rules: Mapping[str, tuple[ObcPeerRule, ...]]
) -> None:
    """Refuse a rules-file rule on a whole array or a missing element, before the run.

    The setup already checks its inline rules the same way; a rules file is parsed
    without the setup, so its rules are checked here, where both meet.
    """

    counts = setup.telemetry_signal_counts()
    for node_name, rules in obc_rules.items():
        for rule in rules:
            problem = rule_signal_problem(rule.condition.signal, counts)
            if problem is not None:
                raise ScenarioRuntimeError(f"rule {node_name}.{rule.name}: {problem}")


def _scheduler_for(engine: DiscreteEventEngine) -> Callable[[int, Callable[[], None]], None]:
    """Let the bus fault injector run a callback after a virtual delay, as an engine event."""

    def schedule(delay: int, action: Callable[[], None]) -> None:
        engine.schedule_timer(delay, handler=lambda _event, _engine: action())

    return schedule


def _bus_fault_reporter(
    tracing: TracingTransportAdapter,
    trace: TextIO | None,
    recorder: RunRecorder | None,
) -> Callable[[BusFaultEvent], None]:
    """Report each injected fault to the trace and the HTML recorder.

    A fault on the send side happens while the frame is still being sent, before the
    tracing adapter has traced it, so it is deferred until the frame has been: the
    trace reads ``TX`` first and ``FAULT`` after. A fault on the receive side has no
    such line to wait for and is reported at once.
    """

    def report(event: BusFaultEvent) -> None:
        def emit() -> None:
            if trace is not None:
                trace.write(format_bus_fault_trace_line(event) + "\n")
                trace.flush()
            if recorder is not None:
                recorder.fault(event)

        if event.direction == "TX":
            tracing.after_frame(emit)
        else:
            emit()

    return report


def build_in_memory_runtime(
    setup: TestbedConfig,
    *,
    obc_rules: Mapping[str, Iterable[ObcPeerRule]] | None = None,
) -> ScenarioRuntime:
    """Build an in-memory runtime from a validated setup config.

    A thin, backward-compatible wrapper around :func:`build_runtime` that
    keeps rejecting non-in-memory transports, for callers that specifically
    want the CI/local-simulation guarantee (no real bus, no wall-clock
    pacing) rather than "whatever the config says".
    """

    if not isinstance(setup.transport, InMemoryTransportConfig):
        raise ScenarioRuntimeError("scenario runner v1 supports setup transport.type='in-memory'")
    return build_runtime(setup, obc_rules=obc_rules)


def run_scenario(
    scenario: ScenarioScript,
    setup: TestbedConfig,
    *,
    output: TextIO | None = None,
    obc_rules: Mapping[str, Iterable[ObcPeerRule]] | None = None,
    clock: Clock | None = None,
    trace: TextIO | None = None,
    recorder: RunRecorder | None = None,
) -> ScenarioRunResult:
    """Build a runtime (in-memory or SocketCAN, per setup) and run a scenario.

    The transport is built here, so it is also closed here -- a HIL run must
    hand its CAN socket back when the scenario ends, whether it passed, failed
    or raised. Callers that build their own runtime (``build_runtime`` plus
    :class:`ScenarioRunner`) keep owning its transport instead.

    ``trace`` is a stream to write a per-frame wire trace to; it should not be
    the same stream as ``output``, which carries the assertion report.
    """

    runtime = build_runtime(setup, obc_rules=obc_rules, clock=clock, trace=trace, recorder=recorder)
    try:
        return ScenarioRunner(runtime, output=output).run(scenario)
    finally:
        runtime.transport.close()


def run_scenario_files(
    setup_path: str | Path,
    scenario_path: str | Path,
    *,
    output: TextIO | None = None,
    obc_rules: Mapping[str, Iterable[ObcPeerRule]] | None = None,
    clock: Clock | None = None,
    trace: TextIO | None = None,
    recorder: RunRecorder | None = None,
) -> ScenarioRunResult:
    """Load setup/scenario files, validate references, and execute them."""

    setup = load_testbed_config(setup_path)
    scenario = load_scenario(scenario_path, setup=setup)
    return run_scenario(
        scenario,
        setup,
        output=output,
        obc_rules=obc_rules,
        clock=clock,
        trace=trace,
        recorder=recorder,
    )


def build_obc_rules_from_file(path: str | Path) -> dict[str, tuple[ObcPeerRule, ...]]:
    """Load a standalone OBC Peer rules file into runtime ``ObcPeerRule``s.

    The result is ready to pass as ``obc_rules`` to :func:`run_scenario_files`
    to override a setup's inline ``[nodes.<node>.rules.*]`` for a given OBC
    node, for example to run the same satellite/testbed setup against several
    different FDIR rule sets.
    """

    rules_by_node = load_obc_rules_file(path)
    return {
        node_name: tuple(obc_peer_rule_from_config(name, rule) for name, rule in rules.items())
        for node_name, rules in rules_by_node.items()
    }


def _build_frame_annotator(
    command_routes: tuple[_CommandRoute, ...],
    telemetry_routes: tuple[_TelemetryRoute, ...],
    request_routes: tuple[_RequestRoute, ...] = (),
    setup: TestbedConfig | None = None,
) -> FrameAnnotator:
    """Build the trace annotator that names the route a traced frame matches.

    Deliberately more forgiving than the delivery path: a frame matching no
    configured route, or several at once, is described as such instead of
    raising. A trace is frequently switched on *because* traffic is unexpected,
    so it has to be able to report the unexpected rather than fail on it.
    """

    def annotate(packet: PacketView) -> str | None:
        fields = packet.fields
        commands = [
            route
            for route in command_routes
            if route.source_address == fields.source
            and route.destination_address == fields.destination
            and route.destination_port == fields.destination_port
            and route.source_port == fields.source_port
        ]
        exact_payload = [route for route in commands if route.payload_hex == packet.payload]
        if len(exact_payload) == 1:
            return _command_annotation(exact_payload[0])
        if len(commands) == 1:
            return _command_annotation(commands[0])
        if commands:
            return "command ambiguous-route"

        telemetry = [route for route in telemetry_routes if route.matches(fields)]
        if len(telemetry) == 1:
            return _telemetry_annotation(telemetry[0], packet.payload)
        if telemetry:
            return "telemetry ambiguous-route"
        return _request_annotation(packet, request_routes, names) or "unrouted"

    names = (
        {}
        if setup is None
        else {node.address: node_name for node_name, node in setup.nodes.items()}
    )

    return annotate


def format_dropped_packet(dropped: DroppedPacket) -> str:
    """Format one dropped packet as a ``DROP t=... node=... reason=...`` report line."""

    parts = ["DROP", f"t={dropped.time}"]
    if dropped.destination is not None:
        parts.append(f"node={dropped.destination}")
    if dropped.sender is not None:
        parts.append(f"sender={dropped.sender}")
    parts.append(f"reason={dropped.reason}")
    if dropped.limit is not None:
        parts.append(f"limit={dropped.limit}")
    parts.append(f"detail={dropped.detail!r}")
    return " ".join(parts)


def format_drop_trace_line(dropped: DroppedPacket) -> str:
    """Format one dropped packet as a ``--trace`` line (``trace`` + the DROP report line)."""

    return f"{TRACE_PREFIX} {format_dropped_packet(dropped)}"


def _command_annotation(route: _CommandRoute) -> str:
    return f"command {route.source_node}.{route.command}->{route.target_node}"


def _telemetry_annotation(route: _TelemetryRoute, payload: bytes) -> str:
    try:
        values = route.decode(payload)
    except TelemetryCodecError as exc:
        return f"telemetry {route.name} undecodable={str(exc)!r}"
    if len(values) == 1:
        ((signal, value),) = values.items()
        return f"telemetry {signal}={value!r}"
    fields = " ".join(
        f"{field_name}={values[signal]!r}"
        for signal, field_name in zip(route.signals, route.field_names, strict=True)
    )
    return f"telemetry {route.label} {fields}"


def _is_ephemeral_port(port: int) -> bool:
    return CSP_EPHEMERAL_PORT_MIN <= port <= _EPHEMERAL_PORT_MAX


def _classify_request(
    fields: PacketFields, request_routes: tuple[_RequestRoute, ...]
) -> tuple[str, str, _RequestRoute | None] | None:
    """Whether a packet is a service or configured request, or the reply to one.

    Returns ``(kind, service, route)`` with ``kind`` ``"request"`` or ``"reply"`` and either
    the service's name (``route`` ``None``) or the configured request's route, or ``None``.
    A libcsp client always asks from an ephemeral source port (17-63) and the reply goes back
    to it, so one side of the pair must be on the service (or request) port and the other on
    an ephemeral port. A packet on port 1 whose other port is not ephemeral is not taken for a
    ping: it is unrouted traffic and stays visible as such.
    """

    for route in request_routes:
        if (
            route.address == fields.destination
            and route.port == fields.destination_port
            and _is_ephemeral_port(fields.source_port)
        ):
            return ("request", route.request, route)
        if (
            route.address == fields.source
            and route.port == fields.source_port
            and _is_ephemeral_port(fields.destination_port)
        ):
            return ("reply", route.request, route)
    service = _SERVICE_NAMES_BY_PORT.get(fields.destination_port)
    if service is not None and _is_ephemeral_port(fields.source_port):
        return ("request", service, None)
    service = _SERVICE_NAMES_BY_PORT.get(fields.source_port)
    if service is not None and _is_ephemeral_port(fields.destination_port):
        return ("reply", service, None)
    return None


def _request_annotation(
    packet: PacketView, request_routes: tuple[_RequestRoute, ...], names: Mapping[int, str]
) -> str | None:
    """Name a request or reply frame: ``request ping to eps``, ``reply eps.requests.hk ...``."""

    fields = packet.fields
    classified = _classify_request(fields, request_routes)
    if classified is None:
        return None
    kind, service, route = classified
    if route is not None:
        if kind == "request":
            return f"request {route.name}"
        try:
            data = strip_crc32(packet.payload, fields.flags)
            decoded = route.codec.decode(data)
        except (ServiceRequestError, TelemetryCodecError) as exc:
            return f"reply {route.name} undecodable={str(exc)!r}"
        shown = " ".join(f"{key}={value!r}" for key, value in decoded.items())
        return f"reply {route.name} {shown}"
    if kind == "request":
        return f"request {service} to {names.get(fields.destination, fields.destination)}"
    sender = names.get(fields.source, fields.source)
    if service == "ping":
        return f"reply ping from {sender}"
    try:
        value = decode_uint32_reply(strip_crc32(packet.payload, fields.flags))
    except ServiceRequestError as exc:
        return f"reply {service} from {sender} undecodable={str(exc)!r}"
    return f"reply {service} from {sender} {SERVICE_REPLY_FIELDS[service]}={value}"


def _seconds(microseconds: int) -> str:
    whole, fraction = divmod(microseconds, 1_000_000)
    return f"{whole}.{fraction:06d} s"


def _signal_samples(signal: str, value: object) -> list[tuple[str, object]]:
    """The telemetry events one decoded field produces.

    A scalar is one event. An array is the whole tuple under its own name and then each
    element as ``signal[i]``, so an assertion or an OBC rule can watch one element as a
    plain number, and the HTML report plots each element.
    """

    samples: list[tuple[str, object]] = [(signal, value)]
    if isinstance(value, tuple):
        samples.extend((f"{signal}[{index}]", element) for index, element in enumerate(value))
    return samples


def _build_command_routes(setup: TestbedConfig) -> tuple[_CommandRoute, ...]:
    routes: list[_CommandRoute] = []
    for source_node, node in setup.nodes.items():
        for command_name, command in node.commands.items():
            target_node = setup.nodes[command.target]
            routes.append(
                _CommandRoute(
                    command=command_name,
                    source_node=source_node,
                    target_node=command.target,
                    source_address=node.address,
                    destination_address=target_node.address,
                    destination_port=command.destination_port,
                    source_port=command.source_port,
                    priority=command.priority,
                    flags=command.flags,
                    payload_hex=command.payload_hex,
                )
            )
    return tuple(routes)


def _build_telemetry_routes(setup: TestbedConfig) -> tuple[_TelemetryRoute, ...]:
    routes: list[_TelemetryRoute] = []
    for definition in setup.telemetry_packets():
        node_name = definition.node_name
        node = setup.nodes[node_name]
        signals: list[str] = []
        field_names: list[str] = []
        for _config_name, signal, _layout in definition.fields:
            signal_node, field_name = _split_telemetry_signal(signal)
            if signal_node != node_name:
                raise ScenarioRuntimeError(
                    f"telemetry signal {signal!r} does not belong to node {node_name!r}"
                )
            signals.append(signal)
            field_names.append(field_name)
        try:
            codec = TelemetryPacketCodec.build(
                [
                    (field_name, layout)
                    for field_name, (_name, _signal, layout) in zip(
                        field_names, definition.fields, strict=True
                    )
                ]
            )
        except TelemetryCodecError as exc:
            raise ScenarioRuntimeError(f"telemetry packet {definition.label}: {exc}") from exc
        addressing = definition
        routes.append(
            _TelemetryRoute(
                label=definition.label,
                node_name=node_name,
                signals=tuple(signals),
                field_names=tuple(field_names),
                codec=codec,
                source_address=node.address,
                destination_address=(
                    node.address if addressing.to is None else setup.nodes[addressing.to].address
                ),
                destination_named=addressing.to is not None,
                destination_port=addressing.destination_port,
                source_port=addressing.source_port,
                priority=addressing.priority,
                flags=addressing.flags,
            )
        )
    return tuple(routes)


def _build_request_routes(setup: TestbedConfig) -> tuple[_RequestRoute, ...]:
    routes: list[_RequestRoute] = []
    for node_name, node in setup.nodes.items():
        for request_name, request in node.requests.items():
            packet = node.packets[request.reply]
            label = f"{node_name}.packets.{request.reply}"
            try:
                codec = TelemetryPacketCodec.build(list(packet.fields.items()))
            except TelemetryCodecError as exc:
                raise ScenarioRuntimeError(f"reply packet {label}: {exc}") from exc
            routes.append(
                _RequestRoute(
                    node_name=node_name,
                    request=request_name,
                    address=node.address,
                    port=request.port,
                    payload_hex=request.payload_hex,
                    reply_label=label,
                    field_names=tuple(packet.fields),
                    codec=codec,
                    simulated=node.mode is NodeMode.SIMULATED,
                )
            )
    return tuple(routes)


def _build_service_endpoints(setup: TestbedConfig) -> dict[int, _ServiceEndpoint]:
    endpoints: dict[int, _ServiceEndpoint] = {}
    for node_name, node in setup.nodes.items():
        if node.services is None or node.mode is not NodeMode.SIMULATED:
            continue
        values = {
            name: value
            for name, value in (
                ("memfree", node.services.memfree),
                ("buf_free", node.services.buf_free),
            )
            if value is not None
        }
        endpoints[node.address] = _ServiceEndpoint(
            node_name=node_name,
            address=node.address,
            services_by_port=node.services.enabled_ports(),
            values=values,
        )
    return endpoints


def _telemetry_plan_from_routes(
    routes: tuple[_TelemetryRoute, ...],
) -> tuple[dict[str, tuple[_TelemetryRoute, ...]], tuple[str, ...]]:
    routes_by_node: dict[str, list[_TelemetryRoute]] = {}
    for route in routes:
        routes_by_node.setdefault(route.node_name, []).append(route)
    return (
        {node: tuple(node_routes) for node, node_routes in routes_by_node.items()},
        tuple(routes_by_node),
    )


def _build_order(
    setup: TestbedConfig,
    participants: Mapping[str, NodeParticipant],
) -> tuple[tuple[str, ModuleRegistration], ...]:
    """Return the simulated nodes to build, in registry build order.

    Sorting is stable, so nodes sharing a ``build_order`` keep the order they
    appear in the setup file -- the run stays reproducible from the config
    alone rather than depending on registration or dictionary order.
    """

    candidates: list[tuple[str, ModuleRegistration]] = []
    for node_name, node in setup.nodes.items():
        if not participants[node_name].is_simulated:
            continue
        # Guaranteed non-None: simulated nodes must declare module_type, and
        # that name was validated against the registry when the config parsed.
        assert node.module_type is not None
        try:
            registration = module_registration(node.module_type)
        except ModuleRegistryError as exc:
            raise ScenarioRuntimeError(str(exc)) from exc
        candidates.append((node_name, registration))
    return tuple(sorted(candidates, key=lambda entry: entry[1].build_order))


def _split_telemetry_signal(signal: str) -> tuple[str, str]:
    parts = signal.split(".", 2)
    if len(parts) != 3 or parts[1] != "telemetry" or not parts[0] or not parts[2]:
        raise ScenarioRuntimeError(
            f"telemetry signal {signal!r} must have form '<node>.telemetry.<field>'"
        )
    return parts[0], parts[2]


def _assertion_name(step: AssertStep, *, index: int) -> str:
    if step.name is not None:
        return step.name
    return f"assert_{index}"


def _fault_type_value(fault_type: FaultType | str) -> str:
    if isinstance(fault_type, FaultType):
        return fault_type.value
    return str(fault_type)


def _validate_virtual_time(name: str, value: VirtualTime) -> VirtualTime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioRuntimeError(f"{name} must be an integer virtual-time tick")
    if value < 0:
        raise ScenarioRuntimeError(f"{name} must be non-negative, got {value}")
    return int(value)


def _validate_positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioRuntimeError(f"{name} must be an integer")
    if value <= 0:
        raise ScenarioRuntimeError(f"{name} must be positive, got {value}")
    return int(value)


__all__ = [
    "ScenarioRunResult",
    "ScenarioRunner",
    "ScenarioRunnerError",
    "ScenarioRuntime",
    "ScenarioRuntimeError",
    "build_in_memory_runtime",
    "build_obc_rules_from_file",
    "build_runtime",
    "format_junit_error_xml",
    "format_junit_xml",
    "run_scenario",
    "run_scenario_files",
]
