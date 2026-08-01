"""Unit-level proof that RealTimeClock actually paces wait(), without needing
real (or virtual) CAN hardware: a fake transport that genuinely blocks inside
receive(timeout=...), the same contract SocketCanAdapter fulfills via
python-can's own recv(timeout=...).
"""

from __future__ import annotations

import dataclasses
import time

from cubesat_testbed.clock import RealTimeClock, VirtualClock
from cubesat_testbed.config import load_testbed_config
from cubesat_testbed.protocol.csp_v2 import CspCanFrame
from cubesat_testbed.scenario import ScenarioRunner, build_runtime
from cubesat_testbed.transport.base import EndpointId, TransportAdapter, TransportEnvelope
from tests.example_paths import DEFAULT_SETUP


class _BlockingFakeTransport(TransportAdapter):
    """A transport double that genuinely sleeps inside receive(timeout=...),
    like a real blocking bus read, instead of always returning instantly.

    Records each call's own actual duration, so a test can tell "returned
    promptly once the frame was due" apart from "blocked for the full
    requested timeout regardless" -- the two would otherwise look identical
    from the outside once the overall wait has finished.
    """

    def __init__(self, *, frame_after_seconds: float | None = None) -> None:
        self._sequence = 0
        self._frame_after_seconds = frame_after_seconds
        self._start = time.perf_counter()
        self._delivered = False
        self.call_durations: list[float] = []

    def send(self, frame: CspCanFrame, *, source: EndpointId | None = None) -> TransportEnvelope:
        envelope = TransportEnvelope(frame=frame, sequence=self._sequence, source=source)
        self._sequence += 1
        return envelope

    def receive(
        self,
        *,
        endpoint: EndpointId | None = None,
        timeout: float | None = None,
    ) -> TransportEnvelope | None:
        call_started = time.perf_counter()
        wait_for = 0.0 if timeout is None else timeout
        if self._frame_after_seconds is not None and not self._delivered:
            due_in = self._frame_after_seconds - (time.perf_counter() - self._start)
            if due_in <= wait_for:
                time.sleep(max(0.0, due_in))
                self._delivered = True
                # The project's own golden vector: a well-formed single-frame
                # CSP v2 ping, so _deliver_transport_envelope can decode it
                # without erroring on an intentionally-invalid stand-in frame.
                frame = CspCanFrame(can_id=0x10004083, data=bytes.fromhex("00 04 14 80 55"))
                envelope = TransportEnvelope(frame=frame, sequence=self._sequence)
                self._sequence += 1
                self.call_durations.append(time.perf_counter() - call_started)
                return envelope
        time.sleep(wait_for)
        self.call_durations.append(time.perf_counter() - call_started)
        return None


def test_wait_with_virtual_clock_does_not_pace_against_wall_clock_time() -> None:
    setup = load_testbed_config(DEFAULT_SETUP)
    runtime = build_runtime(setup)
    assert isinstance(runtime.clock, VirtualClock)
    runner = ScenarioRunner(runtime)

    started = time.perf_counter()
    runner.wait(2_000_000)  # 2 virtual seconds
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5


def test_wait_with_real_time_clock_paces_against_wall_clock_time() -> None:
    setup = load_testbed_config(DEFAULT_SETUP)
    runtime = build_runtime(setup, clock=RealTimeClock())
    runner = ScenarioRunner(runtime)

    started = time.perf_counter()
    runner.wait(200_000)  # 0.2 virtual seconds
    elapsed = time.perf_counter() - started

    assert elapsed >= 0.18


def test_wait_with_real_time_clock_delivers_an_early_frame_promptly() -> None:
    """A frame arriving partway through a paced wait is picked up as soon as
    it is due, not only once some later window's full timeout has elapsed --
    even though the overall wait still takes the full requested real time
    either way, since wait() is a fixed time advance, not an
    until-condition-met operation.
    """

    setup = load_testbed_config(DEFAULT_SETUP)
    fake_transport = _BlockingFakeTransport(frame_after_seconds=0.02)
    base_runtime = build_runtime(setup, clock=RealTimeClock())
    runtime = dataclasses.replace(base_runtime, transport=fake_transport)
    runner = ScenarioRunner(runtime)

    started = time.perf_counter()
    runner.wait(100_000)  # 0.1 virtual seconds -- paces to ~0.1 real seconds
    elapsed = time.perf_counter() - started

    assert elapsed >= 0.09
    assert fake_transport.call_durations, "receive() was never called"
    # The frame was due at ~0.02s; the call that delivered it must not have
    # blocked for anywhere near its own ~0.1s timeout budget.
    assert fake_transport.call_durations[0] < 0.06
