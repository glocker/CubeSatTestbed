"""CSP v1 over CAN golden vectors: what is committed under ``csp_v1/can/``.

These tests verify the *vectors* themselves (golden vectors are committed and checked
before the codec they will test). They never import a CSP v1 codec. Two things are
compared: the metadata, written from what ``csp_client`` was asked to send, against the
capture, which is what libcsp put on the wire; and the capture against the CFP 1.x layout
libcsp documents in ``csp_if_can.h`` and ``csp_id.h`` (field positions below are copied from
there). A helper that ignored an option, or a libcsp that framed differently than assumed,
fails here rather than becoming the truth the codec is later written against.

CFP1 CAN identifier, 29 bits, most significant first: source (5) | destination (5) |
type (1: 0 begin, 1 more) | remaining frames (8) | CFP identification (10). The first frame
carries the 4-byte CSP v1 header (big-endian: priority 2, source 5, destination 5,
destination port 6, source port 6, flags 8), a 2-byte big-endian payload length and up to two
payload bytes; later frames carry up to eight payload bytes.
"""

from __future__ import annotations

import math

import pytest

from tests.golden_vector_loader import (
    CLASSIC_CAN_MAX_DLC,
    CSP_V1_CAN_VECTOR_DIR,
    EXTENDED_CAN_ID_MAX,
    PINNED_LIBCSP_COMMIT,
    PINNED_LIBCSP_REPO,
    PINNED_LIBCSP_TAG,
    GoldenVector,
    load_golden_vectors,
    parse_can_id_hex,
    parse_payload_hex,
)

VECTORS = load_golden_vectors(CSP_V1_CAN_VECTOR_DIR)

REQUIRED_METADATA_FIELDS = {
    "name",
    "capture",
    "libcsp_repo",
    "libcsp_tag",
    "libcsp_commit",
    "command",
    "capture_command",
    "interface",
    "meaning",
    "protocol",
    "csp_version",
    "source_address",
    "destination_address",
    "priority",
    "destination_port",
    "csp_flags",
    "payload_length",
    "trailer_length",
    "packet_count",
    "expected_frame_count",
    "expected_extended_can_ids",
    "expected_frame_payloads_hex",
    "expected_csp_headers_hex",
    "expected_reassembled_payloads_hex",
}

CFP1_BEGIN_DATA = 2
CFP1_MORE_DATA = 8
PAYLOAD_BASE = 0x55
PING_PORT = 1

# What the v1 matrix has to cover, mirroring the v2 matrix (priority, address width,
# payload bounds, flags, reversed source/destination) plus what only CFP1 has: the
# remaining-frames counter and the identification counter.
REQUIRED_VECTORS = {
    "ping",
    "priority_critical",
    "priority_low",
    "high_address",
    "reverse_direction",
    "payload_empty",
    "payload_single_frame_max",
    "flag_crc32",
    "flag_hmac",
    "multi_frame_3",
    "multi_frame_10",
    "multi_frame_11",
    "multi_frame_18",
    "multi_frame_64",
    "multi_frame_200",
    "multi_frame_crc32",
    "multi_frame_two_packets",
}


def _ids() -> list[str]:
    return [vector.name for vector in VECTORS]


def test_the_vector_matrix_is_complete_and_names_are_unique() -> None:
    names = [vector.name for vector in VECTORS]

    assert len(names) == len(set(names))
    assert REQUIRED_VECTORS <= set(names)


def test_every_capture_has_sibling_metadata_and_the_other_way_round() -> None:
    captures = {path.stem for path in CSP_V1_CAN_VECTOR_DIR.glob("*.txt")}
    metadata = {
        path.name.removesuffix(".meta.toml") for path in CSP_V1_CAN_VECTOR_DIR.glob("*.meta.toml")
    }

    assert captures
    assert captures == metadata


@pytest.mark.parametrize("vector", VECTORS, ids=_ids())
def test_metadata_contract(vector: GoldenVector) -> None:
    metadata = vector.metadata

    assert REQUIRED_METADATA_FIELDS <= metadata.keys()
    assert metadata["name"] == vector.name
    assert vector.metadata_path.name == f"{vector.capture_path.stem}.meta.toml"
    assert metadata["libcsp_repo"] == PINNED_LIBCSP_REPO
    assert metadata["libcsp_tag"] == PINNED_LIBCSP_TAG
    assert metadata["libcsp_commit"] == PINNED_LIBCSP_COMMIT
    assert metadata["csp_version"] == 1
    assert metadata["interface"] == "vcan0"
    assert "csp_client" in metadata["command"]
    assert " -v 1 " in metadata["command"]
    assert "vcan0" in metadata["command"]
    assert "vcan0" in metadata["capture_command"]
    assert "CSP v1" in metadata["protocol"]
    assert metadata["meaning"].strip()


