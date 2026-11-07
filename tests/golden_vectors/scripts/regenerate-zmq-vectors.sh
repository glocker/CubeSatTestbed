#!/bin/sh
# Regenerates every committed ZeroMQ golden vector under tests/golden_vectors/zmq/
# from the pinned libcsp build, the same way regenerate-kiss-vectors.sh does for the
# serial line: libcsp's own zmqproxy hub runs on localhost, zmq_capture subscribes to
# it and records each message, and csp_client sends through libcsp's own ZeroMQ
# interface. Nothing is synthesised and no vcan0 is needed:
#
#   docker compose exec -T libcsp-vectors /app/tests/golden_vectors/scripts/regenerate-zmq-vectors.sh
#
set -eu

VECTOR_DIR="${VECTOR_DIR:-/app/tests/golden_vectors}"
ZMQ_DIR="${VECTOR_DIR}/zmq"
BIN_DIR="${BIN_DIR:-${VECTOR_DIR}/bin}"
HELPER="${HELPER:-${BIN_DIR}/csp_client}"
PROXY="${PROXY:-${BIN_DIR}/zmqproxy}"
CAPTURE="${CAPTURE:-${BIN_DIR}/zmq_capture}"
HUB_HOST="127.0.0.1"
APP_UID="$(stat -c '%u' "${VECTOR_DIR}" 2>/dev/null || echo 0)"
APP_GID="$(stat -c '%g' "${VECTOR_DIR}" 2>/dev/null || echo 0)"

mkdir -p "${ZMQ_DIR}"
chown "${APP_UID}:${APP_GID}" "${ZMQ_DIR}"

# libcsp's own hub: clients publish to :6000 and subscribe from :7000.
"${PROXY}" >/dev/null 2>&1 &
proxy_pid=$!
trap 'kill "${proxy_pid}" 2>/dev/null || true' EXIT
sleep 1

# name, the number of ZeroMQ messages libcsp publishes, then the csp_client arguments
# that follow "-z <hub> -p". zmq_capture fails if fewer arrive within its timeout.
generate_zmq() {
    name="$1"
    messages="$2"
    shift 2
    capture="${ZMQ_DIR}/${name}.txt"
    "${CAPTURE}" "tcp://${HUB_HOST}:7000" "${messages}" 15 "${capture}" &
    capture_pid=$!
    # Let the capture's subscription reach the hub before csp_client sends.
    sleep 1
    "${HELPER}" -z "${HUB_HOST}" -p "$@"
    wait "${capture_pid}"
    chown "${APP_UID}:${APP_GID}" "${capture}"
}

generate_zmq ping 1 -d 2
generate_zmq payload_empty 1 -d 2 -l 0
generate_zmq payload_64 1 -d 2 -l 64
generate_zmq payload_200 1 -d 2 -l 200
generate_zmq priority_critical 1 -d 2 -r 0
generate_zmq priority_low 1 -d 2 -r 3
# Like KISS, a ZeroMQ message carries the full 14-bit source address.
generate_zmq high_address 1 -a 65 -d 66
generate_zmq max_address 1 -a 16383 -d 16382
generate_zmq reverse_direction 1 -a 2 -d 1
generate_zmq flag_crc32 1 -d 2 -o crc32 -l 4
generate_zmq flag_hmac 1 -d 2 -o hmac -l 4
generate_zmq two_packets 2 -d 2 -l 3 -n 2

echo "Regenerated $(ls "${ZMQ_DIR}"/*.txt | wc -l) ZeroMQ golden vector captures in ${ZMQ_DIR}."
