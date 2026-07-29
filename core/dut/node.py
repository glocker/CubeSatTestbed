"""
Defines a "node" in a test setup: a named subsystem slot (obc, eps, adcs,
payload, ...) with a mode:
- simulated -- runs a module plugin (see modules/) as a software peer
- hardware  -- the real board, reached over a real transport/protocol adapter
- software  -- a non-hardware reference implementation running as its own
               process (e.g. someone else's flight software binary), still
               going over the real protocol/transport stack

Switching a node's mode is a config change (see configs/schema), not a code
change -- this is the actual "DUT" mechanism the whole project is built
around.
"""
