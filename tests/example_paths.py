"""Paths to the packaged examples the test suite runs against.

The examples ship inside ``cubesat_testbed`` rather than in a repository
directory, so tests address them through the package instead of through
``configs/`` string literals -- which also makes the suite independent of the
working directory it is invoked from.
"""

from __future__ import annotations

from pathlib import Path

from cubesat_testbed.examples import get_example

DEFAULT_SETUP: Path = get_example("default").setup_path
DEFAULT_SCENARIO: Path = get_example("default").scenario_path
SOCKETCAN_SETUP: Path = get_example("socketcan-hil").setup_path
MODULE_PARAMS_SETUP: Path = get_example("module-params").setup_path

__all__ = [
    "DEFAULT_SCENARIO",
    "DEFAULT_SETUP",
    "MODULE_PARAMS_SETUP",
    "SOCKETCAN_SETUP",
]
