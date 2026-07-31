# CubeSatTestbed

A modular CubeSat subsystem emulation and hardware-in-the-loop test framework.

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
PASS t=3000000 assert_3: payload.telemetry.power_status == 'offline'; actual='offline'
SUMMARY scenario='EPS Low Battery Protection Test' assertions=1 passed=1 failed=0 started_at=0 finished_at=3000000
```

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
management, pytest + ruff + mypy for development. FastAPI/Uvicorn will be
added once the Mission Control API phase lands (see
[`docs/roadmap.md`](docs/roadmap.md)); they are not a v1 dependency.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
