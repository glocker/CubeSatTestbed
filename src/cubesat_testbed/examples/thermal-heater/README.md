# Example: `thermal-heater`

A thermal control loop that closes over the CAN bus, and the worked example
behind [`docs/writing-a-module.md`](https://github.com/glocker/CubeSatTestbed/blob/main/docs/writing-a-module.md).

`thermal_rc` is a lumped-capacitance node: one temperature, one first-order
ODE, one commandable heater. It is the smallest module in the package that is
still worth running, and it is registered through exactly the same public
`register_module` call a module you write yourself would use — there is no
privileged path for the built-ins.

The point of the example is what is *not* in it: the thermal module has no
reference to the OBC, and the OBC has no knowledge of the thermal model's
internals. Everything between them is configuration.

1. The node cools, because it loses more heat to its environment than it
   generates. Its temperature is published as real CSP frames.
2. The OBC's `heater_on_when_cold` rule reads that decoded telemetry —
   the same bytes any bus listener would see — and answers with the configured
   `thermal_heater_on` command.
3. That command crosses the same bus back into the module, which turns its
   heater on and warms up until `heater_off_when_warm` switches it off again.

```sh
cubesat-testbed run --config setup.toml --scenario scenario.yaml
```

```text
PASS t=4000000 node_cools_toward_ambient: thermal.telemetry.temperature_c < 0.0; actual=-3.615999937057495
PASS t=5000000 obc_commanded_the_heater_on: thermal.telemetry.heater_status == 'on'; actual='on'
PASS t=9000000 heater_loop_recovers_the_node: thermal.telemetry.temperature_c > 5.0; actual=5.538309097290039
PASS t=19000000 stuck_heater_defeats_the_loop: thermal.telemetry.temperature_c < -5.0; actual=-17.257844924926758
```

Add `--trace` to watch the loop close, frame by frame:

```text
trace t=4000000 TX ... dport=22 sport=22 ... telemetry thermal.telemetry.temperature_c=-3.615999937057495
trace t=4000000 TX ... dport=11 sport=11 ... payload=01 command obc.thermal_heater_on->thermal
trace t=5000000 TX ... dport=23 sport=23 ... payload=01 telemetry thermal.telemetry.heater_status='on'
```

The last assertion injects the module's own `heater_stuck_off` named fault.
The rule still fires and the command still reaches the module every tick — the
hardware simply stops responding, and the temperature falls anyway. That
separation is deliberate: the Fault Injection Engine is passive and only
applies what it is asked to, while every threshold decision belongs to the OBC
rule engine.

Things worth trying:

- Raise `thermal_capacity_j_per_k` in `[nodes.thermal.params]`: a heavier node
  has a longer time constant and recovers more slowly, so
  `heater_loop_recovers_the_node` starts failing on its timeout.
- Set `tick_seconds` above `thermal_capacity_j_per_k / conductance_w_per_k` to
  watch setup validation reject an integration step that would make the model
  oscillate instead of settle.
- Swap the fault for `radiator_degraded`, which cuts the node's conductance to
  its environment: the same heater then overheats it past the upper threshold.