@pytest.mark.parametrize("vector", VECTORS, ids=_ids())
def test_capture_is_classic_extended_can(vector: GoldenVector) -> None:
    assert vector.frames
    for frame in vector.frames:
        assert frame.interface == "vcan0"
        assert frame.is_extended_id
        assert 0 <= frame.can_id <= EXTENDED_CAN_ID_MAX
        assert 0 <= frame.dlc <= CLASSIC_CAN_MAX_DLC
        assert len(frame.payload) == frame.dlc


@pytest.mark.parametrize("vector", VECTORS, ids=_ids())
def test_metadata_matches_the_capture(vector: GoldenVector) -> None:
    metadata = vector.metadata
    frames = vector.frames

    assert metadata["expected_frame_count"] == len(frames)
    assert [frame.can_id for frame in frames] == [
        parse_can_id_hex(item) for item in metadata["expected_extended_can_ids"]
    ]
    assert [frame.payload for frame in frames] == [
        parse_payload_hex(item) for item in metadata["expected_frame_payloads_hex"]
    ]
    assert len(metadata["expected_reassembled_payloads_hex"]) == metadata["packet_count"]
    assert len(metadata["expected_csp_headers_hex"]) == metadata["packet_count"]


def _fields(can_id: int) -> dict[str, int]:
    return {
        "source": (can_id >> 24) & 0x1F,
        "destination": (can_id >> 19) & 0x1F,
        "type": (can_id >> 18) & 0x1,
        "remain": (can_id >> 10) & 0xFF,
        "ident": can_id & 0x3FF,
    }


def _packets(vector: GoldenVector) -> list[list[tuple[dict[str, int], bytes]]]:
    """Group the frames into packets: a packet starts at every ``begin`` frame."""

    packets: list[list[tuple[dict[str, int], bytes]]] = []
    for frame in vector.frames:
        fields = _fields(frame.can_id)
        if fields["type"] == 0:
            packets.append([])
        assert packets, "the capture must start with a begin frame"
        packets[-1].append((fields, frame.payload))
    return packets


@pytest.mark.parametrize("vector", VECTORS, ids=_ids())
def test_capture_follows_the_cfp1_layout(vector: GoldenVector) -> None:
    metadata = vector.metadata
    packets = _packets(vector)
    declared = metadata["payload_length"] + metadata["trailer_length"]
    expected_frames = (
        1
        if declared <= CFP1_BEGIN_DATA
        else 1 + math.ceil((declared - CFP1_BEGIN_DATA) / CFP1_MORE_DATA)
    )

    assert len(packets) == metadata["packet_count"]
    for index, packet in enumerate(packets):
        assert len(packet) == expected_frames

        # CAN identifier: addresses, remaining-frames countdown, one identification number
        # per packet that counts up from packet to packet.
        for position, (fields, _data) in enumerate(packet):
            assert fields["source"] == metadata["source_address"]
            assert fields["destination"] == metadata["destination_address"]
            assert fields["type"] == (0 if position == 0 else 1)
            assert fields["remain"] == expected_frames - 1 - position
            assert fields["ident"] == index

        # First frame: the 4-byte CSP v1 header, then the 2-byte length.
        first = packet[0][1]
        assert len(first) >= 6
        header = int.from_bytes(first[:4], "big")
        assert header >> 30 == metadata["priority"]
        assert (header >> 25) & 0x1F == metadata["source_address"]
        assert (header >> 20) & 0x1F == metadata["destination_address"]
        assert (header >> 14) & 0x3F == metadata["destination_port"] == PING_PORT
        assert header & 0xFF == metadata["csp_flags"]
        assert first[:4] == parse_payload_hex(metadata["expected_csp_headers_hex"][index])
        assert int.from_bytes(first[4:6], "big") == declared

        # Frame sizes: every frame but the last is full; the data adds up to the length.
        data = first[6:] + b"".join(frame_data for _fields_, frame_data in packet[1:])
        assert len(first) - 6 <= CFP1_BEGIN_DATA
        for _fields_, frame_data in packet[1:-1]:
            assert len(frame_data) == CFP1_MORE_DATA
        if len(packet) > 1:
            assert len(first) == 8
            assert 1 <= len(packet[-1][1]) <= CFP1_MORE_DATA
        assert len(data) == declared
        assert data == parse_payload_hex(metadata["expected_reassembled_payloads_hex"][index])

        # The application bytes are the requested incrementing pattern.
        pattern = bytes(
            (PAYLOAD_BASE + offset) & 0xFF for offset in range(metadata["payload_length"])
        )
        assert data[: metadata["payload_length"]] == pattern
        assert len(data) - metadata["payload_length"] == metadata["trailer_length"]


def test_the_source_port_is_libcsps_own_and_changes_between_packets() -> None:
    """The one header field the helper does not choose: libcsp numbers its connections."""

    vector = next(v for v in VECTORS if v.name == "multi_frame_two_packets")
    headers = [
        int.from_bytes(parse_payload_hex(item), "big")
        for item in vector.metadata["expected_csp_headers_hex"]
    ]

    source_port_mask = 0x3F << 8
    assert len({header & source_port_mask for header in headers}) == 2
    # Nothing else about the two headers differs.
    assert len({header & ~source_port_mask for header in headers}) == 1
