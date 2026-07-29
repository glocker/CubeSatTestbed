"""
OBC Peer module: not a physical model, a small rule engine:
"if <signal> <op> <threshold> from <module> then send <command>"

Used whenever a real EPS/ADCS/payload is the DUT and needs a plausible OBC
to talk to -- lets that subsystem's real hardware/firmware be verified
against realistic OBC reactions without needing real flight OBC software.

Rules are defined in this module's own config.
"""
