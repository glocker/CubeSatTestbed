# Config schema draft

The v1 configuration is split into two file types:

- satellite/testbed setup: TOML;
- scenario scripts: YAML.

## Satellite setup TOML

A setup config defines:

- `transport.type`: `in-memory` or `socketcan`;
- `transport.interface` for `socketcan` (defaults to `vcan0`);
- `nodes`: named subsystem slots such as `obc`, `eps`, `payload`;
- each node's `mode`: `simulated`, `software`, or `hardware`;
- simulated node `module_type`: any name registered in the module registry.
  This package ships `generic_eps`, `obc_peer`, `simple_payload` and
  `thermal_rc`; a module from another package registers its own name (see
  [`docs/writing-a-module.md`](../../docs/writing-a-module.md));
- unique CSP `address` values in the CSP v2 address range;
- optional named command mappings under `nodes.<node>.commands`;
- optional telemetry mappings under `nodes.<node>.telemetry`;
- optional OBC Peer rules under `nodes.<node>.rules` (`module_type =
  "obc_peer"` nodes only).
- optional module parameter overrides under `nodes.<node>.params`.

Invalid combinations fail during Pydantic validation. Examples:

- `simulated` nodes must declare `module_type`;
- `software` and `hardware` nodes must not declare `module_type`;
- `hardware` nodes require `transport.type = "socketcan"`;
- command `target` values must reference configured nodes;
- telemetry signals must resolve under `<node>.telemetry.*` and be unique;
- telemetry mappings and packet fields must declare a valid wire layout (see
  "Signal codec" below) — there is no way to configure a telemetry signal
  without saying how it is encoded on the wire;
- the fields of one telemetry packet must not overlap, and two packets of one
  node must not share source and destination ports (unless `to` names
  different nodes), since a receiver could not tell them apart;
- an index (`signal[i]`) in an assertion or a rule must name an element of an
  array field, and a rule must not name a whole array;
- `services` is only valid on simulated nodes; a request's `reply` must name a
  packet of the same node, two requests must not share both port and payload,
  and a request or service port must not also be a port the node takes a
  command on or sends telemetry from.
- `rules` is only valid for `module_type = "obc_peer"` nodes.
- `params` is only valid on `simulated` nodes and must match the target
  module's own parameter names/types/ranges.

Command mappings define the CSP ports/flags and optional payload bytes. A payload
over 4 bytes is sent as several CFP2 frames; it must fit the sending node's
`max_packet_bytes` (default 256):

```toml
[nodes.obc.commands.payload_power_off]
target = "payload"
destination_port = 10
source_port = 10
priority = 2
flags = 0
payload_hex = "00"
```

Telemetry travels in packets. A packet with several signals — a housekeeping
struct — is declared under `nodes.<node>.packets.<name>` with the CSP
addressing, and each signal under `.fields.<field>` with its wire layout (see
"Signal codec" below):

```toml
[nodes.eps.packets.housekeeping]
source_port = 20
destination_port = 20
to = "obc"            # optional: who the frames are addressed to
priority = 2
flags = 0

[nodes.eps.packets.housekeeping.fields.battery_voltage_v]
offset = 0
length = 4
type = "float"

[nodes.eps.packets.housekeeping.fields.cell_voltages_v]
offset = 4
length = 4
count = 2             # an array: two floats, bytes 4-11
type = "float"

[nodes.eps.packets.housekeeping.fields.power_mode]
offset = 12
bit_offset = 0        # the byte's most significant bit
bit_length = 2
type = "uint"
```

A packet with `periodic = false` is never beaconed: it is only the layout of a reply
(below), takes no ports, `to`, priority or flags, and its fields are not telemetry signals.

A simulated node can answer libcsp's standard services, and any node can have requests of
its own; see [`docs/v1-scope.md`](../../docs/v1-scope.md#requests-and-replies):

```toml
[nodes.eps.services]          # simulated nodes only
ping = true                   # port 1, echo
uptime = true                 # port 6, uint32 seconds
memfree = 131072              # port 3, uint32 (this number)
buf_free = 10                 # port 5, uint32 (this number)

[nodes.eps.requests.get_housekeeping]
port = 8                      # 7-63; 0-6 are libcsp's services
payload_hex = "01"            # optional; tells requests on one port apart
reply = "housekeeping"        # a packet of this node, the reply's layout
```

A telemetry mapping is the shorthand for a packet holding one signal: the same
CSP ports/flags/`to` and the wire layout in one table, plus optional passive
metadata. If `signal` is omitted (in either form), it is derived as
`<node>.telemetry.<mapping-or-field-name>`.

```toml
[nodes.eps.telemetry.battery_percent]
source_port = 20
destination_port = 20
units = "percent"
min = 0
max = 100
offset = 0
length = 4
type = "float"
```

Non-numeric telemetry declares an `enum` table mapping each raw integer wire
value (given as a string key, since TOML table keys are strings) to a label:

```toml
[nodes.payload.telemetry.power_status]
source_port = 21
destination_port = 21
offset = 0
length = 1
type = "uint"

[nodes.payload.telemetry.power_status.enum]
0 = "offline"
1 = "online"
```

All schemas are validated with Pydantic.

### OBC Peer rules

An `obc_peer` node's threshold rules are configured, not built into the
engine. Each rule needs a `signal` (must be `<node>.telemetry.<field>`), a
numeric threshold `op` (`"<"`, `"<="`, `">"`, or `">="`, matching
`ObcPeerThresholdCondition`'s operator set — not the six scenario-assertion
operators), a `threshold`, and one or more `actions`. `for` (aliased since
`for` is a Python keyword) and `cooldown` are virtual durations, same rules as
scenario durations below; both default to `0`.

```toml
[nodes.obc.rules.low_battery_shed_payload]
signal = "eps.telemetry.battery_percent"
op = "<"
threshold = 30.0
for = "3s"
cooldown = "0s"

[[nodes.obc.rules.low_battery_shed_payload.actions]]
type = "send_command"
command = "payload_power_off"
```

An action's `type` is `"send_command"` (`command`, matching a configured named
command) or `"inject_fault"` (`fault_type`, `target`, optional `value`/
`duration`/`cycles`, validated the same way as a scenario `inject_fault` step).

