"""ZMQ transport: adapter routing, the pyzmq link, and scenarios through a hub."""

from __future__ import annotations

import io
import json
import struct
import sys
import threading
import time
import warnings
from collections import deque
from pathlib import Path

import pytest

from cubesat_testbed.clock import RealTimeClock
from cubesat_testbed.config import ZmqTransportConfig, parse_scenario, parse_testbed_config
from cubesat_testbed.main import main
from cubesat_testbed.protocol.csp_v2 import CspFields, pack
from cubesat_testbed.protocol.csp_v2_packet import CspPacketFrame, pack_header
from cubesat_testbed.protocol.csp_v2_zmq import encode_zmq_message
from cubesat_testbed.scenario import ScenarioRunner, build_runtime
from cubesat_testbed.transport import (
    PyzmqMessageLink,
    TransportError,
    ZmqAdapter,
    build_transport_adapter,
)
from tests.zmq_hub import InProcessHub


class FakeLink:
    """An in-process message link: tests push what 'arrives', and read what was published."""

    def __init__(self) -> None:
        self.incoming: deque[bytes] = deque()
        self.sent: list[bytes] = []
        self.closed = False

    def send(self, message: bytes) -> None:
        self.sent.append(message)

    def receive(self, timeout: float = 0.0) -> bytes | None:
        return self.incoming.popleft() if self.incoming else None

    def close(self) -> None:
        self.closed = True


def _packet(
    destination: int, payload: bytes = b"\x55", source: int = 1, **kwargs: int
) -> CspPacketFrame:
    fields = CspFields(
        priority=2,
        source=source,
        destination=destination,
        destination_port=1,
        source_port=18,
        **kwargs,
    )
    return CspPacketFrame(fields=fields, payload=payload)


# --- the adapter ---------------------------------------------------------------------


def test_packet_for_a_local_node_stays_inside_the_testbed() -> None:
    link = FakeLink()
    adapter = ZmqAdapter(link, local_addresses=[2, 3])

    envelope = adapter.send(_packet(2), source=1)

    assert link.sent == []
    assert adapter.receive(endpoint=2) is envelope
    assert adapter.receive(endpoint=3) is None
    assert adapter.receive() is envelope
    assert isinstance(envelope.frame, CspPacketFrame) and envelope.frame.link == ""


def test_packet_for_a_remote_node_is_published_as_one_message_and_not_queued_locally() -> None:
    link = FakeLink()
    adapter = ZmqAdapter(link, local_addresses=[2])

    envelope = adapter.send(_packet(9, b"\xc0\x01"), source=1)

    expected = encode_zmq_message(_packet(9).fields, b"\xc0\x01")
    assert link.sent == [expected]
    assert isinstance(envelope.frame, CspPacketFrame)
    assert (envelope.frame.wire, envelope.frame.link) == (expected, "zmq")
    assert adapter.receive() is None


def test_messages_from_the_hub_reach_the_monitor_and_the_addressed_endpoint() -> None:
    link = FakeLink()
    adapter = ZmqAdapter(link, local_addresses=[2, 3])
    link.incoming.append(encode_zmq_message(_packet(3, b"\x07", source=9).fields, b"\x07"))

    monitor = adapter.receive()

    assert isinstance(monitor.frame, CspPacketFrame)  # type: ignore[union-attr]
    assert monitor.frame.payload == b"\x07" and monitor.frame.link == "zmq"  # type: ignore[union-attr]
    assert adapter.receive(endpoint=3) is not None
    assert adapter.receive(endpoint=2) is None


def test_every_message_already_waiting_is_taken_in_one_receive_cycle() -> None:
    link = FakeLink()
    adapter = ZmqAdapter(link, local_addresses=[3])
    for payload in (b"\x01", b"\x02", b"\x03"):
        link.incoming.append(encode_zmq_message(_packet(3, payload, source=9).fields, payload))

    received = []
    while (envelope := adapter.receive()) is not None:
        assert isinstance(envelope.frame, CspPacketFrame)
        received.append(envelope.frame.payload)

    assert received == [b"\x01", b"\x02", b"\x03"]


def test_the_hubs_echo_of_our_own_publication_is_ignored() -> None:
    link = FakeLink()
    adapter = ZmqAdapter(link, local_addresses=[1, 2])
    own = _packet(9, b"\x01", source=1)
    adapter.send(own, source=1)
    link.incoming.extend(link.sent)  # a hub broadcasts to every subscriber, the sender included

    assert adapter.receive() is None
    assert adapter.take_drops() == ()  # an echo is expected traffic, not a drop


def test_rejected_messages_are_reported_by_take_drops_then_forgotten() -> None:
    link = FakeLink()
    adapter = ZmqAdapter(link, local_addresses=[3], max_packet_bytes=8)
    link.incoming.append(b"\x80\x02")  # shorter than a header
    link.incoming.append(pack_header(_packet(3, source=9).fields) + bytes(9))  # over the limit

    assert adapter.receive() is None
    drops = adapter.take_drops()

    assert [(d.reason, d.limit) for d in drops] == [("too_short", None), ("overflow", 8)]
    assert (drops[1].source, drops[1].destination) == (9, 3)
    assert adapter.take_drops() == ()


