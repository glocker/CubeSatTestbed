"""Scenario assertion primitives.

Assertions compare decoded telemetry against expected values and produce
PASS/FAIL results with virtual timestamps and expected/actual context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cubesat_testbed.config import AssertionOperator
from cubesat_testbed.engine import VirtualTime


class ScenarioAssertionError(ValueError):
    """Raised when an assertion declaration cannot be evaluated."""


@dataclass(frozen=True, slots=True)
class AssertionResult:
    """Deterministic result of one telemetry assertion evaluation."""

    name: str
    signal: str
    operator: AssertionOperator
    expected: object
    actual: object
    passed: bool
    evaluated_at: VirtualTime
    detail: str


@dataclass(frozen=True, slots=True)
class TelemetryAssertion:
    """Declarative comparison over one decoded telemetry signal."""

    name: str
    signal: str
    operator: AssertionOperator | str
    expected: object
    timeout: VirtualTime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_non_empty_str("assertion name", self.name))
        object.__setattr__(self, "signal", _validate_non_empty_str("assertion signal", self.signal))
        object.__setattr__(self, "operator", _coerce_operator(self.operator))
        if self.timeout is not None:
            object.__setattr__(self, "timeout", _validate_virtual_time("timeout", self.timeout))

    def evaluate(self, actual: object, *, now: VirtualTime) -> AssertionResult:
        """Evaluate this assertion against the latest telemetry value."""

        evaluated_at = _validate_virtual_time("evaluation time", now)
        operator = _coerce_operator(self.operator)
        passed, detail = compare_values(
            actual=actual,
            operator=operator,
            expected=self.expected,
            signal=self.signal,
        )
        return AssertionResult(
            name=self.name,
            signal=self.signal,
            operator=operator,
            expected=self.expected,
            actual=actual,
            passed=passed,
            evaluated_at=evaluated_at,
            detail=detail,
        )


def compare_values(
    *,
    actual: object,
    operator: AssertionOperator | str,
    expected: object,
    signal: str,
) -> tuple[bool, str]:
    """Compare ``actual`` and ``expected`` using a scenario assertion operator."""

    op = _coerce_operator(operator)
    if op is AssertionOperator.EQ:
        passed = actual == expected
    elif op is AssertionOperator.NE:
        passed = actual != expected
    else:
        passed = _compare_ordered(actual=actual, operator=op, expected=expected)

    detail = f"{signal} {op.value} {expected!r}; actual={actual!r}"
    return passed, detail


def format_assertion_result(result: AssertionResult) -> str:
    """Format one assertion result as deterministic console output."""

    status = "PASS" if result.passed else "FAIL"
    return f"{status} t={result.evaluated_at} {result.name}: {result.detail}"


def _compare_ordered(
    *,
    actual: object,
    operator: AssertionOperator,
    expected: object,
) -> bool:
    actual_value: Any = actual
    expected_value: Any = expected
    try:
        if operator is AssertionOperator.GT:
            return bool(actual_value > expected_value)
        if operator is AssertionOperator.GTE:
            return bool(actual_value >= expected_value)
        if operator is AssertionOperator.LT:
            return bool(actual_value < expected_value)
        if operator is AssertionOperator.LTE:
            return bool(actual_value <= expected_value)
    except TypeError:
        return False

    raise ScenarioAssertionError(f"unsupported assertion operator {operator.value!r}")


def _coerce_operator(operator: AssertionOperator | str) -> AssertionOperator:
    if isinstance(operator, AssertionOperator):
        return operator
    try:
        return AssertionOperator(operator)
    except ValueError as exc:
        supported = ", ".join(op.value for op in AssertionOperator)
        raise ScenarioAssertionError(
            f"unsupported assertion operator {operator!r}; expected one of: {supported}"
        ) from exc


def _validate_virtual_time(name: str, value: VirtualTime) -> VirtualTime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioAssertionError(f"{name} must be an integer virtual-time tick")
    if value < 0:
        raise ScenarioAssertionError(f"{name} must be non-negative, got {value}")
    return int(value)


def _validate_non_empty_str(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise ScenarioAssertionError(f"{name} must be a string")
    if not value.strip():
        raise ScenarioAssertionError(f"{name} must not be empty")
    return value


__all__ = [
    "AssertionResult",
    "ScenarioAssertionError",
    "TelemetryAssertion",
    "compare_values",
    "format_assertion_result",
]
