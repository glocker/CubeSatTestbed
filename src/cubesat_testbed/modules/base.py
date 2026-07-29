"""Base contract for simulated CubeSat subsystem modules.

Modules are isolated finite state machines. They react to events, scheduled
virtual-time timers, commands, and fault requests. They must not run independent
wall-clock polling loops.
"""
