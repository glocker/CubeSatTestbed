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

   The helper's full option set (`-p -h` for usage) covers the committed
   vector matrix:

   ```text
   -a <source-address>       CSP source/interface address (default: 1)
   -d <destination-address>  CSP destination address (default: 2)
   -r <priority>             CSP priority 0-3: 0=critical 1=high 2=norm 3=low
   -l <payload-length>       payload bytes, 0-4; incrementing pattern from 0x55
   -o <flag[,flag...]>       comma-separated CSP options: crc32, rdp, hmac
   ```

   `-o rdp` cannot be captured this way: RDP is a stateful, connection-oriented
   protocol that blocks on a SYN handshake with no peer on the bus to answer
   it. The RDP flag bit is covered by a synthetic pack/decode round trip in
   `tests/test_csp_v2.py` instead of a captured vector. `crc32`/`hmac` append
   their own trailer to the payload, so use `-l 0` with them to stay within
   the v1 single-frame payload limit (a 1-byte payload plus a 4-byte trailer
   would push libcsp into multi-frame transmission, which is out of v1 scope).

5. `candump -n 1` exits after the first received frame. Commit the resulting
   fixture files under `tests/golden_vectors/`.

   If the dump file is root-owned because it was written through the Docker bind
   mount, fix ownership from inside the container before committing:

   ```sh
   chown --reference=/app/pyproject.toml /app/tests/golden_vectors/ping.txt
   ```
6. Commit a sibling metadata file next to each dump, for example
   `ping.meta.toml` for `ping.txt`.

To regenerate every committed vector at once (for example, to check for
upstream libcsp drift before merging a codec change), run the same script CI
uses instead of repeating steps 3-5 by hand:

```sh
docker compose exec -T libcsp-vectors /app/tests/golden_vectors/scripts/regenerate-vectors.sh
```

Then inspect `git diff -- tests/golden_vectors/*.txt`. An empty diff means
libcsp's on-wire behavior still matches every committed fixture.

## Committed vector matrix

| Vector | Covers |
| --- | --- |
| `ping_node_2` (`ping.txt`) | Baseline: `src=1 dst=2 prio=NORM flags=none payload=1B` |
| `priority_critical` | `prio=0` (CRITICAL), the low end of the 2-bit priority field |
| `priority_low` | `prio=3` (LOW), the high end of the 2-bit priority field |
| `high_address` | `src=65 dst=66`: the CAN-ID `sender` field is only 6 bits (libcsp uses `interface_address & 0x3F`), so this is the only committed vector where `sender != source` -- every other vector uses addresses below 64, where they are trivially equal |
| `payload_empty` | 0-byte application payload, the lower bound of the v1 single-frame payload range |
| `payload_max` | 4-byte application payload, the upper bound of the v1 single-frame payload range |
| `flag_crc32` | `CSP_FLAG_CRC32` set |
| `flag_hmac` | `CSP_FLAG_HMAC` set |
| `reverse_direction` | `src=2 dst=1`: the same node pair as the baseline, addressed the other way |

`CSP_FLAG_RDP` has no committed vector (see the generation-workflow note
above); it is covered by a synthetic pack/decode round trip instead.

Regenerating every vector and diffing against what is committed is required
before merging any change to `tests/golden_vectors/src/csp_client.c` or
`src/cubesat_testbed/protocol/csp_v2.py`, and also runs nightly in CI (see
`.github/workflows/golden-vectors.yml`) so an upstream libcsp change (or a
moved `v2.1` tag) is caught even without a local change triggering it.

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
