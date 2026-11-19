"""CSP v1 over CAN (CFP1): header, fragmentation and reassembly against the golden vectors.

The vectors under ``tests/golden_vectors/csp_v1/can/`` are what libcsp ``v2.1`` (commit
``48f7fb0``), run as CSP v1, put on ``vcan0``; they are the oracle. The codec is checked two
ways: reassembling the captured frames must give the packet the generator asked for, and
fragmenting that packet again must give back the captured frames byte for byte.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from cubesat_testbed.protocol import csp_v1
from cubesat_testbed.protocol.csp_v1 import (
    CFP1_IDENTIFICATION_MAX,
    CSP_FLAG_CRC32,
    CSP_FLAG_FRAGMENT,
    CSP_V1_MAX_PAYLOAD_BYTES,
    CspV1CanFrame,
    CspV1CodecError,
    CspV1Fields,
    CspV1Fragmenter,
    CspV1FrameError,
    CspV1RangeError,
    CspV1Reassembler,
    ReassemblyDropReason,
    decode_can_id,
    decode_first_frame_header,
    decode_frame,
    fragment_packet,
    pack_header,
    unpack_header,
)
from tests.golden_vector_loader import (
    CSP_V1_CAN_VECTOR_DIR,
    GoldenVector,
    load_golden_vectors,
    parse_payload_hex,
)

VECTORS = load_golden_vectors(CSP_V1_CAN_VECTOR_DIR)


def _ids() -> list[str]:
    return [vector.name for vector in VECTORS]


def _frames(vector: GoldenVector) -> list[CspV1CanFrame]:
    return [CspV1CanFrame(f.can_id, f.payload) for f in vector.frames]


def _packets(vector: GoldenVector) -> list[list[CspV1CanFrame]]:
    per_packet = len(vector.frames) // vector.metadata["packet_count"]
    frames = _frames(vector)
    return [frames[i : i + per_packet] for i in range(0, len(frames), per_packet)]


FIELDS = CspV1Fields(priority=2, source=1, destination=2, destination_port=1, source_port=18)


def _frame(packet: int, *, payload: bytes = b"", **kwargs: int) -> tuple[CspV1CanFrame, ...]:
    return fragment_packet(FIELDS, payload, identification=packet, **kwargs)


# --- the golden vectors ------------------------------------------------------------------


@pytest.mark.parametrize("vector", VECTORS, ids=_ids())
def test_reassembling_the_captured_frames_gives_the_requested_packet(
    vector: GoldenVector,
) -> None:
    metadata = vector.metadata
    reassembler = CspV1Reassembler()

    completed = []
    now = 0
    for frame in _frames(vector):
        now += 100
        for event in reassembler.feed(frame, now=now):
            assert event.kind in {"pending", "complete"}, event
            if event.kind == "complete":
                assert event.packet is not None
                completed.append(event.packet)

    assert reassembler.pending_count == 0
    assert len(completed) == metadata["packet_count"]
    for index, packet in enumerate(completed):
        assert packet.fields.priority == metadata["priority"]
        assert packet.fields.source == metadata["source_address"]
        assert packet.fields.destination == metadata["destination_address"]
        assert packet.fields.destination_port == metadata["destination_port"]
        assert packet.fields.flags == metadata["csp_flags"]
        assert pack_header(packet.fields) == parse_payload_hex(
            metadata["expected_csp_headers_hex"][index]
        )
        assert packet.payload == parse_payload_hex(
            metadata["expected_reassembled_payloads_hex"][index]
        )
        assert packet.frame_count == len(vector.frames) // metadata["packet_count"]
        assert packet.can_fields.identification == index


@pytest.mark.parametrize("vector", VECTORS, ids=_ids())
def test_fragmenting_the_packet_again_reproduces_the_captured_frames(
    vector: GoldenVector,
) -> None:
    reassembler = CspV1Reassembler()
    fragmenter = CspV1Fragmenter()

    regenerated: list[CspV1CanFrame] = []
    for packet_frames in _packets(vector):
        packet = next(
            event.packet
            for frame in packet_frames
            for event in reassembler.feed(frame, now=0)
            if event.kind == "complete"
        )
        assert packet is not None
        regenerated.extend(fragmenter.fragment(packet.fields, packet.payload))

    assert [(f.can_id, f.data) for f in regenerated] == [
        (f.can_id, f.data) for f in _frames(vector)
    ]


@pytest.mark.parametrize("vector", VECTORS, ids=_ids())
def test_first_frame_header_is_readable_before_the_packet_ends(vector: GoldenVector) -> None:
    first = _frames(vector)[0]

    fields = decode_first_frame_header(first)

    assert fields.source == vector.metadata["source_address"]
    assert fields.destination == vector.metadata["destination_address"]
    assert decode_can_id(first.can_id).begin


def test_a_single_frame_vector_decodes_on_its_own() -> None:
    ping = next(v for v in VECTORS if v.name == "ping")

    packet = decode_frame(_frames(ping)[0])

    assert packet.payload == b"\x55"
    assert packet.fields == CspV1Fields(
        priority=2, source=1, destination=2, destination_port=1, source_port=18
    )
    assert packet.frame_count == 1


def test_a_multi_frame_vector_does_not_decode_as_a_single_frame() -> None:
    long = next(v for v in VECTORS if v.name == "multi_frame_10")

    with pytest.raises(CspV1FrameError, match="more frame"):
        decode_frame(_frames(long)[0])
    with pytest.raises(CspV1FrameError, match="continuation"):
        decode_frame(_frames(long)[1])


# --- the header --------------------------------------------------------------------------


def test_the_header_is_four_big_endian_bytes() -> None:
    # The ping vector's header: 0x82205200.
    assert pack_header(FIELDS) == bytes.fromhex("82205200")
    assert unpack_header(bytes.fromhex("82205200")) == FIELDS


@pytest.mark.parametrize(
    "fields",
    [
        CspV1Fields(0, 0, 0, 0, 0, 0),
        CspV1Fields(3, 31, 31, 63, 63, 0xFF),
        CspV1Fields(1, 30, 29, 10, 11, CSP_FLAG_CRC32),
    ],
)
def test_header_round_trips(fields: CspV1Fields) -> None:
    assert unpack_header(pack_header(fields)) == fields


@pytest.mark.parametrize(
    "kwargs",
    [
        {"priority": 4},
        {"source": 32},
        {"destination": 32},
        {"destination_port": 64},
        {"source_port": 64},
        {"flags": 0x100},
        {"priority": -1},
        {"source": True},
    ],
)
def test_fields_outside_the_v1_ranges_are_rejected(kwargs: dict[str, int]) -> None:
    base = {
        "priority": 2,
        "source": 1,
        "destination": 2,
        "destination_port": 1,
        "source_port": 1,
    }
    with pytest.raises(CspV1RangeError):
        CspV1Fields(**{**base, **kwargs})  # type: ignore[arg-type]


def test_a_short_header_is_rejected() -> None:
    with pytest.raises(CspV1FrameError):
        unpack_header(b"\x82\x20\x52")


# --- fragmentation -----------------------------------------------------------------------


@pytest.mark.parametrize("length", range(60))
def test_frame_count_and_remaining_follow_the_payload_length(length: int) -> None:
    payload = bytes(range(length))

    frames = _frame(0, payload=payload)

    expected = 1 if length <= 2 else 1 + -(-(length - 2) // 8)
    assert len(frames) == expected
    assert [decode_can_id(f.can_id).remaining for f in frames] == list(range(expected - 1, -1, -1))
    assert [decode_can_id(f.can_id).begin for f in frames] == [True] + [False] * (expected - 1)
    assert all(len(f.data) == 8 for f in frames[:-1])
    reassembled = b"".join(f.data for f in frames)
    assert reassembled[:4] == pack_header(FIELDS)
    assert int.from_bytes(reassembled[4:6], "big") == length
    assert reassembled[6:] == payload


def test_the_identification_is_the_same_in_every_frame_of_a_packet() -> None:
    frames = _frame(7, payload=bytes(30))

    assert {decode_can_id(f.can_id).identification for f in frames} == {7}


def test_a_routed_packet_uses_the_next_hop_as_the_can_destination() -> None:
    frames = _frame(0, payload=b"\x01", via=5)

    can_fields = decode_can_id(frames[0].can_id)
    assert (can_fields.source, can_fields.destination) == (1, 5)
    # The packet's own destination stays in the CSP header.
    assert decode_first_frame_header(frames[0]).destination == 2


def test_the_largest_payload_the_remaining_field_can_describe() -> None:
    frames = _frame(0, payload=bytes(CSP_V1_MAX_PAYLOAD_BYTES))

    assert len(frames) == 256
    assert decode_can_id(frames[0].can_id).remaining == 255
    with pytest.raises(CspV1FrameError, match="at most"):
        _frame(0, payload=bytes(CSP_V1_MAX_PAYLOAD_BYTES + 1))


def test_a_packet_with_the_fragmentation_flag_is_refused() -> None:
    fields = CspV1Fields(2, 1, 2, 1, 1, CSP_FLAG_FRAGMENT)

    with pytest.raises(CspV1FrameError, match="CSP_FFRAG"):
        fragment_packet(fields, b"")


def test_the_fragmenter_counts_packets_per_sender_and_wraps() -> None:
    fragmenter = CspV1Fragmenter()
    other = CspV1Fields(2, 3, 2, 1, 1)

    identifications = [
        decode_can_id(fragmenter.fragment(FIELDS, b"x")[0].can_id).identification
        for _ in range(CFP1_IDENTIFICATION_MAX + 3)
    ]
    other_first = decode_can_id(fragmenter.fragment(other, b"x")[0].can_id).identification

    assert identifications[:3] == [0, 1, 2]
    assert identifications[CFP1_IDENTIFICATION_MAX] == CFP1_IDENTIFICATION_MAX
    assert identifications[CFP1_IDENTIFICATION_MAX + 1 :] == [0, 1]  # 10 bits, then wraps
    assert other_first == 0  # another sender has its own counter


def test_the_fragmenter_refuses_a_payload_over_the_nodes_buffer() -> None:
    fragmenter = CspV1Fragmenter(max_packet_bytes=8)

    assert len(fragmenter.fragment(FIELDS, bytes(8))) == 2
    with pytest.raises(CspV1FrameError, match="max_packet_bytes=8"):
        fragmenter.fragment(FIELDS, bytes(9))
    with pytest.raises(CspV1RangeError):
        CspV1Fragmenter(max_packet_bytes=0)


# --- reassembly --------------------------------------------------------------------------


def _drops(events: tuple[csp_v1.ReassemblyEvent, ...]) -> list[ReassemblyEvent]:
    return [event for event in events if event.kind == "dropped"]


ReassemblyEvent = csp_v1.ReassemblyEvent


def test_a_lost_frame_drops_the_packet() -> None:
    first, second, third = _frame(0, payload=bytes(range(18)))
    reassembler = CspV1Reassembler()

    reassembler.feed(first, now=0)
    events = reassembler.feed(third, now=10)  # the second frame never arrived

    (drop,) = _drops(events)
    assert drop.reason is ReassemblyDropReason.FRAME_LOST
    assert (drop.sender, drop.destination) == (1, 2)
    assert "expected remaining=1, got 0" in drop.detail
    assert reassembler.pending_count == 0
    # What is still on the way no longer belongs to a packet.
    assert (
        _drops(reassembler.feed(second, now=20))[0].reason is ReassemblyDropReason.ORPHAN_FRAGMENT
    )


def test_a_continuation_frame_with_no_packet_is_an_orphan() -> None:
    _first, second = _frame(0, payload=bytes(5))

    (drop,) = _drops(CspV1Reassembler().feed(second, now=0))

    assert drop.reason is ReassemblyDropReason.ORPHAN_FRAGMENT


def test_a_surplus_frame_after_the_countdown_ended_counts_as_lost() -> None:
    # The declared length says 20 bytes but the countdown claims three frames carry 10.
    frames = list(_frame(0, payload=bytes(10)))
    first = frames[0]
    lying = CspV1CanFrame(first.can_id, first.data[:4] + (20).to_bytes(2, "big") + first.data[6:])
    reassembler = CspV1Reassembler()

    reassembler.feed(lying, now=0)
    reassembler.feed(frames[1], now=1)  # remaining 0: the countdown is over, 10 of 20 bytes in
    events = reassembler.feed(frames[1], now=2)  # one more frame than the countdown allows

    assert [e.reason for e in _drops(events)] == [ReassemblyDropReason.FRAME_LOST]


def test_a_new_first_frame_restarts_a_packet_in_progress() -> None:
    first, *_ = _frame(0, payload=bytes(20))
    reassembler = CspV1Reassembler()
    reassembler.feed(first, now=0)

    events = reassembler.feed(first, now=5)

    assert [e.reason for e in _drops(events)] == [ReassemblyDropReason.RESTARTED]
    assert reassembler.pending_count == 1  # the new one is in progress


def test_a_packet_longer_than_the_receivers_buffer_is_dropped_at_its_first_frame() -> None:
    first, *rest = _frame(0, payload=bytes(40))
    reassembler = CspV1Reassembler(max_packet_bytes=32)

    (drop,) = _drops(reassembler.feed(first, now=0))

    assert drop.reason is ReassemblyDropReason.OVERFLOW
    assert drop.limit == 32
    assert "40" in drop.detail
    assert reassembler.pending_count == 0
    # Its remaining frames are strays now.
    assert (
        _drops(reassembler.feed(rest[0], now=1))[0].reason is ReassemblyDropReason.ORPHAN_FRAGMENT
    )


def test_a_packet_exactly_the_size_of_the_buffer_fits() -> None:
    reassembler = CspV1Reassembler(max_packet_bytes=32)

    events = [e for f in _frame(0, payload=bytes(32)) for e in reassembler.feed(f, now=0)]

    assert events[-1].kind == "complete"
    assert not _drops(tuple(events))


def test_without_a_limit_any_size_is_reassembled() -> None:
    reassembler = CspV1Reassembler(max_packet_bytes=None)

    events = [e for f in _frame(0, payload=bytes(1000)) for e in reassembler.feed(f, now=0)]

    assert events[-1].kind == "complete"
    assert events[-1].packet is not None
    assert len(events[-1].packet.payload) == 1000


def test_more_data_than_the_length_field_declared_is_malformed() -> None:
    first, second = _frame(0, payload=bytes(10))
    short = CspV1CanFrame(first.can_id, first.data[:4] + (5).to_bytes(2, "big") + first.data[6:])
    reassembler = CspV1Reassembler()
    reassembler.feed(short, now=0)

    (drop,) = _drops(reassembler.feed(second, now=1))

    assert drop.reason is ReassemblyDropReason.MALFORMED
    assert "declared" in drop.detail


def test_a_first_frame_without_the_length_field_is_malformed() -> None:
    first, *_ = _frame(0, payload=b"")
    truncated = CspV1CanFrame(first.can_id, first.data[:5])

    (drop,) = _drops(CspV1Reassembler().feed(truncated, now=0))

    assert drop.reason is ReassemblyDropReason.MALFORMED


def test_a_standard_identifier_frame_is_malformed() -> None:
    frame = CspV1CanFrame(0x123, b"\x00", is_extended_id=False)

    (drop,) = _drops(CspV1Reassembler().feed(frame, now=0))

    assert drop.reason is ReassemblyDropReason.MALFORMED


def test_a_completed_packet_with_the_fragmentation_flag_is_malformed() -> None:
    first, *_ = _frame(0, payload=b"\x01")
    flagged = CspV1CanFrame(
        first.can_id,
        pack_header(CspV1Fields(2, 1, 2, 1, 18, CSP_FLAG_FRAGMENT)) + first.data[4:],
    )

    (drop,) = _drops(CspV1Reassembler().feed(flagged, now=0))

    assert drop.reason is ReassemblyDropReason.MALFORMED
    assert "CSP_FFRAG" in drop.detail


def test_two_connections_interleaved_are_reassembled_independently() -> None:
    a = _frame(0, payload=bytes(range(20)))
    b_fields = CspV1Fields(2, 3, 2, 1, 5)
    b = fragment_packet(b_fields, bytes(range(100, 120)), identification=0)
    c = _frame(1, payload=bytes(range(50, 70)))  # same endpoints as a, next identification
    reassembler = CspV1Reassembler()

    completed = []
    for frames in zip(a, b, c, strict=True):
        for frame in frames:
            completed += [e.packet for e in reassembler.feed(frame, now=0) if e.kind == "complete"]

    assert sorted(p.payload[0] for p in completed if p is not None) == [0, 50, 100]
    assert reassembler.pending_count == 0


def test_a_stalled_packet_times_out_in_virtual_time() -> None:
    first, *_ = _frame(0, payload=bytes(20))
    reassembler = CspV1Reassembler(timeout_us=1_000_000)
    reassembler.feed(first, now=100)

    assert reassembler.expire(1_000_100) == ()  # idle for exactly the timeout: not yet
    (drop,) = reassembler.expire(1_000_101)

    assert drop.reason is ReassemblyDropReason.TIMEOUT
    assert drop.time == 1_000_100  # when the window closed, not when it was noticed
    assert drop.frame is None
    assert reassembler.pending_count == 0


def test_each_frame_restarts_the_timeout() -> None:
    first, second, third = _frame(0, payload=bytes(18))
    reassembler = CspV1Reassembler(timeout_us=1_000_000)
    reassembler.feed(first, now=0)
    reassembler.feed(second, now=900_000)

    assert reassembler.expire(1_800_000) == ()
    events = reassembler.feed(third, now=1_800_000)
    assert events[-1].kind == "complete"


def test_a_new_packet_flushes_stale_ones_like_libcsp_does() -> None:
    stale, *_ = _frame(0, payload=bytes(20))
    fresh, *_ = _frame(1, payload=bytes(20))
    reassembler = CspV1Reassembler()
    reassembler.feed(stale, now=0)

    events = reassembler.feed(fresh, now=2_000_000)

    assert [e.reason for e in _drops(events)] == [ReassemblyDropReason.TIMEOUT]
    assert reassembler.pending_count == 1


# --- the module's independence ----------------------------------------------------------


def test_the_v1_codec_does_not_import_the_v2_codec() -> None:
    """The two protocol versions share no codec code (see AGENTS.md, Protocol rules)."""

    source = Path(csp_v1.__file__).read_text(encoding="utf-8")
    imported = {
        alias.name if isinstance(node, ast.Import) else node.module or ""
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (node.names if isinstance(node, ast.Import) else [None])
    }

    assert not any("csp_v2" in name for name in imported)
    assert issubclass(CspV1FrameError, CspV1CodecError)
