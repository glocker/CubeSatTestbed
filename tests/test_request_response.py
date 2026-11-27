"""Requests and replies (backlog item 18): libcsp services, configured requests, scenario steps."""

from __future__ import annotations

import io
from collections import deque
from collections.abc import Iterator, Mapping

import can
import pytest
from pydantic import ValidationError

import cubesat_testbed.scenario.runner as runner_module
from cubesat_testbed.config import ScenarioReferenceError, parse_scenario, parse_testbed_config
from cubesat_testbed.engine import VirtualTime
from cubesat_testbed.modules import GenericEpsModule, ModuleRequest
from cubesat_testbed.modules.eps import GenericEpsConfig
from cubesat_testbed.modules.registry import register_module, unregister_module
from cubesat_testbed.protocol.csp_services import crc32c
from cubesat_testbed.protocol.csp_v2 import (
    CspCanFrame,
    CspFields,
    CspReassembler,
    fragment_packet,
)
from cubesat_testbed.report import RunRecorder, render_report
from cubesat_testbed.scenario import run_scenario
from cubesat_testbed.scenario.runner import ScenarioRunResult
from cubesat_testbed.transport import SocketCanAdapter

NODES = """
[transport]
type = "in-memory"

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "simulated"
module_type = "{eps_module}"
address = 2
"""

SERVICES = """
[nodes.eps.services]
ping = true
uptime = true
memfree = 131072
buf_free = 10
"""

HOUSEKEEPING = """
[nodes.eps.requests.get_housekeeping]
port = 8
payload_hex = "01"
reply = "housekeeping"

[nodes.eps.packets.housekeeping]
periodic = false

[nodes.eps.packets.housekeeping.fields.battery_percent]
offset = 0
length = 4
type = "float"

[nodes.eps.packets.housekeeping.fields.cell_voltages_v]
offset = 4
length = 4
count = 2
type = "float"

[nodes.eps.packets.housekeeping.fields.power_mode]
offset = 12
bit_offset = 0
bit_length = 2
type = "uint"

[nodes.eps.packets.housekeeping.fields.power_mode.enum]
0 = "nominal"
1 = "low_power"
2 = "safe"
3 = "off"
"""


def _setup(*parts: str, eps_module: str = "generic_eps") -> str:
    return NODES.format(eps_module=eps_module) + "".join(parts)


def _run(setup_text: str, steps: str) -> tuple[ScenarioRunResult, str, str]:
    setup = parse_testbed_config(setup_text)
    scenario = parse_scenario(f"name: requests\nsteps:\n{steps}", setup=setup)
    output, trace = io.StringIO(), io.StringIO()
    result = run_scenario(scenario, setup, output=output, trace=trace)
    return result, output.getvalue(), trace.getvalue()


def _details(result: ScenarioRunResult) -> list[str]:
    return [f"{'PASS' if a.passed else 'FAIL'} {a.detail}" for a in result.assertions]


# --- libcsp services on a simulated node --------------------------------------------------------


def test_a_simulated_node_answers_ping_with_the_echo() -> None:
    result, _out, trace = _run(
        _setup(SERVICES),
        "  - {action: request, source: obc, target: eps, service: ping, payload_hex: 'deadbeef'}\n",
    )

    assert result.passed, _details(result)
    assert "eps echoed deadbeef" in result.assertions[0].detail
    assert "request ping to eps" in trace
    assert "src=2 dst=1 dport=17 sport=1" in trace and "reply ping from eps" in trace


def test_uptime_memfree_and_buf_free_reply_as_libcsp_does() -> None:
    result, _out, trace = _run(
        _setup(SERVICES),
        """
  - {action: wait, virtual_time: 5s}
  - {action: request, source: obc, target: eps, service: uptime, expect: [{field: uptime_s, op: "==", value: 5}]}
  - {action: request, source: obc, target: eps, service: memfree, expect: [{field: memfree_bytes, op: "==", value: 131072}]}
  - {action: request, source: obc, target: eps, service: buf_free, expect: [{field: buf_free, op: "==", value: 10}]}
""",
    )

    assert result.passed, _details(result)
    assert len(result.assertions) == 6
    # 4-byte big-endian unsigned integers, as csp_service_handler sends them.
    assert "payload=00000005 reply uptime from eps uptime_s=5" in trace
    assert "payload=00020000 reply memfree from eps memfree_bytes=131072" in trace


