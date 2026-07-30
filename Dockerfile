FROM python:3.11-slim AS libcsp-vector-env

ARG LIBCSP_REPO=https://github.com/libcsp/libcsp.git
ARG LIBCSP_TAG=v2.1
ARG LIBCSP_COMMIT=48f7fb0

ENV DEBIAN_FRONTEND=noninteractive \
    LIBCSP_REPO=${LIBCSP_REPO} \
    LIBCSP_TAG=${LIBCSP_TAG} \
    LIBCSP_COMMIT=${LIBCSP_COMMIT}

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        can-utils \
        git \
        iproute2 \
        kmod \
        libsocketcan-dev \
        meson \
        ninja-build \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# The tag is checked against the expected short commit so a moved or mistyped
# upstream tag fails the build before any vectors can be generated from it.
RUN git clone --branch "${LIBCSP_TAG}" --depth 1 "${LIBCSP_REPO}" /opt/libcsp \
    && cd /opt/libcsp \
    && test "$(git rev-parse --short=7 HEAD)" = "${LIBCSP_COMMIT}" \
    && meson setup build \
        --default-library=static \
        -Denable_reproducible_builds=true \
        -Duse_rtable=true \
        -Dversion=2 \
    && meson compile -C build

COPY tests/golden_vectors/src/csp_client.c /app/tests/golden_vectors/src/csp_client.c

# Keep the helper under /opt as the runtime source of truth. The Compose bind
# mount hides image-time /app contents, so the entrypoint copies this binary into
# the mounted repository on container start.
RUN mkdir -p /opt/cubesat_testbed/bin /app/tests/golden_vectors/bin \
    && cc \
        -std=gnu11 \
        -Wall \
        -Wextra \
        -Wpedantic \
        -Werror \
        -I/opt/libcsp/include \
        -I/opt/libcsp/src \
        -I/opt/libcsp/build/include \
        /app/tests/golden_vectors/src/csp_client.c \
        /opt/libcsp/build/libcsp.a \
        -lsocketcan \
        -lpthread \
        -lrt \
        -lutil \
        -o /opt/cubesat_testbed/bin/csp_client \
    && cp /opt/cubesat_testbed/bin/csp_client /app/tests/golden_vectors/bin/csp_client

COPY tests/golden_vectors/scripts/vector-entrypoint.sh /usr/local/bin/cubesat-vector-entrypoint
RUN chmod +x /usr/local/bin/cubesat-vector-entrypoint

# Include a repository snapshot for direct `docker run` use; Compose still mounts
# the live checkout at /app when generating fixtures.
COPY . /app

ENTRYPOINT ["/usr/local/bin/cubesat-vector-entrypoint"]
CMD ["sleep", "infinity"]
