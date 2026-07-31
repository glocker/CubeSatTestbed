from __future__ import annotations

import pytest

from cubesat_testbed.protocol.csp_v2 import (
    CSP_FLAG_CRC32,
    CSP_FLAG_FRAGMENT,
    CSP_FLAG_HMAC,
    CSP_FLAG_RDP,
    CSP_PORT_PING,
    CSP_PRIO_NORM,
    CSP_V2_ADDRESS_MAX,
    CSP_V2_FLAGS_MAX,
    CSP_V2_PORT_MAX,
    CSP_V2_PRIORITY_MAX,
    CSP_V2_SINGLE_FRAME_MAX_PAYLOAD_BYTES,
    CspCanFrame,
    CspFields,
    CspV2CodecError,
    CspV2FrameError,
    CspV2RangeError,
    DecodedCspPacket,
    decode_frame,
    pack,
    unpack,
)
from tests.golden_vector_loader import GoldenVector

PING_VECTOR_NAME = "ping_node_2"
PING_SOURCE_PORT = 18
PING_PAYLOAD = b"\x55"


def test_pack_matches_committed_ping_golden_vector(
    golden_vectors: tuple[GoldenVector, ...],
) -> None:
    vector = _get_vector(golden_vectors, PING_VECTOR_NAME)
    frame = pack(
        CspFields(
            priority=CSP_PRIO_NORM,
            source=_metadata_int(vector, "source_address"),
            destination=_metadata_int(vector, "destination_address"),
            destination_port=CSP_PORT_PING,
            source_port=PING_SOURCE_PORT,
            flags=0,
        ),
        PING_PAYLOAD,
    )

    assert frame.is_extended_id
    assert frame.can_id == vector.first_frame.can_id
    assert frame.data == vector.first_frame.payload
    assert frame.can_id_hex == "0x10004083"


def test_unpack_matches_committed_ping_golden_vector(
    golden_vectors: tuple[GoldenVector, ...],
) -> None:
    vector = _get_vector(golden_vectors, PING_VECTOR_NAME)
    decoded = unpack(
        vector.first_frame.can_id,
        vector.first_frame.payload,
        is_extended_id=vector.first_frame.is_extended_id,
    )

    assert decoded.fields == CspFields(
        priority=CSP_PRIO_NORM,
        source=1,
        destination=2,
        destination_port=CSP_PORT_PING,
        source_port=PING_SOURCE_PORT,
        flags=0,
    )
    assert decoded.payload == PING_PAYLOAD
    assert decoded.frame.dlc == 5
    assert decoded.can_fields.sender == 1
    assert decoded.can_fields.packet_count == 0
    assert decoded.can_fields.frame_count == 0
    assert decoded.can_fields.begin
    assert decoded.can_fields.end


def test_every_committed_golden_vector_decodes_and_repacks_byte_for_byte(
    golden_vector: GoldenVector,
) -> None:
    first_frame = golden_vector.first_frame
    decoded = unpack(
        first_frame.can_id, first_frame.payload, is_extended_id=first_frame.is_extended_id
    )
    repacked = pack(
        decoded.fields,
        decoded.payload,
        sender=decoded.can_fields.sender,
        packet_count=decoded.can_fields.packet_count,
    )

    assert repacked.can_id == first_frame.can_id
    assert repacked.data == first_frame.payload


def test_pack_unpack_round_trip_with_explicit_can_metadata() -> None:
    fields = CspFields(
        priority=3,
        source=0x1234,
        destination=0x2345,
        destination_port=12,
        source_port=34,
        flags=CSP_FLAG_CRC32,
    )
    frame = pack(fields, b"ABCD", sender=0x2A, packet_count=3)
    decoded = decode_frame(frame)

    assert decoded == DecodedCspPacket(
        fields=fields,
        payload=b"ABCD",
        frame=frame,
        can_fields=decoded.can_fields,
    )
    assert decoded.can_fields.priority == fields.priority
    assert decoded.can_fields.destination == fields.destination
    assert decoded.can_fields.sender == 0x2A
    assert decoded.can_fields.packet_count == 3
    assert decoded.can_fields.frame_count == 0
    assert decoded.can_fields.begin
    assert decoded.can_fields.end


def test_pack_unpack_round_trip_with_rdp_flag() -> None:
    """No committed golden vector carries CSP_FLAG_RDP: RDP is a stateful,
    connection-oriented protocol layered inside the CSP payload, so a
    one-shot ``csp_client -p`` capture with no responding peer on the bus
    cannot produce one (the SYN handshake blocks forever waiting for a reply
    that never arrives). Unlike CRC32/HMAC, which just append an
    algorithmically-derived trailer to an otherwise ordinary payload, RDP
    cannot be captured this way at all -- so this flag bit is exercised as a
    synthetic pack/decode round trip instead, the same way the CRC32 case
    above already is for its non-golden-vector coverage.
    """

    fields = CspFields(
        priority=2,
        source=1,
        destination=2,
        destination_port=1,
        source_port=18,
        flags=CSP_FLAG_RDP,
    )
    frame = pack(fields, b"\x55")
    decoded = decode_frame(frame)

    assert decoded.fields.flags == CSP_FLAG_RDP


