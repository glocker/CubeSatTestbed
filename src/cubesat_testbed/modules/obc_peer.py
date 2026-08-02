"""Reference OBC Peer module.

The OBC Peer is a stateless rule engine. It listens to decoded telemetry and
executes rules of the form ``IF <condition> THEN <action>``.

v1 supports threshold conditions, ``for`` duration, ``cooldown``, named command
emission through the configured codec/bus, and explicit calls into the passive
Fault Injection Engine. It does not wait for ACKs and does not implement
stateful on-enter/on-exit rules.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, TypeAlias

from cubesat_testbed.engine import (
    DiscreteEventEngine,
    EventHandler,
    EventKind,
    SimulationEvent,
    TelemetryPayload,
    VirtualTime,
)
from cubesat_testbed.fault_injection import (
    ActiveOverride,
    FaultInjectionEngine,
    FaultInjectionKind,
    NamedFaultFlag,
)
from cubesat_testbed.modules.base import (
    ModuleError,
    SimulatedModule,
    _coerce_finite_float_value,
    _coerce_non_negative_int_value,
    _validate_identifier,
    _validate_optional_endpoint,
)
from cubesat_testbed.protocol.csp_v2 import CspCanFrame, CspFields, CspV2CodecError, pack
from cubesat_testbed.transport.base import EndpointId, TransportAdapter, TransportEnvelope

if TYPE_CHECKING:
    from cubesat_testbed.config import (
        ObcRule,
        ObcRuleCommandAction,
        ObcRuleFaultAction,
        TestbedConfig,
    )
    from cubesat_testbed.modules.registry import ModuleBuildContext

BytesLike: TypeAlias = bytes | bytearray | memoryview
FaultActionResult: TypeAlias = ActiveOverride | NamedFaultFlag | None

_PATH_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*)+$")


class ThresholdOperator(str, Enum):
    """Numeric threshold operators supported by OBC Peer v1."""

    LT = "<"
    LTE = "<="
    GT = ">"
    GTE = ">="


@dataclass(frozen=True, slots=True)
class ObcPeerThresholdCondition:
    """One numeric telemetry threshold condition."""

    signal: str
    operator: ThresholdOperator | str
    threshold: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "signal",
            _validate_path("telemetry signal", self.signal, min_segments=3),
        )
        object.__setattr__(self, "operator", _coerce_threshold_operator(self.operator))
        object.__setattr__(
            self,
            "threshold",
            _coerce_finite_float_value("threshold", self.threshold),
        )

    def matches(self, *, signal: str, value: object) -> bool:
        """Return whether this condition matches a telemetry sample."""

        if signal != self.signal:
            return False

        telemetry_value = _coerce_finite_float_value(f"telemetry {signal!r} value", value)
        if self.operator is ThresholdOperator.LT:
            return telemetry_value < self.threshold
        if self.operator is ThresholdOperator.LTE:
            return telemetry_value <= self.threshold
        if self.operator is ThresholdOperator.GT:
            return telemetry_value > self.threshold
        return telemetry_value >= self.threshold


@dataclass(frozen=True, slots=True)
class ObcPeerCommandAction:
    """Rule action that emits a configured named command on the CSP bus."""

    command: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", _validate_identifier("command", self.command))


@dataclass(frozen=True, slots=True)
class ObcPeerFaultAction:
    """Rule action that explicitly calls the passive FaultInjectionEngine."""

    fault_type: FaultInjectionKind | str
    target: str
    value: object = None
    duration: VirtualTime | None = None
    cycles: int | None = None

    def __post_init__(self) -> None:
        fault_type = _coerce_fault_kind(self.fault_type)
        object.__setattr__(self, "fault_type", fault_type)
        object.__setattr__(self, "target", _validate_fault_target(fault_type, self.target))
        object.__setattr__(
            self,
            "duration",
            _validate_optional_non_negative_int("fault duration", self.duration),
        )
        object.__setattr__(
            self,
            "cycles",
            _validate_optional_non_negative_int("fault cycles", self.cycles),
        )
        if fault_type is FaultInjectionKind.NAMED_FAULT and (
            self.duration is not None or self.cycles is not None
        ):
            raise ModuleError("named_fault rule actions do not support duration or cycles")


ObcPeerRuleAction: TypeAlias = ObcPeerCommandAction | ObcPeerFaultAction


@dataclass(frozen=True, slots=True)
class ObcPeerRule:
    """One stateless threshold rule with temporal trigger gates."""

    name: str
    condition: ObcPeerThresholdCondition
    actions: tuple[ObcPeerRuleAction, ...]
    for_duration: VirtualTime = 0
    cooldown: VirtualTime = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_identifier("rule name", self.name))
        if not isinstance(self.condition, ObcPeerThresholdCondition):
            raise ModuleError("rule condition must be an ObcPeerThresholdCondition")
        actions = tuple(self.actions)
        if not actions:
            raise ModuleError("rule actions must contain at least one action")
        for action in actions:
            if not isinstance(action, (ObcPeerCommandAction, ObcPeerFaultAction)):
                raise ModuleError(
                    "rule actions must be ObcPeerCommandAction or ObcPeerFaultAction instances"
                )
        object.__setattr__(self, "actions", actions)
        object.__setattr__(
            self,
            "for_duration",
            _coerce_non_negative_int_value("rule for_duration", self.for_duration),
        )
        object.__setattr__(
            self,
            "cooldown",
            _coerce_non_negative_int_value("rule cooldown", self.cooldown),
        )


@dataclass(frozen=True, slots=True)
class ObcPeerNamedCommand:
    """Configured binary CSP command emitted by name from an OBC rule."""

    source_address: int
    destination_address: int
    destination_port: int
    source_port: int
    priority: int = 2
    flags: int = 0
    payload: BytesLike = b""

    def __post_init__(self) -> None:
        payload = _coerce_bytes("command payload", self.payload)
        fields = _validated_csp_fields(
            priority=self.priority,
            source=self.source_address,
            destination=self.destination_address,
            destination_port=self.destination_port,
            source_port=self.source_port,
            flags=self.flags,
        )
        object.__setattr__(self, "priority", fields.priority)
        object.__setattr__(self, "source_address", fields.source)
        object.__setattr__(self, "destination_address", fields.destination)
        object.__setattr__(self, "destination_port", fields.destination_port)
        object.__setattr__(self, "source_port", fields.source_port)
        object.__setattr__(self, "flags", fields.flags)
        object.__setattr__(self, "payload", payload)
        self.encode()

    def encode(self, *, packet_count: int = 0) -> CspCanFrame:
        """Encode this named command into one product-v1 CSP-over-CAN frame."""

        fields = _validated_csp_fields(
            priority=self.priority,
            source=self.source_address,
            destination=self.destination_address,
            destination_port=self.destination_port,
            source_port=self.source_port,
            flags=self.flags,
        )
        try:
            return pack(fields, self.payload, packet_count=packet_count)
        except CspV2CodecError as exc:
            raise ModuleError(f"named command cannot be encoded as a v1 CSP frame: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ObcPeerConfig:
    """Configuration for :class:`ObcPeerModule`."""

    name: str = "obc"
    endpoint: EndpointId | None = None
    rules: tuple[ObcPeerRule, ...] = ()
    commands: Mapping[str, ObcPeerNamedCommand] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_identifier("module name", self.name))
        object.__setattr__(
            self,
            "endpoint",
            _validate_optional_endpoint("module endpoint", self.endpoint),
        )

        rules = tuple(self.rules)
        rule_names: set[str] = set()
        for rule in rules:
            if not isinstance(rule, ObcPeerRule):
                raise ModuleError("OBC Peer rules must be ObcPeerRule instances")
            if rule.name in rule_names:
                raise ModuleError(f"duplicate OBC Peer rule name {rule.name!r}")
            rule_names.add(rule.name)
        object.__setattr__(self, "rules", rules)

        commands = dict(self.commands)
        for command_name, command in commands.items():
            _validate_identifier("named command", command_name)
            if not isinstance(command, ObcPeerNamedCommand):
                raise ModuleError("OBC Peer command mappings must be ObcPeerNamedCommand instances")
        object.__setattr__(self, "commands", commands)


@dataclass(frozen=True, slots=True)
class ObcPeerActionResult:
    """Result of one executed OBC Peer rule action."""

    action: ObcPeerRuleAction
    command_envelope: TransportEnvelope | None = None
    fault_result: FaultActionResult = None


@dataclass(frozen=True, slots=True)
class ObcPeerRuleResult:
    """Result of one OBC Peer rule trigger."""

    rule_name: str
    signal: str
    value: object
    triggered_at: VirtualTime
    action_results: tuple[ObcPeerActionResult, ...]


@dataclass(slots=True)
class _RuleRuntimeState:
    condition_since: VirtualTime | None = None
    last_triggered_at: VirtualTime | None = None


class ObcPeerModule(SimulatedModule):
    """Deterministic v1 OBC Peer rule engine.

    The module keeps only temporal trigger bookkeeping required by ``for`` and
    ``cooldown``. It has no on-enter/on-exit state machine and never waits for
    command acknowledgements.
    """

    def __init__(
        self,
        config: ObcPeerConfig | None = None,
        *,
        transport: TransportAdapter | None = None,
        fault_engine: FaultInjectionEngine | None = None,
    ) -> None:
        self.config = ObcPeerConfig() if config is None else config
        self._transport = transport
        self._engine: DiscreteEventEngine | None = None
        self._event_handler: EventHandler = self.handle_event
        self._runtime_by_rule: dict[str, _RuleRuntimeState] = {
            rule.name: _RuleRuntimeState() for rule in self.config.rules
        }
        self._last_results: tuple[ObcPeerRuleResult, ...] = ()
        super().__init__(
            self.config.name,
            endpoint=self.config.endpoint,
            fault_engine=fault_engine,
        )

    @classmethod
    def from_testbed_config(
        cls,
        setup: TestbedConfig,
        *,
        source_node: str = "obc",
        rules: Iterable[ObcPeerRule] = (),
        transport: TransportAdapter | None = None,
        fault_engine: FaultInjectionEngine | None = None,
    ) -> ObcPeerModule:
        """Build an OBC Peer from setup command mappings and explicit rules."""

        try:
            node = setup.nodes[source_node]
        except KeyError as exc:
            raise ModuleError(f"OBC Peer source node {source_node!r} is not configured") from exc

        return cls(
            ObcPeerConfig(
                name=source_node,
                endpoint=node.address,
                rules=tuple(rules),
                commands=obc_peer_commands_from_testbed_config(setup, source_node),
            ),
            transport=transport,
            fault_engine=fault_engine,
        )

    @property
    def transport(self) -> TransportAdapter | None:
        """Transport adapter used for named command emission."""

        return self._transport

    @property
    def attached_engine(self) -> DiscreteEventEngine | None:
        """DES engine whose telemetry events are handled by this instance."""

        return self._engine

    @property
    def last_results(self) -> tuple[ObcPeerRuleResult, ...]:
        """Most recent telemetry-observation trigger results."""

        return self._last_results

    def set_transport(self, transport: TransportAdapter | None) -> None:
        """Replace the transport adapter used for command emission."""

        self._transport = transport

    def attach(self, engine: DiscreteEventEngine) -> None:
        """Register this OBC Peer as a handler for DES ``TELEMETRY`` events."""

        if self._engine is engine:
            return
        if self._engine is not None:
            self.detach()
        engine.add_handler(EventKind.TELEMETRY, self._event_handler)
        self._engine = engine

    def detach(self) -> None:
        """Unregister this OBC Peer from its attached DES engine, if any."""

        if self._engine is None:
            return
        self._engine.remove_handler(EventKind.TELEMETRY, self._event_handler)
        self._engine = None

    def handle_event(self, event: SimulationEvent, engine: DiscreteEventEngine) -> None:
        """Evaluate rules against one decoded telemetry event."""

        if event.kind is not EventKind.TELEMETRY:
            raise ModuleError(f"OBC Peer cannot handle non-telemetry event {event.kind.value!r}")
        if not isinstance(event.payload, TelemetryPayload):
            raise ModuleError("telemetry events handled by OBC Peer must carry a TelemetryPayload")
        self.observe_telemetry(event.payload.signal, event.payload.value, now=engine.now)

    def observe_telemetry(
        self,
        signal: str,
        value: object,
        *,
        now: VirtualTime | None = None,
    ) -> tuple[ObcPeerRuleResult, ...]:
        """Evaluate matching rules for one decoded telemetry sample.

        Unrelated telemetry signals leave rule timing state unchanged. Matching
        signals reset their ``for`` timer when the threshold condition is false.
        """

        validated_signal = _validate_path("telemetry signal", signal, min_segments=3)
        current_time = self._resolve_time(now)
        results: list[ObcPeerRuleResult] = []

        for rule in self.config.rules:
            if rule.condition.signal != validated_signal:
                continue
            runtime = self._runtime_by_rule[rule.name]
            if not rule.condition.matches(signal=validated_signal, value=value):
                runtime.condition_since = None
                continue
            if runtime.condition_since is None:
                runtime.condition_since = current_time
            if current_time - runtime.condition_since < rule.for_duration:
                continue
            if _is_in_cooldown(rule, runtime, now=current_time):
                continue

            runtime.last_triggered_at = current_time
            results.append(
                self._execute_rule(rule, signal=validated_signal, value=value, now=current_time)
            )

        self._last_results = tuple(results)
        return self._last_results

    def reset_rule_timing(self, rule_name: str | None = None) -> None:
        """Clear temporal ``for``/``cooldown`` bookkeeping for one or all rules."""

        if rule_name is None:
            for runtime in self._runtime_by_rule.values():
                runtime.condition_since = None
                runtime.last_triggered_at = None
            return

        validated_name = _validate_identifier("rule name", rule_name)
        try:
            runtime = self._runtime_by_rule[validated_name]
        except KeyError as exc:
            raise ModuleError(f"unknown OBC Peer rule {validated_name!r}") from exc
        runtime.condition_since = None
        runtime.last_triggered_at = None

    def _execute_rule(
        self,
        rule: ObcPeerRule,
        *,
        signal: str,
        value: object,
        now: VirtualTime,
    ) -> ObcPeerRuleResult:
        action_results: list[ObcPeerActionResult] = []
        for action in rule.actions:
            if isinstance(action, ObcPeerCommandAction):
                action_results.append(
                    ObcPeerActionResult(action=action, command_envelope=self._emit_command(action))
                )
            else:
                action_results.append(
                    ObcPeerActionResult(
                        action=action, fault_result=self._apply_fault(action, now=now)
                    )
                )

        return ObcPeerRuleResult(
            rule_name=rule.name,
            signal=signal,
            value=value,
            triggered_at=now,
            action_results=tuple(action_results),
        )

    def _emit_command(self, action: ObcPeerCommandAction) -> TransportEnvelope:
        if self._transport is None:
            raise ModuleError("OBC Peer command actions require a transport adapter")
        try:
            command = self.config.commands[action.command]
        except KeyError as exc:
            raise ModuleError(f"OBC Peer command {action.command!r} is not configured") from exc

        return self._transport.send(command.encode(), source=self.default_source)

    def _apply_fault(self, action: ObcPeerFaultAction, *, now: VirtualTime) -> FaultActionResult:
        if self.fault_engine is None:
            raise ModuleError("OBC Peer fault actions require a FaultInjectionEngine")
        return self.fault_engine.apply(
            action.fault_type,
            action.target,
            value=action.value,
            duration=action.duration,
            cycles=action.cycles,
            now=now,
        )

    def _resolve_time(self, now: VirtualTime | None) -> VirtualTime:
        if now is not None:
            return _coerce_non_negative_int_value("virtual time", now)
        if self._engine is not None:
            return self._engine.now
        return 0


def build_obc_peer(context: ModuleBuildContext) -> ObcPeerModule:
    """Registry factory for the ``obc_peer`` module type.

    The OBC Peer is the one built-in that needs the runtime itself rather than
    just its own params: it emits commands through the transport and observes
    telemetry events on the engine, so it is built last and attached here.
    """

    rules = obc_peer_rules_from_config(context.node.rules)
    if context.obc_rules is not None and context.node_name in context.obc_rules:
        rules = tuple(context.obc_rules[context.node_name])

    module = ObcPeerModule.from_testbed_config(
        context.setup,
        source_node=context.node_name,
        rules=rules,
        transport=context.transport,
        fault_engine=context.fault_engine,
    )
    module.attach(context.engine)
    return module


def obc_peer_rules_from_config(rules: Mapping[str, ObcRule]) -> tuple[ObcPeerRule, ...]:
    """Convert validated config rules into runtime :class:`ObcPeerRule`s.

    The config layer deliberately does not import this package, so the
    translation between the two rule shapes lives here, next to the runtime
    one, and is shared by inline ``[nodes.<node>.rules.*]`` and standalone
    rules files.
    """

    return tuple(obc_peer_rule_from_config(name, rule) for name, rule in rules.items())


def obc_peer_rule_from_config(name: str, rule: ObcRule) -> ObcPeerRule:
    """Convert one validated config ``ObcRule`` into a runtime ``ObcPeerRule``."""

    return ObcPeerRule(
        name=name,
        condition=ObcPeerThresholdCondition(rule.signal, rule.op, rule.threshold),
        actions=tuple(_rule_action_from_config(action) for action in rule.actions),
        for_duration=rule.for_duration,
        cooldown=rule.cooldown,
    )


def _rule_action_from_config(
    action: ObcRuleCommandAction | ObcRuleFaultAction,
) -> ObcPeerRuleAction:
    if action.type == "send_command":
        return ObcPeerCommandAction(action.command)
    return ObcPeerFaultAction(
        fault_type=action.fault_type.value,
        target=action.target,
        value=action.value,
        duration=action.duration,
        cycles=action.cycles,
    )


def obc_peer_commands_from_testbed_config(
    setup: TestbedConfig,
    source_node: str = "obc",
) -> dict[str, ObcPeerNamedCommand]:
    """Convert setup TOML command mappings into OBC Peer named commands.

    The setup config owns command names, target nodes, CSP ports, priority, flags
    and binary payload bytes. This helper adds the source/target CSP addresses
    from the configured nodes so the OBC Peer can encode commands through the CSP
    codec before putting them on the bus.
    """

    try:
        source = setup.nodes[source_node]
    except KeyError as exc:
        raise ModuleError(f"OBC Peer source node {source_node!r} is not configured") from exc

    commands: dict[str, ObcPeerNamedCommand] = {}
    for command_name, command in source.commands.items():
        try:
            target = setup.nodes[command.target]
        except KeyError as exc:
            raise ModuleError(
                f"command {source_node}.{command_name} targets unknown node {command.target!r}"
            ) from exc
        commands[command_name] = ObcPeerNamedCommand(
            source_address=source.address,
            destination_address=target.address,
            destination_port=command.destination_port,
            source_port=command.source_port,
            priority=command.priority,
            flags=command.flags,
            payload=command.payload_hex,
        )
    return commands


def _is_in_cooldown(
    rule: ObcPeerRule,
    runtime: _RuleRuntimeState,
    *,
    now: VirtualTime,
) -> bool:
    if runtime.last_triggered_at is None:
        return False
    return now - runtime.last_triggered_at < rule.cooldown


def _validated_csp_fields(
    *,
    priority: int,
    source: int,
    destination: int,
    destination_port: int,
    source_port: int,
    flags: int,
) -> CspFields:
    try:
        return CspFields(
            priority=priority,
            source=source,
            destination=destination,
            destination_port=destination_port,
            source_port=source_port,
            flags=flags,
        )
    except CspV2CodecError as exc:
        raise ModuleError(f"invalid named command CSP fields: {exc}") from exc


def _coerce_threshold_operator(value: ThresholdOperator | str) -> ThresholdOperator:
    try:
        return ThresholdOperator(value)
    except ValueError as exc:
        supported = ", ".join(operator.value for operator in ThresholdOperator)
        raise ModuleError(f"threshold operator must be one of: {supported}") from exc


def _coerce_fault_kind(value: FaultInjectionKind | str) -> FaultInjectionKind:
    try:
        return FaultInjectionKind(value)
    except ValueError as exc:
        supported = ", ".join(kind.value for kind in FaultInjectionKind)
        raise ModuleError(f"fault action type must be one of: {supported}") from exc


def _validate_fault_target(kind: FaultInjectionKind, target: str) -> str:
    min_segments = 2 if kind is FaultInjectionKind.NAMED_FAULT else 3
    validated = _validate_path("fault target", target, min_segments=min_segments)
    if kind is FaultInjectionKind.STATE_OVERRIDE and ".model." not in validated:
        raise ModuleError("state_override fault actions must target a .model. path")
    if kind is FaultInjectionKind.SIGNAL_OVERRIDE and ".telemetry." not in validated:
        raise ModuleError("signal_override fault actions must target a .telemetry. path")
    if kind is FaultInjectionKind.NAMED_FAULT and any(
        marker in validated for marker in (".model.", ".telemetry.")
    ):
        raise ModuleError("named_fault actions must target a module fault flag")
    return validated


def _validate_optional_non_negative_int(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    return _coerce_non_negative_int_value(name, value)


def _validate_path(kind: str, value: str, *, min_segments: int) -> str:
    if not isinstance(value, str) or not _PATH_RE.fullmatch(value):
        raise ModuleError(
            f"{kind} must be a dot-separated path of identifiers, for example "
            "'eps.telemetry.voltage'"
        )
    if len(value.split(".")) < min_segments:
        raise ModuleError(f"{kind} must contain at least {min_segments} path segments")
    return value


def _coerce_bytes(name: str, value: BytesLike) -> bytes:
    try:
        return bytes(value)
    except TypeError as exc:
        raise ModuleError(f"{name} must be bytes-like") from exc


__all__ = [
    "FaultActionResult",
    "ObcPeerActionResult",
    "ObcPeerCommandAction",
    "ObcPeerConfig",
    "ObcPeerFaultAction",
    "ObcPeerModule",
    "ObcPeerNamedCommand",
    "ObcPeerRule",
    "ObcPeerRuleAction",
    "ObcPeerRuleResult",
    "ObcPeerThresholdCondition",
    "ThresholdOperator",
    "build_obc_peer",
    "obc_peer_commands_from_testbed_config",
    "obc_peer_rule_from_config",
    "obc_peer_rules_from_config",
]
