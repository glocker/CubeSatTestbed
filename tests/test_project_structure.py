from __future__ import annotations

import cubesat_testbed


def test_package_imports() -> None:
    assert cubesat_testbed.__name__ == "cubesat_testbed"
