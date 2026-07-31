from __future__ import annotations

import pytest

from cubesat_testbed.clock import ClockError, RealTimeClock, VirtualClock


def test_virtual_clock_never_waits() -> None:
    clock = VirtualClock()

    assert clock.seconds_until(0) == 0.0
    assert clock.seconds_until(10_000_000_000) == 0.0


def test_real_time_clock_reports_remaining_seconds_at_origin() -> None:
    wall_time = iter([100.0, 100.0])
    clock = RealTimeClock(origin_virtual_time=0, wall_clock=lambda: next(wall_time))

    assert clock.seconds_until(500_000) == pytest.approx(0.5)


def test_real_time_clock_accounts_for_elapsed_wall_time() -> None:
    wall_time = iter([100.0, 100.3])
    clock = RealTimeClock(origin_virtual_time=0, wall_clock=lambda: next(wall_time))

    assert clock.seconds_until(500_000) == pytest.approx(0.2)


def test_real_time_clock_never_returns_negative_seconds() -> None:
    wall_time = iter([100.0, 101.0])
    clock = RealTimeClock(origin_virtual_time=0, wall_clock=lambda: next(wall_time))

    assert clock.seconds_until(500_000) == 0.0


def test_real_time_clock_applies_time_scale() -> None:
    wall_time = iter([100.0, 100.0])
    clock = RealTimeClock(origin_virtual_time=0, time_scale=2.0, wall_clock=lambda: next(wall_time))

    # Running twice as fast: 1s of virtual time only needs 0.5 real seconds.
    assert clock.seconds_until(1_000_000) == pytest.approx(0.5)


def test_real_time_clock_honors_a_non_zero_origin_virtual_time() -> None:
    wall_time = iter([100.0, 100.0])
    clock = RealTimeClock(origin_virtual_time=3_000_000, wall_clock=lambda: next(wall_time))

    assert clock.seconds_until(3_500_000) == pytest.approx(0.5)


def test_real_time_clock_rejects_non_positive_time_scale() -> None:
    with pytest.raises(ClockError, match="time_scale"):
        RealTimeClock(time_scale=0.0)
    with pytest.raises(ClockError, match="time_scale"):
        RealTimeClock(time_scale=-1.0)


def test_real_time_clock_rejects_negative_origin_virtual_time() -> None:
    with pytest.raises(ClockError, match="origin_virtual_time"):
        RealTimeClock(origin_virtual_time=-1)
