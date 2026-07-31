"""Pydantic schemas and parsers for setup TOML and scenario YAML.

Static satellite/testbed setup is TOML. Scenario scripts are YAML. This module
validates both contracts and deliberately does not execute scenarios; execution
belongs to :mod:`cubesat_testbed.scenario.runner`.
"""

from __future__ import annotations

import re
import tomllib
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, Self, TypeAlias

import yaml  # type: ignore[import-untyped]
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from cubesat_testbed.protocol.csp_v2 import (
    CSP_V2_ADDRESS_MAX,
    CSP_V2_FLAGS_MAX,
    CSP_V2_PORT_MAX,
    CSP_V2_PRIORITY_MAX,
    CSP_V2_SINGLE_FRAME_MAX_PAYLOAD_BYTES,
)

_RESERVED_PARAM_NAMES = frozenset({"name", "endpoint"})
"""params keys module config dataclasses always receive from the node itself."""

_SCHEMA_MODEL_CONFIG = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_PATH_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*)+$")
_DURATION_RE = re.compile(r"^([0-9]+)\s*([A-Za-zµ]*)$")
_CYCLE_RE = re.compile(r"^([0-9]+)\s*([A-Za-z]*)$")

# VirtualTime's base unit is microseconds (see cubesat_testbed.engine). A duration
# string must spell out its unit so a config author never silently gets a value
# 1,000,000x smaller than intended; the sole exception is the literal `0`, which
# is unambiguous in any unit.
_DURATION_UNIT_MICROSECONDS: dict[str, int] = {
    "s": 1_000_000,
    "sec": 1_000_000,
    "secs": 1_000_000,
    "second": 1_000_000,
    "seconds": 1_000_000,
    "ms": 1_000,
    "msec": 1_000,
    "msecs": 1_000,
    "millisecond": 1_000,
    "milliseconds": 1_000,
    "us": 1,
    "µs": 1,
    "usec": 1,
    "usecs": 1,
    "microsecond": 1,
    "microseconds": 1,
}

CspAddress: TypeAlias = Annotated[int, Field(ge=0, le=CSP_V2_ADDRESS_MAX, strict=True)]
CspPort: TypeAlias = Annotated[int, Field(ge=0, le=CSP_V2_PORT_MAX, strict=True)]
CspPriority: TypeAlias = Annotated[int, Field(ge=0, le=CSP_V2_PRIORITY_MAX, strict=True)]
CspFlags: TypeAlias = Annotated[int, Field(ge=0, le=CSP_V2_FLAGS_MAX, strict=True)]
VirtualDuration: TypeAlias = Annotated[
    int,
    BeforeValidator(lambda value: _parse_virtual_duration(value)),
    Field(ge=0, strict=True),
]
VirtualCycleCount: TypeAlias = Annotated[
    int,
    BeforeValidator(lambda value: _parse_cycle_count(value)),
    Field(ge=0, strict=True),
]


class ConfigParseError(ValueError):
    """Raised when setup TOML cannot be parsed before schema validation."""


class ScenarioParseError(ValueError):
    """Raised when scenario YAML cannot be parsed before schema validation."""


class ScenarioReferenceError(ValueError):
    """Raised when a parsed scenario references unknown setup objects."""


class NodeMode(str, Enum):
    """Supported product-v1 node modes."""

    SIMULATED = "simulated"
    SOFTWARE = "software"
    HARDWARE = "hardware"


class ModuleType(str, Enum):
    """Built-in simulated module types in product v1."""

    GENERIC_EPS = "generic_eps"
    OBC_PEER = "obc_peer"
    SIMPLE_PAYLOAD = "simple_payload"


class FaultType(str, Enum):
    """Scenario fault-injection request kinds."""

    STATE_OVERRIDE = "state_override"
    SIGNAL_OVERRIDE = "signal_override"
    NAMED_FAULT = "named_fault"


class AssertionOperator(str, Enum):
    """Supported declarative telemetry assertion operators."""

    EQ = "=="
    NE = "!="
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="


class InMemoryTransportConfig(BaseModel):
    """Deterministic in-process transport configuration."""

    model_config = _SCHEMA_MODEL_CONFIG

    type: Literal["in-memory"]


