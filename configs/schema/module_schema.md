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
- simulated node `module_type`: `generic_eps`, `obc_peer`, or `simple_payload`;
- unique CSP `address` values in the CSP v2 address range;
- optional named command mappings under `nodes.<node>.commands`;
- optional telemetry mappings under `nodes.<node>.telemetry`;
- optional OBC Peer rules under `nodes.<node>.rules` (`module_type =
  "obc_peer"` nodes only).

Invalid combinations fail during Pydantic validation. Examples:

- `simulated` nodes must declare `module_type`;
- `software` and `hardware` nodes must not declare `module_type`;
- `hardware` nodes require `transport.type = "socketcan"`;
- command `target` values must reference configured nodes;
- telemetry signals must resolve under `<node>.telemetry.*` and be unique;
- `rules` is only valid for `module_type = "obc_peer"` nodes.

Command mappings define the CSP ports/flags and optional product-v1 single-frame
payload bytes:

```toml
[nodes.obc.commands.payload_power_off]
target = "payload"
destination_port = 10
source_port = 10
priority = 2
flags = 0
payload_hex = "00"
```

Telemetry mappings define the CSP ports/flags and optional passive metadata. If
`signal` is omitted, it is derived as `<node>.telemetry.<mapping-name>`.

```toml
[nodes.eps.telemetry.battery_percent]
source_port = 20
destination_port = 20
units = "percent"
min = 0
max = 100
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

## Scenario YAML

A scenario defines ordered steps:

- `inject_fault` — state override, signal override, or named fault request;
- `wait` — advance virtual time;
- `send_command` — reference a named command from setup config;
- `assert` — evaluate telemetry against an expected condition.

Scenario parsing is separate from scenario execution. Parsers can optionally
cross-validate references against a parsed setup config before the runner starts.

Supported assertion operators are `==`, `!=`, `>`, `>=`, `<`, and `<=`. Virtual
durations (`VirtualTime`'s base unit is microseconds) must spell out an
explicit unit — `s`/`sec`/`seconds`, `ms`/`millisecond(s)`, or
`us`/`microsecond(s)` — for example `"3s"` or `"500ms"`. The bare integer `0`
is the sole exception, since it is unambiguous in any unit.

## Signal codec v1

Fields are byte-aligned only:

- `offset`: byte offset;
- `type`: scalar signed/unsigned integer or IEEE 754 float;
- `endian`: `big` or `little`;
- `scale` / `offset_value`: physical/raw conversion;
- optional passive metadata: `units`, `min`, `max`.

No bit-level packing, arrays, enums, Motorola/Intel bit numbering, or custom
field plugins in v1.
