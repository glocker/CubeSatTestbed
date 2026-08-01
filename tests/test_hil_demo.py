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
import threading
import time
from pathlib import Path

import pytest

from cubesat_testbed.config import parse_testbed_config
from cubesat_testbed.main import main
from cubesat_testbed.protocol.csp_v2 import (
    CSP_PORT_PING,
    CspCanFrame,
    CspFields,
    decode_frame,
    pack,
)
from cubesat_testbed.scenario import ScenarioRunner, build_runtime
from cubesat_testbed.transport import SocketCanAdapter
from tests.example_paths import SOCKETCAN_SETUP

pytestmark = pytest.mark.socketcan

_HIL_EXAMPLE_CONFIG = SOCKETCAN_SETUP
# The shipped example's payload node: a `hardware` DUT at CSP address 3 whose
# power_status telemetry is a self-addressed single-byte enum on port 21.
_PAYLOAD_ADDRESS = 3
_PAYLOAD_TELEMETRY_PORT = 21
_PAYLOAD_OFFLINE = b"\x00"
_PEER_RESPONSE_DELAY_SECONDS = 0.4


def _interface() -> str:
    interface = os.environ.get("CUBESAT_TESTBED_SOCKETCAN_INTERFACE")
    if interface is None:
        pytest.skip("set CUBESAT_TESTBED_SOCKETCAN_INTERFACE to run SocketCAN HIL demo tests")
    return interface


def _hil_setup_file(tmp_path: Path, interface: str) -> Path:
    """Copy the shipped SocketCAN example config, pointed at ``interface``.

    Using the real example rather than an inline config keeps the file the
    README sends HIL users to on the tested path; only the interface name is
    substituted, since the environment may expose something other than
    ``vcan0``.
    """

    text = _HIL_EXAMPLE_CONFIG.read_text(encoding="utf-8")
    setup_path = tmp_path / _HIL_EXAMPLE_CONFIG.name
    setup_path.write_text(
        text.replace('interface = "vcan0"', f'interface = "{interface}"'), "utf-8"
    )
    return setup_path


def _payload_offline_scenario(tmp_path: Path) -> Path:
    scenario_path = tmp_path / "payload_offline.yaml"
    scenario_path.write_text(
        """
name: Payload reports itself offline over the real bus
steps:
  - action: assert
    name: payload_offline
    signal: payload.telemetry.power_status
    op: "=="
    value: "offline"
    timeout: "2s"
""".lstrip(),
        encoding="utf-8",
    )
    return scenario_path


def _start_payload_peer(interface: str) -> threading.Thread:
    """Answer as the payload DUT would, after a real wall-clock delay.

    The delay is the whole point: an unpaced run finishes long before it
    elapses, so this peer is only ever heard by a run that actually gives real
    hardware time to speak.
    """

    frame = pack(
        CspFields(
            priority=2,
            source=_PAYLOAD_ADDRESS,
            destination=_PAYLOAD_ADDRESS,
            destination_port=_PAYLOAD_TELEMETRY_PORT,
            source_port=_PAYLOAD_TELEMETRY_PORT,
        ),
        _PAYLOAD_OFFLINE,
    )

    def _respond(frame: CspCanFrame = frame) -> None:
        time.sleep(_PEER_RESPONSE_DELAY_SECONDS)
        subprocess.run(
            ["cansend", interface, f"{frame.can_id:08X}#{frame.data.hex()}"],
            check=True,
        )

    thread = threading.Thread(target=_respond, daemon=True)
    thread.start()
    return thread


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

    runtime = build_runtime(setup)
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
    runtime = build_runtime(setup)
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


def test_cli_realtime_run_passes_against_a_peer_answering_on_the_real_bus(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The headline HIL claim, end to end from the CLI and nothing else.

    `cubesat-testbed run --realtime` against the shipped SocketCAN example,
    with the payload DUT answering from outside this process over a real
    (virtual) CAN bus: no Python glue, no `clock=` argument, exit code 0.
    """

    interface = _interface()
    setup_path = _hil_setup_file(tmp_path, interface)
    scenario_path = _payload_offline_scenario(tmp_path)

    peer = _start_payload_peer(interface)
    try:
        exit_code = main(["run", "--realtime", "-c", str(setup_path), "-s", str(scenario_path)])
    finally:
        peer.join(timeout=5.0)

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "PASS" in captured.out
    assert "payload_offline: payload.telemetry.power_status == 'offline'" in captured.out
    assert captured.err == ""


def test_cli_run_without_realtime_outruns_the_peer_and_says_so(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same run without `--realtime`: virtual time jumps straight through
    the assertion's 2s timeout in milliseconds, so the peer's answer arrives
    long after the run is over. That is exactly the mistake the CLI warns
    about, and this test pins both halves -- the warning and the miss.
    """

    interface = _interface()
    setup_path = _hil_setup_file(tmp_path, interface)
    scenario_path = _payload_offline_scenario(tmp_path)

    peer = _start_payload_peer(interface)
    try:
        exit_code = main(["run", "-c", str(setup_path), "-s", str(scenario_path)])
    finally:
        peer.join(timeout=5.0)

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "without --realtime" in captured.err
    assert "payload.telemetry.power_status was never observed on the bus" in captured.out
