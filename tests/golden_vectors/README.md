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

### CSP v1 over CAN vectors

`csp_v1/can/` holds twenty vectors for CSP **v1** over CAN (CFP 1.x). They come from the same pinned libcsp `v2.1` build: the release speaks both
protocol versions and picks one at run time, so `csp_client -v 1` is all it takes and no
second libcsp is pinned. Unlike the v2 CAN vectors above, their `*.meta.toml` files are
*written by* `scripts/regenerate-v1-can-vectors.py` (run by `regenerate-vectors.sh`, so CI
regenerates and diffs captures and metadata alike). The script holds the table of what each
vector asks `csp_client` for; the metadata splits into what was *asked* (addresses, priority,
flags, payload length, packet count) and what libcsp *sent* (CAN IDs, frame data, headers,
reassembled payloads), and `tests/test_golden_vectors_csp_v1.py` compares the two and checks
the capture against the CFP1 layout, so the helper cannot become its own oracle. Regenerate
with:

```sh
docker compose exec -T libcsp-vectors python3 /app/tests/golden_vectors/scripts/regenerate-v1-can-vectors.py
```

What the vectors show about libcsp `v2.1` running as CSP v1:

- the CAN identifier is `source (5) | destination (5) | type (1) | remaining (8) |
  identification (10)`; type is 0 for the first frame of a packet, 1 for the rest;
- **every** packet is fragmented, even an empty one: the first frame carries the 4-byte
  big-endian CSP header (priority 2, source 5, destination 5, destination port 6, source
  port 6, flags 8), a 2-byte big-endian payload length and at most two payload bytes; later
  frames carry up to eight. A packet fits one frame only up to two payload bytes (the v2
  limit is four);
- `remaining` counts the frames still to come, so the first frame says how many follow
  (`multi_frame_200` runs 25 down to 0) and a lost frame shows as a gap in the countdown;
- the 10-bit identification field is a per-interface packet counter: it is the same in every
  frame of a packet and increases by one for the next packet (`multi_frame_two_packets`);
- the CRC32 and HMAC trailers are four bytes appended to the payload and counted in the length
  field, as in v2; both flags are single bits in the header's flags byte (`0x01`, `0x08`);
- the source *port* in the header is chosen by libcsp (it counts connections) and is the one
  header field `csp_client` does not control; the destination port is the ping port, 1;
- addresses are 5 bits: `csp_client -v 1` refuses anything above 31, and 31 is the broadcast
  address, so the highest usable pair is 30 and 29 (`high_address`). A source equal to the
  destination is delivered by libcsp's loopback and never reaches the bus, so no vector uses it.

Not captured, and why: RDP (stateful, as for v2); XTEA (removed from libcsp in 2.0, so `v2.1`
has no way to send it); the 10-bit identification counter wrapping (it takes 1024 packets and
`csp_client` sends at most eight per run); CSP v1 over KISS and over ZeroMQ (no vectors yet).

### CSP v1 over KISS and ZeroMQ vectors

