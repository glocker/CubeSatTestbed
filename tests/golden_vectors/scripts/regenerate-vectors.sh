#!/bin/sh
# Regenerates every committed golden vector from the pinned libcsp build inside
# the running libcsp-vectors container, so a fresh capture can be diffed
# against what is committed under tests/golden_vectors/. Run from the host:
#
#   docker compose exec -T libcsp-vectors /app/tests/golden_vectors/scripts/regenerate-vectors.sh
#
set -eu

CAN_INTERFACE="${CAN_INTERFACE:-vcan0}"
VECTOR_DIR="/app/tests/golden_vectors"
HELPER="${VECTOR_DIR}/bin/csp_client"
APP_UID="$(stat -c '%u' /app 2>/dev/null || echo 0)"
APP_GID="$(stat -c '%g' /app 2>/dev/null || echo 0)"

# name, the number of CAN frames libcsp is expected to put on the bus, then the
# csp_client arguments that follow "-c $CAN_INTERFACE -p". candump exits after
# exactly that many frames, so a frame count that disagrees with what libcsp
# really sends makes this script hang (too many expected) or truncate the
# capture (too few) -- a mistake that shows up as a failed diff, not silently.
generate_frames() {
    name="$1"
    frames="$2"
    shift 2
    capture="${VECTOR_DIR}/${name}.txt"
    candump -n "${frames}" "${CAN_INTERFACE}" >"${capture}" &
    dump_pid=$!
    # Give candump a moment to attach before csp_client sends its one frame.
    sleep 0.3
    "${HELPER}" -c "${CAN_INTERFACE}" -p "$@"
    wait "${dump_pid}"
    chown "${APP_UID}:${APP_GID}" "${capture}"
}

# Single-frame vectors: one packet, one CAN frame.
generate() {
    name="$1"
    shift
    generate_frames "${name}" 1 "$@"
}

generate ping -d 2
generate priority_critical -d 2 -r 0
generate priority_low -d 2 -r 3
generate high_address -a 65 -d 66
generate payload_empty -d 2 -l 0
generate payload_max -d 2 -l 4
generate flag_crc32 -d 2 -o crc32 -l 0
generate flag_hmac -d 2 -o hmac -l 0
generate reverse_direction -a 2 -d 1

# Multi-frame vectors (CFP2 fragmentation): one CSP packet carried by several
# CAN frames. The first frame holds 4 header bytes plus up to 4 payload bytes,
# every later frame up to 8 payload bytes; payload bytes plus any CRC32/HMAC
# trailer decide the frame count.
generate_frames multi_frame_5 2 -d 2 -l 5
generate_frames multi_frame_8 2 -d 2 -l 8
generate_frames multi_frame_12 2 -d 2 -l 12
generate_frames multi_frame_20 3 -d 2 -l 20
# 4 + 60 payload bytes: 9 frames, so frame_count wraps from 7 back to 0.
generate_frames multi_frame_64_counter_wrap 9 -d 2 -l 64
# A 4-byte payload fits one frame; the 4-byte CRC32 trailer pushes it to two.
generate_frames multi_frame_crc32 2 -d 2 -o crc32 -l 4
generate_frames multi_frame_hmac 2 -d 2 -o hmac -l 4
# The CAN-ID sender field is 6 bits wide and is repeated in every fragment.
generate_frames multi_frame_high_address 2 -a 65 -d 66 -l 12
generate_frames multi_frame_reverse_direction 2 -a 2 -d 1 -l 12
generate_frames multi_frame_priority_critical 2 -d 2 -r 0 -l 12
# Two back-to-back packets from one sender: the CFP2 packet counter increments.
generate_frames multi_frame_two_packets 4 -d 2 -l 12 -n 2

echo "Regenerated $(ls "${VECTOR_DIR}"/*.txt | wc -l) golden vector captures in ${VECTOR_DIR}."

# The serial (KISS) vectors live in kiss/ and need no vcan0.
"${VECTOR_DIR}/scripts/regenerate-kiss-vectors.sh"

# The ZeroMQ vectors live in zmq/ and need no vcan0 either.
"${VECTOR_DIR}/scripts/regenerate-zmq-vectors.sh"
