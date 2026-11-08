"""ZeroMQ codec: golden vectors from libcsp v2.1 plus the receive-side corner cases."""

from __future__ import annotations

import pytest

from cubesat_testbed.protocol.csp_v2 import CspFields, CspV2FrameError
from cubesat_testbed.protocol.csp_v2_packet import CSP_V2_HEADER_BYTES, pack_header
from cubesat_testbed.protocol.csp_v2_zmq import (
    ZmqDropReason,
    decode_zmq_message,
    encode_zmq_message,
)
from tests.golden_vector_loader import ZmqGoldenVector

FIELDS = CspFields(priority=2, source=1, destination=2, destination_port=1, source_port=18)


def test_decoder_and_encoder_reproduce_every_zmq_vector_byte_for_byte(
    zmq_vector: ZmqGoldenVector,
) -> None:
    for message in zmq_vector.messages:
        event = decode_zmq_message(message)

        assert event.packet is not None, event
        assert event.packet.wire == message and event.packet.link == "zmq"
        assert event.packet.fields.source == zmq_vector.metadata["source_address"]
        assert event.packet.fields.destination == zmq_vector.metadata["destination_address"]
        assert encode_zmq_message(event.packet.fields, event.packet.payload) == message


def test_a_message_is_exactly_the_header_and_the_payload() -> None:
    message = encode_zmq_message(FIELDS, b"\x01\x02\x03")

    assert message == pack_header(FIELDS) + b"\x01\x02\x03"
    assert len(message) == CSP_V2_HEADER_BYTES + 3


def test_a_flag_packet_gets_no_second_crc32_unlike_kiss(zmq_vector: ZmqGoldenVector) -> None:
    if zmq_vector.name != "flag_crc32":
        pytest.skip("only the CRC32-flag vector")
    event = decode_zmq_message(zmq_vector.messages[0])

    assert event.packet is not None and event.packet.fields.flags == 1
    # 4 application bytes plus the flag's own trailer, nothing appended by the link.
    assert len(event.packet.payload) == 8


def test_a_message_shorter_than_a_header_is_dropped() -> None:
    event = decode_zmq_message(b"\x80\x02\x00")

    assert event.packet is None and event.reason is ZmqDropReason.TOO_SHORT


def test_fragment_flag_is_dropped_as_malformed() -> None:
    fields = CspFields(
        priority=2, source=1, destination=2, destination_port=1, source_port=1, flags=0x10
    )

    event = decode_zmq_message(encode_zmq_message(fields, b"x"))

    assert event.reason is ZmqDropReason.MALFORMED and event.fields == fields


@pytest.mark.parametrize(("payload_length", "accepted"), [(256, True), (257, False)])
def test_receive_limit_is_the_payload_against_max_packet_bytes(
    payload_length: int, accepted: bool
) -> None:
    message = pack_header(FIELDS) + bytes(payload_length)

    event = decode_zmq_message(message)

    if accepted:
        assert event.packet is not None
    else:
        assert event.reason is ZmqDropReason.OVERFLOW
        assert event.limit == 256 and event.fields == FIELDS


def test_a_larger_receive_buffer_accepts_what_the_default_refuses() -> None:
    event = decode_zmq_message(pack_header(FIELDS) + bytes(400), max_packet_bytes=1000)

    assert event.packet is not None and len(event.packet.payload) == 400


def test_encoder_refuses_a_payload_over_the_senders_buffer() -> None:
    encode_zmq_message(FIELDS, bytes(256))
    with pytest.raises(CspV2FrameError, match="max_packet_bytes=256"):
        encode_zmq_message(FIELDS, bytes(257))
    with pytest.raises(CspV2FrameError, match="max_packet_bytes=8"):
        encode_zmq_message(FIELDS, bytes(9), max_packet_bytes=8)
