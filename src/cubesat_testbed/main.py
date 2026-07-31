"""Command-line interface for the CubeSat Testbed."""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from cubesat_testbed.clock import Clock, RealTimeClock
from cubesat_testbed.config import SocketCanTransportConfig, TestbedConfig, load_testbed_config
from cubesat_testbed.scenario import (
    ScenarioRunResult,
    build_obc_rules_from_file,
    format_junit_error_xml,
    format_junit_xml,
    run_scenario_files,
)
from cubesat_testbed.scenario.assertions import AssertionResult

_EXIT_SUCCESS = 0
_EXIT_ASSERTION_FAILURE = 1
_EXIT_EXECUTION_ERROR = 2
_EXIT_INTERRUPTED = 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cubesat-testbed",
        description="Run CubeSat Testbed deterministic simulation and hardware-in-the-loop scenarios.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    run_parser = subcommands.add_parser(
        "run",
        help="run a scenario against a setup config",
        description=(
            "Run a YAML scenario against a TOML setup config, on the in-memory runtime by "
            "default or against a real bus with --realtime."
        ),
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
        "--rules",
        type=Path,
        metavar="PATH",
        default=None,
        help=(
            "path to a standalone OBC Peer rules TOML file overriding a setup's "
            "inline [nodes.<node>.rules.*]"
        ),
    )
    run_parser.add_argument(
        "--realtime",
        action="store_true",
        help=(
            "pace virtual time against wall-clock time instead of jumping through it, "
            "so a real software/hardware peer gets time to respond; required for HIL runs"
        ),
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
    run_parser.add_argument(
        "--junit-xml",
        type=Path,
        metavar="PATH",
        default=None,
        help="write a JUnit XML report to PATH, one testcase per assertion",
    )
    run_parser.set_defaults(handler=_run_command)

    return parser


def _run_command(args: argparse.Namespace) -> int:
    suppress_assertion_output = bool(args.quiet or args.json)
    output = io.StringIO() if suppress_assertion_output else None
    clock: Clock | None = RealTimeClock() if args.realtime else None

    started = time.perf_counter()
    try:
        # The setup is loaded here as well as inside run_scenario_files, because the
        # transport/--realtime mismatch is a CLI-level warning and has to be raised
        # before the run starts. Both loads fail the same way, so a bad config still
        # reports one concise error and exit code 2.
        _warn_on_unpaced_socketcan(load_testbed_config(args.config), realtime=args.realtime)
        obc_rules = build_obc_rules_from_file(args.rules) if args.rules is not None else None
        result = run_scenario_files(
            args.config,
            args.scenario,
            output=output,
            obc_rules=obc_rules,
            clock=clock,
        )
    except Exception as exc:  # noqa: BLE001
        # CLI contract: execution errors map to 2; assertion failures map to 1 via result.passed.
        if args.junit_xml is not None:
            _write_junit_error_xml(
                args.junit_xml, str(exc), kind="execution_error", started=started
            )
        _print_error(
            str(exc),
            json_output=args.json,
            exit_code=_EXIT_EXECUTION_ERROR,
            kind="execution_error",
        )
        return _EXIT_EXECUTION_ERROR

    if args.junit_xml is not None:
        elapsed = time.perf_counter() - started
        args.junit_xml.write_text(
            format_junit_xml(result, wall_time_seconds=elapsed), encoding="utf-8"
        )

    if not result.assertions:
        print("warning: 0 assertions in scenario", file=sys.stderr)

    exit_code = _exit_code_for_result(result)
    if args.json:
        _print_json(_result_payload(result, exit_code=exit_code))
    elif not args.quiet:
        _print_summary(result)
    return exit_code


def _warn_on_unpaced_socketcan(setup: TestbedConfig, *, realtime: bool) -> None:
    """Warn when a real bus is configured but virtual time is not paced against it.

    Without ``--realtime`` the run uses the default ``VirtualClock``, which jumps
    straight to the next scheduled virtual instant and so never gives a real
    ``software``/``hardware`` peer wall-clock time to answer. That is almost
    always a mistake on a SocketCAN transport -- but not an error: a SocketCAN
    setup whose nodes are all ``simulated`` still runs correctly unpaced, and so
    does a run deliberately kept fast while a peer is not connected yet.
    """

    if realtime or not isinstance(setup.transport, SocketCanTransportConfig):
        return
    print(
        f"warning: transport.type='socketcan' (interface {setup.transport.interface!r}) "
        "without --realtime; virtual time is not paced against wall-clock time, so a "
        "real peer gets no time to respond",
        file=sys.stderr,
    )


def _write_junit_error_xml(path: Path, message: str, *, kind: str, started: float) -> None:
    elapsed = time.perf_counter() - started
    path.write_text(
        format_junit_error_xml(message, kind=kind, wall_time_seconds=elapsed),
        encoding="utf-8",
    )


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
    args: argparse.Namespace | None = None
    started = time.perf_counter()
    try:
        args = parser.parse_args(argv)
        json_output = bool(getattr(args, "json", json_output))
        handler = cast("Callable[[argparse.Namespace], int]", args.handler)
        return handler(args)
    except KeyboardInterrupt:
        junit_xml = getattr(args, "junit_xml", None)
        if junit_xml is not None:
            _write_junit_error_xml(junit_xml, "interrupted", kind="interrupted", started=started)
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
