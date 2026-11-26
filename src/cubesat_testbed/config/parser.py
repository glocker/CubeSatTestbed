"""Pydantic schemas and parsers for setup TOML and scenario YAML.

Static satellite/testbed setup is TOML. Scenario scripts are YAML. This module
validates both contracts and deliberately does not execute scenarios; execution
belongs to :mod:`cubesat_testbed.scenario.runner`.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
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

from cubesat_testbed.protocol.csp_v1 import (
    CSP_V1_ADDRESS_MAX,
    CSP_V1_BROADCAST_ADDRESS,
)
from cubesat_testbed.protocol.csp_v2 import (
    CSP_V2_ADDRESS_MAX,
    CSP_V2_DEFAULT_MAX_PACKET_BYTES,
    CSP_V2_FLAGS_MAX,
    CSP_V2_PORT_MAX,
    CSP_V2_PRIORITY_MAX,
)
from cubesat_testbed.protocol.csp_v2_kiss import KISS_CRC32_BYTES

_RESERVED_PARAM_NAMES = frozenset({"name", "endpoint"})
"""params keys module config dataclasses always receive from the node itself."""

_SCHEMA_MODEL_CONFIG = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_PATH_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*)+$")
_SIGNAL_REF_RE = re.compile(
    r"^(?P<base>[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*)+)(?:\[(?P<index>[0-9]+)\])?$"
)
"""A telemetry signal as assertions and OBC rules name it: a path, or ``path[i]`` for
one element of an array field."""
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
    """Names of the simulated module types shipped in this package.

    These are constants, not the validation gate: a node's ``module_type`` is
    checked against
    :mod:`cubesat_testbed.modules.registry`, which a module defined outside
    this package extends through ``register_module`` (see
    ``docs/writing-a-module.md``). A third-party type is therefore a valid
    ``module_type`` without appearing here.
    """

    GENERIC_EPS = "generic_eps"
    OBC_PEER = "obc_peer"
    SIMPLE_PAYLOAD = "simple_payload"
    THERMAL_RC = "thermal_rc"


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


class KissTransportConfig(BaseModel):
    """CSP v2 over a serial line with KISS framing.

    ``device`` is a tty such as ``/dev/ttyUSB0`` or the path of a pty (CI, or a
    libcsp process on the same machine). The line joins two ends: packets for the
    nodes the testbed simulates stay inside it, packets for any other address go out
    on the line, and everything arriving on the line is delivered.
    """

    model_config = _SCHEMA_MODEL_CONFIG

    type: Literal["kiss"]
    device: str = Field(min_length=1)
    baudrate: int = Field(default=115200, ge=1)


class ZmqTransportConfig(BaseModel):
    """CSP v2 through a libcsp ZeroMQ hub (``zmqproxy``), as one more client of it.

    The names and defaults are libcsp's own (``csp_zmqhub_init_w_endpoints``):
    ``publish_endpoint`` is where the testbed publishes, the hub's *subscribe* port
    (6000), and ``subscribe_endpoint`` where it listens, the hub's *publish* port
    (7000). Packets for the nodes the testbed simulates stay inside it; everything
    else is published to the hub. Needs the optional ``pyzmq`` extra
    (``pip install 'cubesat-testbed[zmq]'``).

    ``settle_seconds`` is the wall-clock pause after connecting that lets the hub and
    this client learn each other's subscriptions; ZeroMQ PUB/SUB drops what is sent
    before that. Use 0 for a hub known to be ready.
    """

    model_config = _SCHEMA_MODEL_CONFIG

    type: Literal["zmq"]
    publish_endpoint: str = "tcp://127.0.0.1:6000"
    subscribe_endpoint: str = "tcp://127.0.0.1:7000"
    settle_seconds: float = Field(default=0.5, ge=0)

    @field_validator("publish_endpoint", "subscribe_endpoint")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        scheme, separator, rest = value.partition("://")
        if not separator or not rest or scheme not in {"tcp", "ipc"}:
            raise ValueError(
                f"ZMQ endpoint must look like 'tcp://host:port' or 'ipc://path', got {value!r}"
            )
        return value


TransportConfig: TypeAlias = Annotated[
    InMemoryTransportConfig | SocketCanTransportConfig | KissTransportConfig | ZmqTransportConfig,
    Field(discriminator="type"),
]


class CommandMapping(BaseModel):
    """Named command metadata required to encode a CSP v2 command packet.

    ``payload_hex`` may be longer than one CAN frame holds: the packet is sent
    as several CFP2 frames. It must fit the sending node's ``max_packet_bytes``,
    which :class:`TestbedConfig` checks; a receiving node with a smaller limit
    drops the packet when it arrives.
    """

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
        return _coerce_hex_bytes(value)


class TelemetryFieldLayout(BaseModel):
    """Where one telemetry signal sits in a packet's payload, and how it is encoded.

    Shared by a ``[nodes.<node>.telemetry.<name>]`` mapping (a packet holding one signal)
    and each ``[nodes.<node>.packets.<packet>.fields.<name>]`` of a multi-field packet.

    A byte-aligned field gives ``offset`` (byte) and ``length`` (bytes); a bit field gives
    ``offset`` (the byte it starts in), ``bit_offset`` (0-7, counted from that byte's most
    significant bit) and ``bit_length`` (1-64), and is read most significant bit first,
    across byte boundaries if needed; ``endian`` does not apply to it. ``count`` above 1
    makes the field a fixed-length array of that many elements laid out back to back; its
    value is a tuple, and ``<signal>[i]`` names one element in assertions and OBC rules.
    ``type``/``endian``/``scale``/``offset_value`` are the scalar encoding of each element.
    ``enum`` optionally maps the raw integer wire value to a label string (and back) for
    non-numeric telemetry such as ``power_status``.

    If ``signal`` is omitted, the signal path is derived as
    ``<node>.telemetry.<field-name>`` during setup-level validation.
    """

    model_config = _SCHEMA_MODEL_CONFIG

    signal: str | None = None
    units: str | None = None
    min: float | int | None = None
    max: float | int | None = None
    offset: int
    length: int | None = None
    bit_offset: int | None = None
    bit_length: int | None = None
    count: int = Field(default=1, ge=1, strict=True)
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

        is_bit_field = self.bit_offset is not None or self.bit_length is not None
        if is_bit_field and "endian" in self.model_fields_set:
            raise ValueError(
                "invalid telemetry wire layout: a bit field is read most significant bit "
                "first; remove endian"
            )
        if not is_bit_field and self.length is None:
            raise ValueError(
                "invalid telemetry wire layout: give length (bytes), or bit_offset and "
                "bit_length for a bit field"
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
                bit_offset=self.bit_offset,
                bit_length=self.bit_length,
                count=self.count,
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

    @property
    def end_byte(self) -> int:
        """Exclusive byte offset right after this field (its last element)."""

        if self.bit_length is not None:
            end_bit = self.offset * 8 + (self.bit_offset or 0) + self.bit_length * self.count
            return -(-end_bit // 8)
        assert self.length is not None
        return self.offset + self.length * self.count

    def resolved_signal(self, node_name: str, mapping_name: str) -> str:
        """Return the explicit or derived full telemetry path."""

        if self.signal is not None:
            return self.signal
        return f"{node_name}.telemetry.{mapping_name}"


class _TelemetryAddressing(BaseModel):
    """CSP header fields of a telemetry packet: ports, priority, flags and ``to``.

    ``to`` names the node a *simulated* node's telemetry is addressed to (an OBC, say): a
    real OBC only receives frames carrying its own CSP address. Without it the frame is
    addressed to its own sender, which no other node on a real bus ever receives. For
    telemetry a *real* node sends, ``to`` is optional too: without it the packet is matched
    on the sender's address and the ports whatever the frame is addressed to, with it only a
    frame addressed to that node counts.
    """

    model_config = _SCHEMA_MODEL_CONFIG

    source_port: CspPort
    destination_port: CspPort
    to: str | None = None
    priority: CspPriority = 2
    flags: CspFlags = 0

    @field_validator("to")
    @classmethod
    def _validate_to(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier("telemetry destination node", value)


class TelemetryMapping(TelemetryFieldLayout, _TelemetryAddressing):
    """One telemetry signal in a packet of its own: ``[nodes.<node>.telemetry.<name>]``.

    The shorthand for a :class:`TelemetryPacket` with a single field: the addressing of
    :class:`_TelemetryAddressing` plus the wire layout of :class:`TelemetryFieldLayout`.
    """


class TelemetryPacketField(TelemetryFieldLayout):
    """One field of a multi-field packet: ``[nodes.<node>.packets.<packet>.fields.<name>]``."""


class TelemetryPacket(BaseModel):
    """Several telemetry signals sent together as one CSP packet.

    ``[nodes.<node>.packets.<name>]`` holds the addressing, and each
    ``[nodes.<node>.packets.<name>.fields.<field>]`` one signal's place in the payload: the
    normal shape of a housekeeping packet. The sender encodes every field into one packet
    and the receiver decodes it field by field; fields must not overlap, gaps are zero.

    ``periodic = false`` makes the packet a reply layout only: it is never beaconed, only
    sent as the answer to a configured request (``[nodes.<node>.requests.<name>]``), so it
    needs no ports (a reply goes back on the ports of the request it answers) and its
    fields are not telemetry signals.
    """

    model_config = _SCHEMA_MODEL_CONFIG

    source_port: CspPort | None = None
    destination_port: CspPort | None = None
    to: str | None = None
    priority: CspPriority = 2
    flags: CspFlags = 0
    periodic: bool = True
    fields: dict[str, TelemetryPacketField] = Field(min_length=1)

    @field_validator("to")
    @classmethod
    def _validate_to(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier("telemetry destination node", value)

    @model_validator(mode="after")
    def _validate_ports(self) -> Self:
        if self.periodic and (self.source_port is None or self.destination_port is None):
            raise ValueError(
                "a periodic telemetry packet needs source_port and destination_port; set "
                "periodic = false for a packet that is only sent as a reply"
            )
        if not self.periodic:
            header = sorted(
                {"source_port", "destination_port", "to", "priority", "flags"}
                & self.model_fields_set
            )
            if header:
                raise ValueError(
                    f"a reply-only packet (periodic = false) takes no {', '.join(header)}: a "
                    "reply goes back to the requester with the request's ports, priority and "
                    "flags"
                )
        return self


CSP_SERVICE_PORTS: dict[str, int] = {"ping": 1, "memfree": 3, "buf_free": 5, "uptime": 6}
"""The libcsp service ports a simulated node can answer (``csp_service_handler``, v2.1)."""

CSP_RESERVED_PORT_MAX = 6
"""Ports 0-6 are libcsp's services (CMP, ping, ps, memfree, reboot, buf_free, uptime)."""

