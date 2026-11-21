#!/usr/bin/env python3
"""Regenerate the CSP v1 over CAN golden vectors under ``csp_v1/can/``.

Run inside the libcsp vector container, where ``vcan0`` exists:

    docker compose exec -T libcsp-vectors \
        python3 /app/tests/golden_vectors/scripts/regenerate-v1-can-vectors.py

Every vector is captured, never synthesised: ``csp_client -v 1`` sends through the
official libcsp ``v2.1`` SocketCAN interface running as CSP v1 (libcsp selects the
protocol version at run time, so the pinned release serves both), ``candump`` records the
frames, and this script writes the capture and a sibling ``*.meta.toml``.

The metadata has two kinds of field. What the *generator was asked to send* (addresses,
priority, flags, payload length, packet count) comes from the table below, written by hand
and independent of the capture; what libcsp *put on the wire* (CAN identifiers, frame data,
the CSP headers, the reassembled payloads) is read back from the capture. The test suite
compares the two, so a helper that ignored an option or a libcsp that framed differently than
documented fails the comparison instead of becoming the truth.

CSP v1 over CAN is CFP 1.x: every packet is fragmented, the first frame carries the 4-byte
CSP header, a 2-byte big-endian length and up to two payload bytes, later frames up to eight.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

VECTOR_DIR = Path(os.environ.get("VECTOR_DIR", "/app/tests/golden_vectors"))
OUTPUT_DIR = VECTOR_DIR / "csp_v1" / "can"
HELPER = Path(os.environ.get("HELPER", VECTOR_DIR / "bin" / "csp_client"))
CAN_INTERFACE = os.environ.get("CAN_INTERFACE", "vcan0")

LIBCSP_REPO = "https://github.com/libcsp/libcsp"
LIBCSP_TAG = "v2.1"
LIBCSP_COMMIT = "48f7fb0"

PAYLOAD_BASE = 0x55
FLAG_BITS = {"crc32": 0x01, "hmac": 0x08}
TRAILER_BYTES = {"crc32": 4, "hmac": 4}
CFP1_BEGIN_DATA = 2
CFP1_MORE_DATA = 8
PING_PORT = 1


@dataclass(frozen=True)
class Vector:
    """One vector: what to ask csp_client for, and how many CAN frames libcsp will send."""

    name: str
    meaning: str
    frames: int
    source: int = 1
    destination: int = 2
    priority: int = 2
    payload_length: int = 1
    flags: tuple[str, ...] = ()
    packets: int = 1
    extra: tuple[str, ...] = field(default=())

    @property
    def trailer_length(self) -> int:
        return sum(TRAILER_BYTES[flag] for flag in self.flags)

    @property
    def csp_flags(self) -> int:
        return sum(FLAG_BITS[flag] for flag in self.flags)

    def helper_args(self) -> list[str]:
        args = [
            "-v",
            "1",
            "-a",
            str(self.source),
            "-d",
            str(self.destination),
            "-r",
            str(self.priority),
            "-l",
            str(self.payload_length),
        ]
        if self.flags:
            args += ["-o", ",".join(self.flags)]
        if self.packets != 1:
            args += ["-n", str(self.packets)]
        return args

    def expected_frames_per_packet(self) -> int:
        data = self.payload_length + self.trailer_length
        if data <= CFP1_BEGIN_DATA:
            return 1
        return 1 + -(-(data - CFP1_BEGIN_DATA) // CFP1_MORE_DATA)


VECTORS = (
    # Single-frame packets: at most two payload bytes after the 4-byte header and the
    # 2-byte length, so the whole packet fits the first (and only) CFP1 frame.
    Vector(
        "ping",
        "Single-frame CSP v1 ping-port request from node 1 to node 2 with one 0x55 payload byte, normal priority, no flags.",
        1,
    ),
    Vector(
        "priority_critical", "Single-frame CSP v1 packet at critical priority (0).", 1, priority=0
    ),
    Vector("priority_low", "Single-frame CSP v1 packet at low priority (3).", 1, priority=3),
    Vector(
        "high_address",
        "Single-frame CSP v1 packet between the highest usable v1 addresses (30 to 29; 31 is the broadcast address): the 5-bit address fields are full.",
        1,
        source=30,
        destination=29,
    ),
    Vector(
        "reverse_direction",
        "Single-frame CSP v1 packet from node 2 to node 1: source and destination are not interchangeable in the CAN identifier.",
        1,
        source=2,
        destination=1,
    ),
    Vector(
        "payload_empty",
        "CSP v1 packet with an empty payload: the first frame holds only the header and a zero length.",
        1,
        payload_length=0,
    ),
    Vector(
        "payload_single_frame_max",
        "Largest CSP v1 payload that still fits one CFP1 frame: two bytes after the 4-byte header and the 2-byte length, a full 8-byte frame.",
        1,
        payload_length=2,
    ),
    # Flags: libcsp appends the trailer to the payload, so these spill into a second frame.
    Vector(
        "flag_crc32",
        "CSP v1 packet with the CRC32 flag and an empty payload: the 4-byte CRC32 trailer is the whole payload, so two frames.",
        2,
        payload_length=0,
        flags=("crc32",),
    ),
    Vector(
        "flag_hmac",
        "CSP v1 packet with the HMAC flag and an empty payload: the 4-byte HMAC trailer is the whole payload, so two frames.",
        2,
        payload_length=0,
        flags=("hmac",),
    ),
    # Multi-frame packets: first frame two payload bytes, later frames up to eight.
    Vector(
        "multi_frame_3",
        "CSP v1 packet one byte past the single-frame limit: 2 + 1 payload bytes.",
        2,
        payload_length=3,
    ),
    Vector(
        "multi_frame_10",
        "CSP v1 packet with a second frame exactly full: 2 + 8 payload bytes.",
        2,
        payload_length=10,
    ),
    Vector(
        "multi_frame_11",
        "CSP v1 packet one byte past a full second frame: 2 + 8 + 1 payload bytes, three frames.",
        3,
        payload_length=11,
    ),
    Vector(
        "multi_frame_18",
        "CSP v1 packet with two full continuation frames: 2 + 8 + 8 payload bytes.",
        3,
        payload_length=18,
    ),
    Vector(
        "multi_frame_64",
        "64-byte CSP v1 payload: nine frames, the remaining-frames counter counts down from 8 to 0.",
        9,
        payload_length=64,
    ),
    Vector(
        "multi_frame_200",
        "200-byte CSP v1 payload, the largest the helper sends: 26 frames, the remaining-frames counter counts down from 25 to 0.",
        26,
        payload_length=200,
    ),
    Vector(
        "multi_frame_crc32",
        "CSP v1 packet with the CRC32 flag and a 4-byte payload: payload plus trailer is 8 bytes over two frames.",
        2,
        payload_length=4,
        flags=("crc32",),
    ),
    Vector(
        "multi_frame_high_address",
        "Multi-frame CSP v1 packet between addresses 30 and 29: the source and destination are repeated in the CAN identifier of every frame.",
        3,
        source=30,
        destination=29,
        payload_length=12,
    ),
    Vector(
        "multi_frame_reverse_direction",
        "Multi-frame CSP v1 packet from node 2 to node 1.",
        3,
        source=2,
        destination=1,
        payload_length=12,
    ),
    Vector(
        "multi_frame_priority_critical",
        "Multi-frame CSP v1 packet at critical priority: the priority lives in the CSP header of the first frame only, not in the CAN identifier.",
        3,
        priority=0,
        payload_length=12,
    ),
    Vector(
        "multi_frame_two_packets",
        "Two back-to-back 12-byte CSP v1 packets (three frames each) from one sender: the 10-bit CFP identification field of the CAN identifier increments from one packet to the next.",
        6,
        payload_length=12,
        packets=2,
    ),
)


def parse_candump(text: str) -> list[tuple[int, bytes]]:
    frames: list[tuple[int, bytes]] = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        # "vcan0  01100000   [7]  82 20 52 00 00 01 55"
        can_id = int(parts[1], 16)
        dlc = int(parts[2].strip("[]"))
        data = bytes(int(token, 16) for token in parts[3 : 3 + dlc])
        frames.append((can_id, data))
    return frames


def capture(vector: Vector) -> tuple[str, list[tuple[int, bytes]]]:
    """Run candump and csp_client; return the raw candump text and the parsed frames."""

    expected = vector.expected_frames_per_packet() * vector.packets
    if expected != vector.frames:
        raise SystemExit(
            f"{vector.name}: the table says {vector.frames} frames but the CFP1 layout "
            f"gives {expected}; fix the table"
        )
    dump = subprocess.Popen(
        ["candump", "-n", str(vector.frames), CAN_INTERFACE],
        stdout=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.3)  # let candump attach before the first frame
    send = subprocess.run(
        [str(HELPER), "-c", CAN_INTERFACE, "-p", *vector.helper_args()],
        capture_output=True,
        text=True,
        check=False,
    )
    if send.returncode != 0:
        dump.kill()
        raise SystemExit(f"{vector.name}: csp_client failed: {send.stderr.strip()}")
    try:
        text, _ = dump.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        dump.kill()
        raise SystemExit(
            f"{vector.name}: candump saw fewer than {vector.frames} frames; "
            "libcsp sent fewer frames than the table expects"
        ) from None
    frames = parse_candump(text)
    if len(frames) != vector.frames:
        raise SystemExit(f"{vector.name}: captured {len(frames)} frames, expected {vector.frames}")
    return text, frames


def split_packets(vector: Vector, frames: list[tuple[int, bytes]]) -> list[list[tuple[int, bytes]]]:
    per_packet = vector.expected_frames_per_packet()
    return [frames[i : i + per_packet] for i in range(0, len(frames), per_packet)]


def reassemble(packet_frames: list[tuple[int, bytes]]) -> tuple[bytes, bytes]:
    """The CSP header and the payload (trailer included) carried by one packet's frames."""

    first = packet_frames[0][1]
    header, length = first[:4], int.from_bytes(first[4:6], "big")
    payload = first[6:] + b"".join(data for _, data in packet_frames[1:])
    if len(payload) != length:
        raise SystemExit(f"length field {length} disagrees with {len(payload)} payload bytes")
    return header, payload


def toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise TypeError(value)


def hex_bytes(data: bytes, upper: bool = True) -> str:
    text = data.hex(" ")
    return text.upper() if upper else text


def write_vector(vector: Vector, text: str, frames: list[tuple[int, bytes]]) -> None:
    packets = split_packets(vector, frames)
    headers: list[str] = []
    reassembled: list[str] = []
    pattern = bytes((PAYLOAD_BASE + i) & 0xFF for i in range(vector.payload_length))
    for packet in packets:
        header, payload = reassemble(packet)
        # The application bytes are the incrementing pattern csp_client was asked for; only
        # the trailer is libcsp's own. A mismatch means the helper ignored an option.
        if payload[: vector.payload_length] != pattern:
            raise SystemExit(f"{vector.name}: payload does not start with the requested pattern")
        if len(payload) != vector.payload_length + vector.trailer_length:
            raise SystemExit(f"{vector.name}: unexpected payload length {len(payload)}")
        headers.append(hex_bytes(header))
        reassembled.append(hex_bytes(payload, upper=False))

    command = f"{HELPER} -c {CAN_INTERFACE} -p {' '.join(vector.helper_args())}"
    capture_path = OUTPUT_DIR / f"{vector.name}.txt"
    meta = {
        "name": vector.name,
        "capture": f"{vector.name}.txt",
        "libcsp_repo": LIBCSP_REPO,
        "libcsp_tag": LIBCSP_TAG,
        "libcsp_commit": LIBCSP_COMMIT,
        "command": command,
        "capture_command": f"candump -n {vector.frames} {CAN_INTERFACE} > {capture_path}",
        "interface": CAN_INTERFACE,
        "meaning": vector.meaning,
        "protocol": "CSP v1 over classic CAN 2.0B extended frames, CFP1 fragmented",
        "csp_version": 1,
        "source_address": vector.source,
        "destination_address": vector.destination,
        "priority": vector.priority,
        "destination_port": PING_PORT,
        "csp_flags": vector.csp_flags,
        "payload_length": vector.payload_length,
        "trailer_length": vector.trailer_length,
        "packet_count": vector.packets,
        "expected_frame_count": vector.frames,
        "expected_extended_can_ids": [f"0x{can_id:08X}" for can_id, _ in frames],
        "expected_frame_payloads_hex": [hex_bytes(data) for _, data in frames],
        "expected_csp_headers_hex": headers,
        "expected_reassembled_payloads_hex": reassembled,
    }
    body = "".join(f"{key} = {toml_value(value)}\n" for key, value in meta.items())

    capture_path.write_text(text)
    meta_path = OUTPUT_DIR / f"{vector.name}.meta.toml"
    meta_path.write_text(body)
    owner = VECTOR_DIR.stat()
    for path in (capture_path, meta_path):
        os.chown(path, owner.st_uid, owner.st_gid)


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    owner = VECTOR_DIR.stat()
    for directory in (OUTPUT_DIR.parent, OUTPUT_DIR):
        os.chown(directory, owner.st_uid, owner.st_gid)
    for vector in VECTORS:
        text, frames = capture(vector)
        write_vector(vector, text, frames)
    print(f"Regenerated {len(VECTORS)} CSP v1 CAN golden vectors in {OUTPUT_DIR}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
