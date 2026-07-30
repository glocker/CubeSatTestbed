# CSP v2 golden vectors

Golden vectors are the source of truth for `cubesat_testbed.protocol.csp_v2`.

The project does not use third-party CAN dumps. Vectors are generated from the
official libcsp `v2.1` release at commit `48f7fb0` by a small C helper owned by
this repository. The actively changing `develop` branch is not used as the
baseline.

## Generation workflow

The container workflow uses `vcan0` consistently.

1. Build and start the vector environment:

   ```sh
   docker compose up --build -d
   ```

   The Compose service builds official libcsp from `https://github.com/libcsp/libcsp.git`,
   checks that tag `v2.1` resolves to short commit `48f7fb0`, installs Linux CAN
   tooling including `can-utils`, prepares `vcan0`, and copies the generated helper
   binary to the mounted repository. If your host uses Compose v1, replace
   `docker compose` with `docker-compose`. The helper path is:

   ```text
   /app/tests/golden_vectors/bin/csp_client
   ```

   `bin/csp_client` is a generated build artifact and is intentionally ignored by
   git.

2. Start a shell in the running vector container:

   ```sh
   docker compose exec libcsp-vectors sh
   ```

3. Start CAN capture inside the container:

   ```sh
   candump -n 1 vcan0 > /app/tests/golden_vectors/ping.txt &
   ```

4. Send the reference CSP packet with the repository-owned C helper:

   ```sh
   /app/tests/golden_vectors/bin/csp_client -c vcan0 -p -d 2
   ```

5. `candump -n 1` exits after the first received frame. Commit the resulting
   fixture files under `tests/golden_vectors/`.

   If the dump file is root-owned because it was written through the Docker bind
   mount, fix ownership from inside the container before committing:

   ```sh
   chown --reference=/app/pyproject.toml /app/tests/golden_vectors/ping.txt
   ```
6. Commit a sibling metadata file next to each dump, for example
   `ping.meta.toml` for `ping.txt`.

## Fixture contract

Every traffic dump must have a sibling `*.meta.toml` file. For example:

```toml
name = "ping_node_2"
capture = "ping.txt"
libcsp_repo = "https://github.com/libcsp/libcsp"
libcsp_tag = "v2.1"
libcsp_commit = "48f7fb0"
command = "/app/tests/golden_vectors/bin/csp_client -c vcan0 -p -d 2"
capture_command = "candump -n 1 vcan0 > /app/tests/golden_vectors/ping.txt"
interface = "vcan0"
meaning = "Single-frame CSP v2 ping request from node 1 to node 2"
```

Committed fixture pairs must record:

- pinned libcsp repository URL, tag and exact commit;
- generation command;
- CAN interface;
- capture file name;
- extended 29-bit CAN ID, either in parsed fixture data or asserted by tests;
- raw CAN payload bytes, either in parsed fixture data or asserted by tests;
- expected packet meaning.

Python implementation starts after these fixtures are fixed. The Python CSP v2
codec must match the committed vectors byte-for-byte.
