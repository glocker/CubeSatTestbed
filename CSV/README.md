# CubeSat Submodules Virtualizer

## What this is
A modular CubeSat subsystem emulation and hardware-in-the-loop test framework,
built around a **Device Under Test (DUT)** concept: any subsystem (OBC, EPS,
ADCS, payload, ...) can be connected as real hardware, while every other
subsystem it talks to is replaced by a configurable software peer. Switching
which node is real and which is simulated is a config change, not a code change.

## Why
- Commercial subsystem simulators (GomSpace, EnduroSat, ...) are proprietary
  and tied to specific hardware.
- Heavier open-source stacks (NASA 42, NOS3) are full mission simulators, not
  a lightweight drop-in test harness.
- Many teams fall back to hardcoded stubs inside their own flight code
  (`if (testing) { voltage = 12.0; }`), which never exercises the real bus,
  and never gives you a pass/fail test result.

## Core ideas

### 1. A universal engine, not universal subsystem models
No single "generic EPS" can faithfully stand in for a GomSpace NanoPower, an
EnduroSat EPS, and a university team's home-built board at once — their
commands, telemetry, state machines, channel counts and protections are all
different. What *is* universal is the framework: the DUT/peer mechanism,
protocol and transport adapters, and the scenario/assertion engine. Concrete
subsystems are plugins built on top of that framework, not baked into it.

### 2. DUT + switchable node modes
Every node in a test setup has a `mode`: `simulated`, `software`, or
`hardware`. Example: to test a real OBC board, set `obc: hardware` (talking
over a real CAN adapter) while `eps: simulated` and `payload: simulated` run
as software peers. To later test a real EPS board instead, flip `eps` to
`hardware` and `obc` to `simulated` (running the OBC Peer plugin) — no code
changes, only config.

### 3. Scenario engine + assertions + PASS/FAIL reporting
Test scenarios are defined in YAML: send a command, wait, inject a fault,
assert that a telemetry signal reaches an expected value within a timeout.
The runner produces a PASS/FAIL report per assertion. This is what turns the
project from "a telemetry generator" into an actual test framework.

## Protocol support
**v1 ships CSP v2 only** (48-bit header, the current libcsp default) — this
is the primary, professional-standard protocol target for this project.

Planned for later (not built in v1, interface designed to allow it):
- CSP v1 (32-bit header) — legacy hardware compatibility
- `raw_can` — plain CAN frames via a DBC file, common in university/hobbyist
  setups that don't use libcsp
- `custom` / `codec_plugin` — user-supplied encode/decode plugin for a
  mission-specific protocol that isn't CSP or DBC-described CAN at all

## Modules (v1)
- **Generic EPS** — electrical power system
- **OBC Peer** — a rule-engine mock OBC ("if signal X crosses threshold, send
  command Y"), used when a real EPS/ADCS/payload is the DUT and needs
  something to talk to
- **Simple Payload** — basic power-draw + command/data-volume behavior,
  chosen over a thermal module as the third v1 module because it exercises
  cross-module interaction more directly

**Planned, not in v1** (placeholders only, so the idea isn't lost):
- **Thermal** — simple RC-style temperature model
- **ADCS** — attitude dynamics; a much bigger scope on its own (quaternions,
  sensor models), deliberately deferred

## Fault injection
Two categories, both discussed here but only the first is built in v1:
- **Virtual-environment faults** (v1): undervoltage, overcurrent, thermal
  spike, dropped/duplicated telemetry, delayed packets, bus-off — injected
  directly into a simulated module's model.
- **Real-device verification faults** (planned, later): when the DUT is real
  hardware, the framework can't directly force a physical fault — that needs
  lab equipment (a programmable power supply / electronic load) driven over
  SCPI, synchronized with the software side. This is a future integration,
  not part of the current scope.

Uplink is treated as a command source with its own fault scenarios (malformed
parameters, replayed/delayed commands, sequence-counter violations, commands
sent during safe mode, lost ACKs) — not a full RF/CCSDS simulation in v1.

## Architecture
Architecture described here `docs/architecture.md`.

## Stack
- Backend: Python (FastAPI, python-can, CSP v2 pack/unpack)
- Frontend: web UI for DUT/node config, fault injection, scenario runs, and
  live telemetry — built after the core engine and scenario runner work
  end-to-end via the console
- Docker Compose to run the framework + virtual CAN bus + optional OpenMCT
  bridge together

## License
TBD
