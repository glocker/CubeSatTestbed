from __future__ import annotations

from xml.etree import ElementTree

from cubesat_testbed.config import AssertionOperator
from cubesat_testbed.scenario import ScenarioRunResult, format_junit_error_xml, format_junit_xml
from cubesat_testbed.scenario.assertions import AssertionResult


def _assertion(*, name: str, passed: bool) -> AssertionResult:
    return AssertionResult(
        name=name,
        signal="eps.telemetry.battery_percent",
        operator=AssertionOperator.LT,
        expected=10,
        actual=25.0,
        passed=passed,
        evaluated_at=3_000_000,
        detail="eps.telemetry.battery_percent < 10; actual=25.0",
    )


def test_format_junit_xml_emits_one_passing_testcase_with_no_failure_element() -> None:
    result = ScenarioRunResult(
        scenario_name="Smoke test",
        started_at=0,
        finished_at=3_000_000,
        assertions=(_assertion(name="assert_1", passed=True),),
    )

    xml = format_junit_xml(result, wall_time_seconds=1.5)

    root = ElementTree.fromstring(xml)
    testsuite = root.find("testsuite")
    assert testsuite is not None
    assert testsuite.get("name") == "Smoke test"
    assert testsuite.get("tests") == "1"
    assert testsuite.get("failures") == "0"
    assert testsuite.get("time") == "1.500000"
    testcase = testsuite.find("testcase")
    assert testcase is not None
    assert testcase.get("name") == "assert_1"
    assert testcase.find("failure") is None


def test_format_junit_xml_emits_a_failure_element_with_the_assertion_detail() -> None:
    result = ScenarioRunResult(
        scenario_name="Smoke test",
        started_at=0,
        finished_at=3_000_000,
        assertions=(_assertion(name="assert_1", passed=False),),
    )

    xml = format_junit_xml(result)

    root = ElementTree.fromstring(xml)
    testsuite = root.find("testsuite")
    assert testsuite is not None
    assert testsuite.get("failures") == "1"
    failure = testsuite.find("testcase/failure")
    assert failure is not None
    assert failure.get("message") == "eps.telemetry.battery_percent < 10; actual=25.0"
    assert failure.text == "eps.telemetry.battery_percent < 10; actual=25.0"


def test_format_junit_xml_counts_only_failed_assertions_as_failures() -> None:
    result = ScenarioRunResult(
        scenario_name="Mixed",
        started_at=0,
        finished_at=1,
        assertions=(
            _assertion(name="pass_1", passed=True),
            _assertion(name="fail_1", passed=False),
            _assertion(name="pass_2", passed=True),
        ),
    )

    xml = format_junit_xml(result)

    testsuite = ElementTree.fromstring(xml).find("testsuite")
    assert testsuite is not None
    assert testsuite.get("tests") == "3"
    assert testsuite.get("failures") == "1"


def test_format_junit_xml_escapes_special_characters_in_detail() -> None:
    result = ScenarioRunResult(
        scenario_name="Escaping",
        started_at=0,
        finished_at=0,
        assertions=(
            AssertionResult(
                name="assert_1",
                signal="eps.telemetry.battery_percent",
                operator=AssertionOperator.GT,
                expected=1000000,
                actual=80.0,
                passed=False,
                evaluated_at=0,
                detail="eps.telemetry.battery_percent > 1000000; actual=80.0 <weird&chars>",
            ),
        ),
    )

    xml = format_junit_xml(result)

    assert "&gt;" in xml
    assert "&lt;" in xml
    assert "&amp;" in xml
    # And it still parses back to the original text despite the escaping.
    failure = ElementTree.fromstring(xml).find("testsuite/testcase/failure")
    assert failure is not None
    assert failure.text == "eps.telemetry.battery_percent > 1000000; actual=80.0 <weird&chars>"


def test_format_junit_error_xml_emits_one_error_testcase() -> None:
    xml = format_junit_error_xml(
        "config.toml not found", kind="execution_error", wall_time_seconds=0.25
    )

    root = ElementTree.fromstring(xml)
    testsuite = root.find("testsuite")
    assert testsuite is not None
    assert testsuite.get("errors") == "1"
    assert testsuite.get("tests") == "1"
    assert testsuite.get("time") == "0.250000"
    error = testsuite.find("testcase/error")
    assert error is not None
    assert error.get("message") == "config.toml not found"