CSP_EPHEMERAL_PORT_MIN = 17
"""First source port the testbed gives a request: libcsp hands outgoing connections source ports
above ``CSP_PORT_MAX_BIND`` (16 in the default build), up to 63."""


class ServicesConfig(BaseModel):
    """``[nodes.<node>.services]``: which libcsp standard services a simulated node answers.

    The same answers ``csp_service_handler`` gives in libcsp ``v2.1``, all replying to the
    requester on the request's own ports: ``ping`` echoes the request's payload, ``uptime``
    returns the node's uptime in seconds (virtual time since the run started) as a 4-byte
    big-endian unsigned integer, and ``memfree``/``buf_free`` return the configured number
    the same way (a real board reports its free heap and free CSP buffers there).
    """

    model_config = _SCHEMA_MODEL_CONFIG

    ping: bool = False
    uptime: bool = False
    memfree: Annotated[int, Field(ge=0, le=0xFFFFFFFF, strict=True)] | None = None
    buf_free: Annotated[int, Field(ge=0, le=0xFFFFFFFF, strict=True)] | None = None

    def enabled_ports(self) -> dict[int, str]:
        """The service port of every enabled service, mapped to the service's name."""

        enabled = {
            "ping": self.ping,
            "uptime": self.uptime,
            "memfree": self.memfree is not None,
            "buf_free": self.buf_free is not None,
        }
        return {CSP_SERVICE_PORTS[name]: name for name, on in enabled.items() if on}


