"""Contract tests for the committed ZeroMQ golden vectors (captured from libcsp v2.1)."""

from __future__ import annotations

from tests.golden_vector_loader import (
    PINNED_LIBCSP_COMMIT,
    PINNED_LIBCSP_REPO,
    PINNED_LIBCSP_TAG,
    ZMQ_VECTOR_DIR,
    ZmqGoldenVector,
    discover_zmq_vector_metadata,
    parse_payload_hex,
)

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
    "source_address",
    "destination_address",
    "expected_message_count",
    "expected_messages_hex",
    "expected_payloads_hex",
}

CSP_V2_HEADER_BYTES = 6


def test_zmq_vectors_are_committed() -> None:
    assert len(discover_zmq_vector_metadata()) >= 10


def test_every_zmq_capture_has_sibling_metadata() -> None:
    captures = sorted(ZMQ_VECTOR_DIR.glob("*.txt"))

    assert captures
    assert [c.name for c in captures if not c.with_suffix(".meta.toml").is_file()] == []


def test_zmq_vector_metadata_contract(zmq_vector: ZmqGoldenVector) -> None:
    metadata = zmq_vector.metadata

    assert REQUIRED_METADATA_FIELDS <= metadata.keys()
    assert metadata["name"] == zmq_vector.name
    assert zmq_vector.metadata_path.name == f"{zmq_vector.capture_path.stem}.meta.toml"
    assert metadata["libcsp_repo"] == PINNED_LIBCSP_REPO
    assert metadata["libcsp_tag"] == PINNED_LIBCSP_TAG
    assert metadata["libcsp_commit"] == PINNED_LIBCSP_COMMIT
    assert metadata["interface"] == "zmq"
    assert "csp_client" in metadata["command"] and "-z" in metadata["command"]
    assert str(metadata["meaning"]).strip()


def test_zmq_vector_metadata_matches_capture(zmq_vector: ZmqGoldenVector) -> None:
    metadata = zmq_vector.metadata

    assert metadata["expected_message_count"] == len(zmq_vector.messages)
    assert [parse_payload_hex(item) for item in metadata["expected_messages_hex"]] == list(
        zmq_vector.messages
    )
    assert [parse_payload_hex(item) for item in metadata["expected_payloads_hex"]] == [
        message[CSP_V2_HEADER_BYTES:] for message in zmq_vector.messages
    ]


def test_zmq_message_is_header_and_payload_only(zmq_vector: ZmqGoldenVector) -> None:
    """No FEND, no CRC32, no topic byte: the header is the first thing in the message."""

    for message in zmq_vector.messages:
        assert len(message) >= CSP_V2_HEADER_BYTES
        destination = ((message[0] << 8) | message[1]) & 0x3FFF
        assert destination == zmq_vector.metadata["destination_address"]
        source = (int.from_bytes(message[2:6], "big") >> 18) & 0x3FFF
        assert source == zmq_vector.metadata["source_address"]


def test_zmq_flag_vectors_carry_only_the_flags_own_trailer(zmq_vector: ZmqGoldenVector) -> None:
    if zmq_vector.name not in {"flag_crc32", "flag_hmac"}:
        return
    (message,) = zmq_vector.messages

    # 4 application bytes plus libcsp's own 4-byte trailer, and nothing after it.
    assert len(message) == CSP_V2_HEADER_BYTES + 4 + 4
