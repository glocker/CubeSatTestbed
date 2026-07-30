# CubeSatTestbed

`CubeSatTestbed` is a modular CubeSat subsystem emulation and hardware-in-the-loop test framework.

## What this is

The framework is built around a **Device Under Test (DUT)** concept: any subsystem
(OBC, EPS, ADCS, payload, ...) can be connected as real hardware, while every
other subsystem it talks to is replaced by a configurable software peer. Switching
which node is real and which is simulated is a config change, not a code change.

## Why

- Commercial subsystem simulators are proprietary and tied to specific hardware.
- Full mission simulators are often too heavy for focused subsystem verification.
- Hardcoded stubs inside flight code do not exercise the real bus and do not
  produce scenario-level PASS/FAIL results.

## Core ideas

### 1. Universal engine, not universal subsystem models

No single "generic EPS" can faithfully stand in for every real EPS board. Real
hardware differs in commands, telemetry layout, protections, channel counts and
state machines. The universal part of this project is the engine: DUT/peer
selection, protocol and transport adapters, deterministic scenario execution,
fault injection and assertions. Concrete subsystem behavior lives in modules.

### 2. DUT + switchable node modes

Every node has a `mode`:

- `simulated` — a module inside `cubesat_testbed` emulates the subsystem.
- `software` — an external software implementation runs as a peer.
- `hardware` — a real board is reached through a real bus adapter.

Example: to test a real OBC board, set `obc = hardware` and keep `eps` and
`payload` simulated. To later test a real EPS board, flip `eps = hardware` and
run `obc` as the OBC Peer module.

### 3. Deterministic scenarios with PASS/FAIL reports

Scenarios are declarative YAML scripts: inject a fault, wait in virtual time,
send a command, assert that telemetry reaches an expected value. The runner is
built on virtual time and produces a PASS/FAIL report per assertion.

## Product v1 scope

v1 is the first version of this product. It is intentionally narrow.

### Protocol support

v1 implements **CSP v2 only**.

The target runtime profile is:

- libcsp-compatible CSP v2 packet pack/unpack;
- validation against project-owned golden binary vectors generated from official
  libcsp `v2.1` at commit `48f7fb0`;
- classic CAN 2.0 frames with 8-byte data fields;
- extended 29-bit CAN identifiers only;
- single-frame packets only;
- no CSP fragmentation/reassembly in v1.

The config exposes the logical CSP fields required by libcsp/CSP routing:

- `priority`
- `source`
- `destination`
- `destination_port`
- `source_port`
- `flags`

The exact wire layout is locked by committed golden vectors, not by informal
README prose. If a packet cannot fit into one classic CAN frame under the chosen
CSP-over-CAN profile, it is rejected in v1.

#### CSP golden-vector workflow

The project does not depend on third-party packet dumps. Golden vectors are
created inside this repository from the official libcsp `v2.1` release at commit
`48f7fb0`. The actively changing `develop` branch is not used as the baseline.

The intended workflow uses `vcan0` consistently:

1. `docker compose up --build -d` builds the libcsp-based helper utility,
   installs Linux CAN tooling including `can-utils`, prepares `vcan0`, and copies
   the generated helper to `tests/golden_vectors/bin/csp_client` inside the
   mounted repository. If your host uses Compose v1, the equivalent command is
   `docker-compose up --build -d`.
2. Start a shell in the running vector container:

   ```sh
   docker compose exec libcsp-vectors sh
   ```

3. Start CAN capture inside the container:

   ```sh
   candump -n 1 vcan0 > /app/tests/golden_vectors/ping.txt &
   ```

4. Send a reference packet with the C helper:

   ```sh
   /app/tests/golden_vectors/bin/csp_client -c vcan0 -p -d 2
   ```

5. Stop capture and commit the resulting fixture under `tests/golden_vectors/`.
6. Commit a sibling metadata file next to the dump, for example
   `tests/golden_vectors/ping.meta.toml`.

Python development of `src/cubesat_testbed/protocol/csp_v2.py` starts after the
vectors are fixed. The codec must match these fixtures byte-for-byte in pytest:
extended CAN ID and raw CAN payload.

Planned later, not implemented in v1:

