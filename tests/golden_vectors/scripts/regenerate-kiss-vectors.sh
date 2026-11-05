#!/bin/sh
# Regenerates every committed KISS golden vector under tests/golden_vectors/kiss/
# from the pinned libcsp build. The serial counterpart of regenerate-vectors.sh:
# csp_client sends through libcsp's own KISS interface onto a pty, and
# capture-kiss.py records the bytes libcsp writes -- nothing is synthesised.
# No vcan0 is needed, only a pty, so this also runs outside the compose service:
#
#   docker compose exec -T libcsp-vectors /app/tests/golden_vectors/scripts/regenerate-kiss-vectors.sh
#
set -eu

VECTOR_DIR="${VECTOR_DIR:-/app/tests/golden_vectors}"
KISS_DIR="${VECTOR_DIR}/kiss"
HELPER="${HELPER:-${VECTOR_DIR}/bin/csp_client}"
CAPTURE="${VECTOR_DIR}/scripts/capture-kiss.py"
APP_UID="$(stat -c '%u' "${VECTOR_DIR}" 2>/dev/null || echo 0)"
APP_GID="$(stat -c '%g' "${VECTOR_DIR}" 2>/dev/null || echo 0)"

mkdir -p "${KISS_DIR}"
chown "${APP_UID}:${APP_GID}" "${KISS_DIR}"

# name, the number of KISS frames libcsp writes, then the csp_client arguments
# that follow "-k <pty> -p". The frame count is checked by capture-kiss.py.
generate_kiss() {
    name="$1"
    frames="$2"
    shift 2
    capture="${KISS_DIR}/${name}.txt"
    python3 "${CAPTURE}" "${capture}" "${frames}" -- "${HELPER}" -k '{tty}' -p "$@"
    chown "${APP_UID}:${APP_GID}" "${capture}"
}

generate_kiss ping 1 -d 2
generate_kiss payload_empty 1 -d 2 -l 0
generate_kiss payload_64 1 -d 2 -l 64
generate_kiss payload_200 1 -d 2 -l 200
generate_kiss priority_critical 1 -d 2 -r 0
generate_kiss priority_low 1 -d 2 -r 3
# KISS carries the full 14-bit source address; the CAN sender field is only 6 bits.
generate_kiss high_address 1 -a 65 -d 66
generate_kiss max_address 1 -a 16383 -d 16382
generate_kiss reverse_direction 1 -a 2 -d 1
generate_kiss flag_crc32 1 -d 2 -o crc32 -l 4
generate_kiss flag_hmac 1 -d 2 -o hmac -l 4
# Payload bytes that collide with the KISS control bytes: FEND (0xC0) and FESC (0xDB).
generate_kiss escape_fend 1 -d 2 -l 4 -b 0xc0
generate_kiss escape_fesc 1 -d 2 -l 4 -b 0xdb
generate_kiss two_packets 2 -d 2 -l 3 -n 2

echo "Regenerated $(ls "${KISS_DIR}"/*.txt | wc -l) KISS golden vector captures in ${KISS_DIR}."
