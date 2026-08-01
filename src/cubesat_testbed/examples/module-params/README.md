# Example: `module-params`

No single "generic EPS" can match a real board's battery pack and thresholds,
so a built-in module's own configuration fields are overridable from the setup
file through `[nodes.<node>.params]` — no Python subclass required. An unknown
parameter name or an out-of-range value fails setup validation the same way
constructing that module directly from Python would.

Here the EPS starts at 90% on a 40 Wh pack instead of the built-in 80% on
20 Wh. The scenario asserts the battery read *off the bus* is above 85%, which
the stock model could never produce — that is what proves the override landed
on the model rather than being quietly ignored.

```sh
cubesat-testbed run --config setup.toml --scenario scenario.yaml
```

```text
PASS t=2000000 params_reached_the_model: eps.telemetry.battery_percent > 85.0; actual=90.00041961669922
```

Try setting `low_power_threshold_percent` above `recovery_threshold_percent`
to see the validation reject an inconsistent pair before the run starts.

Every tunable parameter is listed in
[`configs/schema/module_schema.md`](https://github.com/glocker/CubeSatTestbed/blob/main/configs/schema/module_schema.md#module-parameters).