- CSP v1 for legacy hardware;
- raw CAN/DBC adapter;
- custom mission codec plugin;
- CSP fragmentation/reassembly.

### AetherFlow boundary

`AetherFlow` and `cubesat_testbed` are separate projects. v1 does **not** reuse
or expose the AetherFlow CAN wire protocol. The signal codec design may reuse
lessons from AetherFlow, but the v1 protocol layer is CSP v2.

### Modules

v1 modules:

- **Generic EPS** — battery/load/power-mode behavior and EPS telemetry.
- **OBC Peer** — stateless rule-engine mock OBC.
- **Simple Payload** — basic power draw and command/data-volume behavior.

Stretch / later:

- **Thermal** — simple RC-style model.
- **ADCS** — deferred; attitude dynamics are intentionally out of v1 scope.

### Fault injection

Fault handling is split into two responsibilities.

#### Fault Injection Engine

`cubesat_testbed.fault_injection` is a passive executor of external commands. It
does not know about thresholds and does not evaluate rules.

It supports:

1. **Direct overrides**
   - `state_override`: override internal model state, e.g.
     `eps.model.temperature = 95.0` for `10s`.
   - `signal_override`: spoof outgoing telemetry/raw signal value, e.g.
     `eps.telemetry.voltage = 4500` for `5s`.
2. **Named fault flags**
   - Activate a named fault inside a module, e.g. `battery_cell_dead`.
   - The module's physical model reacts to the flag and evolves normally from
     that altered state.

Overrides expire by virtual time / cycles as defined by the scenario. Named
faults remain active until cleared or until the module's own logic clears them.

#### OBC Peer / Rule Engine

Threshold-triggered behavior belongs to the OBC Peer rule engine, not to the
Fault Injection Engine.

The OBC Peer listens to decoded telemetry, evaluates rules of the form
`IF <condition> THEN <action>`, and can:

- send a named CSP command through the configured codec/bus;
- call the Fault Injection Engine to trigger a named fault or override.

v1 rule semantics:

- supports threshold conditions;
- supports `for` duration on a trigger;
- supports `cooldown`;
- does not wait for ACKs;
- commands are configured as named commands mapped to binary payloads;
- no stateful "on enter/on exit" rules in v1.

### Config and scenario files

Static CubeSat/testbed setup uses TOML:

```toml
[transport]
type = "in-memory"

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "simulated"
module_type = "generic_eps"
address = 2
```

Scenario scripts use YAML:

```yaml
name: "EPS Low Battery Protection Test"
description: "Verify that OBC shuts down payload when battery drops below 30%"

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

Pydantic validates both setup config and scenario DSL. Python `tomllib` is used
for TOML parsing.

### Running scenarios from CLI

v1 CLI runs deterministic in-memory scenarios without requiring a custom
Python wrapper script:

```sh
uv run cubesat-testbed run \
  --config configs/default_satellite.toml \
  --scenario configs/scenarios/low_battery.yaml