`csp_v1/kiss/` (thirteen vectors) and `csp_v1/zmq/` (eleven) hold CSP **v1** for the links that
carry a packet whole, from the same pinned libcsp `v2.1` run as CSP v1 (`csp_client -v 1 -k` and
`-z`). Their `*.meta.toml` files are written by `scripts/regenerate-v1-whole-packet-vectors.py`
(run by `regenerate-vectors.sh`; no `vcan0` needed, only a pty and libcsp's own `zmqproxy`), which
refuses to write a vector whose wire bytes disagree with what it asked `csp_client` for. As for
CAN, the metadata keeps what was *asked* apart from what libcsp *sent*, and
`tests/test_golden_vectors_csp_v1_whole_packet.py` compares them and checks the layout. Regenerate
with:

```sh
docker compose exec -T libcsp-vectors python3 /app/tests/golden_vectors/scripts/regenerate-v1-whole-packet-vectors.py
```

What the vectors show about libcsp `v2.1` running as CSP v1:

- **KISS** is the v2 framing with the 4-byte header: `C0 00 <escaped: header(4) payload crc32c(4)> C0`,
  `DB DC`/`DB DD` escaping, the CRC-32C appended to every frame whether or not `CSP_FLAG_CRC32` is
  set (that flag then gives two checksums), no fragmentation. The header is big-endian: priority 2,
  source 5, destination 5, destination port 6, source port 6, flags 8;
- **ZeroMQ** is `<destination address (1)> <header(4)> <payload>`: the first byte is the destination
  address, which libcsp uses as the topic, and it is stripped on receipt without being compared with
  the header. There is no framing and no checksum of the link's own;
- the header is **big-endian** on both links, as on CAN (a libcsp fork pull request,
  EmOne/libcsp #5, says v1's ZeroMQ interface once used host byte order; the pinned `v2.1` does not);
- as on CAN, the source port is the one header field `csp_client` does not choose (libcsp numbers its
  connections), and the highest usable address is 30 (31 is the broadcast address).

### KISS (serial) vectors

`kiss/` holds the same kind of fixtures for CSP v2 over a serial line with KISS
framing. They need no `vcan0`: `scripts/capture-kiss.py` opens a pty pair, runs
`csp_client -k <pty slave> -p ...` (libcsp's own KISS interface) and records the
bytes libcsp writes. Regenerate them with:

```sh
docker compose exec -T libcsp-vectors /app/tests/golden_vectors/scripts/regenerate-kiss-vectors.sh
```

(`regenerate-vectors.sh` runs it too, which is what CI uses.) A capture file has
one line per KISS frame: the exact wire bytes, `FEND` to `FEND` inclusive, as
space-separated upper-case hex. Its `*.meta.toml` records `interface = "pty"`,
the generation command and, in place of CAN IDs, `expected_frames_hex` (the wire
frames) and `expected_packets_hex` (each frame with the KISS framing and escaping
removed).

What the vectors show about libcsp `v2.1`:

- a frame is `C0 00 <escaped packet> C0`: `FEND`, the TNC command byte `0x00`,
  the packet, `FEND`; inside, `0xC0` is sent as `DB DC` and `0xDB` as `DB DD`;
- the packet is the 6-byte CSP v2 header (priority, destination, source, ports and
  flags in network byte order, the full 14-bit addresses), the payload, and a
  4-byte CRC32;
- libcsp's KISS interface **always** appends that CRC32, whether or not
  `CSP_FLAG_CRC32` is set; with the flag the payload already ends in the flag's own
  4-byte trailer and a second CRC32 follows it (`flag_crc32`, `flag_hmac`);
- the CRC32 is CRC-32C (Castagnoli, polynomial `0x82F63B78`, initial value and final
  XOR `0xFFFFFFFF`), computed over the payload only -- not the header -- and sent
  big-endian (`ping`: payload `55` gives `36 A9 9D 9E`);
- there is no fragmentation: 64- and 200-byte payloads are one frame.

| Vector | Covers |
| --- | --- |
| `ping` | baseline: `src=1 dst=2`, 1-byte payload |
| `payload_empty`, `payload_64`, `payload_200` | payload bounds, no fragmentation |
| `priority_critical`, `priority_low` | the priority field; `priority_low` puts `0xC0` in the header (escaped) |
| `high_address`, `max_address` | 14-bit addresses kept whole, unlike the 6-bit CAN sender |
| `reverse_direction` | the node pair addressed the other way |
| `flag_crc32`, `flag_hmac` | flag trailer plus the KISS CRC32 |
| `escape_fend`, `escape_fesc` | payloads containing the KISS control bytes |
| `two_packets` | back-to-back frames |

### ZeroMQ vectors

`zmq/` holds fixtures for CSP v2 through a libcsp ZeroMQ hub. They need no `vcan0`
and no pty: `scripts/regenerate-zmq-vectors.sh` starts libcsp's own `zmqproxy` hub on
localhost, subscribes the repository-owned `bin/zmq_capture` (built from
`src/zmq_capture.c`, a listener like `candump`) to its publish port, and sends with
`csp_client -z 127.0.0.1 -p ...` through libcsp's own ZeroMQ interface. Regenerate
them with:

```sh
docker compose exec -T libcsp-vectors /app/tests/golden_vectors/scripts/regenerate-zmq-vectors.sh
```

(`regenerate-vectors.sh` runs it too, which is what CI uses.) The image needs
`libzmq3-dev` for libcsp to build its ZeroMQ interface; the `Dockerfile` installs it.
A capture file has one line per message, space-separated upper-case hex. Its
`*.meta.toml` records `interface = "zmq"`, the generation command, and
`expected_messages_hex` / `expected_payloads_hex`.

What the vectors show about libcsp `v2.1`:

- a message is the 6-byte CSP v2 header (network byte order, full 14-bit addresses)
  followed by the payload, and nothing else: no framing, no escaping, no topic or
  address prefix (the prefix byte exists only for CSP v1), **no CRC32**;
- unlike KISS, the interface adds nothing to a packet, so a `CSP_FLAG_CRC32` or
  `CSP_FLAG_HMAC` packet carries only the flag's own 4-byte trailer;
- there is no fragmentation: 64- and 200-byte payloads are one message;
- libcsp's ZeroMQ interface subscribes to *everything* (an empty filter, the
  `rxfilter` arguments of the init functions are ignored in `v2.1`) and a client
  publishes to the hub's subscribe port (6000) and subscribes from its publish
  port (7000).

| Vector | Covers |
| --- | --- |
| `ping` | baseline: `src=1 dst=2`, 1-byte payload |
| `payload_empty`, `payload_64`, `payload_200` | payload bounds, no fragmentation |
| `priority_critical`, `priority_low` | the priority field |
| `high_address`, `max_address` | 14-bit addresses kept whole |
| `reverse_direction` | the node pair addressed the other way |
| `flag_crc32`, `flag_hmac` | the flag's own trailer and no second CRC32 |
| `two_packets` | back-to-back messages |

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
