"""CSP v1 golden vectors for the links that carry a packet whole: KISS and ZeroMQ.

Like ``test_golden_vectors_csp_v1.py`` for CAN, these verify the *vectors* before the codecs
that will be written against them, and import no codec. The metadata, written from what
``csp_client`` was asked to send, is compared with the captured wire bytes, and the bytes with
the layout libcsp ``v2.1`` run as CSP v1 gives each link (what the vectors show is in
``tests/golden_vectors/README.md``):

- KISS: ``C0 00 <escaped: header(4) payload crc32c(4)> C0``, the CRC always appended;
- ZeroMQ: ``<destination address (1)> <header(4)> <payload>``, no checksum.

The CSP v1 header is four bytes, big-endian: priority 2, source 5, destination 5, destination
port 6, source port 6, flags 8.
"""

from __future__ import annotations

import pytest

from tests.golden_vector_loader import (
    CSP_V1_KISS_VECTOR_DIR,
    CSP_V1_ZMQ_VECTOR_DIR,
    PINNED_LIBCSP_COMMIT,
    PINNED_LIBCSP_REPO,
    PINNED_LIBCSP_TAG,
    KissGoldenVector,
    ZmqGoldenVector,
    load_kiss_vectors,
    load_zmq_vectors,
    parse_payload_hex,
)

KISS_VECTORS = load_kiss_vectors(CSP_V1_KISS_VECTOR_DIR)
ZMQ_VECTORS = load_zmq_vectors(CSP_V1_ZMQ_VECTOR_DIR)

HEADER_BYTES = 4
PAYLOAD_BASE = 0x55
PING_PORT = 1
FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD

COMMON_FIELDS = {
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
    "expected_csp_headers_hex",
    "expected_payloads_hex",
}
KISS_FIELDS = COMMON_FIELDS | {
    "expected_frame_count",
    "expected_frames_hex",
    "expected_packets_hex",
}
ZMQ_FIELDS = COMMON_FIELDS | {
    "expected_message_count",
    "expected_messages_hex",
    "expected_destination_prefixes",
}

REQUIRED = {
    "ping",
    "payload_empty",
    "payload_64",
    "payload_200",
    "priority_critical",
    "priority_low",
    "high_address",
    "reverse_direction",
    "flag_crc32",
    "flag_hmac",
    "two_packets",
}


def _crc32c(data: bytes) -> int:
    value = 0xFFFFFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ 0x82F63B78 if value & 1 else value >> 1
    return value ^ 0xFFFFFFFF


def _unescape(frame: bytes) -> bytes:
    assert frame[:2] == bytes((FEND, 0x00)) and frame[-1] == FEND
    out = bytearray()
    escaped = False
    for byte in frame[2:-1]:
        assert byte != FEND, "a bare FEND inside a frame"
        if escaped:
            assert byte in (TFEND, TFESC)
            out.append(FESC if byte == TFESC else FEND)
            escaped = False
        elif byte == FESC:
            escaped = True
        else:
            out.append(byte)
    assert not escaped
    return bytes(out)


def _header(header: bytes) -> dict[str, int]:
    value = int.from_bytes(header, "big")
    return {
        "priority": value >> 30,
        "source": (value >> 25) & 0x1F,
        "destination": (value >> 20) & 0x1F,
        "destination_port": (value >> 14) & 0x3F,
        "source_port": (value >> 8) & 0x3F,
        "flags": value & 0xFF,
    }


def _pattern(metadata: dict[str, object]) -> bytes:
    length = metadata["payload_length"]
    assert isinstance(length, int)
    # escape_fend / escape_fesc start the incrementing pattern at 0xC0 / 0xDB.
    base = {"escape_fend": 0xC0, "escape_fesc": 0xDB}.get(str(metadata["name"]), PAYLOAD_BASE)
    return bytes((base + i) & 0xFF for i in range(length))


def _check_asked(metadata: dict[str, object], header: bytes, payload: bytes) -> None:
    fields = _header(header)
    assert fields["priority"] == metadata["priority"]
    assert fields["source"] == metadata["source_address"]
    assert fields["destination"] == metadata["destination_address"]
    assert fields["destination_port"] == metadata["destination_port"] == PING_PORT
    assert fields["flags"] == metadata["csp_flags"]
    length = metadata["payload_length"]
    assert isinstance(length, int)
    assert payload[:length] == _pattern(metadata)
    assert len(payload) == length + int(metadata["trailer_length"])  # type: ignore[call-overload]


def _common_contract(metadata: dict[str, object], vector_name: str, fields: set[str]) -> None:
    assert fields <= metadata.keys()
    assert metadata["name"] == vector_name
    assert metadata["libcsp_repo"] == PINNED_LIBCSP_REPO
    assert metadata["libcsp_tag"] == PINNED_LIBCSP_TAG
    assert metadata["libcsp_commit"] == PINNED_LIBCSP_COMMIT
    assert metadata["csp_version"] == 1
    assert " -v 1" in str(metadata["command"]) and "csp_client" in str(metadata["command"])
    assert "CSP v1" in str(metadata["protocol"])
    assert str(metadata["meaning"]).strip()


def _kiss_ids() -> list[str]:
    return [v.name for v in KISS_VECTORS]


def _zmq_ids() -> list[str]:
    return [v.name for v in ZMQ_VECTORS]


def test_the_matrix_is_complete_for_both_links() -> None:
    assert REQUIRED <= {v.name for v in KISS_VECTORS}
    assert REQUIRED <= {v.name for v in ZMQ_VECTORS}
    assert {"escape_fend", "escape_fesc"} <= {v.name for v in KISS_VECTORS}