def test_a_service_the_node_does_not_offer_times_out_without_a_warning(
    recwarn: pytest.WarningsRecorder,
) -> None:
    result, output, _trace = _run(
        _setup("[nodes.eps.services]\nping = true\n"),
        "  - {action: request, source: obc, target: eps, service: uptime, timeout: 300ms}\n",
    )

    assert not result.passed
    assert result.assertions[0].detail == (
        "eps.services.uptime: no reply from eps within 0.300000 s"
    )
    assert result.finished_at == 300_000
    assert "FAIL t=300000 request_1" in output
    assert not [w for w in recwarn if "no configured route" in str(w.message)]


def test_successive_requests_take_successive_ephemeral_ports() -> None:
    _result, _out, trace = _run(
        _setup(SERVICES),
        "  - {action: request, source: obc, target: eps, service: ping}\n" * 3,
    )

    sports = [
        token
        for line in trace.splitlines()
        if " TX " in line and "request ping" in line
        for token in line.split()
        if token.startswith("sport=")
    ]
    assert sports == ["sport=17", "sport=18", "sport=19"]


def test_ping_works_on_csp_v1() -> None:
    result, _out, trace = _run(
        "protocol_version = 1\n" + _setup(SERVICES),
        "  - {action: request, source: obc, target: eps, service: ping}\n",
    )

    assert result.passed, _details(result)
    assert "cfp=" in trace and "rem=" in trace  # CFP1 frames


# --- configured requests --------------------------------------------------------------------


def test_a_configured_request_is_answered_from_telemetry_and_checked_field_by_field() -> None:
    result, _out, trace = _run(
        _setup(HOUSEKEEPING),
        """
  - {action: wait, virtual_time: 1s}
  - action: request
    source: obc
    target: eps
    request: get_housekeeping
    timeout: 500ms
    expect:
      - {field: battery_percent, op: ">", value: 70}
      - {field: "cell_voltages_v[1]", op: ">", value: 3.5}
      - {field: power_mode, op: "==", value: nominal}
      - {field: cell_voltages_v, op: "!=", value: [0, 0]}
""",
    )

    assert result.passed, _details(result)
    assert [a.name for a in result.assertions] == [
        "request_2",
        "request_2.battery_percent",
        "request_2.cell_voltages_v[1]",
        "request_2.power_mode",
        "request_2.cell_voltages_v",
    ]
    assert "request eps.requests.get_housekeeping" in trace
    assert "reply eps.requests.get_housekeeping battery_percent=" in trace
    # A reply-only packet is never beaconed.
    assert "telemetry eps" not in trace
    assert trace.count("reply eps.requests") == 2  # TX and RX of the reply's last frame


def test_a_failed_expectation_names_the_field_and_the_value() -> None:
    result, _out, _trace = _run(
        _setup(HOUSEKEEPING),
        """
  - action: request
    source: obc
    target: eps
    request: get_housekeeping
    expect: [{field: power_mode, op: "==", value: safe}]
""",
    )

    assert not result.passed
    assert result.assertions[0].passed  # the reply itself came
    assert result.assertions[1].detail == (
        "eps.requests.get_housekeeping.power_mode == 'safe'; actual='nominal'"
    )


class _AnsweringEps(GenericEpsModule):
    """An EPS that answers get_housekeeping itself, or refuses when asked with 0xFF."""

    def answer_request(
        self, request: ModuleRequest, *, now: VirtualTime
    ) -> Mapping[str, object] | None:
        self.requests.append(request)
        if request.payload == b"\xff":
            return None
        return {"battery_percent": 12.5, "cell_voltages_v": (3.0, 2.5), "power_mode": "safe"}


@pytest.fixture
def answering_eps() -> Iterator[list[ModuleRequest]]:
    seen: list[ModuleRequest] = []

    def build(context: object) -> _AnsweringEps:
        module = _AnsweringEps(context.module_config(GenericEpsConfig))  # type: ignore[attr-defined]
        module.requests = seen  # type: ignore[attr-defined]
        return module

    register_module("answering_eps", build)
    try:
        yield seen
    finally:
        unregister_module("answering_eps")


def test_a_modules_answer_request_hook_supplies_the_reply(
    answering_eps: list[ModuleRequest],
) -> None:
    result, _out, _trace = _run(
        _setup(HOUSEKEEPING, eps_module="answering_eps"),
        """
  - action: request
    source: obc
    target: eps
    request: get_housekeeping
    expect: [{field: battery_percent, op: "==", value: 12.5}, {field: power_mode, op: "==", value: safe}]
  - {action: request, source: obc, target: eps, request: get_housekeeping, payload_hex: "ff", timeout: 100ms}
""",
    )

    assert [a.passed for a in result.assertions] == [True, True, True, False]
    assert result.assertions[3].detail.endswith("no reply from eps within 0.100000 s")
    assert [(r.request, r.payload, r.source, r.source_address) for r in answering_eps] == [
        ("get_housekeeping", b"\x01", "obc", 1),
        ("get_housekeeping", b"\xff", "obc", 1),
    ]


