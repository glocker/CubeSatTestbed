# Example configs

Current v1 examples live at:

- `configs/default_satellite.toml` — static in-memory testbed setup;
- `configs/examples/socketcan_hil.toml` — SocketCAN/HIL setup using `vcan0` by default;
- `configs/scenarios/low_battery.yaml` — scenario DSL example.

Additional examples should keep the same split: TOML for infrastructure and YAML
for ordered scenario actions.
