# Example: `requests`

A simulated node that **answers**, not only beacons. Real flight software talks
request/response: an OBC pings a node and expects the echo, asks how long it has been up,
polls its housekeeping, and treats a node that stays silent as dead. Here the EPS answers all
of that, and the scenario is the OBC asking.

- `[nodes.eps.services]` switches on libcsp's standard services: `ping` (port 1, echoes the
  request), `uptime` (port 6, seconds since start) and `buf_free` (port 5, a configured
  number). Every libcsp node answers these; a simulated one does once they are listed here.
- `[nodes.eps.requests.get_housekeeping]` is a request of the EPS's own: `0x01` on port 8,
  answered with the `housekeeping` packet. That packet has `periodic = false`, so it is never
  beaconed, only sent as a reply; `generic_eps` has no request code at all, so the reply is
  built from its current telemetry values (a module can define `answer_request` to decide
  itself, see `docs/writing-a-module.md`).
- A `request` step sends from a simulated node (`source`) to any node (`target`), waits up to
  `timeout` for the reply on the bus, and reports one result for the reply plus one per
  `expect` entry, each checking a decoded reply field.

```sh
cubesat-testbed run --config setup.toml --scenario scenario.yaml
```

```text
PASS t=3000000 request_2: eps.services.ping: eps echoed 00010203 after 0.000000 s
PASS t=3000000 request_3: eps.services.uptime: eps answered uptime_s=3 after 0.000000 s
PASS t=3000000 request_3.uptime_s: eps.services.uptime.uptime_s == 3; actual=3
PASS t=3000000 request_4: eps.requests.get_housekeeping: eps answered after 0.000000 s: battery_voltage_v=7.920050144195557 battery_percent=80.00208282470703 cell_voltages_v=(3.9600250720977783, 3.9600250720977783) power_mode='nominal'
PASS t=3000000 request_4.power_mode: eps.requests.get_housekeeping.power_mode == 'nominal'; actual='nominal'
PASS t=3000000 request_4.cell_voltages_v[1]: eps.requests.get_housekeeping.cell_voltages_v[1] > 3.5; actual=3.9600250720977783
PASS t=3000000 request_6: eps.requests.get_housekeeping: eps answered after 0.000000 s: battery_voltage_v=6.0 battery_percent=50.0 cell_voltages_v=(3.5999999046325684, 2.4000000953674316) power_mode='nominal'
PASS t=3000000 request_6.cell_voltages_v[1]: eps.requests.get_housekeeping.cell_voltages_v[1] < 3.0; actual=2.4000000953674316
SUMMARY scenario='EPS Requests' assertions=8 passed=8 failed=0 started_at=0 finished_at=3000000
```

Add `--trace` to see each request and its reply on the bus (`request ping to eps`,
`reply ping from eps`, `reply eps.requests.get_housekeeping battery_voltage_v=...`). The same
steps work against a real board: make `eps` a `hardware` node on SocketCAN and the testbed
sends the requests to the board and reads its replies.