def test_request_results_reach_json_and_the_html_report(capsys: pytest.CaptureFixture[str]) -> None:
    setup = parse_testbed_config(_setup(SERVICES))
    scenario = parse_scenario(
        "name: r\nsteps:\n  - {action: request, source: obc, target: eps, service: ping}\n",
        setup=setup,
    )
    recorder = RunRecorder()
    result = run_scenario(scenario, setup, output=io.StringIO(), recorder=recorder)

    assert result.passed
    assert recorder.assertions[0].evidence_frame is not None
    assert "eps.services.ping" in render_report(recorder, result)


# --- a real node on the other end (scripted SocketCAN bus) --------------------------------------


class _ReplyingBus:
    """A CAN bus with a real board on it: answers ping and get_housekeeping like firmware would."""

    def __init__(self, incoming: list[can.Message] | None = None, *, answer: bool = True) -> None:
        self.incoming: deque[can.Message] = deque(incoming or [])
        self.sent: list[can.Message] = []
        self.answer = answer
        self._reassembler = CspReassembler()
        self.received: list[tuple[CspFields, bytes]] = []

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self.sent.append(msg)
        for event in self._reassembler.feed(
            CspCanFrame(msg.arbitration_id, bytes(msg.data)), now=0
        ):
            if event.kind != "complete" or event.packet is None:
                continue
            fields, payload = event.packet.fields, event.packet.payload
            self.received.append((fields, payload))
            if not self.answer or fields.destination != 2:
                continue
            if fields.destination_port == 1:
                data = payload
            elif fields.destination_port == 8:
                data = bytes.fromhex("41480000 40400000 40200000 80")  # 12.5, (3.0, 2.5), safe
            else:
                continue
            reply = CspFields(
                fields.priority, 2, fields.source, fields.source_port, fields.destination_port
            )
            self.incoming.extend(
                can.Message(arbitration_id=f.can_id, data=f.data, is_extended_id=True)
                for f in fragment_packet(reply, data)
            )

    def recv(self, timeout: float | None = None) -> can.Message | None:
        return self.incoming.popleft() if self.incoming else None

    def shutdown(self) -> None:
        pass


def _on_bus(monkeypatch: pytest.MonkeyPatch, bus: _ReplyingBus) -> None:
    monkeypatch.setattr(
        runner_module,
        "build_transport_adapter",
        lambda *_a, **_k: SocketCanAdapter(interface="vcan0", bus=bus),
    )


BOARD = """
[transport]
type = "socketcan"
interface = "vcan0"

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "hardware"
address = 2
"""


def test_the_testbed_pings_a_real_board_and_polls_its_housekeeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _ReplyingBus()
    _on_bus(monkeypatch, bus)

    result, _out, _trace = _run(
        BOARD + HOUSEKEEPING,
        """
  - {action: request, source: obc, target: eps, service: ping}
  - action: request
    source: obc
    target: eps
    request: get_housekeeping
    expect: [{field: "cell_voltages_v[1]", op: "==", value: 2.5}, {field: power_mode, op: "==", value: safe}]
""",
    )

    assert result.passed, _details(result)
    assert [(f.destination_port, f.source_port, p) for f, p in bus.received] == [
        (1, 17, bytes(range(4))),
        (8, 18, b"\x01"),
    ]


def test_a_silent_board_fails_the_request_step(monkeypatch: pytest.MonkeyPatch) -> None:
    _on_bus(monkeypatch, _ReplyingBus(answer=False))

    result, _out, _trace = _run(
        BOARD,
        "  - {action: request, source: obc, target: eps, service: ping, timeout: 2s}\n",
    )

    assert not result.passed
    assert result.assertions[0].detail == "eps.services.ping: no reply from eps within 2.000000 s"


def _frames(fields: CspFields, payload: bytes) -> list[can.Message]:
    return [
        can.Message(arbitration_id=f.can_id, data=f.data, is_extended_id=True)
        for f in fragment_packet(fields, payload)
    ]


SIMULATED_EPS_REAL_OBC = """
[transport]
type = "socketcan"
interface = "vcan0"

[nodes.obc]
mode = "hardware"
address = 1

[nodes.eps]
mode = "simulated"
module_type = "generic_eps"
address = 2
"""


