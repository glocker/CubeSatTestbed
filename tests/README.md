# Test strategy

v1 tests are deterministic and should run without SocketCAN by default.

Coverage:

- CSP v2 codecs against repository-owned golden vectors, all captured from official
  libcsp `v2.1` at commit `48f7fb0` by C helpers built in Docker, each with a sibling
  `*.meta.toml` file for every traffic dump:
  - CAN: single-frame and multi-frame (CFP2) vectors on `vcan0`
    (`tests/golden_vectors/*.txt`), plus fragmentation and reassembly tests
    (`test_csp_v2.py`, `test_csp_v2_cfp.py`);
  - CSP v1 over CAN (CFP1), captured from the same libcsp build run as v1
    (`tests/golden_vectors/csp_v1/can/`, `test_golden_vectors_csp_v1.py`); the vectors are
    checked here against the CFP1 layout, independently of any codec;
  - CSP v1 over KISS and ZeroMQ (`tests/golden_vectors/csp_v1/{kiss,zmq}/`,
    `test_golden_vectors_csp_v1_whole_packet.py`, codecs and adapters in
    `test_csp_v1_whole_packet.py`);
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

### Interop with a real libcsp node

Tests marked `libcsp` (`test_libcsp_interop.py`) run a real libcsp node against the CLI instead of
replaying recorded bytes, on every link (CAN, KISS over a pty pair, ZeroMQ through libcsp's own
`zmqproxy`) and as CSP v1 and v2, in both directions: a libcsp node sends housekeeping that a
scenario assertion reads, and a libcsp node listening on a port receives the telemetry a simulated
EPS addresses to the OBC. They need the helpers the vector container builds (`csp_client`,
`csp_rx`, `zmqproxy` in `tests/golden_vectors/bin`) and a virtual CAN bus, which the container
creates in its own network namespace, so they run inside it, with the project's virtual
environment from the checkout (the container has the same Python as `.python-version`):

```sh
uv sync --extra dev
docker compose up -d --no-build        # or `docker compose up -d --build` where there is a network
docker compose exec -T libcsp-vectors sh -c '
  cd /app &&
  export PYTHONPATH=/app/.venv/lib/python3.11/site-packages:/app/src \
         CUBESAT_TESTBED_SOCKETCAN_INTERFACE=vcan0 \
         CUBESAT_TESTBED_LIBCSP_BIN=/app/tests/golden_vectors/bin &&
  python3 -m pytest -p no:cacheprovider -m libcsp'
```

Without `CUBESAT_TESTBED_LIBCSP_BIN` they are skipped, as the CAN cases are without
`CUBESAT_TESTBED_SOCKETCAN_INTERFACE`. Twelve cases take about eighty seconds, because the testbed
runs paced against real time. A container started from an image older than a helper does not have
that helper; compile it with the `cc` line of the `Dockerfile` (the entrypoint also overwrites
`bin/csp_client` with the image's copy on every start).
