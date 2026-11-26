"""A real libcsp node and the testbed, on every link and in both CSP versions.

Everything else in the suite checks the testbed against *recorded* libcsp bytes. These tests run
the real thing: the repository-built ``csp_client`` (a libcsp node that sends), ``csp_rx`` (a
libcsp node that listens, with libcsp's own destination filter, or answers libcsp's services) and
``csp_ping`` (libcsp's own ``csp_ping()``), against the CLI, over a real
(virtual) CAN bus, a KISS serial line and a ZeroMQ hub, as CSP v1 and as CSP v2:

- *libcsp to testbed*: a real node 2 sends housekeeping to the OBC (node 1) and a scenario
  assertion reads it;
- *testbed to libcsp*: a simulated EPS addresses its telemetry to the OBC with ``to``, and a real
  libcsp node 1 listening on a port receives it;
- *requests*: a real node 1 pings a simulated EPS with ``csp_ping()`` and gets the echo, and a
  scenario's ``request`` steps ping a real node 2 and read its ``buf_free`` reply.

They need the helpers the vector container builds, so they are skipped unless
``CUBESAT_TESTBED_LIBCSP_BIN`` names the directory holding ``csp_client`` and ``csp_rx``
(``tests/golden_vectors/bin``) -- see ``tests/README.md``. The CAN cases also need
``CUBESAT_TESTBED_SOCKETCAN_INTERFACE``; the ZeroMQ cases also need ``zmqproxy`` in that
directory and ``pyzmq``. Each run takes several wall-clock seconds, because the testbed runs
paced against real time.
"""

from __future__ import annotations

import contextlib
import os
import pty
import select
import subprocess
import threading
import time
import tty
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from cubesat_testbed.main import main

pytestmark = pytest.mark.libcsp

LIBCSP_FIRST_SOURCE_PORT = 18
"""The source port libcsp picks for its first outgoing connection in the default build."""
TELEMETRY_PORT = 10
"""A port a real libcsp node can bind: it binds none above CSP_PORT_MAX_BIND (16)."""
RECEIVER_SECONDS = 9


@dataclass(frozen=True)
class Link:
    """What differs per link: the setup's transport block and how libcsp attaches to it."""

    transport: str
    client_args: tuple[str, ...]
    receiver_spec: str


def _helper(name: str) -> Path:
    directory = os.environ.get("CUBESAT_TESTBED_LIBCSP_BIN")
    if directory is None:
        pytest.skip("set CUBESAT_TESTBED_LIBCSP_BIN to the directory with csp_client and csp_rx")
    path = Path(directory) / name
    if not path.is_file():
        pytest.skip(f"{path} is missing; build the helpers in the vector container")
    return path


@contextlib.contextmanager
def _can_link() -> Iterator[Link]:
    interface = os.environ.get("CUBESAT_TESTBED_SOCKETCAN_INTERFACE")
    if interface is None:
        pytest.skip("set CUBESAT_TESTBED_SOCKETCAN_INTERFACE to run the CAN interop cases")
    yield Link(
        transport=f'type = "socketcan"\ninterface = "{interface}"',
        client_args=("-c", interface),
        receiver_spec=f"can:{interface}",
    )


class _PtyRelay:
    """Two ptys joined so the testbed and libcsp can talk over a serial line."""

    def __init__(self) -> None:
        self._master_a, slave_a = pty.openpty()
        self._master_b, slave_b = pty.openpty()
        tty.setraw(slave_a)
        tty.setraw(slave_b)
        self.path_a = os.ttyname(slave_a)
        self.path_b = os.ttyname(slave_b)
        self._slaves = (slave_a, slave_b)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            ready, _, _ = select.select([self._master_a, self._master_b], [], [], 0.1)
            for fd in ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    return
                os.write(self._master_b if fd == self._master_a else self._master_a, data)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        for fd in (self._master_a, self._master_b, *self._slaves):
            os.close(fd)