def _replies(bus: _ReplyingBus) -> list[tuple[CspFields, bytes]]:
    return [(f, p) for f, p in bus.received if f.source == 2]


def test_a_real_obc_pinging_a_simulated_node_gets_the_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    ping = CspFields(2, 1, 2, 1, 23)
    bus = _ReplyingBus(_frames(ping, b"\x55\x56\x57"), answer=False)
    _on_bus(monkeypatch, bus)

    result, _out, _trace = _run(
        SIMULATED_EPS_REAL_OBC + SERVICES, "  - {action: wait, virtual_time: 1s}\n"
    )

    assert result.passed
    ((fields, payload),) = _replies(bus)
    assert (fields.destination, fields.destination_port, fields.source_port) == (1, 23, 1)
    assert payload == b"\x55\x56\x57"


def test_a_crc32_ping_is_answered_with_a_fresh_crc32(monkeypatch: pytest.MonkeyPatch) -> None:
    data = b"\x00\x01\x02\x03"
    ping = CspFields(2, 1, 2, 1, 23, flags=0x01)
    bus = _ReplyingBus(_frames(ping, data + crc32c(data).to_bytes(4, "big")), answer=False)
    _on_bus(monkeypatch, bus)

    _run(SIMULATED_EPS_REAL_OBC + SERVICES, "  - {action: wait, virtual_time: 1s}\n")

    ((fields, payload),) = _replies(bus)
    assert fields.flags == 0x01
    assert payload == data + crc32c(data).to_bytes(4, "big")


def test_an_hmac_request_is_a_reported_drop_not_a_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    ping = CspFields(2, 1, 2, 1, 23, flags=0x08)
    bus = _ReplyingBus(_frames(ping, bytes(8)), answer=False)
    _on_bus(monkeypatch, bus)

    result, _out, trace = _run(
        SIMULATED_EPS_REAL_OBC + SERVICES, "  - {action: wait, virtual_time: 1s}\n"
    )

    assert _replies(bus) == []
    (drop,) = result.dropped_packets
    assert drop.reason == "unanswerable"
    assert "HMAC" in drop.detail
    assert "reason=unanswerable" in trace


