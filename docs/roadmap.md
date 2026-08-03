# Roadmap

**Current status:** v1.1.0 released 2026-08-03 and published to PyPI. See
[`CHANGELOG.md`](../CHANGELOG.md) for the release contents and
[`docs/v1-scope.md`](v1-scope.md) for what v1 deliberately leaves out.

## Shipped in v1.0.0

### Phase 0: Dev automation

- [x] `src/cubesat_testbed` package layout.
- [x] `uv` workflow and committed `uv.lock`.
- [x] GitHub Actions baseline: Ruff format check, Ruff lint, mypy, pytest.

### Phase 1: CSP source of truth

- [x] Pin official libcsp `v2.1` at commit `48f7fb0`.
- [x] Build `tests/golden_vectors/bin/csp_client` in Docker.
- [x] Generate and commit `vcan0` golden-vector fixtures plus sibling
      `*.meta.toml` files under `tests/golden_vectors/`.
- [x] Add pytest fixture loader for golden vectors.
- [x] Implement the CSP v2 single-frame codec against the fixed vectors.

### Phase 2: Core Engine & CLI

- [x] In-memory bus adapter for CI/integration tests.
- [x] Deterministic DES core loop with virtual timeline.
- [x] TOML setup parser and YAML scenario runner schemas.
- [x] Byte-aligned signal codec.
- [x] Fault Injection Engine: state override, signal override, named faults.
- [x] Generic EPS, OBC Peer and Simple Payload modules.
- [x] Console PASS/FAIL scenario report.
- [x] SocketCAN adapter for Linux/HIL.

## Shipped in v1.1.0

v1.1 was aimed at one group of users: **flight-software engineers building
smallsats.** They work from a terminal, Linux, CI and git, they evaluate a tool
by reading its source and its wire-level output, and they already own real
hardware and real buses. The items below are ordered by how directly they
unblocked that user, highest first.

1. [x] **HIL runs from the CLI.** `run --realtime` paces the run on a
       `RealTimeClock`, so a real peer gets wall-clock time to answer; a
       SocketCAN transport configured without the flag warns on `stderr`.
       Covered end to end against `vcan0` under the `socketcan` pytest marker.
2. [x] **Wire-level frame trace.** `run --trace` writes decoded frames to
       stderr: virtual timestamp, direction, CSP header fields, raw payload
       bytes, and the command route or telemetry signal decoded from them.
       Traced at the transport boundary, so outgoing SocketCAN frames and
       frames an OBC Peer sends straight to the bus are visible too. Composes
       with `--json`/`--quiet`; observability only, with no effect on scenario
       semantics or determinism.
3. [x] **Consistent release status in the docs.** The README status line, the
       changelog's versioning note and the `Development Status` trove
       classifier now all state the same thing: 1.0.0 is tagged, released and
       on PyPI.
4. [x] **A working install path from PyPI.** The examples moved into
       `cubesat_testbed.examples` and now ship in the wheel. `run --example NAME`
       runs one in place and `init [DIR]` copies one out to edit, so
       `pip install cubesat-testbed && cubesat-testbed run --example default`
       produces a PASS with no clone. The README quickstart leads with that path.
5. [x] **Documented path to writing a custom module.**
       [`docs/writing-a-module.md`](writing-a-module.md) walks through the
       module contract, params, telemetry wire layouts and registration, with
       the `thermal_rc` module and the `thermal-heater` example as the worked
       case. The module registry became a real extension point in the same
       change: the built-ins register through the same public
       `register_module` call a third-party module uses, and
       `run --module-import` loads one from the CLI.

### Release and presentation hygiene

6. [x] Cut a GitHub Release for every pushed tag, using the matching
       `CHANGELOG.md` entry as the body. `v1.0.0` was published retroactively;
       it is a standing step in the release workflow now.
7. [x] Record a demo of a run, including a deliberate failure and the resulting
       non-zero exit code. Embedded in the README and reproducible from
       [`docs/demo/`](demo/).
8. [x] CI, PyPI, license and supported-Python badges on the README, landed
       alongside item 3.

## Next

1. [ ] **CSP v1 support.** A large share of flown and lab hardware still runs
       CSP v1 and cannot use the testbed at all today. Golden vectors first,
       codec after, exactly as for CSP v2; protocol version selectable from
       setup config. This is the largest single expansion of addressable
       users and the most expensive item on the list: it widens the protocol
       surface the project has to keep correct permanently, which is why it
       waited until the existing product was credible.

## Deferred

### Mission Control API

A FastAPI wrapper, WebSocket telemetry streams and REST fault-injection
endpoints were previously planned as Phase 3. Deferred: they exist to serve a
UI, and there is no UI planned (below).

### Visual test harness UI

A telemetry dashboard, interactive timeline, live bus analyzer and fault control
panel were previously planned as Phase 4. Deferred, because the users v1.1
targets work from a terminal and CI, where a dashboard adds no leverage and a
large maintenance surface. `frontend/` and `bridges/openmct/` stay dormant.

If this is revisited, the likely first step is not a live UI but a
self-contained HTML run report — a single static file with the virtual-time
timeline, per-assertion results, the decoded frame trace and the telemetry byte
layout — since that serves CI artifacts and integration engineers without
requiring a server.
