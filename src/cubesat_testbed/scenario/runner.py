"""Scenario runner over deterministic virtual time.

A scenario is an ordered YAML script containing actions such as fault injection,
virtual waits, command sends, and assertions. The runner schedules these actions
on the DES engine and collects PASS/FAIL results.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO, TypeVar, runtime_checkable
from xml.etree.ElementTree import Element, SubElement, tostring

from cubesat_testbed.config import (
    AssertStep,
    FaultType,
    InjectFaultStep,
    InMemoryTransportConfig,
    ModuleType,
    NodeMode,
    ScenarioScript,
    SendCommandStep,
    TestbedConfig,
    WaitStep,
    load_scenario,
    load_testbed_config,
)
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
    PAYLOAD_POWER_OFF_COMMAND,
    GenericEpsConfig,
    GenericEpsModule,
    ObcPeerCommandAction,
    ObcPeerModule,
    ObcPeerRule,
    ObcPeerThresholdCondition,
    SimplePayloadConfig,
    SimplePayloadModule,
    SimulatedModule,
    TelemetrySample,
    ThresholdOperator,
)
from cubesat_testbed.protocol.csp_v2 import CspFields, DecodedCspPacket, decode_frame, pack
from cubesat_testbed.scenario.assertions import (
    AssertionResult,
    TelemetryAssertion,
    format_assertion_result,
)
from cubesat_testbed.transport import EndpointId, InMemoryBusAdapter, TransportEnvelope

_ModuleT = TypeVar("_ModuleT", bound=SimulatedModule)


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
class _TelemetryEmitterModule(Protocol):
    def telemetry(self, *, now: VirtualTime | None = None) -> dict[str, object]: ...

    def emit_telemetry(
        self,
        engine: DiscreteEventEngine,
        *,
        names: Iterable[str] | None = None,
        delay: VirtualTime = 0,
        source: EndpointId | None = None,
    ) -> tuple[TelemetrySample, ...]: ...


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


@dataclass(slots=True)
class ScenarioRuntime:
    """Runtime objects required to execute an in-memory scenario."""

    setup: TestbedConfig
    engine: DiscreteEventEngine
    fault_engine: FaultInjectionEngine
    transport: InMemoryBusAdapter
    modules: dict[str, SimulatedModule]
    command_routes: tuple[_CommandRoute, ...]
    telemetry_fields_by_node: dict[str, tuple[str, ...]]
    telemetry_order: tuple[str, ...]
    tick_order: tuple[str, ...]

    def module(self, name: str) -> SimulatedModule:
        """Return a configured simulated module by node name."""

        try:
            return self.modules[name]
        except KeyError as exc:
            raise ScenarioRuntimeError(f"simulated module {name!r} is not available") from exc


@dataclass(frozen=True, slots=True)
class ScenarioRunResult:
    """Summary of one deterministic scenario run."""

    scenario_name: str
    started_at: VirtualTime
    finished_at: VirtualTime
    assertions: tuple[AssertionResult, ...]

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
        self.runtime.engine.add_handler(EventKind.TELEMETRY, self._record_telemetry)

    @property
    def latest_telemetry(self) -> Mapping[str, object]:
        """Latest decoded telemetry values observed by this runner."""

        return dict(self._latest_telemetry)

    def run(self, scenario: ScenarioScript) -> ScenarioRunResult:
        """Execute ``scenario`` deterministically and return assertion results."""

        started_at = self.runtime.engine.now
        self._assertion_results = []

        for index, step in enumerate(scenario.steps, start=1):
            if isinstance(step, WaitStep):
                self.wait(step.virtual_time)
            elif isinstance(step, InjectFaultStep):
                self.inject_fault(step)
            elif isinstance(step, SendCommandStep):
                self.send_command(step)
            elif isinstance(step, AssertStep):
                self.assert_step(step, index=index)
            else:
                raise ScenarioRuntimeError(f"unsupported scenario step at index {index}: {step!r}")

        self._run_current_events()
        return ScenarioRunResult(
            scenario_name=scenario.name,
            started_at=started_at,
            finished_at=self.runtime.engine.now,
            assertions=tuple(self._assertion_results),
        )

    def wait(self, virtual_time: VirtualTime) -> None:
        """Advance virtual time and drive configured in-memory modules."""

        delay = _validate_virtual_time("wait virtual_time", virtual_time)
        if delay == 0:
            self._run_current_events()
            self._emit_configured_telemetry()
            return

        for _tick in range(delay):
            target_time = self.runtime.engine.now + 1
            self.runtime.engine.run_until(target_time, max_events=self._max_settle_events)
            self._drain_transport_commands()
            self.runtime.fault_engine.advance_cycles(1, now=self.runtime.engine.now)
            self._tick_modules(1)
            self._emit_configured_telemetry()

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
        """Evaluate a scenario assertion, optionally waiting until its timeout."""

        assertion = TelemetryAssertion(
            _assertion_name(step, index=index),
            step.signal,
            step.op,
            step.value,
            timeout=step.timeout,
        )
        deadline = self.runtime.engine.now + (assertion.timeout or 0)

        while True:
            result = assertion.evaluate(
                self._read_signal(assertion.signal),
                now=self.runtime.engine.now,
            )
            if result.passed or self.runtime.engine.now >= deadline:
                break
            self.wait(1)

        self._assertion_results.append(result)
        print(format_assertion_result(result), file=self._output)
        self.runtime.engine.schedule_assertion(
            result.name, passed=result.passed, detail=result.detail
        )
        self._run_current_events()
        return result

    def _record_telemetry(self, event: SimulationEvent, _engine: DiscreteEventEngine) -> None:
        if not isinstance(event.payload, TelemetryPayload):
            raise ScenarioRuntimeError("telemetry events must carry a TelemetryPayload")
        self._latest_telemetry[event.payload.signal] = event.payload.value

    def _tick_modules(self, ticks: int) -> None:
        for node_name in self.runtime.tick_order:
            module = self.runtime.module(node_name)
            if isinstance(module, _TickableModule):
                module.tick(ticks)

    def _emit_configured_telemetry(self) -> None:
        for node_name in self.runtime.telemetry_order:
            module = self.runtime.module(node_name)
            if not isinstance(module, _TelemetryEmitterModule):
                continue
            fields = self.runtime.telemetry_fields_by_node[node_name]
            module.emit_telemetry(self.runtime.engine, names=fields)
            self._run_current_events()

    def _run_current_events(self) -> None:
        self.runtime.engine.run_until(
            self.runtime.engine.now,
            max_events=self._max_settle_events,
        )
        self._drain_transport_commands()

    def _send_route(self, route: _CommandRoute, *, payload: object) -> TransportEnvelope:
        frame = pack(
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
        envelope = self.runtime.transport.send(frame, source=route.source_address)
        if payload is not None:
            self._semantic_payloads_by_envelope[envelope.sequence] = payload
        return envelope

    def _drain_transport_commands(self) -> None:
        for envelope in self.runtime.transport.drain():
            self._deliver_transport_envelope(envelope)

    def _deliver_transport_envelope(self, envelope: TransportEnvelope) -> None:
        packet = decode_frame(envelope.frame)
        route = self._route_for_packet(packet)
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

    def _route_for_packet(self, packet: DecodedCspPacket) -> _CommandRoute:
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
            raise ScenarioRuntimeError(
                "received CSP command frame with no configured route: "
                f"src={fields.source} dst={fields.destination} "
                f"dport={fields.destination_port} sport={fields.source_port}"
            )
        raise ScenarioRuntimeError("received CSP command frame matches multiple configured routes")

    def _read_signal(self, signal: str) -> object:
        node_name, field_name = _split_telemetry_signal(signal)
        if node_name in self.runtime.modules:
            module = self.runtime.module(node_name)
            if isinstance(module, _TelemetryEmitterModule):
                telemetry = module.telemetry(now=self.runtime.engine.now)
                try:
                    return telemetry[field_name]
                except KeyError:
                    pass
        try:
            return self._latest_telemetry[signal]
        except KeyError as exc:
            raise ScenarioRuntimeError(
                f"telemetry signal {signal!r} has not been observed"
            ) from exc


def build_in_memory_runtime(
    setup: TestbedConfig,
    *,
    obc_rules: Mapping[str, Iterable[ObcPeerRule]] | None = None,
    install_default_obc_rules: bool = True,
) -> ScenarioRuntime:
    """Build an in-memory runtime from a validated setup config.

    The default OBC rules mirror the repository's example low-battery scenario:
    when an OBC node has a configured ``payload_power_off`` command and EPS battery
    telemetry is mapped, it sheds payload after three virtual ticks below 30%.
    Pass ``obc_rules`` to provide explicit rules for a given OBC node instead.
    """

    if not isinstance(setup.transport, InMemoryTransportConfig):
        raise ScenarioRuntimeError("scenario runner v1 supports setup transport.type='in-memory'")

    engine = DiscreteEventEngine()
    fault_engine = FaultInjectionEngine(engine)
    transport = InMemoryBusAdapter(endpoints=(node.address for node in setup.nodes.values()))
    command_routes = _build_command_routes(setup)

    modules: dict[str, SimulatedModule] = {}

    for node_name, node in setup.nodes.items():
        if node.module_type is ModuleType.SIMPLE_PAYLOAD:
            modules[node_name] = SimplePayloadModule(
                SimplePayloadConfig(name=node_name, endpoint=node.address),
                fault_engine=fault_engine,
            )

    first_payload = _first_module_of_type(modules, SimplePayloadModule)
    for node_name, node in setup.nodes.items():
        if node.module_type is ModuleType.GENERIC_EPS:
            modules[node_name] = GenericEpsModule(
                GenericEpsConfig(name=node_name, endpoint=node.address),
                payload=first_payload,
                fault_engine=fault_engine,
            )

    for node_name, node in setup.nodes.items():
        if node.module_type is ModuleType.OBC_PEER:
            rules = _rules_for_obc_node(
                setup,
                node_name,
                obc_rules=obc_rules,
                install_default_obc_rules=install_default_obc_rules,
            )
            obc = ObcPeerModule.from_testbed_config(
                setup,
                source_node=node_name,
                rules=rules,
                transport=transport,
                fault_engine=fault_engine,
            )
            obc.attach(engine)
            modules[node_name] = obc

    for node_name, node in setup.nodes.items():
        if node.mode is not NodeMode.SIMULATED:
            continue
        if node_name not in modules:
            raise ScenarioRuntimeError(f"simulated node {node_name!r} has no runtime module")

    telemetry_fields_by_node, telemetry_order = _build_telemetry_plan(setup)
    tick_order = _build_tick_order(setup, modules)

    return ScenarioRuntime(
        setup=setup,
        engine=engine,
        fault_engine=fault_engine,
        transport=transport,
        modules=modules,
        command_routes=command_routes,
        telemetry_fields_by_node=telemetry_fields_by_node,
        telemetry_order=telemetry_order,
        tick_order=tick_order,
    )


def run_scenario(
    scenario: ScenarioScript,
    setup: TestbedConfig,
    *,
    output: TextIO | None = None,
    obc_rules: Mapping[str, Iterable[ObcPeerRule]] | None = None,
    install_default_obc_rules: bool = True,
) -> ScenarioRunResult:
    """Build an in-memory runtime and execute a parsed scenario."""

    runtime = build_in_memory_runtime(
        setup,
        obc_rules=obc_rules,
        install_default_obc_rules=install_default_obc_rules,
    )
    return ScenarioRunner(runtime, output=output).run(scenario)


def run_scenario_files(
    setup_path: str | Path,
    scenario_path: str | Path,
    *,
    output: TextIO | None = None,
    obc_rules: Mapping[str, Iterable[ObcPeerRule]] | None = None,
    install_default_obc_rules: bool = True,
) -> ScenarioRunResult:
    """Load setup/scenario files, validate references, and execute them."""

    setup = load_testbed_config(setup_path)
    scenario = load_scenario(scenario_path, setup=setup)
    return run_scenario(
        scenario,
        setup,
        output=output,
        obc_rules=obc_rules,
        install_default_obc_rules=install_default_obc_rules,
    )


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


def _build_telemetry_plan(
    setup: TestbedConfig,
) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    fields_by_node: dict[str, tuple[str, ...]] = {}
    order: list[str] = []
    for node_name, node in setup.nodes.items():
        fields: list[str] = []
        for telemetry_name, mapping in node.telemetry.items():
            signal = mapping.resolved_signal(node_name, telemetry_name)
            signal_node, field_name = _split_telemetry_signal(signal)
            if signal_node != node_name:
                raise ScenarioRuntimeError(
                    f"telemetry signal {signal!r} does not belong to node {node_name!r}"
                )
            fields.append(field_name)
        if fields:
            fields_by_node[node_name] = tuple(fields)
            order.append(node_name)
    return fields_by_node, tuple(order)


def _build_tick_order(
    setup: TestbedConfig, modules: Mapping[str, SimulatedModule]
) -> tuple[str, ...]:
    candidates = [node_name for node_name in setup.nodes if node_name in modules]
    return tuple(sorted(candidates, key=lambda name: _tick_rank(setup.nodes[name].module_type)))


def _tick_rank(module_type: ModuleType | None) -> int:
    if module_type is ModuleType.SIMPLE_PAYLOAD:
        return 0
    if module_type is ModuleType.GENERIC_EPS:
        return 1
    return 2


def _rules_for_obc_node(
    setup: TestbedConfig,
    node_name: str,
    *,
    obc_rules: Mapping[str, Iterable[ObcPeerRule]] | None,
    install_default_obc_rules: bool,
) -> tuple[ObcPeerRule, ...]:
    if obc_rules is not None and node_name in obc_rules:
        return tuple(obc_rules[node_name])
    if not install_default_obc_rules:
        return ()
    return _default_obc_rules(setup, source_node=node_name)


def _default_obc_rules(setup: TestbedConfig, *, source_node: str) -> tuple[ObcPeerRule, ...]:
    node = setup.nodes[source_node]
    if PAYLOAD_POWER_OFF_COMMAND not in node.commands:
        return ()
    if "eps.telemetry.battery_percent" not in setup.telemetry_signal_names():
        return ()
    return (
        ObcPeerRule(
            name="low_battery_shed_payload",
            condition=ObcPeerThresholdCondition(
                "eps.telemetry.battery_percent",
                ThresholdOperator.LT,
                30.0,
            ),
            actions=(ObcPeerCommandAction(PAYLOAD_POWER_OFF_COMMAND),),
            for_duration=3,
        ),
    )


def _first_module_of_type(
    modules: Mapping[str, SimulatedModule],
    module_type: type[_ModuleT],
) -> _ModuleT | None:
    for module in modules.values():
        if isinstance(module, module_type):
            return module
    return None


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
    "format_junit_error_xml",
    "format_junit_xml",
    "run_scenario",
    "run_scenario_files",
]