# --- config and scenario validation -------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (SERVICES.replace("eps", "obc").replace("ping = true", "ping = true\nbogus = 1"), "bogus"),
        ("[nodes.eps.requests.r]\nport = 1\nreply = 'x'\n", "reserved for libcsp's services"),
        ("[nodes.eps.requests.r]\nport = 8\nreply = 'nope'\n", "does not define"),
        (
            HOUSEKEEPING
            + "[nodes.eps.requests.twin]\nport = 8\npayload_hex = '01'\nreply = 'housekeeping'\n",
            "cannot tell them apart",
        ),
        (
            SERVICES
            + "[nodes.obc.commands.c]\ntarget = 'eps'\ndestination_port = 1\nsource_port = 9\n",
            "where 'eps' answers service ping",
        ),
        (
            HOUSEKEEPING
            + "[nodes.eps.telemetry.load_power_w]\nsource_port = 8\ndestination_port = 20\n"
            "offset = 0\nlength = 4\ntype = 'float'\n",
            "sent from port 8",
        ),
        (
            (
                "[nodes.eps.packets.p]\nperiodic = false\nsource_port = 3\n"
                "[nodes.eps.packets.p.fields.x]\noffset = 0\nlength = 1\ntype = 'uint'\n"
            ),
            "takes no source_port",
        ),
        (
            "[nodes.eps.packets.p]\n[nodes.eps.packets.p.fields.x]\noffset = 0\nlength = 1\ntype = 'uint'\n",
            "needs source_port and destination_port",
        ),
    ],
)
def test_bad_request_config_is_refused_at_load(extra: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_testbed_config(_setup(extra))


def test_services_on_a_real_node_are_refused() -> None:
    with pytest.raises(ValidationError, match="only for simulated nodes"):
        parse_testbed_config(BOARD + SERVICES)


def test_a_reply_only_packet_is_not_telemetry() -> None:
    setup = parse_testbed_config(_setup(HOUSEKEEPING))

    assert setup.telemetry_signal_names() == frozenset()


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ("{action: request, source: obc, target: eps, request: nope}", "has no request 'nope'"),
        ("{action: request, source: obc, target: ghost, service: ping}", "unknown node 'ghost'"),
        ("{action: request, source: obc, target: obc, service: ping}", "both 'obc'"),
        (
            (
                "{action: request, source: obc, target: eps, request: get_housekeeping, "
                "expect: [{field: nope, op: '==', value: 1}]}"
            ),
            "field 'nope' is not mapped",
        ),
        (
            (
                "{action: request, source: obc, target: eps, request: get_housekeeping, "
                "expect: [{field: 'cell_voltages_v[2]', op: '==', value: 1}]}"
            ),
            "out of range",
        ),
    ],
)
def test_bad_request_steps_are_refused_when_the_scenario_loads(step: str, message: str) -> None:
    setup = parse_testbed_config(_setup(HOUSEKEEPING))

    with pytest.raises(ScenarioReferenceError, match=message):
        parse_scenario(f"name: x\nsteps:\n  - {step}\n", setup=setup)


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ("{action: request, source: obc, target: eps}", "exactly one of service or request"),
        (
            "{action: request, source: obc, target: eps, service: ping, request: r}",
            "exactly one of service or request",
        ),
        (
            "{action: request, source: obc, target: eps, service: ping, expect: [{field: x, op: '==', value: 1}]}",
            "no fields to expect",
        ),
        (
            "{action: request, source: obc, target: eps, service: uptime, expect: [{field: x, op: '==', value: 1}]}",
            "one field, 'uptime_s'",
        ),
    ],
)
def test_malformed_request_steps_are_refused(step: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_scenario(f"name: x\nsteps:\n  - {step}\n")


def test_a_request_from_a_real_node_is_refused() -> None:
    setup = parse_testbed_config(SIMULATED_EPS_REAL_OBC)

    with pytest.raises(ScenarioReferenceError, match="must be a simulated node"):
        parse_scenario(
            "name: x\nsteps:\n  - {action: request, source: obc, target: eps, service: ping}\n",
            setup=setup,
        )


# --- bug 16: simulated nodes talking to each other over SocketCAN -------------------------------


class _SilentBus:
    """A CAN bus as Linux gives it without receive_own_messages: a socket's own frames never
    come back, and no other node is on it."""

    def __init__(self) -> None:
        self.sent: list[can.Message] = []

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self.sent.append(msg)

    def recv(self, timeout: float | None = None) -> can.Message | None:
        return None

    def shutdown(self) -> None:
        pass


SIMULATED_PAIR_ON_SOCKETCAN = """
[transport]
type = "socketcan"
interface = "vcan0"
{own}

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "simulated"
module_type = "generic_eps"
address = 2

[nodes.eps.services]
ping = true
"""


def _on_silent_bus(monkeypatch: pytest.MonkeyPatch, bus: _SilentBus, *, own: bool) -> None:
    def build(*_args: object, **kwargs: object) -> SocketCanAdapter:
        return SocketCanAdapter(
            interface="vcan0",
            bus=bus,
            receive_own_messages=own,
            local_addresses=kwargs["local_addresses"],  # type: ignore[arg-type]
        )

    monkeypatch.setattr(runner_module, "build_transport_adapter", build)


def test_a_request_between_simulated_nodes_on_socketcan_is_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _SilentBus()
    _on_silent_bus(monkeypatch, bus, own=False)

    result, _out, trace = _run(
        SIMULATED_PAIR_ON_SOCKETCAN.format(own=""),
        "  - {action: request, source: obc, target: eps, service: ping}\n",
    )

    assert result.passed, _details(result)
    # Both packets still went out on the bus, where a real analyser or node would see them.
    assert len(bus.sent) == 2
    assert "request ping to eps" in trace and "reply ping from eps" in trace


def test_with_receive_own_messages_the_kernel_echo_is_not_doubled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _SilentBus()
    _on_silent_bus(monkeypatch, bus, own=True)

    result, _out, _trace = _run(
        SIMULATED_PAIR_ON_SOCKETCAN.format(own="receive_own_messages = true"),
        "  - {action: request, source: obc, target: eps, service: ping, timeout: 200ms}\n",
    )

    # The adapter leaves the echo to the kernel, which this scripted bus does not provide.
    assert not result.passed


def test_a_frame_for_a_real_node_is_not_handed_back() -> None:
    bus = _SilentBus()
    adapter = SocketCanAdapter(interface="vcan0", bus=bus, local_addresses=(1,))

    (to_real,) = fragment_packet(CspFields(2, 1, 3, 10, 10), b"\x00")
    (to_local,) = fragment_packet(CspFields(2, 3, 1, 10, 10), b"\x00")
    adapter.send(to_real)
    adapter.send(to_local)

    received = adapter.receive()
    assert received is not None and received.frame == to_local
    assert adapter.receive() is None
    assert len(bus.sent) == 2
