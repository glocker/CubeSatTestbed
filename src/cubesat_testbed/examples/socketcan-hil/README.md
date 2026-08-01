# Example: `socketcan-hil`

The `default` example with one node switched over to real hardware: `payload`
is `mode = "hardware"`, and the transport is SocketCAN on `vcan0`. The OBC and
EPS stay simulated. Which node is real is a config change, not a code change —
that is the whole point of this pair of examples.

Run it with `--realtime`, which paces virtual time against wall-clock time so
the board on the other end actually gets time to answer:

```sh
cubesat-testbed run --realtime --config setup.toml --scenario scenario.yaml
```

Without `--realtime` the engine jumps straight through virtual time and no real
peer is ever heard; the CLI warns on `stderr` when it sees that combination.

For loopback without hardware, bring up `vcan0` first:

```sh
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
```

Then point `interface` at a physical bus such as `can0` when you have one.
Something has to answer as the payload — with nothing on the bus the assertion
correctly fails, because no `power_status` frame ever arrives.

`--trace` is worth switching on here: SocketCAN never receives its own
messages, so the trace is the only place what the testbed sent and what the
board answered appear side by side.
