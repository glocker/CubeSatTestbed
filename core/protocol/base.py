"""
Protocol adapter interface: encode(logical_message) -> bytes,
decode(bytes) -> logical_message.

Every protocol adapter (csp_v2 now; csp_v1, raw_can, custom later)
implements this same interface, so the core engine and modules never need to
know which protocol a given node actually speaks on the wire.
"""
