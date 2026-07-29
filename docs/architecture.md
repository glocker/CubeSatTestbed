# Architecture

Layers:

1. **DUT / nodes** (`core/dut/`) -- each node has a mode (simulated/
   software/hardware). This is the mechanism that lets any subsystem be the
   Device Under Test while everything else runs as a software peer.

2. **Protocol & transport adapters** (`core/protocol/`, `core/transport/`)
   -- pluggable encode/decode and send/receive, so the rest of the system
   never hardcodes "CSP" or "CAN" as assumptions. v1 ships one adapter of
   each (CSP v2, SocketCAN); more are planned (see README).

3. **Modules** (`modules/`) -- each subsystem type (EPS, OBC Peer, Payload,
   ...) implements the shared interface in `modules/base.py` and is looked
   up via `modules/registry.py`. Adding a subsystem type means adding a
   module, not touching the core.

4. **Scenario engine** (`core/scenario/`) -- runs YAML-defined test
   scenarios against whatever nodes are currently configured, using
   assertions and producing a PASS/FAIL report. This is what makes the
   project a test framework rather than just an emulator.

The engine and the adapters/scenario runner are universal; the modules are
not -- see the README's "Core ideas" section for why.

It's better to have an Event-Driven architecture (event-driven), rather than Time-Driven. The model should not move according to the sleep(0.1) timer, but according to the internal ticks of the virtual core of the simulator (Discrete Event Simulation)