@pytest.mark.parametrize(
    "kwargs",
    [
        {"priority": -1},
        {"priority": CSP_V2_PRIORITY_MAX + 1},
        {"source": -1},
        {"source": CSP_V2_ADDRESS_MAX + 1},
        {"destination": -1},
        {"destination": CSP_V2_ADDRESS_MAX + 1},
        {"destination_port": -1},
        {"destination_port": CSP_V2_PORT_MAX + 1},
        {"source_port": -1},
        {"source_port": CSP_V2_PORT_MAX + 1},
        {"flags": -1},
        {"flags": CSP_V2_FLAGS_MAX + 1},
    ],
)
def test_csp_fields_reject_invalid_ranges(kwargs: dict[str, int]) -> None:
    values = {
        "priority": 0,
        "source": 1,
        "destination": 2,
        "destination_port": 3,
        "source_port": 4,
        "flags": 0,
    }
    values.update(kwargs)

    with pytest.raises(CspV2RangeError):
        CspFields(**values)


def test_pack_rejects_fragment_flag() -> None:
    fields = CspFields(
        priority=0,
        source=1,
        destination=2,
        destination_port=3,
        source_port=4,
        flags=CSP_FLAG_FRAGMENT,
    )

    with pytest.raises(CspV2FrameError, match="CSP_FFRAG"):
        pack(fields, b"")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sender": -1},
        {"sender": 0x40},
        {"packet_count": -1},
        {"packet_count": 4},
    ],
)
def test_pack_rejects_invalid_can_metadata(kwargs: dict[str, int]) -> None:
    fields = CspFields(
        priority=0,
        source=1,
        destination=2,
        destination_port=3,
        source_port=4,
        flags=0,
    )

    with pytest.raises(CspV2RangeError):
        pack(fields, b"", **kwargs)


def test_pack_rejects_oversize_single_frame_payload() -> None:
    fields = CspFields(
        priority=0,
        source=1,
        destination=2,
        destination_port=3,
        source_port=4,
        flags=0,
    )

    with pytest.raises(CspV2FrameError, match="single-frame"):
        pack(fields, b"X" * (CSP_V2_SINGLE_FRAME_MAX_PAYLOAD_BYTES + 1))


def test_unpack_rejects_standard_can_identifiers() -> None:
    with pytest.raises(CspV2FrameError, match="extended"):
        unpack(0x123, bytes(4), is_extended_id=False)


def test_unpack_rejects_oversize_classic_can_data() -> None:
    with pytest.raises(CspV2FrameError, match="classic CAN"):
        unpack(0x10004083, bytes(9))


def test_unpack_rejects_short_begin_frame() -> None:
    with pytest.raises(CspV2FrameError, match="header extension"):
        unpack(0x10004083, bytes(3))


def test_unpack_rejects_fragment_flag() -> None:
    with pytest.raises(CspV2FrameError, match="CSP_FFRAG"):
        unpack(0x10004083, bytes.fromhex("00 04 14 90 55"))


@pytest.mark.parametrize(
    "mutated_can_id",
    [
        0x10004083 & ~0x1,
        0x10004083 & ~0x2,
        0x10004083 | (1 << 2),
    ],
)
def test_unpack_rejects_fragmented_or_continuation_can_frames(mutated_can_id: int) -> None:
    with pytest.raises(CspV2FrameError):
        unpack(mutated_can_id, bytes.fromhex("00 04 14 80 55"))


def test_decode_frame_accepts_can_frame_objects() -> None:
    frame = CspCanFrame(can_id=0x10004083, data=bytes.fromhex("00 04 14 80 55"))

    decoded = decode_frame(frame)

    assert decoded.frame is frame
    assert decoded.payload == PING_PAYLOAD


def test_csp_codec_errors_share_common_base_class() -> None:
    with pytest.raises(CspV2CodecError):
        unpack(0x10004083, bytes(3))


def test_high_address_vector_diverges_sender_from_source(
    golden_vectors: tuple[GoldenVector, ...],
) -> None:
    """CSP v2's CAN-ID 'sender' field is only 6 bits wide; libcsp fills it with
    the low 6 bits of the outgoing interface address. Every other committed
    vector uses addresses below 64, where ``sender == source`` trivially, so a
    bug that silently truncated the 14-bit ``source`` field to those 6 bits
    would go unnoticed by every other test in this file. This vector's
    source address (65) was chosen specifically so the two numbers differ.
    """

    vector = _get_vector(golden_vectors, "high_address")
    decoded = unpack(
        vector.first_frame.can_id,
        vector.first_frame.payload,
        is_extended_id=vector.first_frame.is_extended_id,
    )

    assert decoded.fields.source == 65
    assert decoded.can_fields.sender == 1
    assert decoded.fields.source != decoded.can_fields.sender


@pytest.mark.parametrize(
    ("vector_name", "expected_flags"),
    [
        ("flag_crc32", CSP_FLAG_CRC32),
        ("flag_hmac", CSP_FLAG_HMAC),
    ],
)
def test_flag_vectors_decode_the_expected_single_flag_bit(
    golden_vectors: tuple[GoldenVector, ...],
    vector_name: str,
    expected_flags: int,
) -> None:
    vector = _get_vector(golden_vectors, vector_name)
    decoded = unpack(
        vector.first_frame.can_id,
        vector.first_frame.payload,
        is_extended_id=vector.first_frame.is_extended_id,
    )

    assert decoded.fields.flags == expected_flags


def _get_vector(vectors: tuple[GoldenVector, ...], name: str) -> GoldenVector:
    for vector in vectors:
        if vector.name == name:
            return vector
    raise AssertionError(f"golden vector {name!r} not found")


def _metadata_int(vector: GoldenVector, key: str) -> int:
    value = vector.metadata[key]
    assert isinstance(value, int)
    return value
