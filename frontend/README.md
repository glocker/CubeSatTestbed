# Frontend (not started yet)

Planned, after the core engine and scenario runner work end-to-end via the
console:
- **Node/DUT panel** -- see all nodes, pick each one's mode
  (simulated/software/hardware), edit its config
- **Fault injection panel** -- trigger built-in faults per simulated module
- **Scenario runner** -- pick and run a scenario, see live PASS/FAIL results
- **Live telemetry / frame view** -- current signal values and raw
  frames per node

Talks to the future backend API layer under `src/cubesat_testbed` over REST + WebSocket.
