from __future__ import annotations

import json
from pathlib import Path

import pytest

from cubesat_testbed.main import main

DEFAULT_CONFIG = "configs/default_satellite.toml"
LOW_BATTERY_SCENARIO = "configs/scenarios/low_battery.yaml"


def _raise_keyboard_interrupt(*_args: object, **_kwargs: object) -> object:
    raise KeyboardInterrupt


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
        "PASS t=3000000 assert_3: payload.telemetry.power_status == 'offline'; actual='offline'\n"
        in captured.out
    )
    assert (
        "SUMMARY scenario='EPS Low Battery Protection Test' "
        "assertions=1 passed=1 failed=0 started_at=0 finished_at=3000000\n" in captured.out
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
    assert payload["finished_at"] == 3_000_000
    assert payload["assertions"] == {
        "failed": 0,
        "passed": 1,
        "results": [
            {
                "actual": "offline",
                "detail": "payload.telemetry.power_status == 'offline'; actual='offline'",
                "evaluated_at": 3_000_000,
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
