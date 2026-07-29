"""Transport adapter boundary.

v1 adapters carry CSP packets/frames through either the deterministic in-memory
bus or Linux SocketCAN. Higher layers should depend on this boundary rather than
on a concrete transport implementation.
"""
