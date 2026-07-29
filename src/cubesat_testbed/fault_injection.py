"""Passive fault injection executor.

The fault engine applies explicit external requests only:

- ``state_override`` for internal model paths such as ``eps.model.temperature``;
- ``signal_override`` for outgoing telemetry paths such as
  ``eps.telemetry.voltage``;
- named module fault flags such as ``battery_cell_dead``.

Threshold evaluation belongs to the OBC Peer rule engine, not here.
"""
