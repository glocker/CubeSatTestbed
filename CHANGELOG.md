# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning will follow [Semantic Versioning](https://semver.org/) once the
first tag is cut.

## [Unreleased]

Product v1 is not tagged yet. Everything below has landed on `main` but is
still pre-release.

### Added

- Deterministic discrete-event simulation engine with virtual time.
- libcsp-compatible CSP v2 single-frame codec, validated against
  repository-owned golden vectors generated from official libcsp `v2.1`.
- Dockerized golden-vector generation workflow (`vcan0` + `candump`).
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
