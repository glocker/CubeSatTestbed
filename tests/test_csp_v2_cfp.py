"""CFP2 fragmentation and reassembly against libcsp v2.1 golden vectors."""

from __future__ import annotations

import pytest

from cubesat_testbed.protocol.csp_v2 import (
    CSP_FLAG_CRC32,
    CSP_V2_DEFAULT_MAX_PACKET_BYTES,
    CSP_V2_REASSEMBLY_TIMEOUT_US,
    CspCanFrame,
    CspFields,
    CspFragmenter,
    CspReassembler,
    CspV2FrameError,
    CspV2RangeError,
    ReassemblyDropReason,
    ReassemblyEvent,
    decode_can_id,
    decode_frame,
    fragment_packet,
    pack,
)
from tests.golden_vector_loader import GoldenVector, parse_payload_hex

PING_FIELDS = CspFields(
    priority=2, source=1, destination=2, destination_port=1, source_port=18, flags=0
)


def _multi_frame_vectors(golden_vectors: tuple[GoldenVector, ...]) -> list[GoldenVector]:
    vectors = [vector for vector in golden_vectors if "expected_frame_count" in vector.metadata]
    assert vectors
    return vectors


def _frames(vector: GoldenVector) -> list[CspCanFrame]:
    return [CspCanFrame(can_id=f.can_id, data=f.payload) for f in vector.frames]


def _completed(events: tuple[ReassemblyEvent, ...]) -> list[ReassemblyEvent]:
    return [event for event in events if event.kind == "complete"]


def _dropped(events: tuple[ReassemblyEvent, ...]) -> list[ReassemblyEvent]:
    return [event for event in events if event.kind == "dropped"]


def _reassemble_vector(vector: GoldenVector) -> list[ReassemblyEvent]:
    reassembler = CspReassembler()
    completed: list[ReassemblyEvent] = []
    for frame in _frames(vector):
        completed.extend(_completed(reassembler.feed(frame, now=0)))
    assert reassembler.pending_count == 0
    return completed


def test_reassembler_rebuilds_every_multi_frame_vector_payload(
    golden_vectors: tuple[GoldenVector, ...],
) -> None:
    for vector in _multi_frame_vectors(golden_vectors):
        expected = [
            parse_payload_hex(item) for item in vector.metadata["expected_reassembled_payloads_hex"]
        ]
        completed = _reassemble_vector(vector)

        assert [event.packet.payload for event in completed if event.packet] == expected, (
            vector.name
        )
        for event in completed:
            assert event.packet is not None
            assert event.packet.frame_count == len(vector.frames) // len(expected)
            assert event.packet.fields.source == vector.metadata["source_address"]
            assert event.packet.fields.destination == vector.metadata["destination_address"]


def test_fragment_packet_reproduces_every_multi_frame_vector_byte_for_byte(
    golden_vectors: tuple[GoldenVector, ...],
) -> None:
    for vector in _multi_frame_vectors(golden_vectors):
        completed = _reassemble_vector(vector)
        regenerated: list[CspCanFrame] = []
        for event in completed:
            packet = event.packet
            assert packet is not None
            regenerated.extend(
                fragment_packet(
                    packet.fields,
                    packet.payload,
                    sender=packet.can_fields.sender,
                    packet_count=packet.can_fields.packet_count,
                )
            )

        assert [(f.can_id, f.data) for f in regenerated] == [
            (f.can_id, f.payload) for f in vector.frames
        ], vector.name


def test_fragmenter_advances_the_packet_counter_like_libcsp(
    golden_vectors: tuple[GoldenVector, ...],
) -> None:
    vector = next(v for v in golden_vectors if v.name == "multi_frame_two_packets")
    first, second = (event.packet for event in _reassemble_vector(vector))
    assert first is not None and second is not None

    fragmenter = CspFragmenter()
    frames = fragmenter.fragment(first.fields, first.payload) + fragmenter.fragment(
        second.fields, second.payload
    )

    assert [(f.can_id, f.data) for f in frames] == [(f.can_id, f.payload) for f in vector.frames]


def test_frame_count_wraps_from_7_to_0_in_the_counter_wrap_vector(
    golden_vectors: tuple[GoldenVector, ...],
) -> None:
    vector = next(v for v in golden_vectors if v.name == "multi_frame_64_counter_wrap")

    counts = [decode_can_id(frame.can_id).frame_count for frame in _frames(vector)]

    assert counts == [0, 1, 2, 3, 4, 5, 6, 7, 0]


def test_crc32_trailer_counts_as_payload_and_forces_a_second_frame(
    golden_vectors: tuple[GoldenVector, ...],
) -> None:
    vector = next(v for v in golden_vectors if v.name == "multi_frame_crc32")
    (event,) = _reassemble_vector(vector)

    assert event.packet is not None
    assert event.packet.fields.flags == CSP_FLAG_CRC32
    assert len(event.packet.payload) == 8
    assert event.packet.frame_count == 2


