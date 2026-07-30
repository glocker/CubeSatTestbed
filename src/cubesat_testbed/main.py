"""Command-line interface for the CubeSat Testbed."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from cubesat_testbed.scenario import ScenarioRunResult, run_scenario_files

_EXIT_SUCCESS = 0
_EXIT_ASSERTION_FAILURE = 1
_EXIT_EXECUTION_ERROR = 2


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
    run_parser.set_defaults(handler=_run_command)

    return parser


def _run_command(args: argparse.Namespace) -> int:
    try:
        result = run_scenario_files(
            args.config,
            args.scenario,
            install_default_obc_rules=not args.no_default_obc_rules,
        )
    except Exception as exc:  # noqa: BLE001
        # CLI contract: execution errors map to 2; assertion failures map to 1 via result.passed.
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_EXECUTION_ERROR

    if not result.assertions:
        print("warning: 0 assertions in scenario", file=sys.stderr)

    _print_summary(result)
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], int]", args.handler)
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
