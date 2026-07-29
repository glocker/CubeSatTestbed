"""
Loads and validates a test setup config (JSON/YAML) against the shared
schema (see schema.py): which nodes exist, each node's mode, which
protocol/transport it uses, and which module to load if simulated. Fails
loudly on invalid combinations instead of silently misbehaving on the bus.
"""
