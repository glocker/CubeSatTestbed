from __future__ import annotations

from functools import cache

import pytest

from tests.golden_vector_loader import GoldenVector, load_golden_vectors


@cache
def _loaded_golden_vectors() -> tuple[GoldenVector, ...]:
    return load_golden_vectors()


@pytest.fixture(scope="session")
def golden_vectors() -> tuple[GoldenVector, ...]:
    return _loaded_golden_vectors()


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "golden_vector" not in metafunc.fixturenames:
        return

    vectors = _loaded_golden_vectors()
    metafunc.parametrize("golden_vector", vectors, ids=[vector.name for vector in vectors])
