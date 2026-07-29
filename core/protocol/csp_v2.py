"""
CSP v2 (48-bit header) protocol adapter -- the only protocol implemented in
v1.

Fields: priority, 14-bit source, 14-bit destination, destination port,
source port, flags. Matches the libcsp 2.0+ default (current upstream
default since 2024).

Planned:
- pack(fields) -> bytes / unpack(raw) -> dict
- validated against a real libcsp 2.x build over vcan0, not just unit tests
  against ourselves
"""
