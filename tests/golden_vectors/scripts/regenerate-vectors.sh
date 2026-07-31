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

# name, then the csp_client arguments that follow "-c $CAN_INTERFACE -p".
generate() {
    name="$1"
    shift
    capture="${VECTOR_DIR}/${name}.txt"
    candump -n 1 "${CAN_INTERFACE}" >"${capture}" &
    dump_pid=$!
    # Give candump a moment to attach before csp_client sends its one frame.
    sleep 0.3
    "${HELPER}" -c "${CAN_INTERFACE}" -p "$@"
    wait "${dump_pid}"
    chown "${APP_UID}:${APP_GID}" "${capture}"
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

echo "Regenerated $(ls "${VECTOR_DIR}"/*.txt | wc -l) golden vector captures in ${VECTOR_DIR}."
