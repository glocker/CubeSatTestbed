from __future__ import annotations

import json
import time
from pathlib import Path
from xml.etree import ElementTree

import pytest

from cubesat_testbed.clock import RealTimeClock
from cubesat_testbed.main import main
from cubesat_testbed.scenario import ScenarioRunResult
from cubesat_testbed.scenario.assertions import AssertionOperator, AssertionResult

DEFAULT_CONFIG = "configs/default_satellite.toml"
LOW_BATTERY_SCENARIO = "configs/scenarios/low_battery.yaml"
SOCKETCAN_CONFIG = "configs/examples/socketcan_hil.toml"


def _raise_keyboard_interrupt(*_args: object, **_kwargs: object) -> object:
    raise KeyboardInterrupt


def _passing_result() -> ScenarioRunResult:
    """A minimal passing run result, for stubbing out an actual scenario run."""

    return ScenarioRunResult(
        scenario_name="stubbed",
        started_at=0,
        finished_at=0,
        assertions=(
            AssertionResult(
                name="assert_1",
                signal="eps.telemetry.battery_percent",
                operator=AssertionOperator.EQ,
                expected=100.0,
                actual=100.0,
                passed=True,
                evaluated_at=0,
                detail="",
            ),
        ),
    )


def _record_run_kwargs(recorded: dict[str, object]) -> object:
    """Stub `run_scenario_files`, capturing the keyword arguments it was given.

    Used by the flag-plumbing tests so they assert on what the CLI hands the
    runner, without paying an actual paced run's wall-clock time.
    """

    def _run(*_args: object, **kwargs: object) -> ScenarioRunResult:
        recorded.update(kwargs)
        return _passing_result()

    return _run


def test_cli_help_includes_run_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "run" in captured.out


def test_cli_run_returns_zero_for_passing_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["run", "-c", DEFAULT_CONFIG, "-s", LOW_BATTERY_SCENARIO])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert (
        "PASS t=4000000 assert_3: payload.telemetry.power_status == 'offline'; actual='offline'\n"
        in captured.out
    )
    assert (
        "SUMMARY scenario='EPS Low Battery Protection Test' "
        "assertions=1 passed=1 failed=0 started_at=0 finished_at=4000000\n" in captured.out
    )


def test_cli_run_quiet_suppresses_stdout_for_passing_scenario(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["run", "--quiet", "-c", DEFAULT_CONFIG, "-s", LOW_BATTERY_SCENARIO])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_cli_run_json_outputs_result_and_overrides_quiet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["run", "--quiet", "--json", "-c", DEFAULT_CONFIG, "-s", LOW_BATTERY_SCENARIO])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["scenario"] == "EPS Low Battery Protection Test"
    assert payload["passed"] is True
    assert payload["exit_code"] == 0
    assert payload["started_at"] == 0
    assert payload["finished_at"] == 4_000_000
    assert payload["assertions"] == {
        "failed": 0,
        "passed": 1,
        "results": [
            {
                "actual": "offline",
                "detail": "payload.telemetry.power_status == 'offline'; actual='offline'",
                "evaluated_at": 4_000_000,
                "expected": "offline",
                "name": "assert_3",
                "operator": "==",
                "passed": True,
                "signal": "payload.telemetry.power_status",
            }
        ],
        "total": 1,
    }