class RequestMapping(BaseModel):
    """``[nodes.<node>.requests.<name>]``: a request the node answers, and with what.

    A request arrives on destination ``port`` (with ``payload_hex`` when several requests
    share a port, the way commands do). The answer is one packet laid out like ``reply``, a
    packet of the same node, sent back to the requester on the request's ports. For a
    simulated node the module's ``answer_request`` hook supplies the values, or its
    ``telemetry()`` when it has no hook; for a real node this describes what the board
    answers, so a scenario can send the request and check the reply.
    """

    model_config = _SCHEMA_MODEL_CONFIG

    port: CspPort
    payload_hex: bytes = b""
    reply: str

    @field_validator("payload_hex", mode="before")
    @classmethod
    def _coerce_payload_hex(cls, value: object) -> bytes:
        return _coerce_hex_bytes(value)

    @field_validator("reply")
    @classmethod
    def _validate_reply(cls, value: str) -> str:
        return _validate_identifier("request reply packet", value)

    @field_validator("port")
    @classmethod
    def _validate_port(cls, value: int) -> int:
        if value <= CSP_RESERVED_PORT_MAX:
            raise ValueError(
                f"request port {value} is reserved for libcsp's services (0-"
                f"{CSP_RESERVED_PORT_MAX}); use [nodes.<node>.services] for ping, uptime, "
                "memfree and buf_free"
            )
        return value


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
        return _validate_signal_reference("rule signal", value)


@dataclass(frozen=True, slots=True)
class TelemetryPacketDefinition:
    """One telemetry packet of a node, whichever way the setup declared it.

    ``fields`` holds ``(config name, signal path, layout)`` per signal; a single-signal
    ``telemetry`` mapping gives one entry. ``label`` names the packet in messages:
    ``<node>.<mapping>`` for a mapping, ``<node>.packets.<packet>`` for a packet.
    """

    node_name: str
    label: str
    source_port: int
    destination_port: int
    to: str | None
    priority: int
    flags: int
    fields: tuple[tuple[str, str, TelemetryFieldLayout], ...]

    @property
    def payload_length(self) -> int:
        return max(layout.end_byte for _name, _signal, layout in self.fields)


