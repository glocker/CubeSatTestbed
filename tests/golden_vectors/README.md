# CSP v2 golden vectors

Golden vectors are the source of truth for `cubesat_testbed.protocol.csp_v2`.

The project does not use third-party CAN dumps. Vectors are generated from the
official libcsp `v2.1` release at commit `48f7fb0` by a small C helper owned by
this repository. The actively changing `develop` branch is not used as the
baseline.

## Generation workflow

The container workflow uses `vcan0` consistently.

The one prerequisite the container cannot satisfy itself is the `vcan` kernel
module. It creates `vcan0` inside its own network namespace, but modules are
global and `/lib/modules` is not mounted into the container, so load it on the
host first:

```sh
sudo modprobe vcan
```

A machine that has generated vectors before usually has it loaded already;
a fresh one (a CI runner, a new laptop) does not, and the container will exit
during startup with an explicit error saying so.

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
   -l <payload-length>       payload bytes, 0-200; incrementing pattern from 0x55
   -o <flag[,flag...]>       comma-separated CSP options: crc32, rdp, hmac
   -n <packets>              send this many identical packets back to back, 1-8
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

### Multi-frame vectors

One CSP packet carried by several CAN frames, in capture order. Frame counts
below are what libcsp `v2.1` puts on `vcan0`.

| Vector | Command arguments | Covers |
| --- | --- | --- |
| `multi_frame_5` | `-l 5` | smallest multi-frame packet: 4 payload bytes in the first frame, 1 in the second |
| `multi_frame_8` | `-l 8` | 4 + 4 payload bytes; the second frame is not full |
| `multi_frame_12` | `-l 12` | 4 + 8: two frames, the second exactly full (three 4-byte telemetry signals in one packet) |
| `multi_frame_20` | `-l 20` | three frames; `frame_count` 0, 1, 2 and `end` only on the last |
| `multi_frame_64_counter_wrap` | `-l 64` | nine frames, so the 3-bit `frame_count` wraps from 7 to 0 |
| `multi_frame_crc32` | `-l 4 -o crc32` | a payload that fits one frame whose 4-byte CRC32 trailer forces a second |
| `multi_frame_hmac` | `-l 4 -o hmac` | the same with the HMAC trailer |
| `multi_frame_high_address` | `-a 65 -d 66 -l 12` | the 6-bit `sender` truncation on every fragment, not only the first |
| `multi_frame_reverse_direction` | `-a 2 -d 1 -l 12` | the multi-frame packet in the other direction |
| `multi_frame_priority_critical` | `-r 0 -l 12` | priority repeated in every fragment's CAN ID |
| `multi_frame_two_packets` | `-l 12 -n 2` | two back-to-back packets from one sender: the CFP2 packet counter increments |

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

### Multi-frame fixtures

A multi-frame capture file holds one `candump` line per CAN frame, in the order
libcsp sent them, and its `*.meta.toml` records the whole ordered sequence
instead of a single frame:

```toml
packet_count = 1                      # CSP packets in the capture
expected_frame_count = 2              # CAN frames in the capture
expected_extended_can_ids = ["0x10004082", "0x10004085"]
expected_frame_payloads_hex = ["00 04 14 80 55 56 57 58", "59 5A 5B 5C 5D 5E 5F 60"]
expected_reassembled_payloads_hex = ["55 56 57 58 59 5a 5b 5c 5d 5e 5f 60"]
```

`expected_reassembled_payloads_hex` holds one entry per CSP packet: the payload
exactly as `csp_client` handed it to libcsp, plus the CRC32/HMAC trailer libcsp
appended when a flag is set. Frames of consecutive packets are listed in capture
order, `expected_frame_count / packet_count` frames per packet. In the CAN ID,
the low bits are `end` (bit 0), `begin` (bit 1) and `frame_count` (bits 2-4);
bits 5-6 are the CFP2 packet counter.

Python implementation starts after these fixtures are fixed. The Python CSP v2
codec must match the committed vectors byte-for-byte.
