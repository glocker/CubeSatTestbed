"""Pluggable virtual-time pacing for HIL runs.

The DES engine (:mod:`cubesat_testbed.engine`) always owns virtual time and
never sleeps; that does not change here. A :class:`Clock` only answers "how
many real seconds remain before virtual time `target` is reached", which the
scenario runner consults when a run's transport is backed by real hardware or
software over SocketCAN: a real peer needs a realistic wall-clock window to
respond, unlike pure simulation, which can jump straight to the next
scheduled event.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from cubesat_testbed.engine import VirtualTime

_MICROSECONDS_PER_SECOND = 1_000_000.0


class ClockError(ValueError):
    """Raised when a clock cannot be constructed or queried."""


@runtime_checkable
class Clock(Protocol):
    """Answers how long, in real seconds, until a virtual time is reached."""

    def seconds_until(self, target: VirtualTime) -> float:
        """Return real seconds remaining before virtual time ``target``.

        Never negative: a ``target`` already in the past (relative to this
        clock's pacing) returns ``0.0``, meaning "no need to wait further".
        """


class VirtualClock:
    """Default clock: virtual time is not paced against wall-clock time.

    Every call returns ``0.0``, so a caller pacing against this clock never
    actually waits -- matching pure in-memory simulation, where the engine
    jumps directly to the next scheduled event.
    """

    def seconds_until(self, target: VirtualTime) -> float:
        return 0.0


class RealTimeClock:
    """Paces virtual time against wall-clock time for HIL runs.

    ``origin_virtual_time`` anchors the mapping: at construction, that virtual
    instant is defined to occur "now" in wall-clock time. ``time_scale`` lets
    a run go faster or slower than real time (``2.0`` runs virtual time at
    double real speed; ``0.5`` runs at half); it must be positive.
    """

    def __init__(
        self,
        *,
        origin_virtual_time: VirtualTime = 0,
        time_scale: float = 1.0,
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        self._origin_virtual_time = _validate_virtual_time(
            "origin_virtual_time", origin_virtual_time
        )
        self._time_scale = _validate_positive_finite("time_scale", time_scale)
        self._wall_clock = time.monotonic if wall_clock is None else wall_clock
        self._origin_wall_time = self._wall_clock()

    def seconds_until(self, target: VirtualTime) -> float:
        validated_target = _validate_virtual_time("target", target)
        virtual_elapsed_us = validated_target - self._origin_virtual_time
        target_wall_time = (
            self._origin_wall_time
            + (virtual_elapsed_us / _MICROSECONDS_PER_SECOND) / self._time_scale
        )
        return max(0.0, target_wall_time - self._wall_clock())


def _validate_virtual_time(name: str, value: VirtualTime) -> VirtualTime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClockError(f"{name} must be an integer virtual-time tick")
    if value < 0:
        raise ClockError(f"{name} must be non-negative, got {value}")
    return value


def _validate_positive_finite(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClockError(f"{name} must be a finite positive number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ClockError(f"{name} must be finite and positive, got {value}")
    return number


__all__ = ["Clock", "ClockError", "RealTimeClock", "VirtualClock"]