class NodeConfig(BaseModel):
    """One configured subsystem node."""

    model_config = _SCHEMA_MODEL_CONFIG

    mode: NodeMode
    address: CspAddress
    max_packet_bytes: int = Field(default=CSP_V2_DEFAULT_MAX_PACKET_BYTES, ge=1)
    """Largest CSP payload this node can send or reassemble, trailer included.

    Mirrors the libcsp build option ``CSP_BUFFER_SIZE`` (default 256 in
    ``v2.1``): libcsp keeps every packet in a fixed-size buffer chosen when the
    library is built, so a real board may differ from its peers. A packet over
    the *receiving* node's limit is dropped by that node, visibly.
    """

    module_type: str | None = None
    """A module type registered in :mod:`cubesat_testbed.modules.registry`.

    Typed as a plain string rather than :class:`ModuleType` on purpose: the
    registry, not this schema, decides which module types exist, so a
    third-party module is configurable here without this package knowing its
    name. Registration happens on import, so such a module must be imported
    before the config is parsed.
    """

    commands: dict[str, CommandMapping] = Field(default_factory=dict)
    telemetry: dict[str, TelemetryMapping] = Field(default_factory=dict)
    packets: dict[str, TelemetryPacket] = Field(default_factory=dict)
    services: ServicesConfig | None = None
    requests: dict[str, RequestMapping] = Field(default_factory=dict)
    rules: dict[str, ObcRule] = Field(default_factory=dict)
    params: dict[str, object] = Field(default_factory=dict)

    @field_validator("module_type")
    @classmethod
    def _validate_module_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Imported lazily for the same reason as _validate_params below: the
        # modules package transitively imports this one.
        from cubesat_testbed.modules.registry import ModuleRegistryError, module_registration

        try:
            module_registration(value)
        except ModuleRegistryError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @model_validator(mode="after")
    def _validate_mode_module_combination(self) -> Self:
        if self.mode is NodeMode.SIMULATED and self.module_type is None:
            raise ValueError("simulated nodes must declare module_type")
        if self.mode is not NodeMode.SIMULATED and self.module_type is not None:
            raise ValueError("module_type is only valid for simulated nodes")
        if self.rules and self.module_type != ModuleType.OBC_PEER.value:
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
        from cubesat_testbed.modules.registry import module_registration

        # Guaranteed non-None: params is non-empty only when mode is
        # SIMULATED, and SIMULATED requires module_type (checked above).
        assert self.module_type is not None
        config_cls = module_registration(self.module_type).config_cls
        if config_cls is None:
            raise ValueError(f"module_type {self.module_type!r} does not accept params")

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
            raise ValueError(f"invalid params for module_type {self.module_type!r}: {exc}") from exc

    def telemetry_packets(self, node_name: str) -> tuple[TelemetryPacketDefinition, ...]:
        """Every telemetry packet this node beacons, single-signal mappings first.

        Reply-only packets (``periodic = false``) are not telemetry and are left out.

        A ``telemetry`` mapping is a packet of its own holding one field; a ``packets``
        entry holds several. Both come out in the same shape, in setup order.
        """

        definitions = [
            TelemetryPacketDefinition(
                node_name=node_name,
                label=f"{node_name}.{mapping_name}",
                source_port=mapping.source_port,
                destination_port=mapping.destination_port,
                to=mapping.to,
                priority=mapping.priority,
                flags=mapping.flags,
                fields=((mapping_name, mapping.resolved_signal(node_name, mapping_name), mapping),),
            )
            for mapping_name, mapping in self.telemetry.items()
        ]
        definitions.extend(
            TelemetryPacketDefinition(
                node_name=node_name,
                label=f"{node_name}.packets.{packet_name}",
                source_port=packet.source_port,
                destination_port=packet.destination_port,
                to=packet.to,
                priority=packet.priority,
                flags=packet.flags,
                fields=tuple(
                    (field_name, field.resolved_signal(node_name, field_name), field)
                    for field_name, field in packet.fields.items()
                ),
            )
            for packet_name, packet in self.packets.items()
            # Validated: a periodic packet has both ports; a reply-only one is not telemetry.
            if packet.periodic
            and packet.source_port is not None
            and packet.destination_port is not None
        )
        return tuple(definitions)


BusFaultType: TypeAlias = Literal["drop", "delay", "corrupt", "duplicate", "reorder"]

BUS_FAULT_DEFAULT_HOLD_US = 1_000_000
"""How long a ``reorder`` rule holds a frame back at most, in virtual microseconds."""


class BusFaultRule(BaseModel):
    """One explicit fault the in-memory bus applies to the frames passing through it.

    A rule says *which* frames (``direction`` plus optional ``port``/``source_port``/
    ``source``/``destination`` selectors), *when* (``nth``/``count``/``probability``)
    and *what happens* (``type``). It is passive data: the bus applies it, nothing
    here decides anything. ``port`` is the CSP destination port; every fragment of a
    multi-frame packet is matched by the header of the packet it belongs to.

    ``nth`` is 1-based and counts the frames that matched this rule's selectors and
    reached it; the rule fires on that frame and on the next ``count - 1`` (default
    1). Without ``nth`` it fires on every matching frame (at most ``count`` times if
    given), or on each with ``probability`` drawn from the run's seeded generator.
    """

    model_config = _SCHEMA_MODEL_CONFIG

    type: BusFaultType
    name: str | None = None
    direction: Literal["tx", "rx"] = "tx"
    port: CspPort | None = None
    source_port: CspPort | None = None
    source: CspAddress | None = None
    destination: CspAddress | None = None
    nth: int | None = Field(default=None, ge=1, strict=True)
    count: int | None = Field(default=None, ge=1, strict=True)
    probability: float | None = Field(default=None, gt=0.0, le=1.0)
    delay: VirtualDuration | None = None
    hold: VirtualDuration | None = None
    byte: int | None = Field(default=None, ge=0, strict=True)
    mask: int | None = Field(default=None, ge=1, le=255, strict=True)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier("bus fault name", value)

    @model_validator(mode="after")
    def _validate_rule(self) -> Self:
        if self.nth is not None and self.probability is not None:
            raise ValueError("a bus fault rule takes either nth or probability, not both")
        if self.type == "delay":
            if self.delay is None or self.delay <= 0:
                raise ValueError("a delay bus fault needs a positive delay, for example '200ms'")
        elif self.delay is not None:
            raise ValueError(f"delay is only valid for a delay bus fault, not {self.type!r}")
        if self.hold is not None:
            if self.type != "reorder":
                raise ValueError(f"hold is only valid for a reorder bus fault, not {self.type!r}")
            if self.hold <= 0:
                raise ValueError("hold must be positive")
        if self.type != "corrupt" and (self.byte is not None or self.mask is not None):
            raise ValueError(
                f"byte and mask are only valid for a corrupt bus fault, not {self.type!r}"
            )
        return self


class BusFaultsConfig(BaseModel):
    """``[bus_faults]``: the seed and the rules in force from the start of the run.

    Every random choice (``probability``, the byte a ``corrupt`` rule picks when it
    names none) is drawn from a generator seeded from ``seed`` and the rule's
    position, never from wall-clock time, so the same seed reproduces the same run.
    Scenario ``inject_bus_fault`` steps add rules after these ones.
    """

    model_config = _SCHEMA_MODEL_CONFIG

    seed: int = Field(default=0, ge=0, strict=True)
    rules: tuple[BusFaultRule, ...] = ()