def test_adapter_refuses_can_frames_and_oversized_packets_and_closes_once() -> None:
    link = FakeLink()
    adapter = ZmqAdapter(link, local_addresses=[2])

    with pytest.raises(TransportError, match="not CAN frames"):
        adapter.send(pack(_packet(2).fields, b"\x01"))
    with pytest.raises(TransportError, match="max_packet_bytes=256"):
        adapter.send(_packet(9, bytes(257)))
    adapter.close()
    adapter.close()
    assert link.closed and adapter.closed
    with pytest.raises(TransportError, match="closed"):
        adapter.send(_packet(2))


def test_new_sender_builds_one_whole_packet_frame() -> None:
    sender = ZmqAdapter(FakeLink()).new_sender(max_packet_bytes=8)

    (frame,) = sender.frames(_packet(2).fields, bytes(8))
    assert isinstance(frame, CspPacketFrame)


# --- the pyzmq link ------------------------------------------------------------------


def test_pyzmq_link_publishes_and_receives_through_a_hub() -> None:
    with InProcessHub() as hub:
        a = PyzmqMessageLink(hub.publish_endpoint, hub.subscribe_endpoint, settle_seconds=0.3)
        b = PyzmqMessageLink(hub.publish_endpoint, hub.subscribe_endpoint, settle_seconds=0.3)
        try:
            a.send(b"hello")
            assert b.receive(timeout=2.0) == b"hello"
            assert a.receive(timeout=2.0) == b"hello"  # a hub echoes to the sender too
            assert b.receive(timeout=0.0) is None
        finally:
            a.close()
            a.close()
            b.close()


def test_pyzmq_link_without_pyzmq_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "zmq", None)

    with pytest.raises(TransportError, match=r"pip install 'cubesat-testbed\[zmq\]'"):
        PyzmqMessageLink("tcp://127.0.0.1:1", "tcp://127.0.0.1:2")


# --- config and factory --------------------------------------------------------------


def test_setup_parses_a_zmq_transport_with_libcsp_defaults() -> None:
    setup = parse_testbed_config(
        """
        [transport]
        type = "zmq"

        [nodes.obc]
        mode = "simulated"
        module_type = "obc_peer"
        address = 1
        """
    )

    assert setup.transport == ZmqTransportConfig(
        type="zmq",
        publish_endpoint="tcp://127.0.0.1:6000",
        subscribe_endpoint="tcp://127.0.0.1:7000",
        settle_seconds=0.5,
    )


@pytest.mark.parametrize("endpoint", ["127.0.0.1:6000", "http://x:1", "tcp://", ""])
def test_setup_rejects_a_malformed_endpoint(endpoint: str) -> None:
    with pytest.raises(ValueError, match="ZMQ endpoint"):
        ZmqTransportConfig(type="zmq", publish_endpoint=endpoint)


def test_factory_builds_a_zmq_adapter_from_config() -> None:
    with InProcessHub() as hub:
        config = ZmqTransportConfig(
            type="zmq",
            publish_endpoint=hub.publish_endpoint,
            subscribe_endpoint=hub.subscribe_endpoint,
            settle_seconds=0.0,
        )
        adapter = build_transport_adapter(config, local_addresses=[2])
        try:
            assert isinstance(adapter, ZmqAdapter)
        finally:
            adapter.close()


# --- scenarios through a hub ---------------------------------------------------------

SETUP = """
[transport]
type = "zmq"
publish_endpoint = "{publish}"
subscribe_endpoint = "{subscribe}"
settle_seconds = 0.3

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "software"
address = 2

[nodes.payload]
mode = "software"
address = 3

[nodes.obc.commands.payload_power_off]
target = "payload"
destination_port = 10
source_port = 10
payload_hex = "00 01 02 03 04 05 06 07 08 09"

[nodes.eps.telemetry.battery_percent]
source_port = 20
destination_port = 20
offset = 0
length = 4
type = "float"
"""

SCENARIO = """
name: ZMQ round trip
steps:
  - action: send_command
    command: payload_power_off
    source: obc
    target: payload
  - action: wait
    virtual_time: 1s
  - action: assert
    signal: eps.telemetry.battery_percent
    op: "=="
    value: 42.0
    timeout: 1s
"""

EPS_TELEMETRY = CspFields(priority=2, source=2, destination=2, destination_port=20, source_port=20)


def _telemetry_message(value: float = 42.0) -> bytes:
    return encode_zmq_message(EPS_TELEMETRY, struct.pack(">f", value))


def _setup_text(hub: InProcessHub) -> str:
    return SETUP.format(publish=hub.publish_endpoint, subscribe=hub.subscribe_endpoint)


def _peer(hub: InProcessHub) -> PyzmqMessageLink:
    return PyzmqMessageLink(hub.publish_endpoint, hub.subscribe_endpoint, settle_seconds=0.3)


