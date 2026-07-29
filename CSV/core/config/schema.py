"""
Defines the config contract for:
- a node (name, mode, protocol adapter, transport adapter, module type if
  simulated)
- a module's own config (commands it accepts, telemetry it sends, its state
  machine, fault hooks it supports)

This is the "universal" part of the project: the engine only ever reads
this shared contract, never subsystem-specific fields directly.
"""
