"""HIL demo: real CSP-over-CAN traffic on a real (virtual) bus.

These tests validate the P1/TO-6 "1a+1b+1c" scope: a clock abstraction, a
deadline-bound transport receive, and a DUT manager that lets ``build_runtime``
build a genuine :class:`~cubesat_testbed.transport.SocketCanAdapter` from
setup config instead of only ever building the in-memory bus. They are
skipped unless ``CUBESAT_TESTBED_SOCKETCAN_INTERFACE`` is set, matching
``tests/test_socketcan_transport.py``'s existing HIL-marker convention, and
require ``can-utils`` (``cansend``) plus a real or virtual CAN interface --
see ``sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0``.

The ping frame replayed below is the project's own committed golden vector
(``tests/golden_vectors/ping.txt`` / ``ping.meta.toml``): the exact bytes
official libcsp v2.1 (commit 48f7fb0) puts on the wire for a single-frame CSP
v2 ping from node 1 to node 2, captured via the repository-owned
``csp_client`` helper. Replaying it with ``cansend`` -- rather than spawning
that helper, which needs libraries only guaranteed present in the Docker
golden-vector environment -- keeps this test's only dependency the same
``can-utils`` the rest of the SocketCAN test suite already assumes, while
still proving genuine interop with real libcsp-produced bytes, not just this
project's own encoder round-tripping against itself.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from cubesat_testbed.config import parse_testbed_config
from cubesat_testbed.protocol.csp_v2 import CSP_PORT_PING, CspFields, decode_frame, pack
from cubesat_testbed.scenario import ScenarioRunner, build_runtime
from cubesat_testbed.transport import SocketCanAdapter

pytestmark = pytest.mark.socketcan


def _interface() -> str:
    interface = os.environ.get("CUBESAT_TESTBED_SOCKETCAN_INTERFACE")
    if interface is None:
        pytest.skip("set CUBESAT_TESTBED_SOCKETCAN_INTERFACE to run SocketCAN HIL demo tests")
    return interface


def test_build_runtime_receives_and_decodes_a_real_libcsp_frame_over_the_bus() -> None:
    interface = _interface()
    setup = parse_testbed_config(
        f"""
        [transport]
        type = "socketcan"
        interface = "{interface}"

        [nodes.obc]
        mode = "simulated"
        module_type = "obc_peer"
        address = 2
        """
    )

    runtime = build_runtime(setup, install_default_obc_rules=False)
    assert isinstance(runtime.transport, SocketCanAdapter)
    try:
        # The committed golden vector: official libcsp v2.1's exact bytes for
        # a single-frame CSP v2 ping from node 1 to node 2.
        subprocess.run(["cansend", interface, "10004083#0004148055"], check=True)

        envelope = runtime.transport.receive(timeout=2.0)
    finally:
        runtime.transport.close()

    assert envelope is not None
    packet = decode_frame(envelope.frame)
    assert packet.fields.source == 1
    assert packet.fields.destination == 2
    assert packet.fields.destination_port == CSP_PORT_PING
    assert packet.payload == b"\x55"


def test_scenario_runner_delivers_a_real_bus_command_to_a_simulated_module() -> None:
    """A command frame arriving over a real (virtual) CAN bus, not sent by
    this process, still reaches and mutates the correct simulated module --
    the same delivery path a genuine external DUT board would exercise.
    """

    interface = _interface()
    setup = parse_testbed_config(
        f"""
        [transport]
        type = "socketcan"
        interface = "{interface}"

        [nodes.obc]
        mode = "software"
        address = 1

        [nodes.payload]
        mode = "simulated"
        module_type = "simple_payload"
        address = 2

        [nodes.obc.commands.payload_power_off]
        target = "payload"
        destination_port = 10
        source_port = 10
        payload_hex = "00"
        """
    )
    runtime = build_runtime(setup, install_default_obc_rules=False)
    assert runtime.module("payload").telemetry()["power_status"] == "online"

    frame = pack(
        CspFields(priority=2, source=1, destination=2, destination_port=10, source_port=10),
        b"\x00",
    )
    runner = ScenarioRunner(runtime)
    try:
        subprocess.run(
            ["cansend", interface, f"{frame.can_id:08X}#{frame.data.hex()}"],
            check=True,
        )
        # The bus delivers asynchronously with respect to this process; poll
        # the runner's normal (non-blocking) drain path a bounded number of
        # times rather than assuming the frame is already queued the instant
        # `cansend` returns.
        for _ in range(50):
            runner.wait(0)
            if runtime.module("payload").telemetry()["power_status"] == "offline":
                break
    finally:
        runtime.transport.close()

    assert runtime.module("payload").telemetry()["power_status"] == "offline"