class TestbedConfig(BaseModel):
    """Validated static satellite/testbed setup loaded from TOML."""

    model_config = _SCHEMA_MODEL_CONFIG

    protocol_version: Literal[1, 2] = 2
    """CSP protocol version of the whole setup: every node and every frame is v1 or v2."""
    transport: TransportConfig
    nodes: dict[str, NodeConfig] = Field(min_length=1)
    bus_faults: BusFaultsConfig | None = None

    @model_validator(mode="after")
    def _validate_cross_references(self) -> Self:
        self._validate_node_names()
        self._validate_unique_addresses()
        self._validate_transport_mode_combination()
        self._validate_protocol_version()
        self._validate_bus_faults_transport()
        self._validate_command_mappings()
        self._validate_telemetry_mappings()
        self._validate_requests_and_services()
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

    def telemetry_packets(self) -> tuple[TelemetryPacketDefinition, ...]:
        """Every node's telemetry packets, in setup order."""

        return tuple(
            definition
            for node_name, node in self.nodes.items()
            for definition in node.telemetry_packets(node_name)
        )

    def telemetry_signal_names(self) -> frozenset[str]:
        """Return all explicit and derived telemetry signal paths."""

        return frozenset(self.telemetry_signal_counts())

    def telemetry_signal_counts(self) -> dict[str, int]:
        """Return each telemetry signal path with its element count (1 for a scalar)."""

        return {
            signal: layout.count
            for definition in self.telemetry_packets()
            for _name, signal, layout in definition.fields
        }

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

    def _validate_protocol_version(self) -> None:
        if self.protocol_version != 1:
            return
        for node_name, node in self.nodes.items():
            if node.address > CSP_V1_ADDRESS_MAX:
                raise ValueError(
                    f"node {node_name!r} has CSP address {node.address}, but CSP v1 addresses "
                    f"are 5 bits wide (0-{CSP_V1_ADDRESS_MAX})"
                )
            if node.address == CSP_V1_BROADCAST_ADDRESS:
                raise ValueError(
                    f"node {node_name!r} has CSP address {node.address}, the CSP v1 broadcast "
                    f"address; the highest usable node address is {CSP_V1_BROADCAST_ADDRESS - 1}"
                )

    @staticmethod
    def _validate_telemetry_routes_are_distinct(
        node_name: str, definitions: tuple[TelemetryPacketDefinition, ...]
    ) -> None:
        """Reject two of a node's packets that a receiver could not tell apart.

        A packet is matched by the sender, the two ports and, if it names one with ``to``,
        the destination. Two packets that match the same frames would be one packet read
        two ways: the run used to abort at the first telemetry frame with "matches multiple
        configured routes". A packet without ``to`` matches whatever the destination is, so
        it overlaps every other packet on the same ports. Signals that belong together go
        in one ``[nodes.<node>.packets.<name>]``.
        """

        seen: dict[tuple[int, int], list[tuple[str, str | None]]] = {}
        for definition in definitions:
            addressing = definition
            ports = (addressing.source_port, addressing.destination_port)
            for other_label, other_to in seen.get(ports, []):
                if addressing.to is None or other_to is None or addressing.to == other_to:
                    raise ValueError(
                        f"telemetry mappings {other_label} and {definition.label} cannot be "
                        f"told apart: both are sent by node {node_name!r} on "
                        f"source_port={addressing.source_port} "
                        f"destination_port={addressing.destination_port}"
                        + (
                            ""
                            if addressing.to is None and other_to is None
                            else " to overlapping destinations"
                        )
                        + "; each is its own packet, so put the signals in one "
                        f"[nodes.{node_name}.packets.<name>] or give one a different port "
                        "(or a different `to`)"
                    )
            seen.setdefault(ports, []).append((definition.label, addressing.to))

    def _validate_bus_faults_transport(self) -> None:
        if self.bus_faults is not None and not isinstance(self.transport, InMemoryTransportConfig):
            raise ValueError(
                "[bus_faults] requires transport.type='in-memory': the frames of a real "
                "bus, serial line or ZeroMQ hub run on wall-clock time, so a fault there "
                "could not be replayed from a seed"
            )

    def _validate_command_mappings(self) -> None:
        commands_by_wire: dict[tuple[str, str, int, int, bytes], str] = {}
        for node_name, node in self.nodes.items():
            for command_name, command in node.commands.items():
                _validate_identifier("command name", command_name)
                if command.target not in self.nodes:
                    raise ValueError(
                        f"command {node_name}.{command_name} targets unknown node "
                        f"{command.target!r}"
                    )
                # Two commands that put the same bytes on the wire are one command to a
                # receiver, which could never tell which was sent. Commands that share ports
                # and differ in payload (power on, power off) are the normal case and fine.
                wire = (
                    node_name,
                    command.target,
                    command.destination_port,
                    command.source_port,
                    command.payload_hex,
                )
                twin = commands_by_wire.setdefault(wire, f"{node_name}.{command_name}")
                if twin != f"{node_name}.{command_name}":
                    raise ValueError(
                        f"commands {twin} and {node_name}.{command_name} send the same bytes "
                        f"from node {node_name!r} to {command.target!r} on destination_port="
                        f"{command.destination_port} source_port={command.source_port}, so a "
                        "receiver cannot tell them apart; give one a different payload_hex "
                        "or port"
                    )
                # Only the sender's limit is a config error: its libcsp could not
                # even build the packet. A target with a smaller buffer is a real
                # situation to simulate, so that packet is sent and the target
                # drops it, visibly, at run time.
                limit = node.max_packet_bytes
                # A KISS frame's 4-byte CRC32 shares the packet buffer with the
                # payload, but only a command that can leave on the line (one whose
                # target is not simulated) is ever framed; a command to a simulated
                # node is delivered inside the testbed and carries no CRC32.
                leaves_on_kiss_line = (
                    isinstance(self.transport, KissTransportConfig)
                    and self.nodes[command.target].mode is not NodeMode.SIMULATED
                )
                payload_limit = max(0, limit - KISS_CRC32_BYTES) if leaves_on_kiss_line else limit
                if len(command.payload_hex) > payload_limit:
                    crc_note = (
                        f" (the {KISS_CRC32_BYTES}-byte KISS CRC32 shares that buffer, leaving "
                        f"{payload_limit} for the payload)"
                        if leaves_on_kiss_line
                        else ""
                    )
                    raise ValueError(
                        f"command {node_name}.{command_name} payload_hex is "
                        f"{len(command.payload_hex)} bytes, over the sending node "
                        f"{node_name!r} max_packet_bytes={limit}{crc_note}"
                    )

    def _validate_telemetry_mappings(self) -> None:
        # Imported lazily for the reason given in TelemetryFieldLayout._validate_wire_layout.
        from cubesat_testbed.protocol.signal_codec import SignalCodecError, SignalLayout
        from cubesat_testbed.protocol.telemetry_codec import (
            TelemetryCodecError,
            signal_field_for_mapping,
        )

        owners_by_signal: dict[str, str] = {}
        for node_name, node in self.nodes.items():
            for telemetry_name in node.telemetry:
                _validate_identifier("telemetry mapping name", telemetry_name)
            for packet_name, packet in node.packets.items():
                _validate_identifier("telemetry packet name", packet_name)
                for field_name in packet.fields:
                    _validate_identifier("telemetry field name", field_name)
            definitions = node.telemetry_packets(node_name)
            self._validate_telemetry_routes_are_distinct(node_name, definitions)
            for definition in definitions:
                to = definition.to
                if to is not None and to not in self.nodes:
                    raise ValueError(
                        f"telemetry mapping {definition.label} is addressed to unknown node {to!r}"
                    )
                try:
                    SignalLayout(
                        tuple(
                            signal_field_for_mapping(name, layout)
                            for name, _signal, layout in definition.fields
                        )
                    )
                except (SignalCodecError, TelemetryCodecError) as exc:
                    raise ValueError(f"telemetry packet {definition.label}: {exc}") from exc
                wire_length = definition.payload_length
                if wire_length > node.max_packet_bytes:
                    raise ValueError(
                        f"telemetry mapping {definition.label} needs a "
                        f"{wire_length}-byte payload, over {node_name!r} "
                        f"max_packet_bytes={node.max_packet_bytes}"
                    )
                for name, signal, _layout in definition.fields:
                    # A shorthand mapping is labelled by its own name; a packet field by both.
                    owner = (
                        definition.label
                        if definition.label == f"{node_name}.{name}"
                        else f"{definition.label}.{name}"
                    )
                    _validate_path("telemetry signal", signal, min_segments=3)
                    if not signal.startswith(f"{node_name}.telemetry."):
                        raise ValueError(
                            f"telemetry mapping {owner} resolves to {signal!r}; "
                            f"expected it to start with {node_name}.telemetry."
                        )
                    previous_owner = owners_by_signal.setdefault(signal, owner)
                    if previous_owner != owner:
                        raise ValueError(
                            f"telemetry signal {signal!r} is mapped by both "
                            f"{previous_owner} and {owner}"
                        )
        self._validate_rule_signals()

    def _validate_requests_and_services(self) -> None:
        """Check services, requests and reply-only packets, and the ports they reserve.

        A node answers a request on its destination port and replies from it, so that port
        must not also be where the node takes a command or sends telemetry from: a frame on
        it would be two things at once.
        """

        # Imported lazily for the reason given in TelemetryFieldLayout._validate_wire_layout.
        from cubesat_testbed.protocol.signal_codec import SignalCodecError, SignalLayout
        from cubesat_testbed.protocol.telemetry_codec import (
            TelemetryCodecError,
            signal_field_for_mapping,
        )

        for node_name, node in self.nodes.items():
            if node.services is not None and node.mode is not NodeMode.SIMULATED:
                raise ValueError(
                    f"[nodes.{node_name}.services] is only for simulated nodes; a real "
                    f"{node.mode.value} node answers (or not) on its own"
                )
            for packet_name, packet in node.packets.items():
                if packet.periodic:
                    continue
                for field_name in packet.fields:
                    _validate_identifier("telemetry field name", field_name)
                label = f"{node_name}.packets.{packet_name}"
                try:
                    layout = SignalLayout(
                        tuple(
                            signal_field_for_mapping(name, field)
                            for name, field in packet.fields.items()
                        )
                    )
                except (SignalCodecError, TelemetryCodecError) as exc:
                    raise ValueError(f"telemetry packet {label}: {exc}") from exc
                if layout.required_length > node.max_packet_bytes:
                    raise ValueError(
                        f"telemetry packet {label} needs a {layout.required_length}-byte "
                        f"payload, over {node_name!r} max_packet_bytes={node.max_packet_bytes}"
                    )

            answered: dict[int, str] = {}
            if node.services is not None:
                answered.update(
                    {
                        port: f"service {name}"
                        for port, name in node.services.enabled_ports().items()
                    }
                )
            requests_by_wire: dict[tuple[int, bytes], str] = {}
            for request_name, request in node.requests.items():
                _validate_identifier("request name", request_name)
                label = f"{node_name}.requests.{request_name}"
                if request.reply not in node.packets:
                    raise ValueError(
                        f"request {label} replies with packet {request.reply!r}, which node "
                        f"{node_name!r} does not define under [nodes.{node_name}.packets]"
                    )
                twin = requests_by_wire.setdefault((request.port, request.payload_hex), label)
                if twin != label:
                    raise ValueError(
                        f"requests {twin} and {label} arrive on port {request.port} with the same "
                        "payload_hex, so the node cannot tell them apart"
                    )
                answered.setdefault(request.port, f"request {request_name}")

            for port, what in answered.items():
                for source_name, source in self.nodes.items():
                    for command_name, command in source.commands.items():
                        if command.target == node_name and command.destination_port == port:
                            raise ValueError(
                                f"command {source_name}.{command_name} is sent to {node_name!r} "
                                f"on port {port}, where {node_name!r} answers {what}"
                            )
                for definition in node.telemetry_packets(node_name):
                    if definition.source_port == port:
                        raise ValueError(
                            f"telemetry {definition.label} is sent from port {port}, where "
                            f"{node_name!r} answers {what}; a reply leaves from that port too"
                        )

    def _validate_rule_signals(self) -> None:
        """An inline OBC rule compares a number, so it cannot name a whole array."""

        counts = self.telemetry_signal_counts()
        for node_name, node in self.nodes.items():
            for rule_name, rule in node.rules.items():
                problem = rule_signal_problem(rule.signal, counts)
                if problem is not None:
                    raise ValueError(f"rule {node_name}.{rule_name}: {problem}")


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


