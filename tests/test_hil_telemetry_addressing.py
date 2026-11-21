"""Telemetry addressing on a real (virtual) CAN bus (known bug 13).

Both directions of the problem the in-process tests cannot show on their own, because the
in-memory bus hands every node every frame: a board's housekeeping addressed to the OBC must
reach an assertion, and a simulated node's telemetry must carry the address its receiver
filters on. Skipped unless ``CUBESAT_TESTBED_SOCKETCAN_INTERFACE`` names a CAN interface;
needs ``can-utils`` (``cansend``, ``candump``).
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from cubesat_testbed.main import main
from cubesat_testbed.protocol import csp_v1
from cubesat_testbed.protocol.csp_v1 import CspV1Fields
from cubesat_testbed.protocol.csp_v2 import CspFields, fragment_packet

pytestmark = pytest.mark.socketcan

_PEER_DELAY_SECONDS = 0.4

_BOARD_SETUP = """
{version_line}
[transport]
type = "socketcan"
interface = "{interface}"

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "hardware"
address = 2

[nodes.eps.telemetry.battery_percent]
source_port = 20
destination_port = 20
offset = 0
length = 4
type = "float"
"""

_BOARD_SCENARIO = """
name: a real board's housekeeping reaches the OBC
steps:
  - action: assert
    signal: eps.telemetry.battery_percent
    op: "=="
    value: 25.0
    timeout: 3s
"""

_SIMULATED_SETUP = """
{version_line}
[transport]
type = "socketcan"
interface = "{interface}"

[nodes.obc]
mode = "software"
address = 1

[nodes.eps]
mode = "simulated"
module_type = "generic_eps"
address = 2

[nodes.eps.telemetry.battery_percent]
source_port = 10
destination_port = 10
to = "obc"
offset = 0
length = 4
type = "float"
"""

_SIMULATED_SCENARIO = """
name: simulated housekeeping on the bus
steps:
  - action: wait
    virtual_time: 2s
"""


def _interface() -> str:
    interface = os.environ.get("CUBESAT_TESTBED_SOCKETCAN_INTERFACE")
    if interface is None:
        pytest.skip("set CUBESAT_TESTBED_SOCKETCAN_INTERFACE to run the SocketCAN HIL tests")
    return interface


def _version_line(version: int) -> str:
    return "" if version == 2 else "protocol_version = 1\n"


def _board_frames(version: int) -> list[tuple[int, bytes]]:
    """A board at node 2 sending 25.0 to the OBC (node 1), framed as the version does."""

    payload = bytes.fromhex("41c80000")
    if version == 1:
        frames = csp_v1.fragment_packet(CspV1Fields(2, 2, 1, 20, 20), payload, identification=0)
    else:
        frames = fragment_packet(CspFields(2, 2, 1, 20, 20), payload)
    return [(frame.can_id, frame.data) for frame in frames]


@pytest.mark.parametrize("version", [1, 2])
def test_a_boards_housekeeping_addressed_to_the_obc_reaches_an_assertion(
    tmp_path: Path, version: int
) -> None:
    interface = _interface()
    setup = tmp_path / "setup.toml"
    scenario = tmp_path / "scenario.yaml"
    setup.write_text(_BOARD_SETUP.format(version_line=_version_line(version), interface=interface))
    scenario.write_text(_BOARD_SCENARIO)
    frames = _board_frames(version)

    def _board() -> None:
        time.sleep(_PEER_DELAY_SECONDS)
        for can_id, data in frames:
            subprocess.run(["cansend", interface, f"{can_id:08X}#{data.hex()}"], check=True)

    peer = threading.Thread(target=_board, daemon=True)
    peer.start()

    code = main(["run", "-c", str(setup), "-s", str(scenario), "--realtime", "--quiet"])
    peer.join(timeout=5)

    assert code == 0


@pytest.mark.parametrize("version", [1, 2])
def test_a_simulated_nodes_telemetry_carries_the_address_its_receiver_filters_on(
    tmp_path: Path, version: int
) -> None:
    interface = _interface()
    setup = tmp_path / "setup.toml"
    scenario = tmp_path / "scenario.yaml"
    setup.write_text(
        _SIMULATED_SETUP.format(version_line=_version_line(version), interface=interface)
    )
    scenario.write_text(_SIMULATED_SCENARIO)
    dump = subprocess.Popen(["candump", "-n", "1", interface], stdout=subprocess.PIPE, text=True)
    time.sleep(0.3)

    code = main(["run", "-c", str(setup), "-s", str(scenario), "--realtime", "--quiet"])
    try:
        output, _ = dump.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        dump.kill()
        pytest.fail("the simulated EPS put no telemetry on the bus")

    assert code == 0
    can_id = int(output.split()[1], 16)
    # The destination field of the CAN identifier: CFP2 bits 13-26, CFP1 bits 19-23.
    destination = (can_id >> 13) & 0x3FFF if version == 2 else (can_id >> 19) & 0x1F
    assert destination == 1
