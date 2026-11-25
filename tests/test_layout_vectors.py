"""The telemetry codec against byte sequences it did not produce (backlog item 17).

Two independent sources: a QUBE beacon decoded by the Kaitai Struct parser of
``satnogs-decoders`` (``tests/layout_vectors/``), and the CSP headers libcsp ``v2.1`` wrote
into the golden vectors, read as bit fields that cross byte boundaries.
"""

from __future__ import annotations

import io
import tomllib
from collections import deque
from collections.abc import Iterator
from pathlib import Path

import can
import pytest

import cubesat_testbed.scenario.runner as runner_module
from cubesat_testbed.config import parse_scenario, parse_testbed_config
from cubesat_testbed.engine import VirtualTime
from cubesat_testbed.modules import SimulatedModule
from cubesat_testbed.modules.registry import register_module, unregister_module
from cubesat_testbed.protocol.csp_v2 import CspFields, fragment_packet
from cubesat_testbed.protocol.telemetry_codec import TelemetryPacketCodec
from cubesat_testbed.scenario import run_scenario
from cubesat_testbed.transport import SocketCanAdapter

TESTS = Path(__file__).resolve().parent
LAYOUT_VECTORS = TESTS / "layout_vectors"
GOLDEN_VECTORS = TESTS / "golden_vectors"

# QUBE telemetry_payload (qube.ksy): name, byte offset, then either ("bytes", length, type,
# endian) or ("bits", bit_offset, bit_length), and an optional count.
QUBE_FIELDS: tuple[tuple[object, ...], ...] = (
    ("beacon_rate", 0, "bytes", 4, "uint", "little"),
    ("uptime", 4, "bytes", 4, "uint", "little"),
    ("blank_012", 8, "bits", 0, 3),
    ("aocs_active", 8, "bits", 3, 1),
    ("radio_2_active", 8, "bits", 4, 1),
    ("powerpath_b_active", 8, "bits", 5, 1),
    ("powerpath_a_active", 8, "bits", 6, 1),
    ("is_mcu_b_active", 8, "bits", 7, 1),
    ("blank_mode", 9, "bits", 0, 2),
    ("panel_xneg_active", 9, "bits", 2, 1),
    ("panel_xpos_active", 9, "bits", 3, 1),
    ("panel_yneg_active", 9, "bits", 4, 1),
    ("panel_ypos_active", 9, "bits", 5, 1),
    ("panel_zneg_active", 9, "bits", 6, 1),
    ("blank_15", 9, "bits", 7, 1),
    ("powerpath_a_temp", 10, "bytes", 1, "int", "little"),
    ("powerpath_a_soc", 11, "bytes", 1, "int", "little"),
    ("powerpath_b_temp", 12, "bytes", 1, "int", "little"),
    ("powerpath_b_soc", 13, "bytes", 1, "int", "little"),
    ("powerpath_a_u", 14, "bytes", 2, "int", "little"),
    ("powerpath_a_i", 16, "bytes", 2, "int", "little"),
    ("powerpath_a_p", 18, "bytes", 2, "int", "little"),
    ("powerpath_b_u", 20, "bytes", 2, "int", "little"),
    ("powerpath_b_i", 22, "bytes", 2, "int", "little"),
    ("powerpath_b_p", 24, "bytes", 2, "int", "little"),
    ("power_total", 26, "bytes", 4, "int", "little"),
    ("temperature_obc", 30, "bytes", 1, "int", "little"),
    ("temperature_panel", 31, "bytes", 1, "int", "little", 5),
    ("frequency_radio", 36, "bytes", 4, "uint", "little"),
    ("seu_rom", 40, "bytes", 1, "uint", "little"),
    ("seu_ram", 41, "bytes", 1, "uint", "little"),
)


def _field_toml(prefix: str, spec: tuple[object, ...]) -> str:
    name, offset, kind, *rest = spec
    lines = [f"[{prefix}.fields.{name}]", f"offset = {offset}"]
    if kind == "bits":
        bit_offset, bit_length = rest
        lines += [f"bit_offset = {bit_offset}", f"bit_length = {bit_length}", 'type = "uint"']
    else:
        length, data_type, endian, *count = rest
        lines += [f"length = {length}", f'type = "{data_type}"', f'endian = "{endian}"']
        if count:
            lines.append(f"count = {count[0]}")
    return "\n".join(lines)


