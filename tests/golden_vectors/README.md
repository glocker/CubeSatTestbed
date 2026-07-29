# CSP v2 golden vectors

Golden vectors are the source of truth for `cubesat_testbed.protocol.csp_v2`.

The project does not use third-party CAN dumps. Vectors are generated from the
official libcsp `v2.1` release at commit `48f7fb0` by a small C helper owned by
this repository. The actively changing `develop` branch is not used as the
baseline.

## Generation workflow

The intended container workflow uses `vcan0` consistently:

1. Build the environment:

   ```sh
   docker-compose up --build
   ```

2. The build produces the helper binary inside the container:

   ```text
   /app/tests/golden_vectors/bin/csp_client
   ```

3. Start a CAN capture inside the container:

   ```sh
   candump vcan0 > /app/tests/golden_vectors/ping.txt &
   ```

4. Send the reference CSP packet with the C helper:

   ```sh
   /app/tests/golden_vectors/bin/csp_client -c vcan0 -p -d 2
   ```

5. Stop capture and commit the resulting fixture files under
   `tests/golden_vectors/`.
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
interface = "vcan0"
meaning = "CSP ping request to node 2"
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

`bin/csp_client` is a generated build artifact; the binary itself is not the
fixture contract.