def test_every_capture_has_sibling_metadata_and_the_other_way_round() -> None:
    for directory in (CSP_V1_KISS_VECTOR_DIR, CSP_V1_ZMQ_VECTOR_DIR):
        captures = {p.stem for p in directory.glob("*.txt")}
        metadata = {p.name.removesuffix(".meta.toml") for p in directory.glob("*.meta.toml")}
        assert captures
        assert captures == metadata


# --- KISS --------------------------------------------------------------------------------------


@pytest.mark.parametrize("vector", KISS_VECTORS, ids=_kiss_ids())
def test_kiss_metadata_contract(vector: KissGoldenVector) -> None:
    _common_contract(vector.metadata, vector.name, KISS_FIELDS)
    assert vector.metadata["interface"] == "pty"
    assert "-k {tty}" in str(vector.metadata["command"])


@pytest.mark.parametrize("vector", KISS_VECTORS, ids=_kiss_ids())
def test_kiss_metadata_matches_the_capture(vector: KissGoldenVector) -> None:
    metadata = vector.metadata

    assert metadata["expected_frame_count"] == len(vector.frames) == metadata["packet_count"]
    assert list(vector.frames) == [parse_payload_hex(f) for f in metadata["expected_frames_hex"]]
    assert [_unescape(f) for f in vector.frames] == [
        parse_payload_hex(p) for p in metadata["expected_packets_hex"]
    ]


@pytest.mark.parametrize("vector", KISS_VECTORS, ids=_kiss_ids())
def test_kiss_frames_follow_the_layout(vector: KissGoldenVector) -> None:
    metadata = vector.metadata
    for index, frame in enumerate(vector.frames):
        packet = _unescape(frame)
        header, body, trailer = (
            packet[:HEADER_BYTES],
            packet[HEADER_BYTES:-4],
            packet[-4:],
        )
        _check_asked(metadata, header, body)
        # The checksum is always there, over the payload (trailer included) only.
        assert int.from_bytes(trailer, "big") == _crc32c(body)
        assert header == parse_payload_hex(metadata["expected_csp_headers_hex"][index])
        assert body == parse_payload_hex(metadata["expected_payloads_hex"][index])
        # No fragmentation: however long, a packet is one frame.
        assert frame.count(bytes((FEND,))) == 2


def test_kiss_escaping_is_exercised() -> None:
    by_name = {v.name: v for v in KISS_VECTORS}

    assert bytes((FESC, TFEND)) in by_name["escape_fend"].frames[0]
    assert bytes((FESC, TFESC)) in by_name["escape_fesc"].frames[0]


def test_a_kiss_flag_gives_two_checksums() -> None:
    vector = next(v for v in KISS_VECTORS if v.name == "flag_crc32")
    packet = _unescape(vector.frames[0])
    body = packet[HEADER_BYTES:-4]

    # libcsp's CRC32 trailer is part of the payload and is the CRC-32C of the application
    # bytes; the KISS CRC32 then covers the application bytes and that trailer.
    application, trailer = body[:4], body[4:]
    assert int.from_bytes(trailer, "big") == _crc32c(application)
    assert int.from_bytes(packet[-4:], "big") == _crc32c(body)


def test_kiss_source_port_is_libcsps_and_changes_between_packets() -> None:
    vector = next(v for v in KISS_VECTORS if v.name == "two_packets")
    headers = [_header(_unescape(f)[:HEADER_BYTES]) for f in vector.frames]

    assert headers[0]["source_port"] != headers[1]["source_port"]
    assert {k: v for k, v in headers[0].items() if k != "source_port"} == {
        k: v for k, v in headers[1].items() if k != "source_port"
    }


# --- ZeroMQ ------------------------------------------------------------------------------------


@pytest.mark.parametrize("vector", ZMQ_VECTORS, ids=_zmq_ids())
def test_zmq_metadata_contract(vector: ZmqGoldenVector) -> None:
    _common_contract(vector.metadata, vector.name, ZMQ_FIELDS)
    assert vector.metadata["interface"] == "zmq"
    assert "-z" in str(vector.metadata["command"])


@pytest.mark.parametrize("vector", ZMQ_VECTORS, ids=_zmq_ids())
def test_zmq_metadata_matches_the_capture(vector: ZmqGoldenVector) -> None:
    metadata = vector.metadata

    assert metadata["expected_message_count"] == len(vector.messages) == metadata["packet_count"]
    assert list(vector.messages) == [
        parse_payload_hex(m) for m in metadata["expected_messages_hex"]
    ]


@pytest.mark.parametrize("vector", ZMQ_VECTORS, ids=_zmq_ids())
def test_zmq_messages_follow_the_layout(vector: ZmqGoldenVector) -> None:
    metadata = vector.metadata
    for index, message in enumerate(vector.messages):
        prefix, header, body = (
            message[0],
            message[1 : 1 + HEADER_BYTES],
            message[1 + HEADER_BYTES :],
        )
        # The first byte is the destination address (libcsp uses it as the topic), then the
        # header, then the payload: no framing and no checksum of the link's own.
        assert prefix == metadata["destination_address"]
        assert metadata["expected_destination_prefixes"][index] == prefix
        _check_asked(metadata, header, body)
        assert header == parse_payload_hex(metadata["expected_csp_headers_hex"][index])
        assert body == parse_payload_hex(metadata["expected_payloads_hex"][index])


def test_a_zmq_flag_adds_only_the_flags_own_trailer() -> None:
    vector = next(v for v in ZMQ_VECTORS if v.name == "flag_crc32")
    body = vector.messages[0][1 + HEADER_BYTES :]

    application, trailer = body[:4], body[4:]
    assert len(trailer) == 4
    assert int.from_bytes(trailer, "big") == _crc32c(application)