class InjectBusFaultStep(BusFaultRule):
    """Scenario step that adds a :class:`BusFaultRule` to the in-memory bus.

    The rule's own fields sit next to ``action`` (``type`` is the kind of bus fault,
    as it is the kind of fault for ``inject_fault``). Frame counting for ``nth``
    starts when the step runs.
    """

    action: Literal["inject_bus_fault"]


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


class ReplyExpectation(BaseModel):
    """One check on a decoded reply field: ``{field: battery_voltage_v, op: ">", value: 7}``."""

    model_config = _SCHEMA_MODEL_CONFIG

    field: str
    op: AssertionOperator
    value: object

    @field_validator("field")
    @classmethod
    def _validate_field(cls, value: str) -> str:
        match = _SIGNAL_REF_RE.fullmatch(f"reply.{value}")
        if match is None:
            raise ValueError(
                "expect field must be a reply field name, or 'name[i]' for an array element"
            )
        return value


SERVICE_REPLY_FIELDS: dict[str, str] = {
    "uptime": "uptime_s",
    "memfree": "memfree_bytes",
    "buf_free": "buf_free",
}
"""The one field each value-returning service's reply decodes to, for ``expect``."""


class RequestStep(BaseModel):
    """Scenario step that sends a request and checks the reply that comes back on the bus.

    ``service`` sends one of libcsp's standard requests (``ping``, ``uptime``, ``memfree``,
    ``buf_free``) to any node; ``request`` sends one of the target's
    ``[nodes.<target>.requests.<name>]``. The step passes when a reply arrives within
    ``timeout`` (and, for ``ping``, echoes the request's bytes); each ``expect`` entry is
    then checked against a decoded reply field and reported as its own result.
    """

    model_config = _SCHEMA_MODEL_CONFIG

    action: Literal["request"]
    source: str
    target: str
    service: Literal["ping", "uptime", "memfree", "buf_free"] | None = None
    request: str | None = None
    payload_hex: bytes | None = None
    timeout: VirtualDuration = 1_000_000
    expect: tuple[ReplyExpectation, ...] = ()
    name: str | None = None

    @field_validator("source", "target")
    @classmethod
    def _validate_node(cls, value: str) -> str:
        return _validate_identifier("request node", value)

    @field_validator("request", "name")
    @classmethod
    def _validate_optional_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier("request name", value)

    @field_validator("payload_hex", mode="before")
    @classmethod
    def _coerce_payload_hex(cls, value: object) -> bytes | None:
        if value is None:
            return None
        return _coerce_hex_bytes(value)

    @model_validator(mode="after")
    def _validate_kind(self) -> Self:
        if (self.service is None) == (self.request is None):
            raise ValueError("a request step names exactly one of service or request")
        if self.service == "ping" and self.expect:
            raise ValueError("a ping reply has no fields to expect; the echo itself is checked")
        if self.service in SERVICE_REPLY_FIELDS:
            allowed = SERVICE_REPLY_FIELDS[self.service]
            for expectation in self.expect:
                if expectation.field != allowed:
                    raise ValueError(
                        f"the {self.service} reply has one field, {allowed!r}, not "
                        f"{expectation.field!r}"
                    )
        return self


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
        return _validate_signal_reference("assertion signal", value)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier("assertion name", value)


