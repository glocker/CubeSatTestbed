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