class SocketCanTransportConfig(BaseModel):
    """Linux SocketCAN transport configuration for HIL paths."""

    model_config = _SCHEMA_MODEL_CONFIG

    type: Literal["socketcan"]
    interface: str = "vcan0"
    receive_own_messages: bool = False

    @field_validator("interface")
    @classmethod
    def _validate_interface(cls, value: str) -> str:
        return _validate_identifier("SocketCAN interface", value)


TransportConfig: TypeAlias = Annotated[
    InMemoryTransportConfig | SocketCanTransportConfig,
    Field(discriminator="type"),
]


class CommandMapping(BaseModel):
    """Named command metadata required to encode a single CSP v2 command frame."""

    model_config = _SCHEMA_MODEL_CONFIG

    target: str
    destination_port: CspPort
    source_port: CspPort
    priority: CspPriority = 2
    flags: CspFlags = 0
    payload_hex: bytes = b""

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        return _validate_identifier("command target", value)

    @field_validator("payload_hex", mode="before")
    @classmethod
    def _coerce_payload_hex(cls, value: object) -> bytes:
        payload = _coerce_hex_bytes(value)
        if len(payload) > CSP_V2_SINGLE_FRAME_MAX_PAYLOAD_BYTES:
            raise ValueError(
                "command payload_hex must fit product-v1 single-frame CSP payload "
                f"({CSP_V2_SINGLE_FRAME_MAX_PAYLOAD_BYTES} bytes max)"
            )
        return payload


class TelemetryMapping(BaseModel):
    """Telemetry signal-to-CSP metadata mapping.

    If ``signal`` is omitted, the signal path is derived as
    ``<node>.telemetry.<mapping-name>`` during setup-level validation.

    ``offset``/``length``/``type``/``endian``/``scale``/``offset_value`` declare
    the byte-aligned wire layout used to encode this signal into its own
    single-frame CSP payload (and decode it back); every telemetry mapping
    requires one, so a configured signal can always be produced/consumed as
    real bytes on the bus, not just read out of Python state. ``enum``
    optionally maps the field's raw integer wire value to a label string (and
    back) for non-numeric telemetry such as ``power_status``, since the wire
    codec itself is scalar-only.
    """

    model_config = _SCHEMA_MODEL_CONFIG

    source_port: CspPort
    destination_port: CspPort
    priority: CspPriority = 2
    flags: CspFlags = 0
    signal: str | None = None
    units: str | None = None
    min: float | int | None = None
    max: float | int | None = None
    offset: int
    length: int
    type: Literal["uint", "int", "float"]
    endian: Literal["big", "little"] = "big"
    scale: float = 1.0
    offset_value: float = 0.0
    enum: dict[str, str] | None = None

    @field_validator("signal")
    @classmethod
    def _validate_signal(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_path("telemetry signal", value, min_segments=3)

    @model_validator(mode="after")
    def _validate_wire_layout(self) -> Self:
        # Imported lazily: cubesat_testbed.protocol.signal_codec has no
        # cubesat_testbed dependencies, so this isn't circular, but importing
        # it at module scope would pull an unrelated codec module into every
        # config parse; deferring it keeps that cost paid only when a
        # telemetry mapping is actually validated.
        from cubesat_testbed.protocol.signal_codec import (
            SignalCodecError,
            SignalDataType,
            SignalEndian,
            SignalField,
        )

        try:
            SignalField(
                name="telemetry-field-check",
                byte_offset=self.offset,
                byte_length=self.length,
                data_type=SignalDataType(self.type),
                endian=SignalEndian(self.endian),
                scale=self.scale,
                offset=self.offset_value,
            )
        except SignalCodecError as exc:
            raise ValueError(f"invalid telemetry wire layout: {exc}") from exc

        if self.enum is not None:
            self._validate_enum()
        return self

    def _validate_enum(self) -> None:
        assert self.enum is not None
        if self.type == "float":
            raise ValueError("enum is only valid for 'uint'/'int' telemetry fields")
        if not self.enum:
            raise ValueError("enum must not be empty when provided")

        labels_seen: set[str] = set()
        for raw_key, label in self.enum.items():
            try:
                int(raw_key)
            except ValueError:
                raise ValueError(
                    f"enum key {raw_key!r} must be an integer raw value, given as a string"
                ) from None
            if label in labels_seen:
                raise ValueError(f"enum label {label!r} is mapped by more than one raw value")
            labels_seen.add(label)

    def resolved_signal(self, node_name: str, mapping_name: str) -> str:
        """Return the explicit or derived full telemetry path."""

        if self.signal is not None:
            return self.signal
        return f"{node_name}.telemetry.{mapping_name}"


class ObcRuleCommandAction(BaseModel):
    """Rule action that emits a configured named command."""

    model_config = _SCHEMA_MODEL_CONFIG

    type: Literal["send_command"]
    command: str

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: str) -> str:
        return _validate_identifier("rule action command", value)


