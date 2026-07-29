"""SocketCAN bus adapter for Linux HIL and Docker setups.

This adapter will use ``python-can`` to communicate with ``vcan0`` or a physical
CAN interface. It is separate from the in-memory adapter so CI can run without
Linux CAN support.
"""
