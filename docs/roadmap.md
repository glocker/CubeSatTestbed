# Roadmap

**Current status:** v1.0.0 released 2026-07-31 and published to PyPI. See
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

## Planned for v1.1

v1.1 is aimed at one group of users: **flight-software engineers building
smallsats.** They work from a terminal, Linux, CI and git, they evaluate a tool
by reading its source and its wire-level output, and they already own real
hardware and real buses. The items below are ordered by how directly they
unblock that user, highest first.

1. [ ] **HIL runs from the CLI.** The CLI never passes a clock to the scenario
       runner, so it always uses the virtual clock. A setup config declaring the
       SocketCAN transport is accepted, but virtual time jumps straight through
       and a real peer never gets wall-clock time to answer — so HIL currently
       works only from the Python API with `clock=RealTimeClock()`. Add a
       `--realtime` flag, and warn when SocketCAN is configured without it.
2. [ ] **Wire-level frame trace.** Add `--trace`, writing decoded frames to
       stderr: virtual timestamp, direction, CSP header fields, raw payload
       bytes, and the signals decoded from them. Composes with
       `--json`/`--quiet`; observability only, with no effect on scenario
       semantics or determinism.
3. [ ] **Consistent release status in the docs.** The README still describes the
       project as pre-v1 even though 1.0.0 is tagged, released and on PyPI.
4. [ ] **A working install path from PyPI.** Every config and scenario currently
       lives in the repository, so there is nothing to run after
       `pip install cubesat-testbed`. Add `cubesat-testbed init` and/or ship the
       examples in the wheel, then lead the README quickstart with that path.
5. [ ] **Documented path to writing a custom module.** The architecture puts
       concrete subsystem behavior in modules, but there is no guide for
       building one. Add `docs/writing-a-module.md` plus a worked example — the
       thermal RC-style model listed as a stretch item in `docs/v1-scope.md` is
       the natural candidate.
6. [ ] **CSP v1 support.** A large share of flown and lab hardware still runs
       CSP v1 and cannot use the testbed at all today. Golden vectors first,
       codec after, exactly as for CSP v2; protocol version selectable from
       setup config.

### Release and presentation hygiene

7. [ ] Cut a GitHub Release for `v1.0.0` (the tag is pushed; no release exists)
       and make this a standing step for every future tag.
8. [ ] Record an asciinema demo of a run, including a deliberate failure and the
       resulting non-zero exit code. Worth recording only after item 2 lands.
9. [ ] Add CI, PyPI, license and supported-Python badges to the README.

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
