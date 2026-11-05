"""Contract tests for the committed KISS golden vectors (captured from libcsp v2.1)."""

from __future__ import annotations

import pytest

from tests.golden_vector_loader import (
    KISS_FEND,
    KISS_FESC,
    KISS_TFEND,
    KISS_TFESC,
    KISS_TNC_DATA,
    KISS_VECTOR_DIR,
    PINNED_LIBCSP_COMMIT,
    PINNED_LIBCSP_REPO,
    PINNED_LIBCSP_TAG,
    GoldenVectorError,
    KissGoldenVector,
    discover_kiss_vector_metadata,
    parse_kiss_capture_file,
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
    "expected_frame_count",
    "expected_frames_hex",
    "expected_packets_hex",
}

CSP_V2_HEADER_BYTES = 6
CRC32_BYTES = 4


def unescape_kiss_frame(frame: bytes) -> bytes:
    """Independent reference unescaper, so the vectors are checked without the codec."""

    assert frame[0] == KISS_FEND and frame[-1] == KISS_FEND
    assert frame[1] == KISS_TNC_DATA
    packet = bytearray()
    body = iter(frame[2:-1])
    for byte in body:
        assert byte != KISS_FEND, "an unescaped FEND inside a frame"
        if byte == KISS_FESC:
            escaped = next(body)
            assert escaped in (KISS_TFEND, KISS_TFESC)
            packet.append(KISS_FEND if escaped == KISS_TFEND else KISS_FESC)
        else:
            packet.append(byte)
    return bytes(packet)


def test_kiss_vectors_are_committed() -> None:
    assert len(discover_kiss_vector_metadata()) >= 10


def test_every_kiss_capture_has_sibling_metadata() -> None:
    captures = sorted(KISS_VECTOR_DIR.glob("*.txt"))

    assert captures
    assert [c.name for c in captures if not c.with_suffix(".meta.toml").is_file()] == []


def test_kiss_vector_metadata_contract(kiss_vector: KissGoldenVector) -> None:
    metadata = kiss_vector.metadata

    assert REQUIRED_METADATA_FIELDS <= metadata.keys()
    assert metadata["name"] == kiss_vector.name
    assert kiss_vector.metadata_path.name == f"{kiss_vector.capture_path.stem}.meta.toml"
    assert metadata["libcsp_repo"] == PINNED_LIBCSP_REPO
    assert metadata["libcsp_tag"] == PINNED_LIBCSP_TAG
    assert metadata["libcsp_commit"] == PINNED_LIBCSP_COMMIT
    assert metadata["interface"] == "pty"
    assert "csp_client" in metadata["command"]
    assert "-k" in metadata["command"]
    assert str(metadata["meaning"]).strip()


def test_kiss_vector_metadata_matches_capture(kiss_vector: KissGoldenVector) -> None:
    metadata = kiss_vector.metadata

    assert metadata["expected_frame_count"] == len(kiss_vector.frames)
    assert [parse_payload_hex(item) for item in metadata["expected_frames_hex"]] == list(
        kiss_vector.frames
    )
    assert [parse_payload_hex(item) for item in metadata["expected_packets_hex"]] == [
        unescape_kiss_frame(frame) for frame in kiss_vector.frames
    ]


def test_kiss_frames_are_fend_delimited_with_a_data_command_byte(
    kiss_vector: KissGoldenVector,
) -> None:
    for frame in kiss_vector.frames:
        assert frame[0] == KISS_FEND and frame[-1] == KISS_FEND
        assert frame[1] == KISS_TNC_DATA
        assert KISS_FEND not in frame[1:-1]


def test_kiss_packet_is_header_payload_and_kiss_crc32(kiss_vector: KissGoldenVector) -> None:
    for frame in kiss_vector.frames:
        packet = unescape_kiss_frame(frame)
        assert len(packet) >= CSP_V2_HEADER_BYTES + CRC32_BYTES
        destination = ((packet[0] << 8) | packet[1]) & 0x3FFF
        assert destination == kiss_vector.metadata["destination_address"]
        source = (int.from_bytes(packet[2:6], "big") >> 18) & 0x3FFF
        assert source == kiss_vector.metadata["source_address"]


def test_kiss_capture_parser_rejects_bad_hex(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bad = tmp_path / "bad.txt"
    bad.write_text("C0 00 ZZ C0\n", encoding="utf-8")

    with pytest.raises(GoldenVectorError):
        parse_kiss_capture_file(bad)
