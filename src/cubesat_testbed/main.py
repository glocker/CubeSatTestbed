"""Command-line interface for the CubeSat Testbed."""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from cubesat_testbed.scenario import ScenarioRunResult, run_scenario_files
from cubesat_testbed.scenario.assertions import AssertionResult

_EXIT_SUCCESS = 0
_EXIT_ASSERTION_FAILURE = 1
_EXIT_EXECUTION_ERROR = 2
_EXIT_INTERRUPTED = 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cubesat-testbed",
        description="Run CubeSat Testbed deterministic in-memory scenarios.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    run_parser = subcommands.add_parser(
        "run",
        help="run a scenario against a setup config",
        description="Run a YAML scenario against a TOML setup config using the v1 in-memory runtime.",
    )
    run_parser.add_argument(
        "-c",
        "--config",
        required=True,
        type=Path,
        metavar="PATH",
        help="path to the testbed setup TOML file",
    )
    run_parser.add_argument(
        "-s",
        "--scenario",
        required=True,
        type=Path,
        metavar="PATH",
        help="path to the scenario YAML file",
    )
    run_parser.add_argument(
        "--no-default-obc-rules",
        action="store_true",
        help="do not install built-in v1 OBC peer rules",
    )
    run_parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress normal stdout output; stderr warnings/errors are still emitted",
    )
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable JSON result to stdout; takes precedence over --quiet",
    )
    run_parser.set_defaults(handler=_run_command)

    return parser


def _run_command(args: argparse.Namespace) -> int:
    suppress_assertion_output = bool(args.quiet or args.json)
    output = io.StringIO() if suppress_assertion_output else None

    try:
        result = run_scenario_files(
            args.config,
            args.scenario,
            output=output,
            install_default_obc_rules=not args.no_default_obc_rules,
        )
    except Exception as exc:  # noqa: BLE001
        # CLI contract: execution errors map to 2; assertion failures map to 1 via result.passed.
        _print_error(
            str(exc),
            json_output=args.json,
            exit_code=_EXIT_EXECUTION_ERROR,
            kind="execution_error",
        )
        return _EXIT_EXECUTION_ERROR

    if not result.assertions:
        print("warning: 0 assertions in scenario", file=sys.stderr)

    exit_code = _exit_code_for_result(result)
    if args.json:
        _print_json(_result_payload(result, exit_code=exit_code))
    elif not args.quiet:
        _print_summary(result)
    return exit_code


def _exit_code_for_result(result: ScenarioRunResult) -> int:
    if result.passed:
        return _EXIT_SUCCESS
    return _EXIT_ASSERTION_FAILURE


def _print_summary(result: ScenarioRunResult) -> None:
    total = len(result.assertions)
    passed = sum(1 for assertion in result.assertions if assertion.passed)
    failed = total - passed
    print(
        f"SUMMARY scenario={result.scenario_name!r} "
        f"assertions={total} passed={passed} failed={failed} "
        f"started_at={result.started_at} finished_at={result.finished_at}"
    )


def _result_payload(result: ScenarioRunResult, *, exit_code: int) -> dict[str, object]:
    total = len(result.assertions)
    passed = sum(1 for assertion in result.assertions if assertion.passed)
    failed = total - passed
    return {
        "scenario": result.scenario_name,
        "passed": result.passed,
        "exit_code": exit_code,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "assertions": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "results": [_assertion_payload(assertion) for assertion in result.assertions],
        },
    }


def _assertion_payload(assertion: AssertionResult) -> dict[str, object]:
    return {
        "name": assertion.name,
        "signal": assertion.signal,
        "operator": assertion.operator.value,
        "expected": assertion.expected,
        "actual": assertion.actual,
        "passed": assertion.passed,
        "evaluated_at": assertion.evaluated_at,
        "detail": assertion.detail,
    }


def _error_payload(message: str, *, exit_code: int, kind: str) -> dict[str, object]:
    return {
        "passed": False,
        "exit_code": exit_code,
        "error": {
            "kind": kind,
            "message": message,
        },
    }


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, default=str, sort_keys=True))


def _print_error(message: str, *, json_output: bool, exit_code: int, kind: str) -> None:
    if json_output:
        _print_json(_error_payload(message, exit_code=exit_code, kind=kind))
    else:
        print(f"error: {message}", file=sys.stderr)


def _argv_requests_json(argv: Sequence[str] | None) -> bool:
    arguments = sys.argv[1:] if argv is None else argv
    return "--json" in arguments


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    json_output = _argv_requests_json(argv)
    try:
        args = parser.parse_args(argv)
        json_output = bool(getattr(args, "json", json_output))
        handler = cast("Callable[[argparse.Namespace], int]", args.handler)
        return handler(args)
    except KeyboardInterrupt:
        if json_output:
            _print_json(
                _error_payload(
                    "interrupted",
                    exit_code=_EXIT_INTERRUPTED,
                    kind="interrupted",
                )
            )
        else:
            print("interrupted", file=sys.stderr)
        return _EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
