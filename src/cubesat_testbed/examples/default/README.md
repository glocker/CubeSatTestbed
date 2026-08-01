# Example: `default`

A three-node CubeSat — OBC, EPS, payload — on the deterministic in-memory bus.
The EPS beacons `battery_percent` as a 4-byte float; the OBC carries one rule
that sends `payload_power_off` when the battery stays under 30% for 3 virtual
seconds; the scenario forces the battery to 25% and asserts that the payload
reports itself `offline`.

Nothing here reads module state directly: the asserted value is decoded from a
frame that actually crossed the bus.

```sh
cubesat-testbed run --config setup.toml --scenario scenario.yaml
```

```text
PASS t=4000000 assert_3: payload.telemetry.power_status == 'offline'; actual='offline'
SUMMARY scenario='EPS Low Battery Protection Test' assertions=1 passed=1 failed=0 started_at=0 finished_at=4000000
```

Add `--trace` to watch the frames themselves, `--json` or `--junit-xml PATH`
for CI. Drop the `inject_fault` step from `scenario.yaml` and the run fails
with exit code 1 — worth doing once, to see what a failure looks like.

Field-by-field syntax lives in
[`configs/schema/module_schema.md`](https://github.com/glocker/CubeSatTestbed/blob/main/configs/schema/module_schema.md).
