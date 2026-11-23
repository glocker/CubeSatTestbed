# Example: `csp-v1`

The `default` satellite, carried by **CSP v1** instead of CSP v2. One line at the top of
`setup.toml` selects it:

```toml
protocol_version = 1
```

Everything else is the same three nodes, the same low-battery rule and the same kind of
assertion, which is the point: a team on CSP v1 hardware changes one line, not its scenarios.

```sh
cubesat-testbed run --config setup.toml --scenario scenario.yaml
```

```text
PASS t=3000000 assert_3: eps.telemetry.battery_percent == 25.0; actual=25.0
PASS t=4000000 assert_4: payload.telemetry.power_status == 'offline'; actual='offline'
SUMMARY scenario='EPS Low Battery Protection Test (CSP v1)' assertions=2 passed=2 failed=0 started_at=0 finished_at=4000000
```

Add `--trace` to see what CSP v1 puts on the bus. The EPS's 4-byte float needs two CAN frames,
because a CSP v1 packet is always framed by CFP 1.x (the first frame holds the header, a length
and two payload bytes at most), and each frame says how many are still to come:

```text
trace t=0 TX can_id=0x02080400 dlc=8 cfp=begin rem=1 id=0 pri=2 src=2 dst=1 dport=20 sport=20 flags=0x00 data=84151400000441c8
trace t=0 TX can_id=0x020C0000 dlc=2 cfp=end rem=0 id=0 dst=1 sender=2 data=0000 frames=2 payload=41c80000 telemetry eps.telemetry.battery_percent=25.0
```

What to know when you move a setup from CSP v2:

- addresses are 5 bits; the highest node address is **30** (31 is the broadcast address);
- `to = "obc"` on a telemetry mapping addresses the frame to the OBC, as a real OBC needs;
- the same `protocol_version = 1` works over `socketcan`, `kiss` and `zmq` transports; only the
  `[transport]` block changes (see `docs/v1-scope.md`, section *CSP v1*).