class ObcRuleFaultAction(BaseModel):
    """Rule action that calls the passive Fault Injection Engine."""

    model_config = _SCHEMA_MODEL_CONFIG

    type: Literal["inject_fault"]
    fault_type: FaultType
    target: str
    value: object = None
    duration: VirtualDuration | None = None
    cycles: VirtualCycleCount | None = None

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        return _validate_path("rule action fault target", value, min_segments=2)

    @model_validator(mode="after")
    def _validate_fault_target_combination(self) -> Self:
        _validate_fault_type_target_combination(
            self.fault_type, self.target, duration=self.duration, cycles=self.cycles
        )
        return self


ObcRuleAction: TypeAlias = Annotated[
    ObcRuleCommandAction | ObcRuleFaultAction,
    Field(discriminator="type"),
]


class ObcRule(BaseModel):
    """One stateless threshold rule for the OBC Peer rule engine.

    Mirrors :class:`cubesat_testbed.modules.obc_peer.ObcPeerRule`; the runner
    builds that runtime dataclass from this validated config so the config
    layer does not need to import the modules package.
    """

    model_config = _SCHEMA_MODEL_CONFIG

    signal: str
    op: Literal["<", "<=", ">", ">="]
    threshold: float
    for_duration: VirtualDuration = Field(default=0, alias="for")
    cooldown: VirtualDuration = 0
    actions: tuple[ObcRuleAction, ...] = Field(min_length=1)

    @field_validator("signal")
    @classmethod
    def _validate_signal(cls, value: str) -> str:
        return _validate_path("rule signal", value, min_segments=3)


class NodeConfig(BaseModel):
    """One configured subsystem node."""

    model_config = _SCHEMA_MODEL_CONFIG

    mode: NodeMode
    address: CspAddress
    module_type: ModuleType | None = None
    commands: dict[str, CommandMapping] = Field(default_factory=dict)
    telemetry: dict[str, TelemetryMapping] = Field(default_factory=dict)
    rules: dict[str, ObcRule] = Field(default_factory=dict)
    params: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_mode_module_combination(self) -> Self:
        if self.mode is NodeMode.SIMULATED and self.module_type is None:
            raise ValueError("simulated nodes must declare module_type")
        if self.mode is not NodeMode.SIMULATED and self.module_type is not None:
            raise ValueError("module_type is only valid for simulated nodes")
        if self.rules and self.module_type is not ModuleType.OBC_PEER:
            raise ValueError("rules is only valid for module_type = 'obc_peer'")
        if self.mode is not NodeMode.SIMULATED and self.params:
            raise ValueError("params is only valid for simulated nodes")
        if self.params:
            self._validate_params()
        return self

    def _validate_params(self) -> None:
        # Imported lazily: cubesat_testbed.modules transitively imports this
        # module (cubesat_testbed.engine -> cubesat_testbed.transport ->
        # transport.factory -> cubesat_testbed.config) so a top-level import
        # here would be circular at package-load time. By the time any config
        # is actually parsed, the whole package has already loaded.
        from cubesat_testbed.modules.registry import MODULE_PARAM_CONFIGS

        # Guaranteed non-None: params is non-empty only when mode is
        # SIMULATED, and SIMULATED requires module_type (checked above).
        assert self.module_type is not None
        try:
            config_cls = MODULE_PARAM_CONFIGS[self.module_type.value]
        except KeyError:
            raise ValueError(
                f"module_type {self.module_type.value!r} does not accept params"
            ) from None

        reserved = _RESERVED_PARAM_NAMES & self.params.keys()
        if reserved:
            raise ValueError(
                f"params must not set {', '.join(sorted(reserved))}; these are "
                "derived from the node's own name and address"
            )

        # Reuse the target module's own dataclass validation instead of
        # duplicating its range/type checks here: attempt to build it with a
        # placeholder name and surface whatever it rejects. The instance
        # built here is discarded; the runtime builds its own from node.params.
        try:
            config_cls(name="params-check", **self.params)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid params for module_type {self.module_type.value!r}: {exc}"
            ) from exc


