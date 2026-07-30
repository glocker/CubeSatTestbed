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
- optional module parameter overrides under `nodes.<node>.params`.

Invalid combinations fail during Pydantic validation. Examples:

- `simulated` nodes must declare `module_type`;
- `software` and `hardware` nodes must not declare `module_type`;
- `hardware` nodes require `transport.type = "socketcan"`;
- command `target` values must reference configured nodes;
- telemetry signals must resolve under `<node>.telemetry.*` and be unique;
- `params` is only valid on `simulated` nodes and must match the target
  module's own parameter names/types/ranges.

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

A third-party module type registers itself in
`cubesat_testbed.modules.registry.MODULE_PARAM_CONFIGS` to support `params`
the same way the built-in modules do.

## Scenario YAML

A scenario defines ordered steps:

- `inject_fault` — state override, signal override, or named fault request;
- `wait` — advance virtual time;
- `send_command` — reference a named command from setup config;
- `assert` — evaluate telemetry against an expected condition.

Scenario parsing is separate from scenario execution. Parsers can optionally
cross-validate references against a parsed setup config before the runner starts.

Supported assertion operators are `==`, `!=`, `>`, `>=`, `<`, and `<=`. Virtual
durations accept non-negative integers or strings such as `3s` and `3 ticks`.

## Signal codec v1

Fields are byte-aligned only:

- `offset`: byte offset;
- `type`: scalar signed/unsigned integer or IEEE 754 float;
- `endian`: `big` or `little`;
- `scale` / `offset_value`: physical/raw conversion;
- optional passive metadata: `units`, `min`, `max`.

No bit-level packing, arrays, enums, Motorola/Intel bit numbering, or custom
field plugins in v1.
