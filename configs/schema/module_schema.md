# Config schema draft

The v1 configuration is split into two file types:

- satellite/testbed setup: TOML;
- scenario scripts: YAML.

## Satellite setup TOML

A setup config defines:

- `transport.type`: `in-memory` or `socketcan`;
- `nodes`: named subsystem slots such as `obc`, `eps`, `payload`;
- each node's `mode`: `simulated`, `software`, or `hardware`;
- simulated node `module_type`;
- CSP address and message/command metadata required by the CSP v2 codec.

All schemas are validated with Pydantic.

## Scenario YAML

A scenario defines ordered steps:

- `inject_fault` — state override, signal override, or named fault request;
- `wait` — advance virtual time;
- `send_command` — send a named command through the configured codec/bus;
- `assert` — evaluate telemetry against an expected condition.

## Signal codec v1

Fields are byte-aligned only:

- `offset`: byte offset;
- `type`: scalar signed/unsigned integer or IEEE 754 float;
- `endian`: `big` or `little`;
- `scale` / `offset_value`: physical/raw conversion;
- optional passive metadata: `units`, `min`, `max`.

No bit-level packing, arrays, enums, Motorola/Intel bit numbering, or custom
field plugins in v1.
