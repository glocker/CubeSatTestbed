"""KISS codec: golden vectors from libcsp v2.1 plus the receive-side corner cases."""

from __future__ import annotations

import pytest

from cubesat_testbed.protocol.csp_v2 import CspFields, CspV2FrameError
from cubesat_testbed.protocol.csp_v2_kiss import (
    KISS_FEND,
    KISS_FESC,
    KissDecoder,
    KissDropReason,
    crc32c,
    encode_kiss_frame,
)
from cubesat_testbed.protocol.csp_v2_packet import (
    CSP_V2_HEADER_BYTES,
    pack_header,
    unpack_header,
)
from tests.golden_vector_loader import KissGoldenVector

FIELDS = CspFields(priority=2, source=1, destination=2, destination_port=1, source_port=18)


def _decode(data: bytes, **kwargs: int):  # type: ignore[no-untyped-def]
    return KissDecoder(**kwargs).feed(data)


def test_crc32c_matches_the_standard_check_value() -> None:
    assert crc32c(b"123456789") == 0xE3069283
    assert crc32c(b"") == 0


def test_header_round_trips_every_field_at_its_limit() -> None:
    fields = CspFields(
        priority=3,
        source=0x3FFF,
        destination=0x3FFE,
        destination_port=0x3F,
        source_port=0x3F,
        flags=0x3F,
    )

    assert len(pack_header(fields)) == CSP_V2_HEADER_BYTES
    assert unpack_header(pack_header(fields)) == fields


def test_decoder_and_encoder_reproduce_every_kiss_vector_byte_for_byte(
    kiss_vector: KissGoldenVector,
) -> None:
    decoder = KissDecoder()
    events = [event for frame in kiss_vector.frames for event in decoder.feed(frame)]

    assert all(event.packet is not None for event in events)
    assert [event.packet.wire for event in events if event.packet] == list(kiss_vector.frames)
    for event, frame in zip(events, kiss_vector.frames, strict=True):
        assert event.packet is not None
        assert encode_kiss_frame(event.packet.fields, event.packet.payload) == frame
        assert event.packet.fields.source == kiss_vector.metadata["source_address"]
        assert event.packet.fields.destination == kiss_vector.metadata["destination_address"]


def test_decoder_is_independent_of_how_the_bytes_are_chunked(
    kiss_vector: KissGoldenVector,
) -> None:
    stream = b"".join(kiss_vector.frames)
    whole = [e.packet for e in KissDecoder().feed(stream)]

    decoder = KissDecoder()
    bytewise = [e.packet for byte in stream for e in decoder.feed(bytes([byte]))]

    assert bytewise == whole
    assert len(whole) == len(kiss_vector.frames)


def test_flag_crc32_vector_carries_two_trailers(kiss_vector: KissGoldenVector) -> None:
    if kiss_vector.name != "flag_crc32":
        pytest.skip("only the CRC32-flag vector")
    (event,) = KissDecoder().feed(kiss_vector.frames[0])

    assert event.packet is not None
    assert event.packet.fields.flags == 1
    # 4 application bytes, libcsp's flag trailer, and the KISS CRC32 stripped on receive.
    assert len(event.packet.payload) == 8
    assert event.packet.payload[4:] == crc32c(event.packet.payload[:4]).to_bytes(4, "big")


def test_garbage_between_frames_and_empty_frames_are_ignored() -> None:
    frame = encode_kiss_frame(FIELDS, b"\x55")

    events = _decode(b"\x01\x02\xff" + bytes([KISS_FEND, KISS_FEND]) + frame + frame)

    assert [e.packet is not None for e in events] == [True, True]


def test_a_closing_fend_does_not_double_as_the_next_opening_fend_like_libcsp() -> None:
    """Generic KISS lets frames share a delimiter; libcsp does not, and neither do we."""

    frame = encode_kiss_frame(FIELDS, b"\x55")

    shared = _decode(frame + frame[1:])
    separate = _decode(frame + frame)

    assert [e.packet is not None for e in shared] == [True]
    assert [e.packet is not None for e in separate] == [True, True]


