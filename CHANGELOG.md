# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `cubesat-testbed run --trace`: writes one decoded line per CAN frame to
  `stderr` -- virtual timestamp, direction, CSP v2 header fields, the raw CAN
  data and CSP payload bytes, and the configured command route or decoded
  telemetry signal the frame carries. A failing scenario can now be diagnosed
  from CLI output alone. See
  [`docs/v1-scope.md`](docs/v1-scope.md#wire-level-frame-trace).
- `TracingTransportAdapter`, a transport wrapper that traces every frame in
  both directions. It sits at the transport boundary, so it also sees frames an
  OBC Peer module sends straight to the bus, and outgoing frames on SocketCAN,
  which the adapter never receives back. `build_runtime`/`run_scenario`/
  `run_scenario_files` take a `trace` stream to enable it from the Python API.
- `cubesat-testbed run --realtime`: runs the scenario on a `RealTimeClock`
  instead of the default `VirtualClock`, pacing virtual time against
  wall-clock time so a real `software`/`hardware` peer gets a window to
  answer. Hardware-in-the-loop runs no longer require Python glue -- a
  `socketcan` setup config plus this flag is the whole path. See
  [`docs/v1-scope.md`](docs/v1-scope.md#hardware-in-the-loop-from-the-cli).
- A `stderr` warning when the setup config declares a `socketcan` transport
  but `--realtime` was not passed: the run would otherwise jump straight
  through virtual time and never hear a real peer. It stays a warning, not an
  error, because a SocketCAN setup whose nodes are all `simulated` runs
  correctly unpaced.
- `tests/test_hil_demo.py` now drives `configs/examples/socketcan_hil.toml`
  end to end through the CLI itself, with an external peer answering on
  `vcan0` in real time, and pins the unpaced run's miss-plus-warning
  behaviour alongside it.

### Changed

- The README now states the shipped status -- 1.0.0, released and on PyPI --
  instead of `pre-v1, under active development`. The changelog's versioning
  note no longer promises SemVer "once the first tag is cut"; the tag exists.
- CI, PyPI, supported-Python and license badges on the README.
- `Development Status` trove classifier raised from `4 - Beta` to
  `5 - Production/Stable`, matching the released 1.0.0. Reaches PyPI with the
  next release.

### Fixed

- `run_scenario`/`run_scenario_files` now close the transport they built when
  the scenario ends -- pass, fail or raise -- so a HIL run hands its CAN
  socket back instead of leaking it. `TransportAdapter.close()` is part of the
  transport interface, defaulting to a no-op for in-process adapters.

## [1.0.0] - 2026-07-31

First versioned release. Everything below has landed on `main` and is
included in v1.

### Added

- Deterministic discrete-event simulation engine with virtual time.
- libcsp-compatible CSP v2 single-frame codec, validated against
  repository-owned golden vectors generated from official libcsp `v2.1`.
- Dockerized golden-vector generation workflow (`vcan0` + `candump`), with a
  committed vector matrix covering priority, address truncation, payload
  bounds, CRC32/HMAC flags, and reversed source/destination, plus a nightly
  CI job that rebuilds libcsp and fails on drift from the committed fixtures.
- In-memory transport adapter for CI/tests and a SocketCAN adapter for
  Linux/HIL.
- Pydantic schemas for TOML setup config and YAML scenario scripts.
- Byte-aligned scalar signal codec (offsets, endianness, scale/offset).
- Passive fault injection engine (state override, signal override, named
  faults).
- Generic EPS, Simple Payload, and OBC Peer (rule engine) simulated modules.
- Scenario runner with deterministic PASS/FAIL assertions.
- `cubesat-testbed run` CLI with `--json`, `--quiet`, and CI-friendly exit
  codes.
- `--junit-xml PATH` CLI flag: writes a JUnit XML report (one `<testcase>`
  per assertion, `<failure>`/`<error>` elements as appropriate) so CI systems
  render PASS/FAIL natively without a custom parser. Execution errors and
  interrupts still produce a report instead of leaving no file.
- Per-node `[nodes.<node>.params]` TOML table overriding a simulated module's
  own tunable parameters (e.g. EPS battery capacity, power-mode thresholds),
  validated against that module's configuration object. Third-party module
  types register in `cubesat_testbed.modules.registry.MODULE_PARAM_CONFIGS` to
  support the same mechanism.

### Changed

- **Breaking:** `VirtualTime`'s base unit changed from virtual seconds to
  microseconds, enabling sub-second scenario timing. Every non-zero duration
  (`duration`, `virtual_time`, `timeout`, `for`, `cooldown`) now requires an
  explicit unit (`s`, `ms`, or `us`); a bare non-zero integer is rejected with
  a validation error instead of being silently reinterpreted. See
  [`docs/v1-scope.md`](docs/v1-scope.md#virtual-time-duration-units).
- The scenario runner no longer drives module ticking, telemetry emission, and
  fault-cycle advancement through a per-second Python loop inside `wait()`.
  These now run on a recurring DES engine timer, so `wait()` jumps directly to
  its target virtual time instead of stepping through it one unit at a time.
- A received CSP frame with no configured route is now a `RuntimeWarning` and
  is dropped, not a scenario-ending error — real CAN buses routinely carry
  frames the testbed does not own.
- **Breaking:** telemetry mappings (`[nodes.<node>.telemetry.<name>]`) now
  require a byte-aligned wire layout (`offset`, `length`, `type`; optional
  `endian`/`scale`/`offset_value`/`enum`). Configured telemetry is encoded
  through `cubesat_testbed.protocol.signal_codec` into its own single-frame
  CSP payload and sent on the bus every physical step; assertions and the OBC
  rule engine only ever observe telemetry by decoding that frame back, the
  same path a real bus listener or hardware peer would use. Assertions no
  longer fall back to reading a module's Python state directly. See
  [`docs/v1-scope.md`](docs/v1-scope.md#telemetry-wire-encoding).
- A telemetry signal that has not yet been observed on the bus is treated as
  a retryable "not matching yet" condition within an assertion's timeout
  window (like a real bus listener that simply hasn't seen a frame yet),
  rather than an immediate error; it only becomes a failed assertion, with an
  explicit "was never observed" detail, once the timeout is reached.
- **Breaking:** OBC Peer rules are now configured, not hardcoded. An
  `obc_peer` node's threshold rules come from its own
  `[nodes.<node>.rules.*]` setup config; the built-in `low_battery_shed_payload`
  default rule and the `--no-default-obc-rules` / `install_default_obc_rules`
  flag are removed. `configs/default_satellite.toml` now declares that rule
  explicitly. `cubesat-testbed run --rules PATH` (or `obc_rules=`/
  `build_obc_rules_from_file()` for library callers) overrides a node's rules
  wholesale from a standalone rules file, for example to run the same
  satellite/testbed setup against several different FDIR rule sets. See
  [`configs/schema/module_schema.md`](configs/schema/module_schema.md#obc-peer-rules).
- `build_runtime` (a new, more general sibling of `build_in_memory_runtime`,
  which stays as a thin backward-compatible in-memory-only wrapper) builds a
  scenario runtime from either transport type, not just in-memory: which
  nodes get a locally simulated module now comes from the new
  `cubesat_testbed.dut.manager.resolve_participants`, the single place that
  decision is made, instead of scattered inline `node.mode`/`node.module_type`
  checks.
- `TransportAdapter.receive()` accepts an optional `timeout` (real seconds) to
  block for an incoming frame instead of only ever polling once;
  `SocketCanAdapter` passes it through to `python-can`'s own blocking
  `recv(timeout=...)`, and `InMemoryBusAdapter` sleeps out the full timeout on
  an empty queue (there is no producer to wait for, but the wall-clock time
  still needs to genuinely pass for real-time pacing to work when rehearsed
  against the in-memory bus).
- New `cubesat_testbed.clock`: `VirtualClock` (default; `wait()` jumps
  straight to its target virtual time, as always) and `RealTimeClock` (paces
  `wait()` against wall-clock time, so a HIL run against a real
  `software`/`hardware` peer gets a realistic window to respond instead of
  the run blasting through virtual time instantly). Pass `clock=` to
  `run_scenario`/`run_scenario_files`/`build_runtime`. See
  [`docs/v1-scope.md`](docs/v1-scope.md#dut-selection-and-hil).
- `tests/test_hil_demo.py`: an end-to-end demo (run with
  `CUBESAT_TESTBED_SOCKETCAN_INTERFACE=vcan0 uv run pytest -m socketcan`)
  replaying the project's committed golden-vector ping over `vcan0` and
  confirming `build_runtime`'s `SocketCanAdapter` receives and decodes real
  libcsp-produced bytes, and that a command frame arriving the same way
  reaches and mutates the correct simulated module through the full scenario
  runner delivery path -- no physical board required.