def test_single_frame_packet_is_one_frame_identical_to_pack() -> None:
    assert fragment_packet(PING_FIELDS, b"\x55") == (pack(PING_FIELDS, b"\x55"),)
    assert fragment_packet(PING_FIELDS, b"") == (pack(PING_FIELDS, b""),)
    assert len(fragment_packet(PING_FIELDS, b"\x01" * 4)) == 1
    assert len(fragment_packet(PING_FIELDS, b"\x01" * 5)) == 2


@pytest.mark.parametrize("length", [0, 1, 4, 5, 8, 12, 13, 20, 64, 100, 256])
def test_fragment_then_reassemble_round_trips_any_length(length: int) -> None:
    payload = bytes(index % 251 for index in range(length))
    frames = fragment_packet(PING_FIELDS, payload, packet_count=2)
    reassembler = CspReassembler()

    events = [event for frame in frames for event in reassembler.feed(frame, now=10)]

    assert [event.kind for event in events] == ["pending"] * (len(frames) - 1) + ["complete"]
    assert events[-1].packet is not None
    assert events[-1].packet.payload == payload
    assert events[-1].packet.fields == PING_FIELDS
    assert all(len(frame.data) <= 8 for frame in frames)


def test_two_senders_interleaving_frames_reassemble_independently() -> None:
    a = fragment_packet(PING_FIELDS, b"A" * 12)
    other = CspFields(
        priority=2, source=3, destination=2, destination_port=1, source_port=19, flags=0
    )
    b = fragment_packet(other, b"B" * 12)
    reassembler = CspReassembler()

    events = [reassembler.feed(frame, now=0) for frame in (a[0], b[0], a[1], b[1])]
    completed = [e for batch in events for e in _completed(batch)]

    assert [e.packet.payload for e in completed if e.packet] == [b"A" * 12, b"B" * 12]


def test_lost_frame_drops_the_whole_packet_and_orphans_the_rest() -> None:
    frames = fragment_packet(PING_FIELDS, bytes(range(28)))  # 4 frames
    assert len(frames) == 4
    reassembler = CspReassembler()

    events = reassembler.feed(frames[0], now=0)
    assert [e.kind for e in events] == ["pending"]
    lost = reassembler.feed(frames[2], now=1)  # frames[1] never arrives
    assert [e.reason for e in _dropped(lost)] == [ReassemblyDropReason.FRAME_LOST]
    assert "expected frame_count=1, got 2" in _dropped(lost)[0].detail
    orphan = reassembler.feed(frames[3], now=2)
    assert [e.reason for e in _dropped(orphan)] == [ReassemblyDropReason.ORPHAN_FRAGMENT]
    assert reassembler.pending_count == 0


def test_continuation_without_a_first_frame_is_an_orphan() -> None:
    frames = fragment_packet(PING_FIELDS, bytes(range(12)))

    events = CspReassembler().feed(frames[1], now=0)

    assert [e.reason for e in events] == [ReassemblyDropReason.ORPHAN_FRAGMENT]


def test_overflow_is_dropped_at_the_receiving_nodes_limit_and_reports_it() -> None:
    frames = fragment_packet(PING_FIELDS, b"\xaa" * 20)
    reassembler = CspReassembler(max_packet_bytes=16)

    events = [event for frame in frames for event in reassembler.feed(frame, now=0)]

    dropped = _dropped(tuple(events))
    assert [e.reason for e in dropped] == [ReassemblyDropReason.OVERFLOW]
    assert dropped[0].limit == 16
    assert "max_packet_bytes=16" in dropped[0].detail
    assert not _completed(tuple(events))
    assert reassembler.pending_count == 0


def test_packet_exactly_at_the_limit_is_accepted() -> None:
    frames = fragment_packet(PING_FIELDS, b"\xaa" * 16)
    reassembler = CspReassembler(max_packet_bytes=16)

    events = [event for frame in frames for event in reassembler.feed(frame, now=0)]

    assert len(_completed(tuple(events))) == 1


def test_default_limit_matches_libcsp_buffer_size() -> None:
    assert CSP_V2_DEFAULT_MAX_PACKET_BYTES == 256
    ok = fragment_packet(PING_FIELDS, b"\x00" * 256)
    too_big = fragment_packet(PING_FIELDS, b"\x00" * 257)

    reassembler = CspReassembler()
    assert len(_completed(tuple(e for f in ok for e in reassembler.feed(f, now=0)))) == 1
    reassembler = CspReassembler()
    events = tuple(e for f in too_big for e in reassembler.feed(f, now=0))
    assert [e.reason for e in _dropped(events)] == [ReassemblyDropReason.OVERFLOW]


