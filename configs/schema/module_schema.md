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
- optional telemetry mappings under `nodes.<node>.telemetry`.

Invalid combinations fail during Pydantic validation. Examples:

- `simulated` nodes must declare `module_type`;
- `software` and `hardware` nodes must not declare `module_type`;
- `hardware` nodes require `transport.type = "socketcan"`;
- command `target` values must reference configured nodes;
- telemetry signals must resolve under `<node>.telemetry.*` and be unique;
- telemetry mappings must declare a valid byte-aligned wire layout (see
  "Signal codec v1" below) — there is no way to configure a telemetry signal
  without saying how it is encoded on the wire.

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

Telemetry mappings define the CSP ports/flags, the byte-aligned wire layout
(required — see "Signal codec v1" below), and optional passive metadata. If
`signal` is omitted, it is derived as `<node>.telemetry.<mapping-name>`.

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
- `length`: field byte length (4 or 8 for `type = "float"`; IEEE 754 binary32/
  binary64);
- `type`: `"uint"`, `"int"`, or `"float"`;
- `endian`: `"big"` (default) or `"little"`;
- `scale` / `offset_value`: physical/raw conversion, `physical = raw * scale +
  offset_value`. Integer fields require the physical value to map to an
  *exact* raw integer, so a continuously-varying value (e.g. a battery
  percentage) should generally use `type = "float"` instead of a scaled
  integer;
- optional passive metadata: `units`, `min`, `max`.

Telemetry mappings may additionally declare `enum` (raw-value-string -> label)
to bridge a non-numeric Python value to the scalar wire codec; the underlying
codec itself has no notion of enums. No bit-level packing, arrays,
Motorola/Intel bit numbering, or custom field plugins in v1.

Every telemetry mapping's `offset`/`length`/`type` are required: a configured
signal is always encoded through this layout into its own single-frame CSP
payload when transmitted, and decoded back through the same layout when
received — assertions and rules only ever see the decoded result, never a
module's raw Python value. See
[`docs/v1-scope.md`](../../docs/v1-scope.md#telemetry-wire-encoding) for the
full rationale.
