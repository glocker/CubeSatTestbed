"""
Assertion primitives used inside a scenario: equals, within-range,
within_ms (a signal must reach an expected value within a timeout). Each
assertion produces a pass/fail result with enough context (expected vs.
actual, timing) to show up clearly in the report.
"""
