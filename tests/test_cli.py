from __future__ import annotations

from pathlib import Path

import pytest

from cubesat_testbed.main import main

DEFAULT_CONFIG = "configs/default_satellite.toml"
LOW_BATTERY_SCENARIO = "configs/scenarios/low_battery.yaml"


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
        "PASS t=3 assert_3: payload.telemetry.power_status == 'offline'; actual='offline'\n"
        in captured.out
    )
    assert (
        "SUMMARY scenario='EPS Low Battery Protection Test' "
        "assertions=1 passed=1 failed=0 started_at=0 finished_at=3\n" in captured.out
    )


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
    timeout: 2
""".lstrip(),
        encoding="utf-8",
    )

    exit_code = main(["run", "-c", DEFAULT_CONFIG, "-s", str(scenario_path)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith(
        "FAIL t=2 critical_battery: eps.telemetry.battery_percent < 10; actual="
    )
    assert (
        "SUMMARY scenario='Battery should not be critical' "
        "assertions=1 passed=0 failed=1 started_at=0 finished_at=2\n" in captured.out
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