def qube_setup(*, simulated: bool = False) -> str:
    """A setup whose ``qube`` node sends the beacon as one packet.

    By default ``qube`` is a real board on SocketCAN; ``simulated=True`` makes it a
    simulated ``qube_beacon`` module on the in-memory bus, with the very same layout.
    """

    prefix = "nodes.qube.packets.beacon"
    transport = 'type = "in-memory"' if simulated else 'type = "socketcan"\ninterface = "vcan0"'
    node = 'mode = "simulated"\nmodule_type = "qube_beacon"' if simulated else 'mode = "hardware"'
    fields = "\n\n".join(_field_toml(prefix, spec) for spec in QUBE_FIELDS)
    return f"""
[transport]
{transport}

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.qube]
{node}
address = 2

[{prefix}]
source_port = 10
destination_port = 10
to = "obc"

{fields}
"""


def _vector(name: str) -> tuple[bytes, dict[str, object]]:
    meta = tomllib.loads((LAYOUT_VECTORS / f"{name}.meta.toml").read_text(encoding="utf-8"))
    data = bytes.fromhex((LAYOUT_VECTORS / meta["capture"]).read_text(encoding="utf-8"))
    assert len(data) == meta["payload_length"]
    return data, meta["expected"]


def _qube_codec() -> TelemetryPacketCodec:
    setup = parse_testbed_config(qube_setup())
    (definition,) = setup.nodes["qube"].telemetry_packets("qube")
    return TelemetryPacketCodec.build([(name, layout) for name, _s, layout in definition.fields])


def _expected_python(expected: dict[str, object]) -> dict[str, object]:
    return {k: tuple(v) if isinstance(v, list) else v for k, v in expected.items()}


def test_the_qube_beacon_decodes_to_what_the_kaitai_parser_reads() -> None:
    data, expected = _vector("qube_telemetry_payload")

    assert _qube_codec().decode(data) == _expected_python(expected)


def test_the_kaitai_values_encode_back_to_the_same_qube_bytes() -> None:
    data, expected = _vector("qube_telemetry_payload")

    assert _qube_codec().encode(_expected_python(expected)) == data


class _Bus:
    def __init__(self, incoming: list[can.Message]) -> None:
        self.incoming: deque[can.Message] = deque(incoming)

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        pass

    def recv(self, timeout: float | None = None) -> can.Message | None:
        return self.incoming.popleft() if self.incoming else None

    def shutdown(self) -> None:
        pass


def test_a_board_sending_the_qube_beacon_is_asserted_on_field_by_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole path: CFP2 frames from a real board, reassembled, decoded, asserted."""

    data, _expected = _vector("qube_telemetry_payload")
    frames = fragment_packet(CspFields(2, 2, 1, 10, 10), data)
    bus = _Bus(
        [can.Message(arbitration_id=f.can_id, data=f.data, is_extended_id=True) for f in frames]
    )
    monkeypatch.setattr(
        runner_module,
        "build_transport_adapter",
        lambda *_a, **_k: SocketCanAdapter(interface="vcan0", bus=bus),
    )
    setup = parse_testbed_config(qube_setup())
    scenario = parse_scenario(
        """
name: qube beacon
steps:
  - {action: wait, virtual_time: 1s}
  - {action: assert, signal: qube.telemetry.powerpath_a_i, op: "==", value: -350}
  - {action: assert, signal: qube.telemetry.panel_zneg_active, op: "==", value: 1}
  - {action: assert, signal: "qube.telemetry.temperature_panel[3]", op: "==", value: -40}
  - {action: assert, signal: qube.telemetry.temperature_panel, op: "==", value: [34, -12, 5, -40, 127]}
""",
        setup=setup,
    )

    result = run_scenario(scenario, setup, output=io.StringIO())

    assert result.passed, [a.detail for a in result.assertions]


class _QubeBeaconModule(SimulatedModule):
    """Reports the values the Kaitai parser read, as a simulated QUBE would."""

    def __init__(self, name: str, values: dict[str, object]) -> None:
        super().__init__(name)
        self.values = values

    def telemetry(self, *, now: VirtualTime | None = None) -> dict[str, object]:
        return dict(self.values)


@pytest.fixture
def qube_beacon_module() -> Iterator[None]:
    _data, expected = _vector("qube_telemetry_payload")
    register_module(
        "qube_beacon",
        lambda context: _QubeBeaconModule(context.node_name, _expected_python(expected)),
    )
    try:
        yield
    finally:
        unregister_module("qube_beacon")


@pytest.mark.usefixtures("qube_beacon_module")
def test_a_simulated_node_puts_the_qube_bytes_on_the_bus_as_one_packet() -> None:
    """The layout that decodes the independent bytes also produces them, from a module."""

    data, _expected = _vector("qube_telemetry_payload")
    setup = parse_testbed_config(qube_setup(simulated=True))
    scenario = parse_scenario(
        """