def test_incomplete_packet_times_out_in_virtual_time() -> None:
    frames = fragment_packet(PING_FIELDS, bytes(range(12)))
    reassembler = CspReassembler()
    reassembler.feed(frames[0], now=100)

    assert reassembler.expire(100 + CSP_V2_REASSEMBLY_TIMEOUT_US) == ()  # boundary: not yet
    (event,) = reassembler.expire(100 + CSP_V2_REASSEMBLY_TIMEOUT_US + 1)

    assert event.reason is ReassemblyDropReason.TIMEOUT
    assert event.time == 100 + CSP_V2_REASSEMBLY_TIMEOUT_US
    assert event.frame is None
    assert reassembler.pending_count == 0


def test_stale_partial_is_discarded_when_a_new_packet_starts() -> None:
    stale = fragment_packet(PING_FIELDS, bytes(range(12)), packet_count=0)
    fresh = fragment_packet(PING_FIELDS, b"\x01", packet_count=1)
    reassembler = CspReassembler()
    reassembler.feed(stale[0], now=0)

    events = reassembler.feed(fresh[0], now=CSP_V2_REASSEMBLY_TIMEOUT_US + 5)

    assert [e.reason for e in _dropped(events)] == [ReassemblyDropReason.TIMEOUT]
    assert len(_completed(events)) == 1


def test_each_frame_refreshes_the_timeout_window() -> None:
    frames = fragment_packet(PING_FIELDS, bytes(range(30)))
    reassembler = CspReassembler()
    step = CSP_V2_REASSEMBLY_TIMEOUT_US - 1
    events = [e for i, f in enumerate(frames) for e in reassembler.feed(f, now=i * step)]

    assert len(_completed(tuple(events))) == 1


def test_new_first_frame_restarts_a_packet_in_progress() -> None:
    frames = fragment_packet(PING_FIELDS, bytes(range(12)))
    reassembler = CspReassembler()
    reassembler.feed(frames[0], now=0)

    restart = reassembler.feed(frames[0], now=1)
    done = reassembler.feed(frames[1], now=2)

    assert [e.reason for e in _dropped(restart)] == [ReassemblyDropReason.RESTARTED]
    assert len(_completed(done)) == 1


def test_standard_id_frame_and_short_first_frame_are_dropped_as_malformed() -> None:
    reassembler = CspReassembler()
    standard = reassembler.feed(
        CspCanFrame(can_id=0x123, data=bytes(4), is_extended_id=False), now=0
    )
    short = reassembler.feed(CspCanFrame(can_id=0x10004082, data=bytes(3)), now=0)

    assert [e.reason for e in standard] == [ReassemblyDropReason.MALFORMED]
    assert [e.reason for e in short] == [ReassemblyDropReason.MALFORMED]
    assert reassembler.pending_count == 0


def test_unlimited_observer_reassembler_accepts_any_size() -> None:
    frames = fragment_packet(PING_FIELDS, b"\x00" * 1000)
    reassembler = CspReassembler(max_packet_bytes=None)

    events = tuple(e for f in frames for e in reassembler.feed(f, now=0))

    assert len(_completed(events)) == 1


def test_fragmenter_refuses_payload_over_the_senders_limit() -> None:
    fragmenter = CspFragmenter(max_packet_bytes=8)

    assert len(fragmenter.fragment(PING_FIELDS, b"\x00" * 8)) == 2
    with pytest.raises(CspV2FrameError, match="max_packet_bytes=8"):
        fragmenter.fragment(PING_FIELDS, b"\x00" * 9)


def test_packet_counter_wraps_at_four_and_is_tracked_per_sender() -> None:
    fragmenter = CspFragmenter()
    other = CspFields(
        priority=2, source=3, destination=2, destination_port=1, source_port=19, flags=0
    )

    counts = [
        decode_can_id(fragmenter.fragment(PING_FIELDS, b"x")[0].can_id).packet_count
        for _ in range(5)
    ]
    other_first = decode_can_id(fragmenter.fragment(other, b"x")[0].can_id).packet_count

    assert counts == [0, 1, 2, 3, 0]
    assert other_first == 0


def test_range_validation() -> None:
    with pytest.raises(CspV2RangeError):
        CspReassembler(max_packet_bytes=0)
    with pytest.raises(CspV2RangeError):
        CspFragmenter(max_packet_bytes=0)


def test_decode_frame_still_rejects_frames_of_a_multi_frame_packet() -> None:
    first, second = fragment_packet(PING_FIELDS, b"\x00" * 12)

    with pytest.raises(CspV2FrameError):
        decode_frame(first)
    with pytest.raises(CspV2FrameError):
        decode_frame(second)
