"""
Simple Payload module: basic power-draw profile plus command/data-volume
behavior (e.g. "start capture" -> power draw rises, data buffer fills ->
"downlink ready" telemetry).

Chosen as the third v1 module (over thermal) because it exercises
cross-module interaction -- payload power draw affecting EPS state -- more
directly than an isolated model would.
"""
