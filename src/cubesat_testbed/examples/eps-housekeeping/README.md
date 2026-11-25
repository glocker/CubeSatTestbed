# Example: `eps-housekeeping`

The `default` satellite, but the EPS sends its telemetry the way a real board does: **one
housekeeping packet carrying seven signals**, not one packet per signal. The packet is
declared once under `[nodes.eps.packets.housekeeping]`, with the ports and the destination,
and each field gets its place in the 25-byte payload:

| bytes | field | encoding |
| --- | --- | --- |
| 0-3 | `battery_voltage_v` | float32 |
| 4-7 | `battery_percent` | float32 |
| 8-11 | `load_power_w` | float32 |
| 12-15 | `solar_input_w` | float32 |
| 16-23 | `cell_voltages_v` | array of 2 float32 (`count = 2`) |
| 24, bits 0-1 | `power_mode` | 2-bit field with an enum |
| 24, bit 2 | `payload_rail_enabled` | 1-bit field |

Bits count from the most significant bit of the byte, as in Kaitai Struct and CCSDS. An
element of the array is named `eps.telemetry.cell_voltages_v[1]`, in assertions and in OBC
rules alike; here the OBC sheds the payload when the second cell stays under 3 V for two
virtual seconds, and the scenario kills that cell with the EPS's `battery_cell_dead` fault.

```sh
cubesat-testbed run --config setup.toml --scenario scenario.yaml
```

```text
PASS t=2000000 assert_2: eps.telemetry.power_mode == 'nominal'; actual='nominal'
PASS t=2000000 assert_3: eps.telemetry.payload_rail_enabled == 1; actual=1
PASS t=2000000 assert_4: eps.telemetry.cell_voltages_v[1] > 3.5; actual=3.960010051727295
PASS t=2000000 assert_6: eps.telemetry.cell_voltages_v[1] < 3.0; actual=2.4000000953674316
PASS t=5000000 assert_7: payload.telemetry.power_status == 'offline'; actual='offline'
SUMMARY scenario='EPS Housekeeping Packet' assertions=5 passed=5 failed=0 started_at=0 finished_at=5000000
```

Add `--trace` to see the packet: four CFP2 frames on the bus, and one line naming every field
decoded from the reassembled payload:

```text
trace t=1000000 TX can_id=0x1000210D dlc=5 cfp=end pc=0 fc=3 dst=1 sender=2 data=407d70b920 frames=4 payload=40fd70b942a000373f99999a3fc00000407d70b9407d70b920 telemetry eps.packets.housekeeping battery_voltage_v=7.920010089874268 battery_percent=80.00041961669922 load_power_w=1.2000000476837158 solar_input_w=1.5 cell_voltages_v=(3.960005044937134, 3.960005044937134) power_mode='nominal' payload_rail_enabled=1
```

The single-signal form, `[nodes.<node>.telemetry.<name>]`, still works and is used here for
the payload's `power_status`: it is shorthand for a packet holding that one signal.
