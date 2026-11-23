#!/usr/bin/env python3
"""Regenerate the CSP v1 golden vectors for the links that carry a packet whole.

Run inside the libcsp vector container (no ``vcan0`` needed, a pty and a local hub are)::

    docker compose exec -T libcsp-vectors \
        python3 /app/tests/golden_vectors/scripts/regenerate-v1-whole-packet-vectors.py

Writes ``csp_v1/kiss/`` (what libcsp's KISS interface puts on a serial line) and
``csp_v1/zmq/`` (what its ZeroMQ interface publishes to a hub), each capture with a sibling
``*.meta.toml``. Nothing is synthesised: ``csp_client -v 1`` sends through the official libcsp
``v2.1`` build run as CSP v1, ``capture-kiss.py`` records the line, ``zmq_capture`` subscribes
to libcsp's own ``zmqproxy``.

As for the CAN vectors, the metadata keeps what the generator was *asked* to send (addresses,
priority, flags, payload length, packet count) apart from what libcsp *sent* (the wire bytes and
the headers, payloads and checksums read back from them), and this script refuses to write a
vector whose wire bytes disagree with what it asked for. The tests compare the two again.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

VECTOR_DIR = Path(os.environ.get("VECTOR_DIR", "/app/tests/golden_vectors"))
BIN_DIR = Path(os.environ.get("BIN_DIR", VECTOR_DIR / "bin"))
HELPER = BIN_DIR / "csp_client"
PROXY = BIN_DIR / "zmqproxy"
ZMQ_CAPTURE = BIN_DIR / "zmq_capture"
CAPTURE_KISS = VECTOR_DIR / "scripts" / "capture-kiss.py"
HUB_HOST = "127.0.0.1"

LIBCSP_REPO = "https://github.com/libcsp/libcsp"
LIBCSP_TAG = "v2.1"
LIBCSP_COMMIT = "48f7fb0"

HEADER_BYTES = 4
KISS_CRC_BYTES = 4
PAYLOAD_BASE = 0x55
PING_PORT = 1
FLAG_BITS = {"crc32": 0x01, "hmac": 0x08}
TRAILER_BYTES = {"crc32": 4, "hmac": 4}
FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD


@dataclass(frozen=True)
class Vector:
    name: str
    meaning: str
    source: int = 1
    destination: int = 2
    priority: int = 2
    payload_length: int = 1
    flags: tuple[str, ...] = ()
    packets: int = 1
    base: int = PAYLOAD_BASE

    @property
    def trailer_length(self) -> int:
        return sum(TRAILER_BYTES[flag] for flag in self.flags)

    @property
    def csp_flags(self) -> int:
        return sum(FLAG_BITS[flag] for flag in self.flags)

    def helper_args(self) -> list[str]:
        args = [
            "-v", "1",
            "-a", str(self.source),
            "-d", str(self.destination),
            "-r", str(self.priority),
            "-l", str(self.payload_length),
        ]  # fmt: skip
        if self.flags:
            args += ["-o", ",".join(self.flags)]
        if self.packets != 1:
            args += ["-n", str(self.packets)]
        if self.base != PAYLOAD_BASE:
            args += ["-b", hex(self.base)]
        return args

    @property
    def pattern(self) -> bytes:
        return bytes((self.base + i) & 0xFF for i in range(self.payload_length))


KISS_VECTORS = (
    Vector("ping", "Baseline CSP v1 ping-port request over KISS from node 1 to node 2 with a 1-byte payload 0x55 and no flags: the 4-byte header, the payload and a 4-byte CRC32 in one frame."),
    Vector("payload_empty", "Empty payload: the frame is the header and the CRC32 of nothing (zero).", payload_length=0),
    Vector("payload_64", "64-byte payload in one frame: serial framing needs no CFP1 fragmentation, unlike CSP v1 over CAN.", payload_length=64),
    Vector("payload_200", "200-byte payload in one frame, the largest the helper sends.", payload_length=200),
    Vector("priority_critical", "Critical priority (0).", priority=0),
    Vector("priority_low", "Low priority (3).", priority=3),
    Vector("high_address", "Between the highest usable CSP v1 addresses (30 to 29; 31 is the broadcast address): the 5-bit address fields are full.", source=30, destination=29),
    Vector("reverse_direction", "Node 2 to node 1: source and destination are not interchangeable in the header.", source=2, destination=1),
    Vector("flag_crc32", "The CRC32 flag: libcsp appends its own 4-byte trailer to the payload, and the KISS interface then adds its CRC32 over both, so there are two checksums.", payload_length=4, flags=("crc32",)),
    Vector("flag_hmac", "The HMAC flag: a 4-byte HMAC trailer in the payload, then the KISS CRC32.", payload_length=4, flags=("hmac",)),
    Vector("escape_fend", "Payload bytes 0xC0..0xC3: the FEND byte is sent as FESC TFEND (DB DC).", payload_length=4, base=0xC0),
    Vector("escape_fesc", "Payload bytes 0xDB..0xDE: the FESC byte is sent as FESC TFESC (DB DD).", payload_length=4, base=0xDB),
    Vector("two_packets", "Two back-to-back packets: two frames, and the source port in the header, which libcsp picks per connection, is the one field that differs.", payload_length=3, packets=2),
)  # fmt: skip

ZMQ_VECTORS = (
    Vector("ping", "Baseline CSP v1 ping-port request through a libcsp ZeroMQ hub from node 1 to node 2 with a 1-byte payload 0x55: a message is the destination address, the 4-byte header and the payload; no CRC32, no framing."),
    Vector("payload_empty", "Empty payload: the message is the destination address and the header.", payload_length=0),
    Vector("payload_64", "64-byte payload in one message: no fragmentation.", payload_length=64),
    Vector("payload_200", "200-byte payload in one message, the largest the helper sends.", payload_length=200),
    Vector("priority_critical", "Critical priority (0).", priority=0),
    Vector("priority_low", "Low priority (3).", priority=3),
    Vector("high_address", "Between the highest usable CSP v1 addresses (30 to 29): the first byte of the message is the destination, 29 (0x1D).", source=30, destination=29),
    Vector("reverse_direction", "Node 2 to node 1: the leading destination byte is 1.", source=2, destination=1),
    Vector("flag_crc32", "The CRC32 flag: libcsp's own 4-byte trailer is in the payload; the ZeroMQ interface adds no checksum of its own.", payload_length=4, flags=("crc32",)),
    Vector("flag_hmac", "The HMAC flag: a 4-byte HMAC trailer in the payload.", payload_length=4, flags=("hmac",)),
    Vector("two_packets", "Two back-to-back packets: two messages, differing in the source port libcsp picks per connection.", payload_length=3, packets=2),
)  # fmt: skip


def crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli), as libcsp's ``csp_crc32_memory`` computes it."""

    value = 0xFFFFFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ 0x82F63B78 if value & 1 else value >> 1
    return value ^ 0xFFFFFFFF


