#!/bin/sh
set -eu

CAN_INTERFACE="${CAN_INTERFACE:-vcan0}"
HELPER_SOURCE="/opt/cubesat_testbed/bin/csp_client"
HELPER_TARGET="/app/tests/golden_vectors/bin/csp_client"

# /app is normally a bind mount from the developer checkout. Preserve that
# owner for generated artifacts so the host user can edit and commit them.
APP_UID="$(stat -c '%u' /app 2>/dev/null || echo 0)"
APP_GID="$(stat -c '%g' /app 2>/dev/null || echo 0)"

mkdir -p "$(dirname "${HELPER_TARGET}")"
install -m 0755 -o "${APP_UID}" -g "${APP_GID}" "${HELPER_SOURCE}" "${HELPER_TARGET}"

if ! ip link show "${CAN_INTERFACE}" >/dev/null 2>&1; then
    # vcan may already be available from the host kernel; modprobe failures are
    # tolerated so the definitive check remains the ip link add command below.
    if command -v modprobe >/dev/null 2>&1; then
        modprobe vcan 2>/dev/null || true
    fi
    ip link add dev "${CAN_INTERFACE}" type vcan
fi

ip link set dev "${CAN_INTERFACE}" up

cat <<EOF
libcsp golden-vector environment is ready.

Pinned libcsp:
  repo:   ${LIBCSP_REPO:-https://github.com/libcsp/libcsp.git}
  tag:    ${LIBCSP_TAG:-v2.1}
  commit: ${LIBCSP_COMMIT:-48f7fb0}

CAN interface:
  ${CAN_INTERFACE}

Helper binary:
  ${HELPER_TARGET}

Example capture workflow:
  candump -n 1 ${CAN_INTERFACE} > /app/tests/golden_vectors/ping.txt &
  /app/tests/golden_vectors/bin/csp_client -c ${CAN_INTERFACE} -p -d 2
  chown --reference=/app/pyproject.toml /app/tests/golden_vectors/ping.txt
EOF

exec "$@"