name: simulated qube
steps:
  - {action: wait, virtual_time: 1s}
  - {action: assert, signal: qube.telemetry.frequency_radio, op: "==", value: 435525000}
""",
        setup=setup,
    )
    trace = io.StringIO()

    result = run_scenario(scenario, setup, output=io.StringIO(), trace=trace)

    assert result.passed
    packets = [
        line for line in trace.getvalue().splitlines() if " TX " in line and "payload=" in line
    ]
    assert packets
    assert all(f"payload={data.hex()}" in line for line in packets)
    assert all("frames=6" in line for line in packets)  # 42 bytes as CFP2: 4 + 4 x 8 + 6


# --- CSP headers written by libcsp, read as bit fields ----------------------------------------

CSP_V1_HEADER = """
[transport]
type = "in-memory"

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.obc.packets.header]
source_port = 1
destination_port = 1

[nodes.obc.packets.header.fields.priority]
offset = 0
bit_offset = 0
bit_length = 2
type = "uint"

[nodes.obc.packets.header.fields.source]
offset = 0
bit_offset = 2
bit_length = 5
type = "uint"

[nodes.obc.packets.header.fields.destination]
offset = 0
bit_offset = 7
bit_length = 5
type = "uint"

[nodes.obc.packets.header.fields.destination_port]
offset = 1
bit_offset = 4
bit_length = 6
type = "uint"

[nodes.obc.packets.header.fields.source_port]
offset = 2
bit_offset = 2
bit_length = 6
type = "uint"

[nodes.obc.packets.header.fields.flags]
offset = 3
length = 1
type = "uint"
"""

CSP_V2_HEADER = """
[transport]
type = "in-memory"

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.obc.packets.header]
source_port = 1
destination_port = 1

[nodes.obc.packets.header.fields.priority]
offset = 0
bit_offset = 0
bit_length = 2
type = "uint"

[nodes.obc.packets.header.fields.destination]
offset = 0
bit_offset = 2
bit_length = 14
type = "uint"

[nodes.obc.packets.header.fields.source]
offset = 2
bit_offset = 0
bit_length = 14
type = "uint"

[nodes.obc.packets.header.fields.destination_port]
offset = 3
bit_offset = 6
bit_length = 6
type = "uint"

[nodes.obc.packets.header.fields.source_port]
offset = 4
bit_offset = 4
bit_length = 6
type = "uint"

[nodes.obc.packets.header.fields.flags]
offset = 5
bit_offset = 2
bit_length = 6
type = "uint"
"""


def _header_codec(setup_text: str) -> TelemetryPacketCodec:
    setup = parse_testbed_config(setup_text)
    (definition,) = setup.nodes["obc"].telemetry_packets("obc")
    return TelemetryPacketCodec.build([(name, layout) for name, _s, layout in definition.fields])


def _metas(*directories: Path) -> list[Path]:
    return sorted(path for directory in directories for path in directory.glob("*.meta.toml"))


V1_METAS = _metas(GOLDEN_VECTORS / "csp_v1" / "kiss", GOLDEN_VECTORS / "csp_v1" / "zmq")
V2_METAS = _metas(GOLDEN_VECTORS / "zmq")


@pytest.mark.parametrize("meta_path", V1_METAS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_libcsp_v1_headers_decode_as_cross_byte_bit_fields(meta_path: Path) -> None:
    meta = tomllib.loads(meta_path.read_text(encoding="utf-8"))
    codec = _header_codec(CSP_V1_HEADER)
    assert meta["expected_csp_headers_hex"]
    for header_hex in meta["expected_csp_headers_hex"]:
        header = bytes.fromhex(header_hex)

        decoded = codec.decode(header)

        assert decoded["priority"] == meta["priority"]
        assert decoded["source"] == meta["source_address"]
        assert decoded["destination"] == meta["destination_address"]
        assert decoded["destination_port"] == meta["destination_port"]
        assert decoded["flags"] == meta["csp_flags"]
        assert codec.encode(decoded) == header


@pytest.mark.parametrize("meta_path", V2_METAS, ids=lambda p: p.stem)
def test_libcsp_v2_headers_decode_as_14_bit_fields(meta_path: Path) -> None:
    meta = tomllib.loads(meta_path.read_text(encoding="utf-8"))
    codec = _header_codec(CSP_V2_HEADER)
    assert meta["expected_messages_hex"]
    for message_hex in meta["expected_messages_hex"]:
        header = bytes.fromhex(message_hex)[:6]

        decoded = codec.decode(header)

        assert decoded["source"] == meta["source_address"]
        assert decoded["destination"] == meta["destination_address"]
        assert codec.encode(decoded) == header
