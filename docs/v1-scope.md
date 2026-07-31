# Product v1 scope

v1 is the first version of this product. It is intentionally narrow. See
[`docs/architecture.md`](architecture.md) for how these pieces fit together
and [`docs/roadmap.md`](roadmap.md) for what's implemented versus planned.

## Protocol support

v1 implements **CSP v2 only**, targeting:

- libcsp-compatible CSP v2 packet pack/unpack;
- classic CAN 2.0 frames with 8-byte data fields;
- extended 29-bit CAN identifiers only;
- single-frame packets only — no CSP fragmentation/reassembly.

The config exposes the logical CSP fields required by libcsp/CSP routing:
`priority`, `source`, `destination`, `destination_port`, `source_port`,
`flags`. The wire layout is locked by committed golden vectors generated from
official libcsp `v2.1` at commit `48f7fb0` — see
[`tests/golden_vectors/README.md`](../tests/golden_vectors/README.md) for the
generation workflow and fixture contract.

Planned later, not implemented in v1:

- CSP v1 for legacy hardware;
- raw CAN/DBC adapter;
- custom mission codec plugin;
- CSP fragmentation/reassembly.

This project does not reuse or expose the CAN wire protocol from the
`AetherFlow` project; the signal codec design may reuse lessons learned there,
but the v1 protocol layer is CSP v2 only.

## Modules

v1 ships three simulated modules:

- **Generic EPS** — battery/load/power-mode behavior and EPS telemetry.
- **OBC Peer** — stateless rule-engine mock OBC.
- **Simple Payload** — basic power draw and command/data-volume behavior.

Stretch / later: **Thermal** (simple RC-style model), **ADCS** (attitude
dynamics are intentionally out of v1 scope).

## Fault injection

Fault handling is split into two responsibilities.

### Fault Injection Engine

`cubesat_testbed.fault_injection` is a passive executor of external commands.
It does not know about thresholds and does not evaluate rules. It supports:

1. **Direct overrides**
   - `state_override`: override internal model state, e.g.
     `eps.model.temperature = 95.0` for `10s`.
   - `signal_override`: spoof outgoing telemetry/raw signal value, e.g.
     `eps.telemetry.voltage = 4500` for `5s`.
2. **Named fault flags** — activate a named fault inside a module, e.g.
   `battery_cell_dead`. The module's physical model reacts to the flag and
   evolves normally from that altered state.

Overrides expire by virtual time / cycles as defined by the scenario. Named
faults remain active until cleared or until the module's own logic clears
them.

### OBC Peer / Rule Engine

Threshold-triggered behavior belongs to the OBC Peer rule engine, not to the
Fault Injection Engine. The OBC Peer listens to decoded telemetry, evaluates
rules of the form `IF <condition> THEN <action>`, and can send a named CSP
command through the configured codec/bus, or call the Fault Injection Engine
to trigger a named fault or override.

v1 rule semantics: threshold conditions, `for` duration on a trigger,
`cooldown`; no ACK waiting; commands are configured as named commands mapped
to binary payloads; no stateful "on enter/on exit" rules.

## Config and scenario files

Static CubeSat/testbed setup uses TOML; scenario scripts use YAML. Both are
validated with Pydantic (`tomllib` parses the TOML). Full field reference:
[`configs/schema/module_schema.md`](../configs/schema/module_schema.md).

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

```yaml
name: "EPS Low Battery Protection Test"
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

## Signal codec v1

The v1 binary signal codec is intentionally small: byte offsets only, no
bit-level packing, `big`/`little` endianness, scalar signed/unsigned integers
and IEEE 754 floats, `scale`/`offset` conversion, and optional passive
metadata (`units`, `min`, `max`). No arrays, enums, Motorola/Intel bit
numbering, or custom field plugins in v1.

## Runtime model

v1 is deterministic by default: a central discrete-event simulation loop,
virtual time (not wall-clock sleeps), reproducible event ordering, no
background polling loops inside modules, and physical models that update on
scheduled events/timers.

Transport adapters in v1:

- `InMemoryBusAdapter` — CI/tests/local simulation without SocketCAN or root
  privileges;
- `SocketCanAdapter` — Linux HIL/Docker path through `python-can` and `vcan0`
  or a physical CAN interface.

```toml
[transport]
type = "socketcan"
interface = "vcan0" # or a physical interface such as "can0"
receive_own_messages = false
```

For local loopback on Linux, bring up `vcan0` first:

```sh
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
```

SocketCAN/HIL smoke tests are outside normal CI and run only when explicitly
given an interface:

```sh
CUBESAT_TESTBED_SOCKETCAN_INTERFACE=vcan0 uv run pytest -m socketcan
```

Hardware traffic record/replay is out of v1 scope.

## Running scenarios from CLI

```sh
uv run cubesat-testbed run \
  --config configs/default_satellite.toml \
  --scenario configs/scenarios/low_battery.yaml
```

Short flags (`-c`, `-s`) are also supported. By default, the command prints
the per-assertion PASS/FAIL report to `stdout`, then a one-line summary:

```text
PASS t=3 assert_3: payload.telemetry.power_status == 'offline'; actual='offline'
SUMMARY scenario='EPS Low Battery Protection Test' assertions=1 passed=1 failed=0 started_at=0 finished_at=3
```

`--quiet` suppresses normal `stdout` output while keeping `stderr`
warnings/errors visible. `--json` emits one machine-readable JSON object to
`stdout` instead; it takes precedence over `--quiet` if both are supplied.
Execution errors and interrupts are also reported as JSON in `--json` mode.

`--junit-xml PATH` writes a JUnit XML report to `PATH`, one `<testcase>` per
assertion with a `<failure>` element for each failed assertion, so CI systems
(GitHub Actions, GitLab CI, Jenkins, ...) can render pass/fail natively
without a custom parser. It composes with `--json`/`--quiet`, since it writes
a file rather than controlling `stdout`. Execution errors and interrupts still
produce a report — one `<testcase>` with an `<error>` element — so a CI step
that expects the file to exist does not additionally fail on a missing file.
`time` attributes are real wall-clock seconds the run took, not virtual time.

Exit codes are intended for CI/CD use:

| Exit code | Meaning |
| ---: | --- |
| `0` | Scenario executed and all assertions passed. |
| `1` | Scenario executed, but one or more assertions failed. |
| `2` | Setup/scenario loading, validation or runtime execution error. Missing files also return `2` with a concise error instead of a traceback. |
| `130` | Run was interrupted, for example by `Ctrl-C` / `KeyboardInterrupt`, without printing a traceback. |

If a scenario contains zero assertions, the CLI prints
`warning: 0 assertions in scenario` to `stderr` so a vacuously passing run is
visible in logs.

v1 CLI intentionally targets the in-memory runtime; long-running HIL flows
remain future work.
