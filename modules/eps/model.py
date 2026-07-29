"""
Generic EPS module: battery voltage/current/temperature over time (simple
discharge/charge curve, not a full electrochemical model), configurable
output channels with current limits, and protections (undervoltage,
overvoltage, overcurrent delay).

This is a reference implementation, not a claim to represent any specific
real EPS board -- different real EPS hardware has different channel counts,
protections and state machines, which is exactly why this exists as a
plugin rather than a hardcoded core feature.
"""