class TestbedConfig(BaseModel):
    """Validated static satellite/testbed setup loaded from TOML."""

    model_config = _SCHEMA_MODEL_CONFIG

    transport: TransportConfig
    nodes: dict[str, NodeConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_cross_references(self) -> Self:
        self._validate_node_names()
        self._validate_unique_addresses()
        self._validate_transport_mode_combination()
        self._validate_command_mappings()
        self._validate_telemetry_mappings()
        return self

    def command_mappings(self) -> dict[tuple[str, str], CommandMapping]:
        """Return command mappings keyed by ``(source_node, command_name)``."""

        return {
            (node_name, command_name): command
            for node_name, node in self.nodes.items()
            for command_name, command in node.commands.items()
        }

    def commands_named(self, command_name: str) -> tuple[tuple[str, CommandMapping], ...]:
        """Return all configured commands with ``command_name``."""

        return tuple(
            (node_name, node.commands[command_name])
            for node_name, node in self.nodes.items()
            if command_name in node.commands
        )

    def telemetry_signal_names(self) -> frozenset[str]:
        """Return all explicit and derived telemetry signal paths."""

        return frozenset(
            mapping.resolved_signal(node_name, telemetry_name)
            for node_name, node in self.nodes.items()
            for telemetry_name, mapping in node.telemetry.items()
        )

    def _validate_node_names(self) -> None:
        for node_name in self.nodes:
            _validate_identifier("node name", node_name)

    def _validate_unique_addresses(self) -> None:
        owners_by_address: dict[int, str] = {}
        for node_name, node in self.nodes.items():
            previous_owner = owners_by_address.setdefault(node.address, node_name)
            if previous_owner != node_name:
                raise ValueError(
                    f"nodes {previous_owner!r} and {node_name!r} share CSP address {node.address}"
                )

    def _validate_transport_mode_combination(self) -> None:
        if isinstance(self.transport, InMemoryTransportConfig):
            hardware_nodes = [
                node_name
                for node_name, node in self.nodes.items()
                if node.mode is NodeMode.HARDWARE
            ]
            if hardware_nodes:
                joined_nodes = ", ".join(sorted(hardware_nodes))
                raise ValueError(
                    "hardware nodes require transport.type='socketcan'; "
                    f"in-memory transport was configured for {joined_nodes}"
                )

    def _validate_command_mappings(self) -> None:
        for node_name, node in self.nodes.items():
            for command_name, command in node.commands.items():
                _validate_identifier("command name", command_name)
                if command.target not in self.nodes:
                    raise ValueError(
                        f"command {node_name}.{command_name} targets unknown node "
                        f"{command.target!r}"
                    )

    def _validate_telemetry_mappings(self) -> None:
        owners_by_signal: dict[str, str] = {}
        for node_name, node in self.nodes.items():
            for telemetry_name, mapping in node.telemetry.items():
                _validate_identifier("telemetry mapping name", telemetry_name)
                signal = mapping.resolved_signal(node_name, telemetry_name)
                _validate_path("telemetry signal", signal, min_segments=3)
                if not signal.startswith(f"{node_name}.telemetry."):
                    raise ValueError(
                        f"telemetry mapping {node_name}.{telemetry_name} resolves to {signal!r}; "
                        f"expected it to start with {node_name}.telemetry."
                    )
                previous_owner = owners_by_signal.setdefault(
                    signal, f"{node_name}.{telemetry_name}"
                )
                current_owner = f"{node_name}.{telemetry_name}"
                if previous_owner != current_owner:
                    raise ValueError(
                        f"telemetry signal {signal!r} is mapped by both "
                        f"{previous_owner} and {current_owner}"
                    )


class WaitStep(BaseModel):
    """Scenario step that advances virtual time."""

    model_config = _SCHEMA_MODEL_CONFIG

    action: Literal["wait"]
    virtual_time: VirtualDuration


class InjectFaultStep(BaseModel):
    """Scenario step that requests passive fault injection."""

    model_config = _SCHEMA_MODEL_CONFIG

    action: Literal["inject_fault"]
    type: FaultType
    target: str
    value: object = None
    duration: VirtualDuration | None = None
    cycles: VirtualCycleCount | None = None

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        return _validate_path("fault target", value, min_segments=2)

    @model_validator(mode="after")
    def _validate_fault_target_combination(self) -> Self:
        _validate_fault_type_target_combination(
            self.type, self.target, duration=self.duration, cycles=self.cycles
        )
        return self


class SendCommandStep(BaseModel):
    """Scenario step that references a configured named command."""

    model_config = _SCHEMA_MODEL_CONFIG

    action: Literal["send_command"]
    command: str
    source: str | None = None
    target: str | None = None
    payload: object = None

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: str) -> str:
        return _validate_identifier("scenario command", value)

    @field_validator("source", "target")
    @classmethod
    def _validate_optional_node(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier("scenario command node", value)


class AssertStep(BaseModel):
    """Scenario step that declares a telemetry assertion."""

    model_config = _SCHEMA_MODEL_CONFIG

    action: Literal["assert"]
    signal: str
    op: AssertionOperator
    value: object
    timeout: VirtualDuration | None = None
    name: str | None = None

    @field_validator("signal")
    @classmethod
    def _validate_signal(cls, value: str) -> str:
        return _validate_path("assertion signal", value, min_segments=3)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier("assertion name", value)


ScenarioStep: TypeAlias = Annotated[
    WaitStep | InjectFaultStep | SendCommandStep | AssertStep,
    Field(discriminator="action"),
]


class ScenarioScript(BaseModel):
    """Validated YAML scenario script.

    This is intentionally a data model only. It can be loaded and cross-validated
    against :class:`TestbedConfig` before any runtime execution is started.
    """

    model_config = _SCHEMA_MODEL_CONFIG

    name: str
    description: str | None = None
    steps: tuple[ScenarioStep, ...] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if value == "":
            raise ValueError("scenario name must not be empty")
        return value


def parse_testbed_config(text: str) -> TestbedConfig:
    """Parse and validate setup TOML from a string."""

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigParseError(f"invalid setup TOML: {exc}") from exc
    return TestbedConfig.model_validate(data)


def load_testbed_config(path: str | Path) -> TestbedConfig:
    """Load and validate setup TOML from ``path``."""

    return parse_testbed_config(_read_text(path))


def parse_obc_rules_file(text: str) -> dict[str, dict[str, ObcRule]]:
    """Parse a standalone OBC Peer rules TOML file.

    A rules file overrides a setup's inline ``[nodes.<node>.rules.*]``, for
    example to run the same satellite/testbed setup against several different
    FDIR rule sets. Its shape is ``[<obc-node-name>.<rule-name>]``, reusing the
    exact same :class:`ObcRule` schema as inline setup rules:

    .. code-block:: toml

        [obc.low_battery_shed_payload]
        signal = "eps.telemetry.battery_percent"
        op = "<"
        threshold = 30.0
        for = "3s"

        [[obc.low_battery_shed_payload.actions]]
        type = "send_command"
        command = "payload_power_off"
    """

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigParseError(f"invalid rules TOML: {exc}") from exc
    adapter = TypeAdapter(dict[str, dict[str, ObcRule]])
    return adapter.validate_python(data)


def load_obc_rules_file(path: str | Path) -> dict[str, dict[str, ObcRule]]:
    """Load and validate a standalone OBC Peer rules TOML file from ``path``."""

    return parse_obc_rules_file(_read_text(path))


def parse_scenario(text: str, *, setup: TestbedConfig | None = None) -> ScenarioScript:
    """Parse and validate scenario YAML from a string.

    Passing ``setup`` additionally validates scenario references to configured
    nodes, named commands and telemetry mappings. It still does not execute the
    scenario.
    """

    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScenarioParseError(f"invalid scenario YAML: {exc}") from exc
    if data is None:
        raise ScenarioParseError("scenario YAML must contain a mapping")

    scenario = ScenarioScript.model_validate(data)
    if setup is not None:
        validate_scenario_references(scenario, setup)
    return scenario


def load_scenario(path: str | Path, *, setup: TestbedConfig | None = None) -> ScenarioScript:
    """Load and validate scenario YAML from ``path``."""

    return parse_scenario(_read_text(path), setup=setup)


def validate_scenario_references(scenario: ScenarioScript, setup: TestbedConfig) -> None:
    """Validate parsed scenario references against parsed setup config.

    This catches unknown command names, ambiguous command names, unknown source or
    target nodes, and assertions over telemetry signals that are not mapped in the
    setup TOML.
    """

    errors: list[str] = []
    configured_nodes = frozenset(setup.nodes)
    telemetry_signals = setup.telemetry_signal_names()

    for index, step in enumerate(scenario.steps, start=1):
        prefix = f"step {index} ({step.action})"
        if isinstance(step, InjectFaultStep):
            _validate_fault_reference(step, configured_nodes, prefix, errors)
        elif isinstance(step, SendCommandStep):
            _validate_command_reference(step, setup, configured_nodes, prefix, errors)
        elif isinstance(step, AssertStep) and step.signal not in telemetry_signals:
            errors.append(f"{prefix}: assertion signal {step.signal!r} is not mapped")

    if errors:
        raise ScenarioReferenceError("; ".join(errors))


def _validate_fault_reference(
    step: InjectFaultStep,
    configured_nodes: frozenset[str],
    prefix: str,
    errors: list[str],
) -> None:
    node_name = _path_root(step.target)
    if node_name not in configured_nodes:
        errors.append(f"{prefix}: fault target references unknown node {node_name!r}")


def _validate_command_reference(
    step: SendCommandStep,
    setup: TestbedConfig,
    configured_nodes: frozenset[str],
    prefix: str,
    errors: list[str],
) -> None:
    if step.source is not None and step.source not in configured_nodes:
        errors.append(f"{prefix}: command source {step.source!r} is not a configured node")
        return
    if step.target is not None and step.target not in configured_nodes:
        errors.append(f"{prefix}: command target {step.target!r} is not a configured node")
        return

    if step.source is None:
        candidates = setup.commands_named(step.command)
        if not candidates:
            errors.append(f"{prefix}: command {step.command!r} is not configured")
            return
        if len(candidates) > 1:
            sources = ", ".join(sorted(source for source, _command in candidates))
            errors.append(
                f"{prefix}: command {step.command!r} is ambiguous; specify source ({sources})"
            )
            return
        source_node, command = candidates[0]
    else:
        source_node = step.source
        try:
            command = setup.nodes[source_node].commands[step.command]
        except KeyError:
            errors.append(f"{prefix}: command {source_node}.{step.command} is not configured")
            return

    if step.target is not None and step.target != command.target:
        errors.append(
            f"{prefix}: command {source_node}.{step.command} targets {command.target!r}, "
            f"not {step.target!r}"
        )


def _parse_virtual_duration(value: object) -> int:
    """Parse a scenario/config duration into microseconds, VirtualTime's base unit.

    A bare integer is only accepted when it is exactly ``0`` (unambiguous in any
    unit). Any non-zero magnitude must spell out its unit ('s', 'ms', or 'us') so
    a config author can never silently end up with a value 1,000,000x smaller
    than intended after this base unit changed from virtual seconds to
    microseconds.
    """

    if isinstance(value, bool):
        raise ValueError(  # noqa: TRY004
            "virtual duration must be the integer 0 or a duration string with an explicit unit"
        )
    if isinstance(value, int):
        if value < 0:
            raise ValueError("virtual duration must be non-negative")
        if value != 0:
            raise ValueError(
                "non-zero virtual durations must spell out an explicit unit, for example "
                f"'{value}s', '{value}ms', or '{value}us'"
            )
        return 0
    if isinstance(value, str):
        stripped = value.strip()
        match = _DURATION_RE.fullmatch(stripped)
        if match is None:
            raise ValueError(
                "virtual duration strings must look like '3s', '500ms', '10us', or the bare "
                "integer 0"
            )
        amount_text, unit = match.groups()
        normalized_unit = unit.lower()
        if normalized_unit == "" and amount_text == "0":
            return 0
        try:
            multiplier = _DURATION_UNIT_MICROSECONDS[normalized_unit]
        except KeyError:
            supported = ", ".join(sorted(_DURATION_UNIT_MICROSECONDS))
            raise ValueError(f"virtual duration unit must be one of: {supported}") from None
        return int(amount_text) * multiplier
    raise ValueError(
        "virtual duration must be the integer 0 or a duration string with an explicit unit"
    )


def _parse_cycle_count(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError(  # noqa: TRY004
            "cycle count must be a non-negative integer or cycle string"
        )
    if isinstance(value, int):
        if value < 0:
            raise ValueError("cycle count must be non-negative")
        return value
    if isinstance(value, str):
        stripped = value.strip()
        match = _CYCLE_RE.fullmatch(stripped)
        if match is None:
            raise ValueError(
                "cycle count strings must look like '3 cycles' or a non-negative integer"
            )
        amount_text, unit = match.groups()
        normalized_unit = unit.lower()
        if normalized_unit not in {"", "cycle", "cycles"}:
            raise ValueError("cycle count unit must be 'cycle' or 'cycles'")
        return int(amount_text)
    raise ValueError("cycle count must be a non-negative integer or cycle string")


def _coerce_hex_bytes(value: object) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray | memoryview):
        return bytes(value)
    if isinstance(value, str):
        compact = value.strip()
        if compact == "":
            return b""
        if compact.startswith(("0x", "0X")):
            compact = compact[2:]
        compact = compact.replace("_", "").replace(":", "").replace("-", "")
        try:
            return bytes.fromhex(compact)
        except ValueError as exc:
            raise ValueError("payload_hex must contain hexadecimal byte pairs") from exc
    raise ValueError("payload_hex must be bytes or a hexadecimal string")


def _validate_identifier(kind: str, value: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            f"{kind} must start with a letter and contain only letters, digits, '_' or '-'"
        )
    return value


def _validate_path(kind: str, value: str, *, min_segments: int) -> str:
    if not _PATH_RE.fullmatch(value):
        raise ValueError(
            f"{kind} must be a dot-separated path of identifiers, for example "
            "'eps.telemetry.voltage'"
        )
    if len(value.split(".")) < min_segments:
        raise ValueError(f"{kind} must contain at least {min_segments} path segments")
    return value


def _validate_fault_type_target_combination(
    fault_type: FaultType,
    target: str,
    *,
    duration: int | None,
    cycles: int | None,
) -> None:
    """Shared by ``InjectFaultStep`` and ``ObcRuleFaultAction``."""

    if fault_type is FaultType.STATE_OVERRIDE and ".model." not in target:
        raise ValueError("state_override targets must include '.model.'")
    if fault_type is FaultType.SIGNAL_OVERRIDE and ".telemetry." not in target:
        raise ValueError("signal_override targets must include '.telemetry.'")
    if fault_type is FaultType.NAMED_FAULT:
        if any(marker in target for marker in (".model.", ".telemetry.")):
            raise ValueError(
                "named_fault targets must name a module fault flag, not model/telemetry"
            )
        if duration is not None or cycles is not None:
            raise ValueError("named_fault requests do not support duration or cycles")


def _path_root(path: str) -> str:
    return path.split(".", maxsplit=1)[0]


def _read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


__all__ = [
    "AssertStep",
    "AssertionOperator",
    "CommandMapping",
    "ConfigParseError",
    "FaultType",
    "InMemoryTransportConfig",
    "InjectFaultStep",
    "ModuleType",
    "NodeConfig",
    "NodeMode",
    "ObcRule",
    "ObcRuleAction",
    "ObcRuleCommandAction",
    "ObcRuleFaultAction",
    "ScenarioParseError",
    "ScenarioReferenceError",
    "ScenarioScript",
    "ScenarioStep",
    "SendCommandStep",
    "SocketCanTransportConfig",
    "TelemetryMapping",
    "TestbedConfig",
    "TransportConfig",
    "VirtualCycleCount",
    "VirtualDuration",
    "WaitStep",
    "load_obc_rules_file",
    "load_scenario",
    "load_testbed_config",
    "parse_obc_rules_file",
    "parse_scenario",
    "parse_testbed_config",
    "validate_scenario_references",
]