def _run(setup_text: str, peer: PyzmqMessageLink, publish: list[bytes], *, trace: io.StringIO):  # type: ignore[no-untyped-def]
    """Build the runtime (so the testbed is connected), publish as the peer, then run."""

    setup = parse_testbed_config(setup_text)
    scenario = parse_scenario(SCENARIO, setup=setup)
    runtime = build_runtime(setup, clock=RealTimeClock(), trace=trace)
    try:
        for message in publish:
            peer.send(message)
        return ScenarioRunner(runtime, output=io.StringIO()).run(scenario)
    finally:
        runtime.transport.close()


def test_a_scenario_sends_a_long_command_and_reads_telemetry_through_the_hub() -> None:
    trace = io.StringIO()
    with InProcessHub() as hub:
        peer = _peer(hub)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter(
                    "error"
                )  # the hub's echo must not surface as an unrouted frame
                result = _run(_setup_text(hub), peer, [_telemetry_message(42.0)], trace=trace)
            # The command was published as one message; the peer hears it, and so did the hub's echo.
            heard = []
            while (message := peer.receive(timeout=1.0)) is not None:
                heard.append(message)
        finally:
            peer.close()

    assert result.passed and result.dropped_packets == ()
    expected = encode_zmq_message(
        CspFields(priority=2, source=1, destination=3, destination_port=10, source_port=10),
        bytes(range(10)),
    )
    assert expected in heard
    lines = trace.getvalue().splitlines()
    assert any(
        " TX " in line
        and "link=zmq" in line
        and "dst=3" in line
        and "command obc.payload_power_off" in line
        for line in lines
    )
    assert any(
        " RX " in line
        and "link=zmq" in line
        and "telemetry eps.telemetry.battery_percent=42.0" in line
        for line in lines
    )


def test_a_bad_message_on_the_hub_is_reported_as_a_dropped_packet() -> None:
    trace = io.StringIO()
    with InProcessHub() as hub:
        peer = _peer(hub)
        try:
            result = _run(
                _setup_text(hub), peer, [b"\x80\x02", _telemetry_message(42.0)], trace=trace
            )
        finally:
            peer.close()

    assert result.passed  # the good telemetry still arrived
    assert [(d.reason, d.destination) for d in result.dropped_packets] == [("too_short", None)]
    assert any(
        line.startswith("trace DROP") and "reason=too_short" in line
        for line in trace.getvalue().splitlines()
    )


def test_a_message_over_the_receiving_nodes_limit_is_dropped_not_delivered() -> None:
    to_payload = CspFields(priority=2, source=2, destination=3, destination_port=9, source_port=9)
    text = None
    with InProcessHub() as hub:
        text = _setup_text(hub).replace(
            '[nodes.payload]\nmode = "software"\naddress = 3',
            '[nodes.payload]\nmode = "software"\naddress = 3\nmax_packet_bytes = 3',
        )
        peer = _peer(hub)
        try:
            result = _run(
                text,
                peer,
                [encode_zmq_message(to_payload, bytes(5)), _telemetry_message(42.0)],
                trace=io.StringIO(),
            )
        finally:
            peer.close()

    assert result.passed
    assert [(d.reason, d.sender, d.destination, d.limit) for d in result.dropped_packets] == [
        ("overflow", 2, 3, 3)
    ]


def test_cli_runs_through_a_hub_traces_the_messages_and_warns_without_realtime(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stop = threading.Event()
    with InProcessHub() as hub:
        setup_path = tmp_path / "setup.toml"
        scenario_path = tmp_path / "scenario.yaml"
        setup_path.write_text(_setup_text(hub), encoding="utf-8")
        scenario_path.write_text(SCENARIO, encoding="utf-8")
        peer = _peer(hub)

        def publish_telemetry() -> None:
            # The CLI connects inside main(); keep publishing until it has, then stop.
            while not stop.is_set():
                peer.send(_telemetry_message(42.0))
                time.sleep(0.1)

        thread = threading.Thread(target=publish_telemetry, daemon=True)
        thread.start()
        try:
            code = main(
                [
                    "run",
                    "--config",
                    str(setup_path),
                    "--scenario",
                    str(scenario_path),
                    "--realtime",
                    "--trace",
                    "--json",
                ]
            )
        finally:
            stop.set()
            thread.join(timeout=2.0)
            peer.close()

    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["passed"] is True
    assert "link=zmq" in captured.err


def test_cli_warns_when_a_zmq_transport_runs_without_realtime(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with InProcessHub() as hub:
        setup_path = tmp_path / "setup.toml"
        scenario_path = tmp_path / "scenario.yaml"
        setup_path.write_text(_setup_text(hub), encoding="utf-8")
        scenario_path.write_text(SCENARIO, encoding="utf-8")

        main(["run", "--config", str(setup_path), "--scenario", str(scenario_path), "--quiet"])

    assert "transport.type='zmq'" in capsys.readouterr().err