@contextlib.contextmanager
def _kiss_link() -> Iterator[Link]:
    relay = _PtyRelay()
    try:
        yield Link(
            transport=f'type = "kiss"\ndevice = "{relay.path_a}"',
            client_args=("-k", relay.path_b),
            receiver_spec=f"kiss:{relay.path_b}",
        )
    finally:
        relay.close()


@contextlib.contextmanager
def _zmq_link() -> Iterator[Link]:
    pytest.importorskip("zmq")
    proxy = subprocess.Popen(
        [str(_helper("zmqproxy"))], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        time.sleep(1.0)
        if proxy.poll() is not None:
            pytest.skip("zmqproxy did not start; its ports 6000 and 7000 may be in use")
        yield Link(
            transport=(
                'type = "zmq"\npublish_endpoint = "tcp://127.0.0.1:6000"\n'
                'subscribe_endpoint = "tcp://127.0.0.1:7000"\nsettle_seconds = 0.5'
            ),
            client_args=("-z", "127.0.0.1"),
            receiver_spec="zmq:127.0.0.1",
        )
    finally:
        proxy.terminate()
        proxy.wait(timeout=5)


LINKS: dict[str, Callable[[], contextlib.AbstractContextManager[Link]]] = {
    "can": _can_link,
    "kiss": _kiss_link,
    "zmq": _zmq_link,
}


def _version_line(version: int) -> str:
    return "" if version == 2 else "protocol_version = 1\n"


BOARD_SETUP = """
{version_line}
[transport]
{transport}

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "software"
address = 2

[nodes.eps.telemetry.housekeeping]
source_port = {source_port}
destination_port = 1
offset = 0
length = 1
type = "uint"
"""

BOARD_SCENARIO = """
name: a libcsp node reports to the OBC
steps:
  - action: wait
    virtual_time: 1s
  - action: assert
    signal: eps.telemetry.housekeeping
    op: "=="
    value: 85
    timeout: 6s
"""

SIMULATED_SETUP = """
{version_line}
[transport]
{transport}

[nodes.obc]
mode = "software"
address = 1

[nodes.eps]
mode = "simulated"
module_type = "generic_eps"
address = 2

[nodes.eps.telemetry.battery_percent]
source_port = {port}
destination_port = {port}
to = "obc"
offset = 0
length = 4
type = "float"
"""

SIMULATED_SCENARIO = """
name: simulated EPS reports to a libcsp OBC
steps:
  - action: wait
    virtual_time: 4s
"""


def _run_cli(tmp_path: Path, setup: str, scenario: str) -> int:
    setup_path = tmp_path / "setup.toml"
    scenario_path = tmp_path / "scenario.yaml"
    setup_path.write_text(setup)
    scenario_path.write_text(scenario)
    return main(["run", "-c", str(setup_path), "-s", str(scenario_path), "--realtime", "--quiet"])


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("link_name", list(LINKS))
def test_a_real_libcsp_node_reports_to_the_testbed(
    tmp_path: Path, link_name: str, version: int
) -> None:
    client = _helper("csp_client")
    with LINKS[link_name]() as link:
        setup = BOARD_SETUP.format(
            version_line=_version_line(version),
            transport=link.transport,
            source_port=LIBCSP_FIRST_SOURCE_PORT,
        )

        def _libcsp_node() -> None:
            time.sleep(2.5)  # the testbed is connected and paced by then
            subprocess.run(
                [
                    str(client),
                    *link.client_args,
                    "-p",
                    "-v",
                    str(version),
                    "-a",
                    "2",
                    "-d",
                    "1",
                    "-l",
                    "1",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

        sender = threading.Thread(target=_libcsp_node, daemon=True)
        sender.start()

        code = _run_cli(tmp_path, setup, BOARD_SCENARIO)
        sender.join(timeout=10)

    assert code == 0, "the assertion on the libcsp node's packet did not pass"


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("link_name", list(LINKS))
def test_a_real_libcsp_node_receives_the_testbeds_telemetry(
    tmp_path: Path, link_name: str, version: int
) -> None:
    receiver = _helper("csp_rx")
    with LINKS[link_name]() as link:
        setup = SIMULATED_SETUP.format(
            version_line=_version_line(version),
            transport=link.transport,
            port=TELEMETRY_PORT,
        )
        listener = subprocess.Popen(
            [
                str(receiver),
                link.receiver_spec,
                "1",
                str(version),
                str(TELEMETRY_PORT),
                str(RECEIVER_SECONDS),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            time.sleep(1.0)
            code = _run_cli(tmp_path, setup, SIMULATED_SCENARIO)
            output, _ = listener.communicate(timeout=RECEIVER_SECONDS + 10)
        finally:
            if listener.poll() is None:
                listener.kill()

    packets = [line for line in output.splitlines() if line.startswith("RX ")]
    assert code == 0
    assert len(packets) >= 2, f"libcsp node 1 received {len(packets)} packets; output: {output!r}"
    assert all(
        line.startswith(f"RX src=2 dst=1 dport={TELEMETRY_PORT} sport={TELEMETRY_PORT} len=4")
        for line in packets
    )


# --- requests and replies (backlog item 18) ----------------------------------------------------

PING_SETUP = """
{version_line}
[transport]
{transport}

[nodes.obc]
mode = "software"
address = 1

[nodes.eps]
mode = "simulated"
module_type = "generic_eps"
address = 2

[nodes.eps.services]
ping = true
"""

PING_SCENARIO = """
name: a libcsp node pings a simulated EPS
steps:
  - action: wait
    virtual_time: 6s
"""

REQUEST_SETUP = """
{version_line}
[transport]
{transport}

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "software"
address = 2
"""

REQUEST_SCENARIO = """
name: the testbed pings a libcsp node
steps:
  - {action: wait, virtual_time: 2s}
  - {action: request, source: obc, target: eps, service: ping, timeout: 3s}
  - action: request
    source: obc
    target: eps
    service: buf_free
    timeout: 3s
    expect: [{field: buf_free, op: ">", value: 0}]
"""


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("link_name", list(LINKS))
def test_a_real_libcsp_node_pings_a_simulated_node(
    tmp_path: Path, link_name: str, version: int
) -> None:
    pinger = _helper("csp_ping")
    with LINKS[link_name]() as link:
        setup = PING_SETUP.format(version_line=_version_line(version), transport=link.transport)
        result: dict[str, str] = {}

        def _libcsp_node() -> None:
            time.sleep(2.5)  # the testbed is connected and paced by then
            completed = subprocess.run(
                [str(pinger), link.receiver_spec, "1", str(version), "2", "4", "1000", "3"],
                capture_output=True,
                text=True,
                check=False,
            )
            result["output"] = completed.stdout

        thread = threading.Thread(target=_libcsp_node, daemon=True)
        thread.start()
        code = _run_cli(tmp_path, setup, PING_SCENARIO)
        thread.join(timeout=15)

    pings = [line for line in result.get("output", "").splitlines() if line.startswith("PING")]
    assert code == 0
    assert len(pings) == 3 and all("time=" in line for line in pings), pings


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("link_name", list(LINKS))
def test_a_scenario_requests_from_a_real_libcsp_node(
    tmp_path: Path, link_name: str, version: int
) -> None:
    receiver = _helper("csp_rx")
    with LINKS[link_name]() as link:
        setup = REQUEST_SETUP.format(version_line=_version_line(version), transport=link.transport)
        node = subprocess.Popen(
            [str(receiver), link.receiver_spec, "2", str(version), "services", "12"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            time.sleep(1.5)
            code = _run_cli(tmp_path, setup, REQUEST_SCENARIO)
        finally:
            node.kill()
            output, _ = node.communicate(timeout=10)

    assert code == 0, f"the libcsp node did not answer; it saw: {output!r}"
