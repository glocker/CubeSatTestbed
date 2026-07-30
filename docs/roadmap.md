# Roadmap

## Phase 0: Dev automation

- [x] `src/cubesat_testbed` package layout.
- [x] `uv` workflow and committed `uv.lock`.
- [x] GitHub Actions baseline: Ruff format check, Ruff lint, mypy, pytest.

## Phase 1: CSP source of truth

- [x] Pin official libcsp `v2.1` at commit `48f7fb0`.
- [x] Build `tests/golden_vectors/bin/csp_client` in Docker.
- [x] Generate and commit `vcan0` golden-vector fixtures plus sibling
      `*.meta.toml` files under `tests/golden_vectors/`.
- [x] Add pytest fixture loader for golden vectors.
- [x] Implement the CSP v2 single-frame codec against the fixed vectors.

## Phase 2: Core Engine & CLI

- [x] In-memory bus adapter for CI/integration tests.
- [x] Deterministic DES core loop with virtual timeline.
- [x] TOML setup parser and YAML scenario runner schemas.
- [x] Byte-aligned signal codec.
- [x] Fault Injection Engine: state override, signal override, named faults.
- [x] Generic EPS, OBC Peer and Simple Payload modules.
- [x] Console PASS/FAIL scenario report.
- [x] SocketCAN adapter for Linux/HIL.

## Phase 3: Mission Control API

- [ ] FastAPI wrapper around the simulation core.
- [ ] WebSocket streams for live telemetry and parsed bus events.
- [ ] REST endpoints for ad-hoc fault injection.

## Phase 4: Visual Test Harness UI

- [ ] Telemetry dashboard.
- [ ] Interactive virtual timeline.
- [ ] Live bus analyzer for parsed CSP frames.
- [ ] Fault control panel.

## Recommended feature branches

- `feature/libcsp-golden-vectors`
- `feature/csp-v2-codec`
- `feature/in-memory-bus`
- `feature/des-engine`
- `feature/config-schema`
- `feature/signal-codec`
- `feature/fault-injection`
- `feature/modules-eps-payload`
- `feature/obc-peer-rules`
- `feature/scenario-runner`