def unescape(frame: bytes) -> bytes:
    """The packet inside one KISS frame ``FEND 00 ... FEND``."""

    if frame[:2] != bytes((FEND, 0x00)) or frame[-1] != FEND:
        raise SystemExit(f"not a KISS data frame: {frame.hex(' ')}")
    out = bytearray()
    escaped = False
    for byte in frame[2:-1]:
        if escaped:
            out.append(FESC if byte == TFESC else FEND)
            escaped = False
        elif byte == FESC:
            escaped = True
        else:
            out.append(byte)
    return bytes(out)


def header_fields(header: bytes) -> dict[str, int]:
    value = int.from_bytes(header, "big")
    return {
        "priority": (value >> 30) & 0x3,
        "source": (value >> 25) & 0x1F,
        "destination": (value >> 20) & 0x1F,
        "destination_port": (value >> 14) & 0x3F,
        "source_port": (value >> 8) & 0x3F,
        "flags": value & 0xFF,
    }


def check_asked(vector: Vector, header: bytes, payload: bytes) -> None:
    """Refuse a capture that does not carry what the helper was asked to send."""

    fields = header_fields(header)
    expected = {
        "priority": vector.priority,
        "source": vector.source,
        "destination": vector.destination,
        "destination_port": PING_PORT,
        "flags": vector.csp_flags,
    }
    for key, value in expected.items():
        if fields[key] != value:
            raise SystemExit(f"{vector.name}: header {key} is {fields[key]}, asked for {value}")
    if payload[: vector.payload_length] != vector.pattern:
        raise SystemExit(f"{vector.name}: the payload does not start with the requested pattern")
    if len(payload) != vector.payload_length + vector.trailer_length:
        raise SystemExit(f"{vector.name}: payload is {len(payload)} bytes")


def toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise TypeError(value)


def hex_upper(data: bytes) -> str:
    return data.hex(" ").upper()


def write_files(
    directory: Path, vector: Vector, capture_text: str, meta: dict[str, object]
) -> None:
    capture = directory / f"{vector.name}.txt"
    meta_path = directory / f"{vector.name}.meta.toml"
    capture.write_text(capture_text)
    meta_path.write_text("".join(f"{key} = {toml_value(value)}\n" for key, value in meta.items()))
    owner = VECTOR_DIR.stat()
    for path in (capture, meta_path):
        os.chown(path, owner.st_uid, owner.st_gid)


def common_meta(vector: Vector, directory: Path) -> dict[str, object]:
    return {
        "name": vector.name,
        "capture": f"{vector.name}.txt",
        "libcsp_repo": LIBCSP_REPO,
        "libcsp_tag": LIBCSP_TAG,
        "libcsp_commit": LIBCSP_COMMIT,
    }


def make_kiss(vector: Vector, directory: Path) -> None:
    out = Path(tempfile.mkstemp(suffix=".txt")[1])
    command = [
        "python3", str(CAPTURE_KISS), str(out), str(vector.packets), "--",
        str(HELPER), "-k", "{tty}", "-p", *vector.helper_args(),
    ]  # fmt: skip
    if subprocess.run(command, check=False).returncode != 0:
        raise SystemExit(f"{vector.name}: capture-kiss.py failed")
    text = out.read_text()
    out.unlink()
    frames = [bytes.fromhex(line) for line in text.splitlines() if line.strip()]
    headers, packets, payloads = [], [], []
    for frame in frames:
        packet = unescape(frame)
        header, body, trailer = (
            packet[:HEADER_BYTES],
            packet[HEADER_BYTES:-KISS_CRC_BYTES],
            packet[-KISS_CRC_BYTES:],
        )
        check_asked(vector, header, body)
        if int.from_bytes(trailer, "big") != crc32c(body):
            raise SystemExit(f"{vector.name}: the KISS CRC32 is not the CRC-32C of the payload")
        headers.append(hex_upper(header))
        packets.append(hex_upper(packet))
        payloads.append(body.hex(" "))
    meta = common_meta(vector, directory) | {
        "command": f"python3 {CAPTURE_KISS} {directory}/{vector.name}.txt {vector.packets} -- "
        f"{HELPER} -k {{tty}} -p {' '.join(vector.helper_args())}",
        "capture_command": "capture-kiss.py opens a pty pair, runs csp_client -k <pty slave> and "
        "records the bytes libcsp's KISS interface writes",
        "interface": "pty",
        "meaning": vector.meaning,
        "protocol": "CSP v1 over KISS (serial), one CSP packet per frame",
        "csp_version": 1,
        "source_address": vector.source,
        "destination_address": vector.destination,
        "priority": vector.priority,
        "destination_port": PING_PORT,
        "csp_flags": vector.csp_flags,
        "payload_length": vector.payload_length,
        "trailer_length": vector.trailer_length,
        "packet_count": vector.packets,
        "expected_frame_count": len(frames),
        "expected_frames_hex": [hex_upper(frame) for frame in frames],
        "expected_packets_hex": packets,
        "expected_csp_headers_hex": headers,
        "expected_payloads_hex": payloads,
    }
    write_files(directory, vector, text, meta)