ScenarioStep: TypeAlias = Annotated[
    WaitStep | InjectFaultStep | InjectBusFaultStep | SendCommandStep | AssertStep | RequestStep,
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
    telemetry_signals = setup.telemetry_signal_counts()

    for index, step in enumerate(scenario.steps, start=1):
        prefix = f"step {index} ({step.action})"
        if isinstance(step, InjectFaultStep):
            _validate_fault_reference(step, configured_nodes, prefix, errors)
        elif isinstance(step, InjectBusFaultStep):
            if not isinstance(setup.transport, InMemoryTransportConfig):
                errors.append(
                    f"{prefix}: bus faults require transport.type='in-memory', "
                    f"not {setup.transport.type!r}"
                )
        elif isinstance(step, SendCommandStep):
            _validate_command_reference(step, setup, configured_nodes, prefix, errors)
        elif isinstance(step, AssertStep):
            problem = signal_reference_problem(step.signal, telemetry_signals)
            if problem is not None:
                errors.append(f"{prefix}: assertion {problem}")
        elif isinstance(step, RequestStep):
            _validate_request_reference(step, setup, prefix, errors)

    if errors:
        raise ScenarioReferenceError("; ".join(errors))


def _validate_request_reference(
    step: RequestStep, setup: TestbedConfig, prefix: str, errors: list[str]
) -> None:
    for role, node_name in (("source", step.source), ("target", step.target)):
        if node_name not in setup.nodes:
            errors.append(f"{prefix}: request {role} references unknown node {node_name!r}")
            return
    if setup.nodes[step.source].mode is not NodeMode.SIMULATED:
        errors.append(
            f"{prefix}: request source {step.source!r} must be a simulated node; the testbed "
            "only sends packets on behalf of the nodes it simulates"
        )
    if step.source == step.target:
        errors.append(f"{prefix}: request source and target are both {step.source!r}")
    if step.request is None:
        return
    target = setup.nodes[step.target]
    request = target.requests.get(step.request)
    if request is None:
        errors.append(
            f"{prefix}: node {step.target!r} has no request {step.request!r} "
            f"(see [nodes.{step.target}.requests])"
        )
        return
    reply = target.packets[request.reply]
    # Field names are not paths; prefix them so the signal-reference rules apply unchanged.
    counts = {f"reply.{name}": field.count for name, field in reply.fields.items()}
    for expectation in step.expect:
        problem = signal_reference_problem(f"reply.{expectation.field}", counts)
        if problem is not None:
            errors.append(
                f"{prefix}: expect on reply {step.target}.packets.{request.reply}: "
                + problem.replace("signal", "field", 1).replace("reply.", "")
            )


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


def split_signal_reference(reference: str) -> tuple[str, int | None]:
    """Split ``path`` or ``path[i]`` into the signal path and the element index."""

    match = _SIGNAL_REF_RE.fullmatch(reference)
    if match is None:
        raise ValueError(f"invalid telemetry signal reference {reference!r}")
    index = match.group("index")
    return match.group("base"), None if index is None else int(index)


def signal_reference_problem(reference: str, counts: dict[str, int]) -> str | None:
    """Why ``reference`` does not name a configured signal or array element, or ``None``.

    ``counts`` maps each configured signal to its element count (1 for a scalar).
    """

    signal, index = split_signal_reference(reference)
    if signal not in counts:
        return f"signal {reference!r} is not mapped"
    count = counts[signal]
    if index is None:
        return None
    if count == 1:
        return f"signal {reference!r} indexes {signal!r}, which is not an array"
    if index >= count:
        return (
            f"signal {reference!r} is out of range: {signal!r} has {count} elements (0-{count - 1})"
        )
    return None


def rule_signal_problem(reference: str, counts: dict[str, int]) -> str | None:
    """Why an OBC rule cannot watch ``reference``, or ``None``.

    A rule compares one number, so it may name a scalar signal or one element of an array,
    not a whole array. A signal the setup does not map at all is not reported: rules have
    always been allowed to watch telemetry nothing produces yet.
    """

    signal, index = split_signal_reference(reference)
    if signal not in counts:
        return None
    problem = signal_reference_problem(reference, counts)
    if problem is None and index is None and counts[signal] > 1:
        problem = (
            f"signal {reference!r} is an array of {counts[signal]}; a rule compares one "
            f"number, so name an element such as {signal}[0]"
        )
    return problem


def _validate_signal_reference(kind: str, value: str) -> str:
    match = _SIGNAL_REF_RE.fullmatch(value)
    if match is None:
        raise ValueError(
            f"{kind} must be a dot-separated path of identifiers, for example "
            "'eps.telemetry.voltage', or 'eps.telemetry.cells[0]' for one array element"
        )
    _validate_path(kind, match.group("base"), min_segments=3)
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
    "CSP_EPHEMERAL_PORT_MIN",
    "CSP_SERVICE_PORTS",
    "SERVICE_REPLY_FIELDS",
    "AssertStep",
    "AssertionOperator",
    "BusFaultRule",
    "BusFaultsConfig",
    "CommandMapping",
    "ConfigParseError",
    "FaultType",
    "InMemoryTransportConfig",
    "InjectBusFaultStep",
    "InjectFaultStep",
    "ModuleType",
    "NodeConfig",
    "NodeMode",
    "ObcRule",
    "ObcRuleAction",
    "ObcRuleCommandAction",
    "ObcRuleFaultAction",
    "ReplyExpectation",
    "RequestMapping",
    "RequestStep",
    "ScenarioParseError",
    "ScenarioReferenceError",
    "ScenarioScript",
    "ScenarioStep",
    "SendCommandStep",
    "ServicesConfig",
    "SocketCanTransportConfig",
    "TelemetryFieldLayout",
    "TelemetryMapping",
    "TelemetryPacket",
    "TelemetryPacketDefinition",
    "TelemetryPacketField",
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
    "rule_signal_problem",
    "signal_reference_problem",
    "split_signal_reference",
    "validate_scenario_references",
]
