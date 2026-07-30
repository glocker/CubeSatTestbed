from __future__ import annotations

import pytest

from tests.golden_vector_loader import (
    CLASSIC_CAN_MAX_DLC,
    EXTENDED_CAN_ID_MAX,
    GOLDEN_VECTOR_DIR,
    PINNED_LIBCSP_COMMIT,
    PINNED_LIBCSP_REPO,
    PINNED_LIBCSP_TAG,
    GoldenVector,
    GoldenVectorError,
    discover_golden_vector_metadata,
    parse_can_id_hex,
    parse_candump_line,
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
}


@pytest.mark.parametrize(
    ("line", "expected_interface", "expected_can_id", "expected_payload", "expected_timestamp"),
    [
        (
            "  vcan0  10004083   [5]  00 04 14 80 55",
            "vcan0",
            0x10004083,
            bytes.fromhex("00 04 14 80 55"),
            None,
        ),
        (
            "(1710000000.125000) vcan0 10004083#0004148055",
            "vcan0",
            0x10004083,
            bytes.fromhex("00 04 14 80 55"),
            1710000000.125,
        ),
    ],
)
def test_parse_candump_data_frame_syntax(
    line: str,
    expected_interface: str,
    expected_can_id: int,
    expected_payload: bytes,
    expected_timestamp: float | None,
) -> None:
    frame = parse_candump_line(line)

    assert frame.interface == expected_interface
    assert frame.can_id == expected_can_id
    assert frame.is_extended_id
    assert frame.dlc == len(expected_payload)
    assert frame.payload == expected_payload
    assert frame.timestamp == expected_timestamp


@pytest.mark.parametrize(
    "line",
    [
        "vcan0 10004083 [9] 00 01 02 03 04 05 06 07 08",
        "vcan0 10004083 [2] 00 01 02",
        "vcan0 10004083#0",
        "vcan0 20000000 [0]",
        "vcan0 10004083##0004148055",
    ],
)
def test_parse_candump_rejects_malformed_or_non_classic_frames(line: str) -> None:
    with pytest.raises(GoldenVectorError):
        parse_candump_line(line)


def test_golden_vector_metadata_files_are_discoverable(
    golden_vectors: tuple[GoldenVector, ...],
) -> None:
    metadata_paths = discover_golden_vector_metadata()

    assert metadata_paths
    assert golden_vectors
    assert {vector.metadata_path for vector in golden_vectors} == set(metadata_paths)


def test_every_committed_capture_has_sibling_metadata() -> None:
    capture_paths = sorted(GOLDEN_VECTOR_DIR.glob("*.txt"))

    assert capture_paths
    missing_metadata = [
        path.name for path in capture_paths if not path.with_suffix(".meta.toml").is_file()
    ]
    assert missing_metadata == []


def test_golden_vector_names_are_unique(golden_vectors: tuple[GoldenVector, ...]) -> None:
    names = [vector.name for vector in golden_vectors]

    assert len(names) == len(set(names))


def test_golden_vector_metadata_contract(golden_vector: GoldenVector) -> None:
    metadata = golden_vector.metadata

    assert REQUIRED_METADATA_FIELDS <= metadata.keys()
    assert _non_empty_str(metadata["name"]) == golden_vector.name
    assert _non_empty_str(metadata["capture"]) == golden_vector.capture_path.name
    assert golden_vector.capture_path.parent == golden_vector.metadata_path.parent
    assert golden_vector.metadata_path.name == f"{golden_vector.capture_path.stem}.meta.toml"

    assert metadata["libcsp_repo"] == PINNED_LIBCSP_REPO
    assert metadata["libcsp_tag"] == PINNED_LIBCSP_TAG
    assert metadata["libcsp_commit"] == PINNED_LIBCSP_COMMIT

    interface = _non_empty_str(metadata["interface"])
    command = _non_empty_str(metadata["command"])
    capture_command = _non_empty_str(metadata["capture_command"])
    meaning = _non_empty_str(metadata["meaning"])

    assert interface == "vcan0"
    assert "vcan0" in command
    assert "vcan0" in capture_command
    assert "csp_client" in command
    assert meaning


def test_golden_vector_capture_frames_are_classic_extended_can(
    golden_vector: GoldenVector,
) -> None:
    expected_interface = _non_empty_str(golden_vector.metadata["interface"])

    assert golden_vector.frames
    for frame in golden_vector.frames:
        assert frame.interface == expected_interface
        assert frame.is_extended_id
        assert 0 <= frame.can_id <= EXTENDED_CAN_ID_MAX
        assert 0 <= frame.dlc <= CLASSIC_CAN_MAX_DLC
        assert len(frame.payload) == frame.dlc
        assert frame.raw_line.strip()
        assert frame.line_number >= 1


def test_golden_vector_expected_values_match_capture(golden_vector: GoldenVector) -> None:
    metadata = golden_vector.metadata
    first_frame = golden_vector.first_frame

    if "expected_extended_can_id" in metadata:
        expected_can_id = parse_can_id_hex(_non_empty_str(metadata["expected_extended_can_id"]))
        assert first_frame.can_id == expected_can_id

    if "expected_payload_hex" in metadata:
        expected_payload = parse_payload_hex(_non_empty_str(metadata["expected_payload_hex"]))
        assert first_frame.payload == expected_payload


def _non_empty_str(value: object) -> str:
    assert isinstance(value, str)
    assert value.strip()
    return value
