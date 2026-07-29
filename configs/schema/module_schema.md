# Config schema (draft)

A test setup config defines, at minimum:
- **nodes**: for each node (obc, eps, payload, ...) -- `mode`
  (simulated/software/hardware), `protocol` (csp_v2 in v1), `transport`
  (socketcan), and, if simulated, which `module_type` to load
- **module config** (per simulated node): commands it accepts, telemetry it
  sends (name, bit offset, length, type, scale, offset), its state machine,
  and named fault hooks it supports
- **scenario** (separate YAML file): ordered steps -- send command, wait,
  inject fault, assert telemetry -- that the scenario engine executes

Formal schema will live in this directory once finalized -- the example
configs in `configs/examples/` will validate against it.