```

Short flags are also supported:

```sh
uv run cubesat-testbed run -c configs/default_satellite.toml -s configs/scenarios/low_battery.yaml
```

The command prints the existing per-assertion PASS/FAIL report to `stdout`, then
a one-line summary:

```text
PASS t=3 assert_3: payload.telemetry.power_status == 'offline'; actual='offline'
SUMMARY scenario='EPS Low Battery Protection Test' assertions=1 passed=1 failed=0 started_at=0 finished_at=3
```

Exit codes are intended for CI/CD use:

| Exit code | Meaning |
| ---: | --- |
| `0` | Scenario executed and all assertions passed. |
| `1` | Scenario executed, but one or more assertions failed. |
| `2` | Setup/scenario loading, validation or runtime execution error. Missing files also return `2` with a concise `stderr` error instead of a traceback. |

If a scenario contains zero assertions, the CLI prints
`warning: 0 assertions in scenario` to `stderr` so a vacuously passing run is
visible in logs.

The v1 CLI intentionally targets the in-memory runtime. Planned later CLI work
includes `--json`, `--quiet`, and explicit interrupt/exit-code handling for
long-running HIL flows.

### Signal codec v1

The v1 binary signal codec is intentionally small:

- byte offsets only;
- no bit-level packing;
- endianness: `big` and `little`;
- scalar integer and float fields only;
- signed and unsigned integers;
- IEEE 754 floats;
- `scale` / `offset` conversion between raw bytes and physical values;
- optional passive metadata: `units`, `min`, `max`.

No arrays, enums, Motorola/Intel bit numbering, or custom field plugins in v1.

### Runtime model

v1 is deterministic by default:

- central discrete-event simulation loop;
- virtual time, not wall-clock sleeps;
- reproducible event ordering;
- no background polling loops inside modules;
- physical models update on scheduled events/timers.

Transport adapters in v1:

- `InMemoryBusAdapter` — CI/tests/local simulation without SocketCAN or root
  privileges;
- `SocketCanAdapter` — Linux HIL/Docker path through `python-can` and `vcan0` or
  a physical CAN interface.

For HIL, set the setup transport to SocketCAN:

```toml
[transport]
type = "socketcan"
interface = "vcan0" # or a physical interface such as "can0"
receive_own_messages = false
```

The configured adapter can be built through `cubesat_testbed.transport.build_transport_adapter`.
For local loopback on Linux, bring up `vcan0` first, for example:

```sh
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
```

SocketCAN/HIL smoke tests are outside normal CI and run only when explicitly
provided an interface:

```sh
CUBESAT_TESTBED_SOCKETCAN_INTERFACE=vcan0 uv run pytest -m socketcan
```

Hardware traffic record/replay is out of v1 scope.

## Development workflow

`uv` is the only dependency and environment-management tool for this project.
Do not use `pip`, Poetry, Pipenv, or a second lockfile workflow for normal
development. Python is pinned for tooling by `.python-version`. Commit `uv.lock`;
CI installs from it with `uv sync --extra dev --locked`.

Local workflow:

```sh
uv sync --extra dev
uv run ruff check .
uv run mypy src
uv run pytest
```

Formatting is handled by Ruff as well; Black is not used:

```sh
uv run ruff format .
```

CI runs the same toolchain through GitHub Actions and also checks Ruff formatting.

## Project roadmap

### Phase 0: Dev automation

- [x] `src/cubesat_testbed` package layout.
- [x] `uv` workflow and committed `uv.lock`.
- [x] GitHub Actions baseline: Ruff format check, Ruff lint, mypy, pytest.

### Phase 1: CSP source of truth

- [ ] Pin official libcsp `v2.1` at commit `48f7fb0`.
- [ ] Build `tests/golden_vectors/bin/csp_client` in Docker.
- [ ] Generate and commit `vcan0` golden-vector fixtures plus sibling `*.meta.toml` files under `tests/golden_vectors/`.
- [ ] Add pytest fixture loader for golden vectors.
- [ ] Implement CSP v2 single-frame codec only after vectors are fixed.

### Phase 2: Core Engine & CLI

- [ ] In-memory bus adapter for CI/integration tests.
- [ ] Deterministic DES core loop with virtual timeline.
- [ ] TOML setup parser and YAML scenario runner schemas.
- [ ] Byte-aligned signal codec.
- [ ] Fault Injection Engine: state override, signal override, named faults.
- [ ] Generic EPS, OBC Peer and Simple Payload modules.
- [ ] Console PASS/FAIL scenario report.
- [ ] SocketCAN adapter for Linux/HIL.

### Phase 3: Mission Control API

- [ ] FastAPI wrapper around the simulation core.
- [ ] WebSocket streams for live telemetry and parsed bus events.
- [ ] REST endpoints for ad-hoc fault injection.

### Phase 4: Visual Test Harness UI

- [ ] Telemetry dashboard.
- [ ] Interactive virtual timeline.
- [ ] Live bus analyzer for parsed CSP frames.
- [ ] Fault control panel.

## Stack

- Python `>=3.11`
- Pydantic for config/scenario validation
- PyYAML for scenario files
- python-can for SocketCAN/HIL
- FastAPI/Uvicorn for later API phase
- uv for dependency/environment management
- pytest, pytest-asyncio, ruff and mypy for development
- Ruff for both linting and formatting

## License

Apache License 2.0.
