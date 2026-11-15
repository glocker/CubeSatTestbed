"""Records what happened in a run, for the HTML report.

The recorder is an observer, in the same sense as ``--trace``: it is handed what the
run already produces (every frame crossing the transport boundary as a
:class:`~cubesat_testbed.transport.tracing.FrameRecord`, every dropped packet, every
decoded telemetry value, every assertion result) and never feeds anything back. It
holds no clock and no randomness, so two runs with the same inputs record the same
thing. It imports nothing from the scenario runner, which in turn depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cubesat_testbed.transport.base import TransportEnvelope
from cubesat_testbed.transport.tracing import FrameRecord

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime import cycle
    from cubesat_testbed.config import ScenarioScript, TestbedConfig
    from cubesat_testbed.scenario.assertions import AssertionResult
    from cubesat_testbed.scenario.runner import DroppedPacket


@dataclass(frozen=True, slots=True)
class RecordedFrame:
    """One traced frame, with the id the report links to (``f<frame_id>``)."""

    frame_id: int
    record: FrameRecord
    envelope_sequence: int


@dataclass(frozen=True, slots=True)
class RecordedDrop:
    """A packet a node or a link discarded, at its virtual time."""

    time: int
    destination: int | None
    sender: int | None
    reason: str
    detail: str
    limit: int | None


@dataclass(frozen=True, slots=True)
class RecordedStep:
    """A scenario step starting, so the timeline shows what the scenario was doing."""

    time: int
    number: int
    text: str


@dataclass(frozen=True, slots=True)
class RecordedAssertion:
    """An assertion result and the frame that carried the value it was judged on.

    ``evidence_frame`` is the id of the frame (the last fragment, for a multi-frame
    packet) from which the signal's latest value was decoded when the assertion was
    evaluated: for a passing assertion the frame that satisfied it, for a failing one
    the frame whose value did not match. ``None`` means the signal was never seen on
    the bus.
    """

    result: AssertionResult
    evidence_frame: int | None


TimelineEntry = RecordedFrame | RecordedDrop | RecordedStep


class RunRecorder:
    """Collects the frames, drops, steps, signal values and assertions of one run."""

    def __init__(self) -> None:
        self.setup: TestbedConfig | None = None
        self.scenario: ScenarioScript | None = None
        self.timeline: list[TimelineEntry] = []
        self.frames: list[RecordedFrame] = []
        self.assertions: list[RecordedAssertion] = []
        self.signals: dict[str, list[tuple[int, object]]] = {}
        self._rx_frame_by_sequence: dict[int, int] = {}

    def begin(self, setup: TestbedConfig, scenario: ScenarioScript) -> None:
        """Remember what is being run; the report describes both."""

        self.setup = setup
        self.scenario = scenario

    # --- frames ---------------------------------------------------------------------

    def observe_frame(self, record: FrameRecord, envelope: TransportEnvelope) -> None:
        """Frame observer for :class:`~cubesat_testbed.transport.TracingTransportAdapter`."""

        frame = RecordedFrame(
            frame_id=len(self.frames) + 1,
            record=record,
            envelope_sequence=envelope.sequence,
        )
        self.frames.append(frame)
        self.timeline.append(frame)
        if record.direction == "RX":
            self._rx_frame_by_sequence[envelope.sequence] = frame.frame_id

    def rx_frame_id(self, envelope: TransportEnvelope) -> int | None:
        """Id of the ``RX`` row for ``envelope``: the frame the runner is routing now."""

        return self._rx_frame_by_sequence.get(envelope.sequence)

    # --- the rest of the run --------------------------------------------------------

    def step(self, time: int, number: int, step: Any) -> None:
        self.timeline.append(RecordedStep(time=time, number=number, text=describe_step(step)))

    def drop(self, dropped: DroppedPacket) -> None:
        self.timeline.append(
            RecordedDrop(
                time=dropped.time,
                destination=dropped.destination,
                sender=dropped.sender,
                reason=dropped.reason,
                detail=dropped.detail,
                limit=dropped.limit,
            )
        )

    def signal(self, time: int, signal: str, value: object) -> None:
        self.signals.setdefault(signal, []).append((time, value))

    def assertion(self, result: AssertionResult, evidence_frame: int | None) -> None:
        self.assertions.append(RecordedAssertion(result=result, evidence_frame=evidence_frame))


_DURATION_KEYS = frozenset({"virtual_time", "duration", "timeout"})


def describe_step(step: Any) -> str:
    """One line for a scenario step: ``action key=value ...``, durations in seconds."""

    data = step.model_dump(exclude_none=True)
    action = str(data.pop("action"))
    parts = [action]
    for key, value in data.items():
        if key in _DURATION_KEYS and isinstance(value, int):
            parts.append(f"{key}={format_virtual_time(value)}")
        else:
            parts.append(f"{key}={getattr(value, 'value', value)}")
    return " ".join(parts)


def format_virtual_time(microseconds: int) -> str:
    """Virtual microseconds as seconds with six decimals, exact and locale-free."""

    sign = "-" if microseconds < 0 else ""
    whole, fraction = divmod(abs(microseconds), 1_000_000)
    return f"{sign}{whole}.{fraction:06d} s"


__all__ = [
    "RecordedAssertion",
    "RecordedDrop",
    "RecordedFrame",
    "RecordedStep",
    "RunRecorder",
    "TimelineEntry",
    "describe_step",
    "format_virtual_time",
]
