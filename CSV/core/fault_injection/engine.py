"""
Fault injection for simulated nodes (v1 scope): either a direct signal
override for N cycles, or a named fault flag (e.g. "undervoltage") that a
module's own model reacts to -- preferred, since it keeps the fault
physically plausible instead of just spoofing a number.

Real-device fault verification (driving a physical fault into a hardware DUT
via a programmable power supply / electronic load over SCPI) is a separate,
later concern -- not implemented here. See README "Fault injection" section.
"""