A node's rules can be overridden wholesale by a caller/CLI-supplied rules
file, which reuses this exact schema but keyed by node name at the top level
instead of nested under `nodes.<node>.rules`:

```toml
[obc.low_battery_shed_payload]
signal = "eps.telemetry.battery_percent"
op = "<"
threshold = 30.0
for = "3s"

[[obc.low_battery_shed_payload.actions]]
type = "send_command"
command = "payload_power_off"
```

### Module parameters

No single "generic EPS" can faithfully stand in for every real EPS board, so
each built-in module's tunable physical parameters (battery capacity,
power-mode thresholds, and so on) are overridable per node instead of fixed in
Python:

```toml
[nodes.eps.params]
battery_capacity_wh = 40.0
low_power_threshold_percent = 25.0
```

`params` is validated by attempting to construct the target module's own
configuration object and surfacing whatever it rejects — an unknown parameter
name or an out-of-range value fails setup validation with the same error a
Python caller would get constructing that module directly, before any
scenario runs. `name` and `endpoint` are derived from the node itself and must
not be set through `params`. `obc_peer` does not accept `params`; its
configuration is rules- and command-driven, not scalar parameters.

A module type defined outside this package supports `params` the same way,
by passing its config dataclass as `config_cls` to
`cubesat_testbed.modules.registry.register_module`. Because registration
happens on import, such a module must be imported before the config is
parsed -- `cubesat-testbed run --module-import PACKAGE` from the CLI. An
unregistered `module_type` fails setup validation with an error listing the
registered names and pointing at that flag.

## Scenario YAML

A scenario defines ordered steps:

- `inject_fault` — state override, signal override, or named fault request;
- `wait` — advance virtual time;
- `send_command` — reference a named command from setup config;
- `assert` — evaluate telemetry against an expected condition;
- `request` — send a service request (`ping`, `uptime`, `memfree`, `buf_free`) or a
  configured request from a simulated node, wait for the reply and check its fields.

Scenario parsing is separate from scenario execution. Parsers can optionally
cross-validate references against a parsed setup config before the runner starts.

Supported assertion operators are `==`, `!=`, `>`, `>=`, `<`, and `<=`. Virtual
durations (`VirtualTime`'s base unit is microseconds) must spell out an
explicit unit — `s`/`sec`/`seconds`, `ms`/`millisecond(s)`, or
`us`/`microsecond(s)` — for example `"3s"` or `"500ms"`. The bare integer `0`
is the sole exception, since it is unambiguous in any unit.

## Signal codec

A field is byte-aligned or a bit field:

- `offset`: byte offset (for a bit field, the byte it starts in);
- `length`: field byte length (4 or 8 for `type = "float"`; IEEE 754 binary32/
  binary64) — byte-aligned fields only;
- `bit_offset` and `bit_length`: a bit field instead of `length`. `bit_offset`
  is 0-7, counted from the most significant bit of the byte at `offset`;
  `bit_length` is 1-64, and the field is read most significant bit first,
  across byte boundaries if needed (Kaitai Struct's `bN`, CCSDS). `type` is
  `"uint"` or `"int"` (two's complement over `bit_length`); `endian` does not
  apply and is refused;
- `count`: elements of a fixed-length array (default 1, a scalar). Elements sit
  back to back, each `length` bytes or `bit_length` bits; the value is a tuple,
  and `<signal>[i]` names element `i` in assertions and OBC rules;
- `type`: `"uint"`, `"int"`, or `"float"`;
- `endian`: `"big"` (default) or `"little"`, byte-aligned fields only;
- `scale` / `offset_value`: physical/raw conversion, `physical = raw * scale +
  offset_value`, per element. Integer fields require the physical value to map
  to an *exact* raw integer, so a continuously-varying value (e.g. a battery
  percentage) should generally use `type = "float"` instead of a scaled
  integer. A Python `bool` packs as 1/0 into an integer field;
- optional passive metadata: `units`, `min`, `max`.

Fields may also declare `enum` (raw-value-string -> label) to bridge a
non-numeric Python value to the wire codec; the codec itself only knows numbers.
Not supported: little-endian bit numbering, nested structs, variant records or
custom field plugins.

Every field's layout is required: a configured signal is always encoded through
it into its packet (several CAN frames when the payload is over 4 bytes) when
transmitted, and decoded back through the same layout when received —
assertions and rules only ever see the decoded result, never a module's raw
Python value. See
[`docs/v1-scope.md`](../../docs/v1-scope.md#telemetry-wire-encoding) for the
full rationale.
