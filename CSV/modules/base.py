"""
Abstract base for a subsystem module plugin. Every module type (EPS,
OBC Peer, Payload, ...) implements:
- load(config): validate and apply its config
- step(dt): advance its internal model by one tick, return telemetry to send
- handle_command(message): react to an incoming command
- apply_fault(fault_name, params): react to an injected fault

The core engine only ever talks to this interface -- it has no
subsystem-specific logic of its own.
"""
