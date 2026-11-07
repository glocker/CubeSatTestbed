from __future__ import annotations

from functools import cache

import pytest

from tests.golden_vector_loader import (
    GoldenVector,
    KissGoldenVector,
    ZmqGoldenVector,
    load_golden_vectors,
    load_kiss_vectors,
    load_zmq_vectors,
)


@cache
def _loaded_golden_vectors() -> tuple[GoldenVector, ...]:
    return load_golden_vectors()


@cache
def _loaded_kiss_vectors() -> tuple[KissGoldenVector, ...]:
    return load_kiss_vectors()


@cache
def _loaded_zmq_vectors() -> tuple[ZmqGoldenVector, ...]:
    return load_zmq_vectors()


@pytest.fixture(scope="session")
def kiss_vectors() -> tuple[KissGoldenVector, ...]:
    return _loaded_kiss_vectors()


@pytest.fixture(scope="session")
def golden_vectors() -> tuple[GoldenVector, ...]:
    return _loaded_golden_vectors()


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "golden_vector" in metafunc.fixturenames:
        vectors = _loaded_golden_vectors()
        metafunc.parametrize("golden_vector", vectors, ids=[vector.name for vector in vectors])
    if "zmq_vector" in metafunc.fixturenames:
        zmq_vectors = _loaded_zmq_vectors()
        metafunc.parametrize("zmq_vector", zmq_vectors, ids=[vector.name for vector in zmq_vectors])
    if "kiss_vector" in metafunc.fixturenames:
        kiss = _loaded_kiss_vectors()
        metafunc.parametrize("kiss_vector", kiss, ids=[vector.name for vector in kiss])