def test_cli_run_rules_flag_overrides_the_setup_s_inline_rule(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rules_path = tmp_path / "rules.toml"
    rules_path.write_text(
        """
        [obc.never_fires]
        signal = "eps.telemetry.battery_percent"
        op = "<"
        threshold = 1.0

        [[obc.never_fires.actions]]
        type = "send_command"
        command = "payload_power_off"
        """,
        encoding="utf-8",
    )

    exit_code = main(
        ["run", "-c", DEFAULT_CONFIG, "-s", LOW_BATTERY_SCENARIO, "--rules", str(rules_path)]
    )

    # The override rule's threshold (1.0) never matches, so the setup's own
    # inline low_battery_shed_payload rule must not have run: payload stays on.
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "actual='online'" in captured.out


def test_cli_run_returns_one_for_failing_assertion(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario_path = tmp_path / "failing_assertion.yaml"
    scenario_path.write_text(
        """
name: Battery should not be critical
steps:
  - action: assert
    name: critical_battery
    signal: eps.telemetry.battery_percent
    op: <
    value: 10
    timeout: "2s"
""".lstrip(),
        encoding="utf-8",
    )

    exit_code = main(["run", "-c", DEFAULT_CONFIG, "-s", str(scenario_path)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith(
        "FAIL t=2000000 critical_battery: eps.telemetry.battery_percent < 10; actual="
    )
    assert (
        "SUMMARY scenario='Battery should not be critical' "
        "assertions=1 passed=0 failed=1 started_at=0 finished_at=2000000\n" in captured.out
    )


def test_cli_run_returns_two_for_missing_config_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_config_path = tmp_path / "missing.toml"

    exit_code = main(["run", "-c", str(missing_config_path), "-s", LOW_BATTERY_SCENARIO])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    assert "missing.toml" in captured.err
    assert "Traceback" not in captured.err


def test_cli_run_returns_two_for_missing_scenario_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_scenario_path = tmp_path / "missing.yaml"

    exit_code = main(["run", "-c", DEFAULT_CONFIG, "-s", str(missing_scenario_path)])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    assert "missing.yaml" in captured.err
    assert "Traceback" not in captured.err


def test_cli_run_json_returns_two_for_missing_config_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_config_path = tmp_path / "missing.toml"

    exit_code = main(["run", "--json", "-c", str(missing_config_path), "-s", LOW_BATTERY_SCENARIO])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Traceback" not in captured.out
    payload = json.loads(captured.out)
    assert payload["passed"] is False
    assert payload["exit_code"] == 2
    assert payload["error"]["kind"] == "execution_error"
    assert "missing.toml" in payload["error"]["message"]


def test_cli_run_warns_when_scenario_has_zero_assertions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario_path = tmp_path / "no_assertions.yaml"
    scenario_path.write_text(
        """
name: No assertions smoke
steps:
  - action: wait
    virtual_time: 0
""".lstrip(),
        encoding="utf-8",
    )

    exit_code = main(["run", "-c", DEFAULT_CONFIG, "-s", str(scenario_path)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == "warning: 0 assertions in scenario\n"
    assert (
        "SUMMARY scenario='No assertions smoke' "
        "assertions=0 passed=0 failed=0 started_at=0 finished_at=0\n" in captured.out
    )


def test_cli_run_returns_130_for_keyboard_interrupt_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("cubesat_testbed.main.run_scenario_files", _raise_keyboard_interrupt)

    exit_code = main(["run", "-c", DEFAULT_CONFIG, "-s", LOW_BATTERY_SCENARIO])

    assert exit_code == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "interrupted\n"
    assert "Traceback" not in captured.err


def test_cli_run_json_returns_130_for_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("cubesat_testbed.main.run_scenario_files", _raise_keyboard_interrupt)

    exit_code = main(["run", "--json", "--quiet", "-c", DEFAULT_CONFIG, "-s", LOW_BATTERY_SCENARIO])

    assert exit_code == 130
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload == {
        "error": {"kind": "interrupted", "message": "interrupted"},
        "exit_code": 130,
        "passed": False,
    }


def test_cli_run_junit_xml_reports_one_testcase_per_assertion(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report_path = tmp_path / "report.xml"

    exit_code = main(
        ["run", "-c", DEFAULT_CONFIG, "-s", LOW_BATTERY_SCENARIO, "--junit-xml", str(report_path)]
    )

    assert exit_code == 0
    root = ElementTree.fromstring(report_path.read_text(encoding="utf-8"))
    testsuite = root.find("testsuite")
    assert testsuite is not None
    assert testsuite.get("name") == "EPS Low Battery Protection Test"
    assert testsuite.get("tests") == "1"
    assert testsuite.get("failures") == "0"
    testcase = testsuite.find("testcase")
    assert testcase is not None
    assert testcase.get("name") == "assert_3"
    assert testcase.find("failure") is None


def test_cli_run_junit_xml_reports_a_failure_element_for_a_failed_assertion(
    tmp_path: Path,
) -> None:
    scenario_path = tmp_path / "failing_assertion.yaml"
    scenario_path.write_text(
        """
name: Battery should not be critical
steps:
  - action: assert
    name: critical_battery
    signal: eps.telemetry.battery_percent
    op: <
    value: 10
    timeout: "2s"
""".lstrip(),
        encoding="utf-8",
    )
    report_path = tmp_path / "report.xml"

    exit_code = main(
        ["run", "-c", DEFAULT_CONFIG, "-s", str(scenario_path), "--junit-xml", str(report_path)]
    )

    assert exit_code == 1
    root = ElementTree.fromstring(report_path.read_text(encoding="utf-8"))
    testsuite = root.find("testsuite")
    assert testsuite is not None
    assert testsuite.get("failures") == "1"
    testcase = testsuite.find("testcase")
    assert testcase is not None
    failure = testcase.find("failure")
    assert failure is not None
    assert "eps.telemetry.battery_percent < 10" in (failure.get("message") or "")


def test_cli_run_junit_xml_reports_an_error_element_for_missing_config(tmp_path: Path) -> None:
    missing_config_path = tmp_path / "missing.toml"
    report_path = tmp_path / "report.xml"

    exit_code = main(
        [
            "run",
            "-c",
            str(missing_config_path),
            "-s",
            LOW_BATTERY_SCENARIO,
            "--junit-xml",
            str(report_path),
        ]
    )

    assert exit_code == 2
    root = ElementTree.fromstring(report_path.read_text(encoding="utf-8"))
    testsuite = root.find("testsuite")
    assert testsuite is not None
    assert testsuite.get("errors") == "1"
    testcase = testsuite.find("testcase")
    assert testcase is not None
    error = testcase.find("error")
    assert error is not None
    assert "missing.toml" in (error.get("message") or "")


def test_cli_run_junit_xml_reports_interrupted_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("cubesat_testbed.main.run_scenario_files", _raise_keyboard_interrupt)
    report_path = tmp_path / "report.xml"

    exit_code = main(
        [
            "run",
            "-c",
            DEFAULT_CONFIG,
            "-s",
            LOW_BATTERY_SCENARIO,
            "--junit-xml",
            str(report_path),
        ]
    )

    assert exit_code == 130
    root = ElementTree.fromstring(report_path.read_text(encoding="utf-8"))
    testsuite = root.find("testsuite")
    assert testsuite is not None
    assert testsuite.get("errors") == "1"


def test_cli_run_realtime_flag_passes_a_real_time_clock_to_the_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}
    monkeypatch.setattr("cubesat_testbed.main.run_scenario_files", _record_run_kwargs(recorded))

    exit_code = main(["run", "--realtime", "-c", DEFAULT_CONFIG, "-s", LOW_BATTERY_SCENARIO])

    assert exit_code == 0
    assert isinstance(recorded["clock"], RealTimeClock)


def test_cli_run_without_realtime_leaves_the_runner_on_the_virtual_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}
    monkeypatch.setattr("cubesat_testbed.main.run_scenario_files", _record_run_kwargs(recorded))

    exit_code = main(["run", "-c", DEFAULT_CONFIG, "-s", LOW_BATTERY_SCENARIO])

    assert exit_code == 0
    # None means "runner's own default", which is the VirtualClock.
    assert recorded["clock"] is None


def test_cli_run_realtime_actually_paces_virtual_time_against_the_wall_clock(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No mocks: a 200ms virtual wait takes about 200ms of real time.

    Run against the in-memory transport, so this proves the flag reaches the
    pacing path itself without needing a CAN interface.
    """

    scenario_path = tmp_path / "paced.yaml"
    scenario_path.write_text(
        """
name: Realtime pacing smoke
steps:
  - action: wait
    virtual_time: 200ms
""".lstrip(),
        encoding="utf-8",
    )

    started = time.perf_counter()
    exit_code = main(["run", "--realtime", "-c", DEFAULT_CONFIG, "-s", str(scenario_path)])
    elapsed = time.perf_counter() - started

    assert exit_code == 0
    assert elapsed >= 0.15
    captured = capsys.readouterr()
    assert captured.err == "warning: 0 assertions in scenario\n"


def test_cli_run_warns_for_socketcan_transport_without_realtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorded: dict[str, object] = {}
    monkeypatch.setattr("cubesat_testbed.main.run_scenario_files", _record_run_kwargs(recorded))

    exit_code = main(["run", "-c", SOCKETCAN_CONFIG, "-s", LOW_BATTERY_SCENARIO])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == (
        "warning: transport.type='socketcan' (interface 'vcan0') without --realtime; "
        "virtual time is not paced against wall-clock time, so a real peer gets no "
        "time to respond\n"
    )


def test_cli_run_socketcan_warning_stays_on_stderr_in_json_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorded: dict[str, object] = {}
    monkeypatch.setattr("cubesat_testbed.main.run_scenario_files", _record_run_kwargs(recorded))

    exit_code = main(["run", "--json", "-c", SOCKETCAN_CONFIG, "-s", LOW_BATTERY_SCENARIO])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "without --realtime" in captured.err
    assert json.loads(captured.out)["passed"] is True


def test_cli_run_does_not_warn_for_socketcan_transport_with_realtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorded: dict[str, object] = {}
    monkeypatch.setattr("cubesat_testbed.main.run_scenario_files", _record_run_kwargs(recorded))

    exit_code = main(["run", "--realtime", "-c", SOCKETCAN_CONFIG, "-s", LOW_BATTERY_SCENARIO])

    assert exit_code == 0
    assert isinstance(recorded["clock"], RealTimeClock)
    assert capsys.readouterr().err == ""
