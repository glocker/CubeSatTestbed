# CubeSatTestbed

A modular CubeSat subsystem emulation and hardware-in-the-loop test framework.

![Project scheme](https://raw.githubusercontent.com/glocker/CubeSatTestbed/main/docs/images/scheme.png)

**Status:** pre-v1, under active development. See
[`docs/roadmap.md`](docs/roadmap.md) for what's implemented.

## What this is

The framework is built around a **Device Under Test (DUT)** concept: any
subsystem (OBC, EPS, ADCS, payload, ...) can be connected as real hardware,
while every other subsystem it talks to is replaced by a configurable
software peer. Switching which node is real and which is simulated is a
config change, not a code change.

## Why

- Commercial subsystem simulators are proprietary and tied to specific
  hardware.
- Full mission simulators are often too heavy for focused subsystem
  verification.
- Hardcoded stubs inside flight code do not exercise the real bus and do not
  produce scenario-level PASS/FAIL results.

## Core ideas

1. **Universal engine, not universal subsystem models.** No single "generic
   EPS" can faithfully stand in for every real EPS board. The universal part
   is the engine: DUT/peer selection, protocol and transport adapters,
   deterministic scenario execution, fault injection and assertions. Concrete
   subsystem behavior lives in modules.
2. **DUT + switchable node modes.** Every node is `simulated` (a module
   inside `cubesat_testbed` emulates the subsystem), `software` (an external
   implementation runs as a peer), or `hardware` (a real board through a bus
   adapter).
3. **Deterministic scenarios with PASS/FAIL reports.** Scenarios are
   declarative YAML scripts: inject a fault, wait in virtual time, send a
   command, assert telemetry. The runner is built on virtual time and
   produces a PASS/FAIL report per assertion.

See [`docs/architecture.md`](docs/architecture.md) for the full layer
breakdown.

## Quickstart

```sh
uv sync --extra dev
uv run cubesat-testbed run \
  --config configs/default_satellite.toml \
  --scenario configs/scenarios/low_battery.yaml
```

```text
PASS t=4000000 assert_3: payload.telemetry.power_status == 'offline'; actual='offline'
SUMMARY scenario='EPS Low Battery Protection Test' assertions=1 passed=1 failed=0 started_at=0 finished_at=4000000
```

## Walkthrough: your first scenario

Same run as Quickstart above, but step by step — what each config field means and
how to read the result.

**1. The CubeSat setup (`configs/default_satellite.toml`)**

Three nodes:

```toml
[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "simulated"
module_type = "generic_eps"
address = 2

[nodes.payload]
mode = "simulated"
module_type = "simple_payload"
address = 3
```

- `mode = "simulated"` — this node runs entirely inside the testbed. Switch to
  `"hardware"` later to point the same setup at a real board over SocketCAN —
  no code changes.
- `address` — the node's CSP address on the bus.

The behavior under test lives here:

```toml
[nodes.eps.telemetry.battery_percent]
offset = 0
length = 4
type = "float"
...

[nodes.obc.rules.low_battery_shed_payload]
signal = "eps.telemetry.battery_percent"
op = "<"
threshold = 30.0
for = "3s"

[[nodes.obc.rules.low_battery_shed_payload.actions]]
type = "send_command"
command = "payload_power_off"
```

EPS reports `battery_percent` as a 4-byte float, encoded onto the bus like a real
telemetry frame, not just held in memory. OBC has one rule: if battery stays below
30% for 3 virtual seconds, send `payload_power_off`.

**2. The scenario (`configs/scenarios/low_battery.yaml`)**

```yaml
steps:
  - action: "inject_fault"
    type: "state_override"
    target: "eps.model.battery_percent"
    value: 25
    duration: "5s"

  - action: "wait"
    virtual_time: "3s"

  - action: "assert"
    signal: "payload.telemetry.power_status"
    op: "=="
    value: "offline"
    timeout: "1s"
```

1. `inject_fault` — force EPS's battery to 25% for 5 virtual seconds (bypasses the
   discharge model to test the *reaction*, not the physics).
2. `wait` — advance virtual time 3s, giving the rule's `for = "3s"` window a
   chance to elapse.
3. `assert` — check `payload.telemetry.power_status == "offline"`, retried
   against incoming telemetry for up to 1s.

**3. Run it**

```sh
uv run cubesat-testbed run \
  --config configs/default_satellite.toml \
  --scenario configs/scenarios/low_battery.yaml
```

```text
PASS t=4000000 assert_3: payload.telemetry.power_status == 'offline'; actual='offline'
SUMMARY scenario='EPS Low Battery Protection Test' assertions=1 passed=1 failed=0 started_at=0 finished_at=4000000
```

**4. Reading it**

- `t=4000000` — virtual microseconds (4s), not 3,000,000: telemetry is only
  re-encoded once per physical step, so the earliest honest chance to see
  "offline" is the beacon *after* the command actually lands. That one-step
  delay is expected, not a bug.
- `assert_3` — auto-generated name (3rd, unnamed step); add `name:` to a step
  for a readable label.
- `passed=1 failed=0` and exit code `0` — wire straight into CI.

**5. When it fails**

Drop the `inject_fault` step (battery never actually drops) and rerun:

```text
FAIL t=4000000 assert_2: payload.telemetry.power_status == 'offline'; actual='online'
```

Exit code `1`. `actual='online'` is the real, decoded-from-the-bus value — not a
guess.

**6. Machine-readable output**

```sh
uv run cubesat-testbed run --config ... --scenario ... --json --quiet
```

```json
{
  "scenario": "EPS Low Battery Protection Test",
  "passed": true,
  "exit_code": 0,
  "assertions": {
    "total": 1, "passed": 1, "failed": 0,
    "results": [{
      "name": "assert_3",
      "signal": "payload.telemetry.power_status",
      "expected": "offline", "actual": "offline",
      "passed": true, "evaluated_at": 4000000
    }]
  }
}
```

Or `--junit-xml PATH` for CI dashboards. Full config syntax:
[`configs/schema/module_schema.md`](configs/schema/module_schema.md).

## v1 scope at a glance

- Protocol: CSP v2 only, single-frame, classic CAN 2.0, extended 29-bit IDs.
- Modules: Generic EPS, OBC Peer (rule engine), Simple Payload.
- Transports: in-memory (CI/tests) and SocketCAN (Linux HIL).
- Deterministic virtual-time engine, TOML setup + YAML scenarios, PASS/FAIL
  CLI report with CI-friendly exit codes and JUnit XML output.

Full detail, constraints, and what's deliberately out of scope for v1:
[`docs/v1-scope.md`](docs/v1-scope.md).

## Documentation

- [Architecture](docs/architecture.md)
- [Product v1 scope](docs/v1-scope.md)
- [Config schema reference](configs/schema/module_schema.md)
- [CSP golden vectors](tests/golden_vectors/README.md)
- [Roadmap](docs/roadmap.md)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

## Stack

Python `>=3.11`, Pydantic, PyYAML, python-can, `uv` for dependency/environment
management, pytest + ruff + mypy for development.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
