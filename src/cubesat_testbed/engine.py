"""Deterministic discrete-event simulation dispatcher.

The engine owns virtual time and dispatches scheduled events between modules,
transports, the scenario runner, the OBC Peer rule engine, and the fault
injection layer. Runtime model updates are event-driven, not wall-clock sleep
loops.
"""