def test_bad_crc_is_dropped_with_the_header_and_the_next_frame_still_decodes() -> None:
    good = encode_kiss_frame(FIELDS, b"\x55")
    corrupted = bytearray(good)
    corrupted[-3] ^= 0x01

    events = _decode(bytes(corrupted) + good)

    assert events[0].reason is KissDropReason.CRC_ERROR
    assert events[0].fields == FIELDS
    assert events[0].wire == bytes(corrupted)
    assert events[1].packet is not None


def test_crc_over_header_and_payload_is_also_accepted_like_libcsp() -> None:
    header = pack_header(FIELDS)
    payload = b"\x01\x02\x03"
    body = header + payload + crc32c(header + payload).to_bytes(4, "big")
    wire = bytes([KISS_FEND, 0]) + body + bytes([KISS_FEND])

    (event,) = _decode(wire)

    assert event.packet is not None and event.packet.payload == payload


def test_a_frame_shorter_than_header_and_crc_is_dropped() -> None:
    (event,) = _decode(bytes([KISS_FEND, 0, 1, 2, 3, KISS_FEND]))

    assert event.reason is KissDropReason.TOO_SHORT


def test_fragment_flag_is_dropped_as_malformed() -> None:
    fields = CspFields(
        priority=2, source=1, destination=2, destination_port=1, source_port=1, flags=0x10
    )

    (event,) = _decode(encode_kiss_frame(fields, b"x"))

    assert event.reason is KissDropReason.MALFORMED


def test_escape_sequences_round_trip_the_control_bytes() -> None:
    payload = bytes([KISS_FEND, KISS_FESC, 0xDC, 0xDD, 0x00])

    wire = encode_kiss_frame(FIELDS, payload)
    (event,) = _decode(wire)

    assert wire.count(bytes([KISS_FEND])) == 2  # only the two delimiters
    assert event.packet is not None and event.packet.payload == payload


def test_unknown_escape_sequence_is_dropped_silently_like_libcsp() -> None:
    header = pack_header(FIELDS)
    payload = b"\x01\x02"
    inner = header + payload + crc32c(payload).to_bytes(4, "big")
    wire = (
        bytes([KISS_FEND, 0])
        + inner[:7]
        + bytes([KISS_FESC, 0x42])
        + inner[7:]
        + bytes([KISS_FEND])
    )

    (event,) = _decode(wire)

    assert event.packet is not None and event.packet.payload == payload


@pytest.mark.parametrize(("payload_length", "accepted"), [(251, True), (252, False)])
def test_receive_buffer_boundary_matches_libcsp(payload_length: int, accepted: bool) -> None:
    """libcsp drops a frame once header plus data reach 262 bytes, one under the buffer."""

    frame = encode_kiss_frame(FIELDS, bytes(payload_length), max_packet_bytes=1000)

    events = _decode(frame)

    if accepted:
        assert [e.packet is not None for e in events] == [True]
    else:
        assert events and events[0].reason is KissDropReason.OVERFLOW
        assert events[0].limit == 256


def test_after_an_overflow_the_decoder_resynchronises_on_the_next_frame() -> None:
    big = encode_kiss_frame(FIELDS, bytes(400), max_packet_bytes=1000)
    good = encode_kiss_frame(FIELDS, b"\x55")

    events = _decode(big + good)

    assert events[0].reason is KissDropReason.OVERFLOW
    assert events[-1].packet is not None and events[-1].packet.payload == b"\x55"


def test_a_larger_receive_buffer_accepts_what_the_default_refuses() -> None:
    frame = encode_kiss_frame(FIELDS, bytes(400), max_packet_bytes=1000)

    (event,) = _decode(frame, max_packet_bytes=1000)

    assert event.packet is not None and len(event.packet.payload) == 400


def test_encoder_refuses_a_packet_over_the_senders_buffer() -> None:
    encode_kiss_frame(FIELDS, bytes(252))  # 252 + 4 CRC32 = 256 fits
    with pytest.raises(CspV2FrameError, match="max_packet_bytes=256"):
        encode_kiss_frame(FIELDS, bytes(253))
    with pytest.raises(CspV2FrameError, match="max_packet_bytes=8"):
        encode_kiss_frame(FIELDS, bytes(5), max_packet_bytes=8)
