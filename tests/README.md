# Test strategy

v1 tests are deterministic and should run without SocketCAN by default.

Coverage:

- CSP v2 codecs against repository-owned golden vectors, all captured from official
  libcsp `v2.1` at commit `48f7fb0` by C helpers built in Docker, each with a sibling
  `*.meta.toml` file for every traffic dump:
  - CAN: single-frame and multi-frame (CFP2) vectors on `vcan0`
    (`tests/golden_vectors/*.txt`), plus fragmentation and reassembly tests
    (`test_csp_v2.py`, `test_csp_v2_cfp.py`);
  - serial KISS on a pty (`tests/golden_vectors/kiss/`, `test_csp_v2_kiss.py`);
  - ZeroMQ through libcsp's own hub (`tests/golden_vectors/zmq/`,
    `test_csp_v2_zmq.py`);
- transport tests: the in-memory bus, SocketCAN (marked, see below), the KISS adapter
  over a real pty and the ZeroMQ adapter through an in-process hub
  (`tests/zmq_hub.py`), including scenarios run end to end over each;
- signal codec tests for byte offsets, endian handling, scalar numeric types,
  scale and offset;
- `InMemoryBusAdapter` integration tests for scenario execution in CI;
- per-module model tests for Generic EPS, OBC Peer, Simple Payload and the RC
  thermal node;
- fault injection tests for `state_override`, `signal_override`, and named fault
  flags;
- scenario runner PASS/FAIL, dropped-packet and `--trace` result tests.

The ZeroMQ transport tests need the optional `pyzmq`, which the `dev` extra installs.
The golden vectors themselves are regenerated and diffed in CI
(`.github/workflows/golden-vectors.yml`); see `tests/golden_vectors/README.md`.

SocketCAN/HIL smoke tests are marked `socketcan` and skipped unless the
environment explicitly provides a Linux CAN interface:

```sh
CUBESAT_TESTBED_SOCKETCAN_INTERFACE=vcan0 uv run pytest -m socketcan
```

Use `vcan0` for local loopback or a physical CAN interface such as `can0` after
it has been configured by the host.