def make_zmq(vector: Vector, directory: Path) -> None:
    out = Path(tempfile.mkstemp(suffix=".txt")[1])
    capture = subprocess.Popen(
        [str(ZMQ_CAPTURE), f"tcp://{HUB_HOST}:7000", str(vector.packets), "15", str(out)]
    )
    time.sleep(1)  # let the subscription reach the hub before libcsp publishes
    subprocess.run(
        [str(HELPER), "-z", HUB_HOST, "-p", *vector.helper_args()],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if capture.wait() != 0:
        raise SystemExit(f"{vector.name}: zmq_capture saw fewer than {vector.packets} messages")
    text = out.read_text()
    out.unlink()
    messages = [bytes.fromhex(line) for line in text.splitlines() if line.strip()]
    prefixes, headers, payloads = [], [], []
    for message in messages:
        prefix, header, body = (
            message[0],
            message[1 : 1 + HEADER_BYTES],
            message[1 + HEADER_BYTES :],
        )
        if prefix != vector.destination:
            raise SystemExit(f"{vector.name}: leading byte {prefix}, expected the destination")
        check_asked(vector, header, body)
        prefixes.append(prefix)
        headers.append(hex_upper(header))
        payloads.append(body.hex(" "))
    meta = common_meta(vector, directory) | {
        "command": f"zmqproxy & zmq_capture tcp://{HUB_HOST}:7000 {vector.packets} 15 "
        f"{directory}/{vector.name}.txt & {HELPER} -z {HUB_HOST} -p {' '.join(vector.helper_args())}",
        "capture_command": "regenerate-v1-whole-packet-vectors.py runs libcsp's zmqproxy hub, "
        "subscribes zmq_capture to its publish port (7000) and sends with csp_client -z "
        "through the subscribe port (6000)",
        "interface": "zmq",
        "meaning": vector.meaning,
        "protocol": "CSP v1 over ZeroMQ (libcsp zmqhub), one CSP packet per message",
        "csp_version": 1,
        "source_address": vector.source,
        "destination_address": vector.destination,
        "priority": vector.priority,
        "destination_port": PING_PORT,
        "csp_flags": vector.csp_flags,
        "payload_length": vector.payload_length,
        "trailer_length": vector.trailer_length,
        "packet_count": vector.packets,
        "expected_message_count": len(messages),
        "expected_messages_hex": [hex_upper(message) for message in messages],
        "expected_destination_prefixes": prefixes,
        "expected_csp_headers_hex": headers,
        "expected_payloads_hex": payloads,
    }
    write_files(directory, vector, text, meta)


def main() -> int:
    kiss_dir = VECTOR_DIR / "csp_v1" / "kiss"
    zmq_dir = VECTOR_DIR / "csp_v1" / "zmq"
    owner = VECTOR_DIR.stat()
    for directory in (kiss_dir.parent, kiss_dir, zmq_dir):
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, owner.st_uid, owner.st_gid)

    for vector in KISS_VECTORS:
        make_kiss(vector, kiss_dir)
    print(f"Regenerated {len(KISS_VECTORS)} CSP v1 KISS golden vectors in {kiss_dir}.")

    proxy = subprocess.Popen([str(PROXY)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1)
        for vector in ZMQ_VECTORS:
            make_zmq(vector, zmq_dir)
    finally:
        proxy.terminate()
    print(f"Regenerated {len(ZMQ_VECTORS)} CSP v1 ZeroMQ golden vectors in {zmq_dir}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
