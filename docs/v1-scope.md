# Product v1 scope

v1 is the first version of this product. It is intentionally narrow. See
[`docs/architecture.md`](architecture.md) for how these pieces fit together
and [`docs/roadmap.md`](roadmap.md) for what's implemented versus planned.

## Protocol support

v1 implements **CSP v2** and **CSP v1** (see [CSP v1](#csp-v1)), targeting:

- libcsp-compatible CSP v2 packet pack/unpack;
- classic CAN 2.0 frames with 8-byte data fields;
- extended 29-bit CAN identifiers only;
- packets of any length up to the node's `max_packet_bytes`: one CAN frame when
  the payload (plus any CRC32/HMAC trailer) is 4 bytes or fewer, otherwise
  several CFP2 frames — see [CFP2 fragmentation](#cfp2-fragmentation-and-reassembly).

The config exposes the logical CSP fields required by libcsp/CSP routing:
`priority`, `source`, `destination`, `destination_port`, `source_port`,
`flags`. The wire layout is locked by committed golden vectors generated from
official libcsp `v2.1` at commit `48f7fb0` — see
[`tests/golden_vectors/README.md`](../tests/golden_vectors/README.md) for the
generation workflow and fixture contract.

CSP v2 also runs over a **serial KISS line** and through a **ZeroMQ hub**, each as a
separate code path from CAN — see [Serial KISS transport](#serial-kiss-transport)
and [ZeroMQ transport](#zeromq-transport).

Planned later, not implemented in v1:

- raw CAN/DBC adapter;
- custom mission codec plugin.

This project does not reuse or expose the CAN wire protocol from the
`AetherFlow` project; the signal codec design may reuse lessons learned there,
but the protocol layer is CSP v2, plus CSP v1 over CAN.

## Modules

The package ships four simulated modules:

- **Generic EPS** (`generic_eps`) — battery/load/power-mode behavior and EPS
  telemetry.
- **OBC Peer** (`obc_peer`) — stateless rule-engine mock OBC.
- **Simple Payload** (`simple_payload`) — basic power draw and command/
  data-volume behavior.
- **Thermal** (`thermal_rc`) — lumped-capacitance RC node with a commandable
  heater.

**ADCS** remains out of scope: attitude dynamics are a different class of
model from anything above.

Each module's tunable physical parameters (battery capacity, power-mode
thresholds, heat capacity, and so on) are overridable per node through an
optional `[nodes.<node>.params]` TOML table, validated against that module's
own configuration object — see
[`configs/schema/module_schema.md`](../configs/schema/module_schema.md#module-parameters).

### Modules are the extension point

The list above is what ships, not what is possible. No `generic_eps` will match
a specific real EPS board, so writing a module is expected to be the first
serious thing a user does. `cubesat_testbed.modules.registry.register_module`
takes a `module_type` name, the factory `build_runtime` calls to construct it,
the dataclass that validates its `params`, and its build/tick order. The four
modules above are registered through that same public call and get no
privileged treatment: a module registered from another package is nameable in
setup TOML, tunable, built, ticked, commanded and observed over the bus
identically.

Registration happens on import, and `module_type` is validated when the setup
config is parsed, so a module defined elsewhere must be imported first —
`cubesat-testbed run --module-import PACKAGE` from the CLI, or a plain import
from the Python API. The full walkthrough is
[`docs/writing-a-module.md`](writing-a-module.md).

## Fault injection

Fault handling is split into two responsibilities.

### Fault Injection Engine

`cubesat_testbed.fault_injection` is a passive executor of external commands.
It does not know about thresholds and does not evaluate rules. It supports:

1. **Direct overrides**
   - `state_override`: override internal model state, e.g.
     `eps.model.temperature = 95.0` for `10s`.
   - `signal_override`: spoof outgoing telemetry/raw signal value, e.g.
     `eps.telemetry.voltage = 4500` for `5s`.
2. **Named fault flags** — activate a named fault inside a module, e.g.
   `battery_cell_dead`. The module's physical model reacts to the flag and
   evolves normally from that altered state.

Overrides expire by virtual time / cycles as defined by the scenario. Named
faults remain active until cleared or until the module's own logic clears
them.

### OBC Peer / Rule Engine

Threshold-triggered behavior belongs to the OBC Peer rule engine, not to the
Fault Injection Engine. The OBC Peer listens to decoded telemetry, evaluates
rules of the form `IF <condition> THEN <action>`, and can send a named CSP
command through the configured codec/bus, or call the Fault Injection Engine
to trigger a named fault or override.

v1 rule semantics: threshold conditions, `for` duration on a trigger,
`cooldown`; no ACK waiting; commands are configured as named commands mapped
to binary payloads; no stateful "on enter/on exit" rules.

Rules live in setup config under `[nodes.<obc-node>.rules.<name>]`, so the
low-battery example above is not built into the engine — it is exactly what
the packaged `default` example declares:

```toml
[nodes.obc.rules.low_battery_shed_payload]
signal = "eps.telemetry.battery_percent"
op = "<"
threshold = 30.0
for = "3s"

[[nodes.obc.rules.low_battery_shed_payload.actions]]
type = "send_command"
command = "payload_power_off"
```

An OBC node's rules can be overridden wholesale from a standalone rules file
(`cubesat-testbed run ... --rules PATH`, or `build_obc_rules_from_file` /
`obc_rules=` for library callers), for example to run the same satellite
setup against several different FDIR rule sets without editing it. A rules
file reuses the exact same schema, keyed by node name at the top level (see
[`configs/schema/module_schema.md`](../configs/schema/module_schema.md#obc-peer-rules)).

### Bus faults

`FaultInjectionEngine` breaks a *module*. Bus faults break the *bus*: the in-memory
transport is wrapped in a decorator (`transport/faults.py`,
`FaultInjectingTransport`) that applies explicit rules to the CAN frames passing
through it. It is passive like the engine above: it decides nothing, it applies the
rules it was given. A rule says which frames, when, and what happens:

```toml
[bus_faults]
seed = 42                 # every random choice derives from this; default 0

[[bus_faults.rules]]
type = "drop"             # drop | delay | corrupt | duplicate | reorder
port = 10                 # destination port; also source_port, source, destination
nth = 2                   # the 2nd matching frame (1-based)
```

The same fields work as a scenario step, which puts the rule in force from that
instant on and starts counting `nth` from there:

```yaml
- action: inject_bus_fault
  type: delay
  port: 20
  nth: 1
  delay: 300ms
```

| field | meaning |
| --- | --- |
| `type` | `drop` loses the frame; `delay` holds it for `delay` of virtual time; `corrupt` XORs `mask` (default `0xFF`) into data byte `byte` (default: one drawn from the seed); `duplicate` delivers it twice; `reorder` holds it until the next frame in that direction has gone by, or for `hold` (default `1s`) if none comes |
| `direction` | `tx` (default) acts when a node sends, before the frame reaches the bus; `rx` acts on what the receiver reads off it. Write two rules for both |
| `port`, `source_port`, `source`, `destination` | selectors; a frame must match all that are given. `port` is the CSP destination port |
| `nth`, `count` | fire on the `nth` matching frame and the next `count - 1` (default 1). Without `nth` the rule fires on every matching frame, at most `count` times if given |
| `probability` | fire on each matching frame with this probability, drawn from the seeded generator; not combinable with `nth` |
| `name` | label in the trace and the result; default `<type>-<position>` |

Things worth knowing:

- **Frames, not packets.** A multi-frame packet is several frames, so `nth` can drop
  exactly one fragment. Only a packet's first frame carries its ports and source
  address, so the decorator remembers each first frame's header and matches the
  continuation fragments by the packet they belong to; a rule on `port = 10` therefore
  reaches every fragment of a packet to port 10. The receiver then reports the loss
  like any other (`frame_lost`, `timeout`, ...), as dropped packets.
- **`byte` indexes the frame's CAN data field**, as `--trace` prints it in `data=`
  (the first frame's first four bytes are the CSP header). Flipping a header byte
  can make the frame unroutable or undecodable, which the trace then says. A `byte`
  past the end of a short frame corrupts nothing; the fault is still reported, with
  `skipped` in its detail.
- **One rule per frame.** Rules are tried in the order they were added (setup first,
  then scenario steps); the first one that fires acts, and a rule counts only the
  frames that reached it.
- **Delay is virtual.** A held frame is released by a scheduled event at its
  virtual time and delivered in that same instant; nothing sleeps.
- **Determinism.** Each rule has its own generator, seeded from `seed` and the rule's
  position, and nothing reads the wall clock, so the same seed, rules and scenario
  give a byte-identical `--trace`. Adding a rule does not change the draws of the
  others. The seed is part of the result (`bus_fault_seed`).
- **Visible.** `--trace` prints a `FAULT` line right after the `TX` line of the frame
  it concerns (an `rx` fault, which has no such line, is printed where it happens):

  ```text
  trace t=3000000 TX can_id=0x10006083 dlc=5 pri=2 src=1 dst=3 dport=10 ... command obc.payload_power_off->payload
  trace FAULT t=3000000 rule=drop-1 action=drop dir=TX match=1 can_id=0x10006083 src=1 dst=3 dport=10 sport=10 data=0004a28000
  ```

  A `delay` fault carries `until=<virtual time>`, a `corrupt` fault the byte, mask and
  before/after values, a `reorder` fault its `hold`. The same events are `FAULT` lines
  in the text output, `bus_faults`/`bus_fault_seed` in `--json`, rows in the HTML
  report, and `bus_faults` on the run result.
- **What `TX` and `RX` mean once rules exist.** The tracing adapter sits above the
  decorator, so `TX` is a frame a node *tried* to send and `RX` a frame that
  *reached* the receiver. A dropped frame is a `TX` with a `FAULT drop` and no `RX`;
  a duplicated one is one `TX` and two `RX`.
- **`rx` faults act on the bus monitor stream**, which is where the runner decodes
  every frame for every node; per-endpoint streams pass through unchanged.

**In-memory transport only.** A real bus, a serial line and a ZeroMQ hub deliver on
wall-clock time, so a delay or an ordering there could not be reproduced from a seed,
and a fault can only touch what the testbed itself sends or reads, not traffic between
two real devices. `[bus_faults]` and `inject_bus_fault` are therefore rejected when
the setup's transport is not `in-memory`. The decorator is always present on the
in-memory bus and is a plain pass-through until a rule exists.

## Config and scenario files

Static CubeSat/testbed setup uses TOML; scenario scripts use YAML. Both are
validated with Pydantic (`tomllib` parses the TOML). Full field reference:
[`configs/schema/module_schema.md`](../configs/schema/module_schema.md).

```toml
[transport]
type = "in-memory"

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "simulated"
module_type = "generic_eps"
address = 2
```

```yaml
name: "EPS Low Battery Protection Test"
steps:
  - action: "inject_fault"
    type: "state_override"
    target: "eps.model.battery_percent"
    value: 25
    duration: "5s"
  - action: "wait"
    virtual_time: "3s"
  - action: "assert"
    signal: "payload.telemetry.power_status"
    op: "=="
    value: "offline"
    timeout: "1s"
```

### Virtual-time duration units

`VirtualTime`'s base unit is microseconds, so sub-second scenario timing (short
command timeouts, fast FDIR reaction windows) can be expressed exactly with
plain integers. Every duration field (`duration`, `virtual_time`, `timeout`,
`for`, `cooldown`) must spell out an explicit unit — `s`/`sec`/`seconds`,
`ms`/`millisecond(s)`, or `us`/`microsecond(s)` — except the literal `0`, which
is unambiguous in any unit and may be given bare. A bare non-zero integer (for
example `duration: 5`) is rejected with a validation error rather than
silently reinterpreted, since the base unit changed from virtual seconds to
microseconds during v1 development.

Simulated modules are ticked, the fault-injection cycle counter is advanced,
and configured telemetry is emitted once per virtual second (1,000,000 µs) via
a recurring engine timer; this cadence is fixed in v1, not yet configurable
per node.

## Signal codec

The binary signal codec (`cubesat_testbed.protocol.signal_codec`) places named
fields at fixed positions in a payload:

- **Byte-aligned fields**: a byte offset and a byte length, signed/unsigned
  integers of either byte order (`big`/`little`) and IEEE 754 binary32/binary64
  floats.
- **Bit fields**: a byte offset, a bit offset inside that byte (0-7) and a bit
  length (1-64), unsigned or two's complement. Bits are numbered from the most
  significant bit of a byte and a field is read most significant bit first,
  across byte boundaries if it needs to: the order of Kaitai Struct's `bN`
  types, CCSDS and network headers. There is one bit order on purpose, and
  `endian` does not apply to a bit field.
- **Fixed-length arrays** of either kind (`count`): the elements sit back to
  back, each the field's own width, and the value is a tuple.

Every element gets the same `scale`/`offset` conversion; `units`, `min` and
`max` are passive metadata. Fields of one payload must not overlap; gaps are
sent as zero. Still out of scope: little-endian bit numbering
(Motorola/Intel-style choices), enums beyond the label map below, nested
structs, variant records and custom field plugins. They are added when a real
layout needs them.

The codec is checked against bytes it did not produce
(`tests/test_layout_vectors.py`): a QUBE beacon decoded by the Kaitai Struct
parser of `satnogs-decoders`, and the CSP v1 and v2 headers libcsp wrote into
the golden vectors, read as bit fields.

## CFP2 fragmentation and reassembly

A CSP packet whose payload does not fit one CAN frame travels as several
frames, exactly as libcsp `v2.1` sends them (CFP2, libcsp's CAN fragmentation
protocol). The layout is fixed by the multi-frame golden vectors
(`tests/golden_vectors/multi_frame_*`):

- the **first frame** has `begin` set and carries the 4-byte CSP header
  extension plus up to 4 payload bytes (the other 16 header bits are in the
  29-bit CAN ID);
- every **later frame** carries up to 8 payload bytes, no header, and a
  `frame_count` that counts 1, 2, ... and wraps from 7 to 0;
- the **last frame** has `end` set — there is no length field;
- a 2-bit **packet counter** tells consecutive packets from one sender apart;
- the CRC32/HMAC trailer libcsp appends counts as payload, so a packet with
  either flag needs a second frame even for a 4-byte payload.

Each node has a `max_packet_bytes` setting (default 256, libcsp's default
`CSP_BUFFER_SIZE`): the most payload, trailer included, it can send or
reassemble. A real board can be built with a different value than its peers, so
it is a per-node setting:

```toml
[nodes.payload]
mode = "simulated"
module_type = "simple_payload"
address = 3
max_packet_bytes = 8
```

A command or telemetry layout longer than the *sending* node's limit is a config
error (its libcsp could not build the packet). A packet longer than the
*receiving* node's limit is sent and then dropped by that node.

A receiving node discards a packet, as libcsp does, when a frame is missing or
out of order (`frame_lost`), the packet outgrows the node's limit (`overflow`),
a continuation frame has no packet in progress (`orphan_fragment`), a new first
frame arrives mid-packet (`restarted`), the frame or header is invalid
(`malformed`), or no frame arrives for one virtual second (`timeout`). A complete
packet whose telemetry payload the signal's codec cannot read (a value with no enum
label, a corrupted byte) is discarded as `undecodable`. Nothing is discarded silently:

- `run` prints a `DROP t=... node=... reason=... detail=...` line per dropped
  packet before the summary (`SUMMARY` gains `dropped_packets=N`);
- `--json` carries a `dropped_packets` list;
- `--trace` writes the same `DROP` line at the instant it happened, and traces
  every fragment (`cfp=begin|more|end pc=... fc=...`) with the reassembled
  packet (`frames=N payload=...`) on the last one.

A dropped packet does not fail the run by itself; an assertion that needed it
does. Reassembly runs on virtual time, so the same frames at the same times give
the same result on every run.

Still out of scope: other physical layers and RDP. A packet carrying several
telemetry signals is a telemetry *packet* (see *Telemetry wire encoding*); it is
fragmented like any other packet.

## CSP v1

A setup selects exactly one CSP version for the whole run:

```toml
protocol_version = 1   # at the top of setup.toml; the default is 2

[transport]
type = "in-memory"     # or "socketcan", "kiss", "zmq"
```

With `protocol_version = 1` every node, command, telemetry signal and frame is CSP v1, on
every transport: framed for CAN by CFP 1.x over `in-memory` and `socketcan`, and as one unit
per packet over `kiss` and `zmq` (see [below](#csp-v1-over-kiss-and-zeromq)). The two versions
are different code paths that share no codec code (`protocol/csp_v1*.py` and
`protocol/csp_v2*.py`) and are not meant to share a network, so a v1 packet is never decoded as
v2 or the other way round, and an adapter refuses a frame of the other version.

What differs from CSP v2, as libcsp `v2.1` run as CSP v1 shows in the committed vectors
(`tests/golden_vectors/csp_v1/`, see the [vector README](../tests/golden_vectors/README.md)):

- addresses are 5 bits (0-31) and **31 is the broadcast address**, so a node can be at most
  30; setup validation says so. Ports (0-63), priorities and the CRC32/HMAC flags are as in v2;
- the CSP header is 4 bytes, and **every** packet is fragmented, even an empty one: the first
  frame carries the header, a 2-byte payload length and at most two payload bytes, later frames
  up to eight. A packet fits one frame only up to two payload bytes (v2: four);
- the CAN identifier carries source, destination, a first-frame/continuation bit, the number of
  frames still to come (`rem=`) and a 10-bit packet identification (`id=`) that counts up per
  sending node and wraps; there is no priority in it;
- a packet is complete when as many bytes arrived as the length field declared. Frames are
  matched to their packet by source, destination and identification; a gap in the `rem=`
  countdown is a lost frame; an incomplete packet is dropped after one virtual second.

Drops are reported exactly as for v2 (`frame_lost`, `orphan_fragment`, `overflow`, `timeout`,
`restarted`, `malformed`, with the node and the limit), with one difference of detail: a packet
longer than the receiving node's `max_packet_bytes` is refused at its *first* frame, since the
length field announces it, and is reported once; its remaining frames are not reported again as
strays. `--trace` shows v1 frames with `cfp=begin|more|end rem=N id=I` in place of v2's
`pc=`/`fc=`, and the HTML report and `--json` carry them the same way. [Bus faults](#bus-faults)
work on v1 frames too: a rule on a port reaches every frame of a packet.

### CSP v1 over KISS and ZeroMQ

The whole-packet links carry a v1 packet as one unit and have no CFP1 framing:

- **KISS** is the v2 serial framing with the 4-byte header:
  `C0 00 <escaped: header(4) payload crc32c(4)> C0`. The CRC-32C is appended to every frame
  whether or not `CSP_FLAG_CRC32` is set (that flag then gives two checksums), so the payload limit
  is `max_packet_bytes - 4`, as for v2. The receive buffer drops a frame once header plus data
  reach `max_packet_bytes + 4` bytes (v2: `+ 6`), one byte short of the buffer, as libcsp does;
- **ZeroMQ** is `<destination address (1)> <header(4)> <payload>`: the first byte is the
  destination, which libcsp uses as the topic and strips on receipt without comparing it with the
  header. No framing and no checksum of the link's own, and the testbed ignores the hub's echo of
  its own publications as for v2;
- the v1 header is big-endian on both links.

Everything else on these links is as for CSP v2: packets for simulated nodes stay inside the
testbed, the rest go out on the line or to the hub, what the link rejects (a CRC32 mismatch, a
too-short frame, an oversized one) is reported as dropped packets, and `--trace` shows one line per
packet with `link=serial` or `link=zmq`. A real libcsp process run as CSP v1 exchanged packets
with the testbed in both directions over a pty and through libcsp's own `zmqproxy`; the
`libcsp`-marked tests repeat this on every link in both versions (see `tests/README.md`). Bus faults
stay in-memory only.

The packaged `csp-v1` example (`run --example csp-v1`) is the default satellite as CSP v1.

Not modelled: libcsp's loopback for a packet a node sends to its own address (a frame
reaches the bus whatever its destination) and its rewrite of the broadcast address to the
receiving node's own.

## Serial KISS transport

`transport.type = "kiss"` carries CSP v2 over a UART, a USB-serial adapter or a pty,
with the framing libcsp `v2.1` uses (fixed by the golden vectors in
`tests/golden_vectors/kiss/`):

```toml
[transport]
type = "kiss"
device = "/dev/ttyUSB0"   # a tty, or the path of a pty
baudrate = 115200         # default; standard rates only
```

- **Frame.** `FEND 0x00 <packet> FEND`, with `0xC0` sent as `DB DC` and `0xDB` as
  `DB DD` inside. The packet is the 6-byte CSP v2 header (full 14-bit addresses),
  the payload and a 4-byte CRC32.
- **CRC32.** libcsp's KISS interface appends it to *every* frame, whether or not
  `CSP_FLAG_CRC32` is set. A packet with that flag therefore carries two trailers:
  its own, inside the payload, and the interface's after it. The CRC is CRC-32C
  (Castagnoli), over the payload only, big-endian; a receiver also accepts it over
  header plus payload, as libcsp does.
- **No fragmentation.** A frame carries one whole packet; the CFP2 rules above are
  CAN-only.
- **Size limit.** The CRC32 shares libcsp's packet buffer with the payload, so on KISS
  the payload limit is `max_packet_bytes − 4` (252 at the default 256), not
  `max_packet_bytes`. A command that can leave on the line and does not fit is a
  config error naming both numbers; one to a simulated node is delivered inside the
  testbed with no CRC32 and keeps the full `max_packet_bytes`. libcsp itself is
  asymmetric at the very edge: it will *send* a 252-byte payload but its receive side
  drops it (a frame must stay under `max_packet_bytes + 6` bytes), so 251 is the
  largest payload that round-trips. The testbed reproduces both halves.
- **A line has two ends.** A packet addressed to a node the testbed simulates is
  delivered inside it and never reaches the line; a packet for any other address
  (`software`/`hardware` nodes) goes out as a KISS frame, and every frame arriving
  on the line is delivered like a bus frame. Like SocketCAN, a frame sent is not
  echoed back.
- **Frames the link rejects are reported, never swallowed.** A frame with a bad
  CRC32 (`crc_error`), too short for a header and CRC (`too_short`), longer than the
  receive buffer (`overflow`) or carrying an unsupported header (`malformed`)
  appears as a `DROP` line, in `--json` and in `--trace`, exactly like a CAN packet
  lost to reassembly; a packet too long for the *receiving node's*
  `max_packet_bytes` is dropped as `overflow` too.
- **Trace.** A packet is one line: `link=serial` with the exact KISS frame as
  `data`, or `link=local` and `data=-` when it never left the testbed, then the
  header fields, `payload` and the usual route annotation.
- **Pacing.** Run with `--realtime`, as for SocketCAN, so a real peer gets
  wall-clock time to answer; without it the CLI warns.

The serial line is opened with `termios` from the standard library, so no extra
dependency is needed (Linux only, like SocketCAN). `libcsp` itself can be pointed at
the same line, or at the other end of a pty pair, with
`csp_usart_open_and_add_kiss_interface`.

Out of scope: KISS over anything but a byte stream, non-standard baud rates, and
hardware flow control.

## ZeroMQ transport

`transport.type = "zmq"` makes the testbed one more client of a libcsp ZeroMQ hub
(`zmqproxy`), the usual way libcsp applications on a Linux host are joined. An
unmodified libcsp process on the same hub can then talk to a simulated node, and
the other way round, with no CAN adapter or kernel module:

```toml
[transport]
type = "zmq"
publish_endpoint = "tcp://127.0.0.1:6000"    # where the testbed publishes (default)
subscribe_endpoint = "tcp://127.0.0.1:7000"  # where it listens (default)
settle_seconds = 0.5                          # default; 0 for a hub known to be ready
```

The names and ports are libcsp's own (`csp_zmqhub_init_w_endpoints`): a client
publishes to the hub's *subscribe* port and subscribes from its *publish* port. It
needs the optional `pyzmq` dependency, imported only when the transport is used:

```sh
pip install 'cubesat-testbed[zmq]'
```

The layout is fixed by the golden vectors in `tests/golden_vectors/zmq/`:

- **Message.** The 6-byte CSP v2 header (full 14-bit addresses) followed by the
  payload, and nothing else: no framing or escaping, no topic or address prefix
  (that byte is CSP v1 only), no fragmentation.
- **No CRC32.** Unlike KISS, the interface adds no trailer. A packet carrying
  `CSP_FLAG_CRC32` has only that flag's own trailer inside its payload.
- **No filter.** libcsp's ZeroMQ interface subscribes to everything, so the testbed
  does too and selects by CSP address itself.
- **Routing**, as for KISS: a packet for a node the testbed simulates is delivered
  inside it and never reaches the hub; a packet for any other address
  (`software`/`hardware` nodes) is published; every message the hub delivers is
  received.
- **The hub echoes.** A hub broadcasts to every subscriber, the sender included, so
  what the testbed publishes comes back. A message whose source is one of the
  testbed's own simulated addresses can only be that echo and is ignored.
- **Messages the link rejects are reported, never swallowed**: shorter than a header
  (`too_short`), a payload over the receive buffer (`overflow`), an unsupported
  header (`malformed`) appear as `DROP` lines, in `--json` and in `--trace`; a
  packet over the *receiving node's* `max_packet_bytes` is dropped as `overflow` too.
- **Trace.** A packet is one line: `link=zmq` with the exact message as `data`, or
  `link=local` and `data=-` when it never left the testbed.
- **Pacing.** ZeroMQ PUB/SUB silently drops anything sent before the hub and the
  client have learned each other's subscriptions, which is why libcsp's own examples
  sleep after connecting. `settle_seconds` is that pause, once, at connect time, in
  wall-clock time and outside virtual time. Run with `--realtime`, as for
  SocketCAN, so a real peer gets time to answer; without it the CLI warns.

Out of scope: a hub embedded in the testbed (it is always a client), ZeroMQ
authentication (CURVE), and CSP v1's one-byte address prefix.

## Telemetry wire encoding

Every configured telemetry signal requires a wire layout (`offset`, then
`length` or `bit_offset`/`bit_length`, `type`, and optionally `count`/
`endian`/`scale`/`offset_value`) — see
[`configs/schema/module_schema.md`](../configs/schema/module_schema.md) for the
field reference. Signals travel in telemetry packets, declared one of two ways:

- `[nodes.<node>.packets.<name>]` holds the addressing (ports, `to`, priority,
  flags) once, and each `[nodes.<node>.packets.<name>.fields.<field>]` one
  signal's place in the payload: one packet carrying a whole housekeeping
  struct, the way a real board sends it. The packaged `eps-housekeeping`
  example sends seven EPS signals, including an array and two bit fields, as
  one 25-byte packet.
- `[nodes.<node>.telemetry.<name>]` is the shorthand for a packet holding that
  one signal. Every setup written before packets existed is of this kind and
  runs unchanged.

A simulated module's telemetry is encoded through that layout, one packet per
definition (as one or several CAN frames), and sent on the bus every physical
step; the receiver decodes every field of a packet from it. The scenario
runner (and any assertion) only ever observes
telemetry by decoding that same frame back, the same path a real bus listener
or hardware peer would use. There is no path that lets an assertion read a
module's Python state directly.

Two consequences worth knowing:

- A value that does not fit its declared layout (wrong magnitude, a scale
  that does not divide it exactly for an integer field, and so on) fails with
  a clear error instead of silently truncating. Continuously-varying physical
  values (like a battery percentage) should generally use `type = "float"`
  rather than a scaled integer, since integer+scale fields require the
  physical value to map to an *exact* raw integer.
- Because a signal is only observed once its frame has actually been sent and
  decoded, a value change that happens as a *reaction* to telemetry (e.g. the
  OBC commanding the payload off in response to a low-battery reading) is not
  visible until that node's *next* telemetry beacon, one physical step later.
  This one-step propagation delay is real bus behavior, not a bug — size an
  assertion's `timeout` accordingly.

`cubesat_testbed.protocol.telemetry_codec` bridges declarative config to the
signal codec.

### Arrays in scenarios and rules

An array field's signal (`eps.telemetry.cell_voltages_v`) has a tuple value, and
each element is also its own signal, `eps.telemetry.cell_voltages_v[1]`, decoded
from the same frame. An assertion may compare the whole array (`==` against a
YAML list) or one element; an OBC rule compares one number, so it must name an
element, and the setup is refused when an inline rule names a whole array (a
rule from a rules file is checked when the run is built). An index past the end,
or an index on a scalar signal, is refused when the scenario loads. The HTML
report plots each element as a signal of its own. A `signal_override` fault
replaces a whole array signal (give a list); there is no per-element override.

### Who a telemetry frame is addressed to

A real node on a real bus only receives frames carrying its own CSP address (libcsp's
SocketCAN driver filters on the destination field), so it matters who a telemetry frame is
addressed to. A mapping may name the receiver with `to`:

```toml
[nodes.eps.telemetry.battery_percent]
source_port = 10
destination_port = 10
to = "obc"             # the frame is sent src=eps dst=obc
offset = 0
length = 4
type = "float"
```

- **A simulated node's telemetry** is sent to the node `to` names. Without `to` it is sent
  to its own sender (`destination == source`): the in-memory bus's monitor queue sees every
  frame whatever its destination, like a promiscuous bus analyzer, so that works for a
  scenario that only watches the bus, but no other node on a real bus ever receives it. Set
  `to` whenever a real node (or a `software` peer on a real link) has to receive the signal.
- **A real node's telemetry** (a `hardware` or `software` node) is matched on the sender's
  address and the two ports. Without `to` the destination is not looked at, so a board that
  sends its housekeeping to the OBC (`src=2 dst=1`, the normal case) and one that addresses
  itself are both read. With `to` only a frame addressed to that node counts.
- `to` must name a configured node. It does not change the scenario's assertions, only the
  frames.
- A receiver tells a node's packets apart by their ports (and `to`), so two packets of one node
  on the same source and destination ports cannot be told apart and the setup is refused when
  it loads, naming both: signals that belong together go in one
  `[nodes.<node>.packets.<name>]`. A packet without `to` overlaps every other on the same ports;
  packets naming different nodes with `to` do not. A single-signal mapping counts as a packet.

Mind the ports on a real libcsp node: it can only `csp_bind` ports up to
`CSP_PORT_MAX_BIND` (16 in the default build), so a real OBC cannot listen for telemetry on
the ports 20-22 the packaged examples use; pick a port a real receiver can bind when the
receiver is real (`to` plus, say, `destination_port = 10`).

Non-numeric telemetry (e.g. `power_status = "offline"`) declares an `enum`
mapping raw integer wire values to labels, since the wire codec itself only
knows numbers (an array with an `enum` maps every element):

```toml
[nodes.payload.telemetry.power_status]
source_port = 21
destination_port = 21
offset = 0
length = 1
type = "uint"

[nodes.payload.telemetry.power_status.enum]
0 = "offline"
1 = "online"
```

## Runtime model

v1 is deterministic by default: a central discrete-event simulation loop,
virtual time (not wall-clock sleeps), reproducible event ordering, no
background polling loops inside modules, and physical models that update on
scheduled events/timers.

Transport adapters in v1:

- `InMemoryBusAdapter` — CI/tests/local simulation without SocketCAN or root
  privileges;
- `SocketCanAdapter` — Linux HIL/Docker path through `python-can` and `vcan0`
  or a physical CAN interface.

```toml
[transport]
type = "socketcan"
interface = "vcan0" # or a physical interface such as "can0"
receive_own_messages = false
```

For local loopback on Linux, bring up `vcan0` first:

```sh
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
```

SocketCAN/HIL smoke tests are outside normal CI and run only when explicitly
given an interface:

```sh
CUBESAT_TESTBED_SOCKETCAN_INTERFACE=vcan0 uv run pytest -m socketcan
```

Hardware traffic record/replay is out of v1 scope.

### DUT selection and HIL

`cubesat_testbed.dut.manager.resolve_participants(setup)` maps every node to
its DUT role (`simulated`, `software`, `hardware`) from `mode` in setup
config; this is the only place that decision is made. `build_runtime` (used
by `run_scenario`/`run_scenario_files`; `build_in_memory_runtime` is a thin
backward-compatible wrapper that still rejects non-in-memory transports)
builds a runtime from either transport type, giving a locally simulated
module only to `simulated` nodes. A `software`/`hardware` node is never
simulated: it is reached and observed purely as CSP-over-CAN traffic on the
transport, whether that traffic comes from a real board, a real process on
`vcan0`, on the other end of a serial line or on a ZeroMQ hub, or a scenario test
manually placing frames on the in-memory bus.

A configured command is delivered to a module only when its *target* is simulated.
When the target is a `software`/`hardware` node, a real peer handles the command, so
seeing it on the bus (the testbed's own `send_command` coming back on the in-memory
bus, or two real nodes talking on a bus the testbed only watches) is observation: it is
traced with its route and nothing else happens.

By default, `wait()` uses `cubesat_testbed.clock.VirtualClock` and jumps
straight to its target virtual time, exactly as before. Passing
`clock=RealTimeClock()` to `run_scenario`/`run_scenario_files`/`build_runtime`
(or `--realtime` on the CLI, which builds the same clock) instead paces that
jump against wall-clock time: `wait()` blocks on the
transport's `receive(timeout=...)` for whatever real time remains before the
next due virtual instant, delivering any frame that arrives as soon as it
does rather than only once the full window elapses. This is what lets a HIL
run give a real `software`/`hardware` peer a realistic window to respond
instead of the engine blasting straight through virtual time. `RealTimeClock`
also works against the in-memory bus (there is nothing to wait *for*, so it
sleeps out the requested time), which is a convenient way to rehearse a
scenario's real-time pacing without CAN hardware.

Validating this against real hardware is out of v1 scope (no board is needed
to ship v1: `SocketCanAdapter` does not distinguish `vcan0` from a physical
interface, so the same code path applies to both). What v1 *does* validate is
interop with real, official libcsp traffic on a real (virtual) bus:
`tests/test_hil_demo.py` (run with
`CUBESAT_TESTBED_SOCKETCAN_INTERFACE=vcan0 uv run pytest -m socketcan`)
replays the project's committed golden-vector ping — the exact bytes official
libcsp v2.1 puts on the wire — over `vcan0` and confirms `build_runtime`'s
`SocketCanAdapter` receives and decodes it, and that a command frame arriving
the same way reaches and mutates the correct simulated module through the
full scenario runner delivery path. The same file also drives the packaged
`socketcan-hil` example end to end through the CLI's own
`run --realtime` path, against a peer answering from outside the process on
`vcan0`.

The transport a run builds for itself is also closed by that run:
`run_scenario`/`run_scenario_files` release the CAN socket when the scenario
ends, whether it passed, failed or raised. A caller that builds its own
runtime through `build_runtime` keeps owning (and closing) that transport.

## Running scenarios from CLI

```sh
cubesat-testbed run \
  --config setup.toml \
  --scenario scenario.yaml
```

Short flags (`-c`, `-s`) are also supported. By default, the command prints
the per-assertion PASS/FAIL report to `stdout`, then a one-line summary:

```text
PASS t=3000000 assert_3: payload.telemetry.power_status == 'offline'; actual='offline'
SUMMARY scenario='EPS Low Battery Protection Test' assertions=1 passed=1 failed=0 started_at=0 finished_at=3000000
```

`--quiet` suppresses normal `stdout` output while keeping `stderr`
warnings/errors visible. `--json` emits one machine-readable JSON object to
`stdout` instead; it takes precedence over `--quiet` if both are supplied.
Execution errors and interrupts are also reported as JSON in `--json` mode.

`--junit-xml PATH` writes a JUnit XML report to `PATH`, one `<testcase>` per
assertion with a `<failure>` element for each failed assertion, so CI systems
(GitHub Actions, GitLab CI, Jenkins, ...) can render pass/fail natively
without a custom parser. It composes with `--json`/`--quiet`, since it writes
a file rather than controlling `stdout`. Execution errors and interrupts still
produce a report — one `<testcase>` with an `<error>` element — so a CI step
that expects the file to exist does not additionally fail on a missing file.
`time` attributes are real wall-clock seconds the run took, not virtual time.

Exit codes are intended for CI/CD use:

| Exit code | Meaning |
| ---: | --- |
| `0` | Scenario executed and all assertions passed. |
| `1` | Scenario executed, but one or more assertions failed. |
| `2` | Setup/scenario loading, validation or runtime execution error. Missing files also return `2` with a concise error instead of a traceback. |
| `130` | Run was interrupted, for example by `Ctrl-C` / `KeyboardInterrupt`, without printing a traceback. |

If a scenario contains zero assertions, the CLI prints
`warning: 0 assertions in scenario` to `stderr` so a vacuously passing run is
visible in logs.

### Packaged examples

The example setups and scenarios are part of the distribution, not of the
repository checkout: they live in `src/cubesat_testbed/examples/` and are
installed with the package. Without that, `pip install cubesat-testbed` would
deliver a CLI with nothing to feed it, and the PyPI publication would be inert.

Each example is a directory holding exactly three files — `setup.toml`,
`scenario.yaml`, and a `README.md` explaining what the pair demonstrates:

| Example | What it shows |
| --- | --- |
| `default` | in-memory three-node satellite; OBC sheds the payload on a low battery |
| `socketcan-hil` | the same run against a real bus: payload as `hardware` on SocketCAN `vcan0` |
| `module-params` | retuning a built-in module through `[nodes.<node>.params]` |
| `thermal-heater` | an OBC heater loop closing over the bus; companion to [`docs/writing-a-module.md`](writing-a-module.md) |

`run --example NAME` runs one in place, without naming any paths, so a fresh
install is one command from a PASS. It replaces `--config`/`--scenario` rather
than complementing them; combining the two is an error.

```sh
cubesat-testbed run --example default
```

`init [DIR]` copies an example out to be edited — the way to start a real
setup of your own. `DIR` defaults to the working directory and is created if
missing; `--example NAME` picks which one (default: `default`), `--list`
prints the catalogue without writing anything, and existing files are never
overwritten unless `--force` is given. The refusal is checked before any file
is written, so a rejected `init` leaves the target directory exactly as it was.

```sh
cubesat-testbed init my-testbed
```

```text
wrote my-testbed/setup.toml
wrote my-testbed/scenario.yaml
wrote my-testbed/README.md

next: cubesat-testbed run --config my-testbed/setup.toml --scenario my-testbed/scenario.yaml
```

The `next:` line carries whatever flags that example needs — `--realtime` for
`socketcan-hil` — so it can be pasted as printed.

`init` failures use the same exit code `2` as any other execution error. The
examples are also what the test suite runs against, and every example that
does not require a real bus is asserted to pass, so a shipped example cannot
rot unnoticed.

### Loading modules from outside the package

A `module_type` exists only once the Python module registering it has been
imported, and `module_type` is validated while the setup config is parsed.
`--module-import MODULE` imports one before that happens:

```sh
cubesat-testbed run --module-import my_modules -c setup.toml -s scenario.yaml
```

The flag is repeatable. The working directory is prepended to `sys.path`
first, deliberately: a console script otherwise puts its own `bin` directory
there rather than the caller's, and a `my_modules.py` sitting next to
`setup.toml` — the layout `init` produces — would not be importable. Nothing
is auto-discovered: entry-point scanning would make a run's result depend on
what happens to be installed in the environment, which is exactly the kind of
implicit input this project keeps out of a scenario run.

An import failure, and a `module_type` that no import registered, are both
ordinary execution errors (exit code `2`, honoured by `--json`). The unknown
`module_type` message lists the registered names and points at this flag.

See [`docs/writing-a-module.md`](writing-a-module.md) for the module side.

### Wire-level frame trace

`--trace` writes one decoded line per CAN frame to `stderr`:

```text
trace t=4000000 TX can_id=0x10004103 dlc=8 pri=2 src=2 dst=2 dport=20 sport=20 flags=0x00 data=0009450041c80000 payload=41c80000 telemetry eps.telemetry.battery_percent=25.0
trace t=4000000 RX can_id=0x10004103 dlc=8 pri=2 src=2 dst=2 dport=20 sport=20 flags=0x00 data=0009450041c80000 payload=41c80000 telemetry eps.telemetry.battery_percent=25.0
trace t=4000000 TX can_id=0x10006083 dlc=5 pri=2 src=1 dst=3 dport=10 sport=10 flags=0x00 data=0004a28000 payload=00 command obc.payload_power_off->payload
```

Each line carries the virtual timestamp in microseconds, the direction, the
extended CAN identifier, and the decoded CSP v2 header fields. `data` is the
full CAN data field as `candump` would print it; `payload` is just the CSP
application bytes after the 4-byte header extension. The trailing annotation
names what the frame *is* according to the setup config: the command route it
matches, or the telemetry signal decoded out of it through the same codec an
assertion uses. A frame matching no configured route is marked `unrouted`, and
one the CSP v2 codec rejects is reported as `undecodable=...` rather than
dropped — an unexpected frame is usually why the trace was switched on.

Tracing happens at the transport boundary, not inside the runner. Two
consequences worth knowing:

- A frame the testbed sends on the in-memory bus appears twice, as `TX` and
  again as `RX` when the runner reads it back off the monitor stream and
  decodes it. That round trip is precisely what makes an assertion an
  observation of the wire rather than of module state (see
  [Telemetry wire encoding](#telemetry-wire-encoding)).
- With bus fault rules in force, `TX` means a frame a node tried to send and `RX`
  one that reached the receiver; injected faults are `FAULT` lines between them
  (see [Bus faults](#bus-faults)).
- On SocketCAN the adapter does not receive its own messages, so outgoing
  frames appear only as `TX` and everything the peer sends appears as `RX`.
  This is the only place an outgoing HIL command is visible at all.

The trace goes to `stderr`, so it composes with `--json` and `--quiet` without
corrupting machine-readable `stdout`. It is observability only: it does not
touch the assertion path, and enabling it does not change scenario results or
determinism. From the Python API, pass a stream as `trace=` to `run_scenario`,
`run_scenario_files` or `build_runtime`.

### HTML report

`--html PATH` writes one self-contained HTML file for the run, in addition to the
normal output:

```sh
cubesat-testbed run --example default --html report.html
```

**What it contains.**

- **Assertions.** Result, expected and actual value, when it was evaluated and an
  *evidence* link: the frame (the last fragment, for a multi-frame packet) from which the
  signal's latest value was decoded when the assertion was evaluated. For a passing
  assertion that is the frame that satisfied it, for a failing one the frame whose value
  did not match; a signal never seen on the bus says so and has no link. Failed
  assertions are repeated in a list at the top.
- **Setup.** The transport and every node (address, mode, module, `max_packet_bytes`).
- **Signals.** A step plot of each numeric telemetry signal over virtual time, with a
  line where an assertion on it was evaluated; a table of value changes for the others
  (enumerations, booleans).
- **Timeline.** Every frame that crossed the transport boundary, decoded exactly as
  `--trace` prints it (both are rendered from the same `FrameRecord`), interleaved with
  the scenario steps and the dropped packets. On the in-memory bus a frame appears twice,
  `TX` when sent and `RX` when read back. Filters: direction, node, port, the three kinds
  of row, and free text. A multi-frame packet shows every fragment, the earlier ones
  dimmed, and the last one with the reassembled payload.

**Properties.**

- *Self-contained:* inline stylesheet, script and SVG; no font, image or network fetch.
  Everything is readable with scripting off: only the filters need it, and the evidence
  links are plain `#f<id>` anchors. Following one to a row a filter has hidden clears the
  filters.
- *Passive:* it is an observer on the same transport wrapper as `--trace`. It changes
  neither scenario semantics, ordering nor determinism, and a run gives the same result
  with and without it.
- *Deterministic:* the file is a pure function of the run, with no wall-clock time, host
  name, absolute path or random id, so the same inputs give the same bytes.
- *Always written:* also when assertions fail (exit code 1) and when the run aborts
  (exit code 2), the latter with the error and everything recorded up to it. If the file
  cannot be written the run is an execution error (exit code 2).
- *Composes* with `--json`, `--quiet` and `--trace`; the only extra output is a note on
  `stderr` unless `--quiet`.

The timestamps in it are the virtual ones described under
[Timestamps of frames from a real peer](#timestamps-of-frames-from-a-real-peer).

From Python, pass a `cubesat_testbed.report.RunRecorder` as `recorder=` to
`run_scenario`, `run_scenario_files` or `build_runtime`, then
`cubesat_testbed.report.write_report(path, recorder, result)`.

### Hardware-in-the-loop from the CLI

`--realtime` runs the scenario on a `RealTimeClock` instead of the default
`VirtualClock`, so virtual time is paced against wall-clock time and a real
`software`/`hardware` peer gets an actual window to answer. Combined with a
`socketcan` transport in the setup config, this is the full HIL path from the
command line, with no Python glue:

```sh
cubesat-testbed init hil --example socketcan-hil
cubesat-testbed run \
  --realtime \
  --config hil/setup.toml \
  --scenario hil/scenario.yaml
```

The flag is independent of the transport: `--realtime` on the in-memory bus
simply sleeps out each wait, which is a way to rehearse a scenario's pacing
without CAN hardware, and a `socketcan` setup whose nodes are all `simulated`
still runs correctly unpaced. Since the *combination* of a real bus and an
unpaced clock is almost always a mistake, a `socketcan` transport without
`--realtime` prints a warning to `stderr` and continues:

```text
warning: transport.type='socketcan' (interface 'vcan0') without --realtime; virtual time is not paced against wall-clock time, so a real peer gets no time to respond
```

Warnings go to `stderr`, so they compose with `--json`/`--quiet` without
corrupting machine-readable `stdout`.

#### Timestamps of frames from a real peer

Virtual time advances only at scheduled instants: the 1 s physical step and any
scenario events. A frame from a real peer that arrives *between* two of them is
stamped with the earlier one. This applies to every frame on every transport and to
everything derived from the stamp: the `t=` of its `--trace` line, of a `DROP` line
and of a `dropped_packets` entry in `--json`. So with `--realtime` such a frame can
read up to one physical step (1 s) earlier than it actually arrived; a bad frame
written 1.4 s into a wait is reported at `t=1000000`. Within one run the stamps are
consistent with each other (a frame and its drop share one), and order is preserved.

This is deliberate: stamping arrivals from the wall clock would make traces depend
on OS scheduling and weaken the guarantee that the same inputs give the same trace.
With the default virtual clock nothing arrives mid-wait, so none of this applies.
Treat `t=` on traffic from a real peer as "at or after this virtual instant".

Note that virtual-time durations now cost real time: a scenario that waits
`30s` takes 30 seconds under `--realtime`. Size scenario waits and assertion
timeouts accordingly.
