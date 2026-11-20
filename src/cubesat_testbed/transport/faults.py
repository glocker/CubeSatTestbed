"""Bus fault injection: a transport decorator that breaks the bus on request.

:class:`FaultInjectingTransport` wraps the in-memory bus and applies explicit
:class:`~cubesat_testbed.config.BusFaultRule` requests to the CAN frames passing
through it: drop one, delay one by a virtual duration, flip a byte, duplicate it,
or deliver it after the frame that followed it. It is the bus-level counterpart of
the passive :class:`~cubesat_testbed.fault_injection.FaultInjectionEngine`, and
obeys the same rule: it decides nothing on its own, it applies the rules it was
given.

Determinism is the point. Time comes from the engine (``now``/``schedule`` are
passed in, so a delay is a scheduled virtual-time event, never a sleep) and every
random choice -- a ``probability`` draw, the byte a ``corrupt`` rule picks when it
names none -- comes from one generator per rule, seeded from the run's seed and the
rule's position. The same seed, rules and scenario give the same events in the same
order, and the seed is part of the run's reported result.

Where it sits. Stack it directly on the bus and put the tracing adapter *above* it,
so a trace shows a node's view of the wire: ``TX`` is a frame a node tried to send,
``RX`` is a frame that reached the receiver, and each :class:`BusFaultEvent` (a
``FAULT`` line) is what the bus did in between. A dropped frame therefore appears
as a ``TX`` with no ``RX`` and a ``FAULT drop`` after it, and is visible as dropped
rather than simply missing.

The decorator owns envelope sequence numbers. A delayed frame is not on the bus yet
when ``send`` returns its envelope, so the sequence handed back is the decorator's
own and every envelope read back is translated to it; the runner keys a command's
semantic payload by that number and finds it again whatever happened to the frame.

Scope, on purpose: CAN frames on the in-memory bus. Faults on the receive side act
on the default receive stream (the bus monitor, which is where the runner decodes
every frame); endpoint-specific streams pass through. A real bus, a serial line and a
ZeroMQ hub deliver on wall-clock time, so a delay or an ordering there could not be
reproduced from a seed; setup validation refuses ``[bus_faults]`` on them.
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace

from cubesat_testbed.config import BUS_FAULT_DEFAULT_HOLD_US, BusFaultRule
from cubesat_testbed.protocol import csp_v1
from cubesat_testbed.protocol.csp_v1 import CspV1CanFrame, CspV1CodecError
from cubesat_testbed.protocol.csp_v2 import (
    CspCanFrame,
    CspV2CodecError,
    decode_can_id,
    decode_first_frame_header,
)
from cubesat_testbed.transport.base import (
    EndpointId,
    Frame,
    PacketSender,
    TransportAdapter,
    TransportDrop,
    TransportEnvelope,
    TransportError,
)
from cubesat_testbed.transport.can_protocol import CanFrame, CanProtocol
from cubesat_testbed.transport.in_memory import InMemoryBusAdapter
from cubesat_testbed.transport.tracing import TRACE_PREFIX

NowSource = Callable[[], int]
"""Current virtual time in microseconds."""

Scheduler = Callable[[int, Callable[[], None]], None]
"""Run a callback after a virtual delay, as an event on the engine's timeline."""

FaultEventHandler = Callable[["BusFaultEvent"], None]
"""Called with every injected fault as it happens; observability only."""

DEFAULT_CORRUPT_MASK = 0xFF


@dataclass(frozen=True, slots=True)
class BusFaultEvent:
    """One fault the bus injected: which rule, what it did and to which frame.

    ``match`` is the 1-based count of frames that matched the rule's selectors and
    reached it, the number ``nth`` refers to. ``data`` is the frame's CAN data field
    as it was before the fault. The header fields are ``None`` where the frame does
    not carry them (a continuation fragment whose first frame was never seen).
    """

    time: int
    rule: str
    type: str
    direction: str
    match: int
    can_id: int
    data: bytes
    source: int | None = None
    destination: int | None = None
    destination_port: int | None = None
    source_port: int | None = None
    detail: str = ""


def format_bus_fault(event: BusFaultEvent) -> str:
    """Format one injected fault as a ``FAULT t=... rule=... action=...`` report line."""

    parts = [
        "FAULT",
        f"t={event.time}",
        f"rule={event.rule}",
        f"action={event.type}",
        f"dir={event.direction}",
        f"match={event.match}",
        f"can_id=0x{event.can_id:08X}",
    ]
    if event.source is not None:
        parts.append(f"src={event.source}")
    if event.destination is not None:
        parts.append(f"dst={event.destination}")
    if event.destination_port is not None:
        parts.append(f"dport={event.destination_port}")
    if event.source_port is not None:
        parts.append(f"sport={event.source_port}")
    parts.append(f"data={event.data.hex() if event.data else '-'}")
    if event.detail:
        parts.append(event.detail)
    return " ".join(parts)


def format_bus_fault_trace_line(event: BusFaultEvent) -> str:
    """Format one injected fault as a ``--trace`` line (``trace`` + the FAULT report line)."""

    return f"{TRACE_PREFIX} {format_bus_fault(event)}"


@dataclass(frozen=True, slots=True)
class _Header:
    """What the rules can select on, as far as the frame (or its packet) tells."""

    source: int | None = None
    destination: int | None = None
    destination_port: int | None = None
    source_port: int | None = None


class _ActiveRule:
    """A rule plus the counters and the generator it needs while it is in force."""

    def __init__(self, spec: BusFaultRule, *, index: int, seed: int) -> None:
        self.spec = spec
        self.name = spec.name or f"{spec.type}-{index + 1}"
        self.rng = random.Random(f"{seed}/{index}")
        self.matched = 0
        self.fired = 0

    def selects(self, direction: str, header: _Header) -> bool:
        spec = self.spec
        if spec.direction != direction:
            return False
        return all(
            wanted is None or wanted == actual
            for wanted, actual in (
                (spec.port, header.destination_port),
                (spec.source_port, header.source_port),
                (spec.source, header.source),
                (spec.destination, header.destination),
            )
        )

    def consider(self) -> int | None:
        """Count one matching frame; its match number if the rule fires on it."""

        spec = self.spec
        self.matched += 1
        if spec.nth is not None:
            fires = spec.nth <= self.matched < spec.nth + (spec.count or 1)
        else:
            fires = spec.count is None or self.fired < spec.count
            if fires and spec.probability is not None:
                fires = self.rng.random() < spec.probability
        if not fires:
            return None
        self.fired += 1
        return self.matched


@dataclass(slots=True)
class _HeldTx:
    frame: Frame
    source: EndpointId | None
    sequence: int


@dataclass(slots=True)
class _HeldRx:
    envelope: TransportEnvelope


class FaultInjectingTransport(TransportAdapter):
    """Applies bus fault rules to the CAN frames crossing an inner adapter.

    ``now`` and ``schedule`` tie it to virtual time (see :data:`NowSource`,
    :data:`Scheduler`). ``seed`` seeds every rule's generator. ``on_event`` is told
    of each injected fault. Rules are added with :meth:`add_rule`, from the setup
    config before the run and from ``inject_bus_fault`` steps during it; a frame is
    affected by at most one rule, tried in the order they were added, and a rule
    counts only the frames that reached it.
    """

    def __init__(
        self,
        inner: InMemoryBusAdapter,
        *,
        now: NowSource,
        schedule: Scheduler,
        seed: int = 0,
        on_event: FaultEventHandler | None = None,
    ) -> None:
        self._inner = inner
        self._now = now
        self._schedule = schedule
        self._seed = seed
        self._on_event = on_event
        self._release_hook: Callable[[], None] | None = None
        self._rules: list[_ActiveRule] = []
        self._events: list[BusFaultEvent] = []
        self._next_sequence = 0
        self._sequence_by_inner: dict[int, int] = {}
        self._lost_sequences: list[int] = []
        self._packet_headers: dict[str, dict[tuple[int, ...], _Header]] = {
            "tx": {},
            "rx": {},
        }
        self._held_tx: list[_HeldTx] = []
        self._held_rx: list[_HeldRx] = []
        self._rx_ready: deque[TransportEnvelope] = deque()

    # --- configuration ----------------------------------------------------------------

    @property
    def inner(self) -> InMemoryBusAdapter:
        """The wrapped adapter that actually carries the frames."""

        return self._inner

    @property
    def seed(self) -> int:
        """The seed every random choice of this run derives from."""

        return self._seed

    @property
    def has_rules(self) -> bool:
        """Whether any rule was ever added, so a result can say a seed mattered."""

        return bool(self._rules)

    def add_rule(self, rule: BusFaultRule) -> str:
        """Put ``rule`` in force from now on; returns the name it is reported under."""

        active = _ActiveRule(rule, index=len(self._rules), seed=self._seed)
        self._rules.append(active)
        return active.name

    def set_event_handler(self, handler: FaultEventHandler | None) -> None:
        """Replace the observer told of each injected fault."""

        self._on_event = handler

    def set_release_hook(self, hook: Callable[[], None] | None) -> None:
        """Set what runs right after a held frame is released at its virtual time.

        The scenario runner uses it to deliver the released frame in the same
        instant instead of at the next time it happens to drain the bus.
        """

        self._release_hook = hook

    @property
    def events(self) -> tuple[BusFaultEvent, ...]:
        """Every fault injected so far, in order."""

        return tuple(self._events)

    def take_lost_sequences(self) -> tuple[int, ...]:
        """Sequences of frames a fault dropped, and forget them.

        A frame that never arrives cannot release whatever the runner keyed by its
        sequence, so the runner asks which ones to let go of.
        """

        lost = tuple(self._lost_sequences)
        self._lost_sequences.clear()
        return lost

    # --- the in-memory bus API, which this decorator stands in for ---------------------

    @property
    def endpoints(self) -> tuple[EndpointId, ...]:
        """Connected endpoint identifiers of the bus underneath."""

        return self._inner.endpoints

    def connect(self, endpoint: EndpointId) -> None:
        """Create an endpoint-specific receive queue on the bus underneath."""

        self._inner.connect(endpoint)

    def connect_endpoint(self, endpoint: EndpointId) -> None:
        self._inner.connect_endpoint(endpoint)

    def disconnect(self, endpoint: EndpointId) -> None:
        self._inner.disconnect(endpoint)

    def disconnect_endpoint(self, endpoint: EndpointId) -> None:
        self._inner.disconnect_endpoint(endpoint)

    def pending_count(self, *, endpoint: EndpointId | None = None) -> int:
        """Frames currently queued for a stream, including ones faults already released."""

        pending = self._inner.pending_count(endpoint=endpoint)
        return pending + len(self._rx_ready) if endpoint is None else pending

    def clear(self, *, endpoint: EndpointId | None = None) -> None:
        """Drop currently queued frames from one stream."""

        self._inner.clear(endpoint=endpoint)
        if endpoint is None:
            self._rx_ready.clear()

    # --- adapter surface ---------------------------------------------------------------

    @property
    def can_protocol(self) -> CanProtocol:
        return self._inner.can_protocol

    def new_sender(self, *, max_packet_bytes: int = 256) -> PacketSender:
        return self._inner.new_sender(max_packet_bytes=max_packet_bytes)

    def payload_limit(self, max_packet_bytes: int) -> int:
        return self._inner.payload_limit(max_packet_bytes)

    def take_drops(self) -> tuple[TransportDrop, ...]:
        return self._inner.take_drops()

    def close(self) -> None:
        self._inner.close()

    def send(
        self,
        frame: Frame,
        *,
        source: EndpointId | None = None,
    ) -> TransportEnvelope:
        """Send ``frame`` as a node would, subject to the rules in force."""

        can_frame = _require_can(frame)
        sequence = self._take_sequence()
        envelope = TransportEnvelope(frame=frame, sequence=sequence, source=source)
        if not self._rules:
            self._transmit(frame, source, sequence)
            return envelope
        header = self._header_of(can_frame, "tx")
        rule, match = self._firing_rule("tx", header)

        if rule is None or match is None:
            self._transmit(frame, source, sequence)
            self._flush_held_tx()
            return envelope

        spec = rule.spec
        if spec.type == "drop":
            self._lost_sequences.append(sequence)
            self._emit(rule, match, "tx", can_frame, header, "")
        elif spec.type == "delay":
            assert spec.delay is not None
            self._emit(rule, match, "tx", can_frame, header, f"until={self._now() + spec.delay}")
            self._schedule(spec.delay, lambda: self._release_tx(frame, source, sequence))
        elif spec.type == "corrupt":
            corrupted, detail = self._corrupt(rule, can_frame)
            self._emit(rule, match, "tx", can_frame, header, detail)
            self._transmit(corrupted, source, sequence)
            self._flush_held_tx()
        elif spec.type == "duplicate":
            self._emit(rule, match, "tx", can_frame, header, "copies=2")
            self._transmit(frame, source, sequence)
            self._transmit(frame, source, sequence)
            self._flush_held_tx()
        else:  # reorder
            hold = spec.hold or BUS_FAULT_DEFAULT_HOLD_US
            self._emit(rule, match, "tx", can_frame, header, f"hold={hold}")
            held = _HeldTx(frame=frame, source=source, sequence=sequence)
            self._held_tx.append(held)
            self._schedule(hold, lambda: self._expire_held_tx(held))
        return envelope

    def receive(
        self,
        *,
        endpoint: EndpointId | None = None,
        timeout: float | None = None,
    ) -> TransportEnvelope | None:
        """Receive the next frame that survives the rules in force, or ``None``."""

        if endpoint is not None:
            inner = self._inner.receive(endpoint=endpoint, timeout=timeout)
            return None if inner is None else self._translate(inner)

        while True:
            if self._rx_ready:
                return self._rx_ready.popleft()
            inner = self._inner.receive(endpoint=None, timeout=timeout)
            if inner is None:
                return None
            self._receive_one(self._translate(inner))

    # --- the transmit side -------------------------------------------------------------

    def _transmit(self, frame: Frame, source: EndpointId | None, sequence: int) -> None:
        envelope = self._inner.send(frame, source=source)
        self._sequence_by_inner[envelope.sequence] = sequence

    def _release_tx(self, frame: Frame, source: EndpointId | None, sequence: int) -> None:
        self._transmit(frame, source, sequence)
        self._flush_held_tx()
        self._after_release()

    def _flush_held_tx(self) -> None:
        held, self._held_tx = self._held_tx, []
        for entry in held:
            self._transmit(entry.frame, entry.source, entry.sequence)

    def _expire_held_tx(self, entry: _HeldTx) -> None:
        if entry not in self._held_tx:
            return  # already released behind a later frame
        self._held_tx.remove(entry)
        self._transmit(entry.frame, entry.source, entry.sequence)
        self._after_release()

    # --- the receive side --------------------------------------------------------------

    def _receive_one(self, envelope: TransportEnvelope) -> None:
        """Apply the rules to one frame read off the bus; queue what is delivered."""

        can_frame = _require_can(envelope.frame)
        if not self._rules:
            self._rx_ready.append(envelope)
            return
        header = self._header_of(can_frame, "rx")
        rule, match = self._firing_rule("rx", header)

        if rule is None or match is None:
            self._rx_ready.append(envelope)
            self._flush_held_rx()
            return

        spec = rule.spec
        if spec.type == "drop":
            self._lost_sequences.append(envelope.sequence)
            self._emit(rule, match, "rx", can_frame, header, "")
        elif spec.type == "delay":
            assert spec.delay is not None
            self._emit(rule, match, "rx", can_frame, header, f"until={self._now() + spec.delay}")
            self._schedule(spec.delay, lambda: self._release_rx(envelope))
        elif spec.type == "corrupt":
            corrupted, detail = self._corrupt(rule, can_frame)
            self._emit(rule, match, "rx", can_frame, header, detail)
            self._rx_ready.append(replace(envelope, frame=corrupted))
            self._flush_held_rx()
        elif spec.type == "duplicate":
            self._emit(rule, match, "rx", can_frame, header, "copies=2")
            self._rx_ready.append(envelope)
            self._rx_ready.append(envelope)
            self._flush_held_rx()
        else:  # reorder
            hold = spec.hold or BUS_FAULT_DEFAULT_HOLD_US
            self._emit(rule, match, "rx", can_frame, header, f"hold={hold}")
            held = _HeldRx(envelope=envelope)
            self._held_rx.append(held)
            self._schedule(hold, lambda: self._expire_held_rx(held))

    def _release_rx(self, envelope: TransportEnvelope) -> None:
        self._rx_ready.append(envelope)
        self._after_release()

    def _flush_held_rx(self) -> None:
        held, self._held_rx = self._held_rx, []
        self._rx_ready.extend(entry.envelope for entry in held)

    def _expire_held_rx(self, entry: _HeldRx) -> None:
        if entry not in self._held_rx:
            return
        self._held_rx.remove(entry)
        self._rx_ready.append(entry.envelope)
        self._after_release()

    # --- shared ------------------------------------------------------------------------

    def _after_release(self) -> None:
        if self._release_hook is not None:
            self._release_hook()

    def _take_sequence(self) -> int:
        sequence = self._next_sequence
        self._next_sequence += 1
        return sequence

    def _translate(self, envelope: TransportEnvelope) -> TransportEnvelope:
        sequence = self._sequence_by_inner.get(envelope.sequence)
        if sequence is None or sequence == envelope.sequence:
            return envelope
        return replace(envelope, sequence=sequence)

    def _firing_rule(
        self, direction: str, header: _Header
    ) -> tuple[_ActiveRule | None, int | None]:
        for rule in self._rules:
            if not rule.selects(direction, header):
                continue
            match = rule.consider()
            if match is not None:
                return rule, match
        return None, None

    def _header_of(self, frame: CanFrame, direction: str) -> _Header:
        """Selectable header of a frame, taking a fragment's from its first frame.

        Only the first frame of a multi-frame packet carries ports and the source
        address; the others carry the sender and destination. The headers seen are
        remembered, per direction and keyed the way a receiver matches fragments
        (CSP v2: priority, destination, sender, packet counter; CSP v1: source, destination,
        identification), so a rule on a port reaches every fragment of the packet, and a rule
        can single one out by ``nth``.
        """

        if not frame.is_extended_id:
            return _Header()
        if isinstance(frame, CspV1CanFrame):
            return self._header_of_v1(frame, direction)
        can_fields = decode_can_id(frame.can_id)
        key = (
            can_fields.priority,
            can_fields.destination,
            can_fields.sender,
            can_fields.packet_count,
        )
        known = self._packet_headers[direction]
        if can_fields.begin:
            try:
                first = decode_first_frame_header(frame)
            except CspV2CodecError:
                return _Header(source=can_fields.sender, destination=can_fields.destination)
            header = _Header(
                source=first.source,
                destination=first.destination,
                destination_port=first.destination_port,
                source_port=first.source_port,
            )
            if can_fields.end:
                known.pop(key, None)
            else:
                known[key] = header
            return header
        remembered = known.get(key)
        if can_fields.end:
            known.pop(key, None)
        if remembered is None:
            return _Header(source=can_fields.sender, destination=can_fields.destination)
        return remembered

    def _header_of_v1(self, frame: CspV1CanFrame, direction: str) -> _Header:
        can_fields = csp_v1.decode_can_id(frame.can_id)
        key = (can_fields.source, can_fields.destination, can_fields.identification)
        known = self._packet_headers[direction]
        last = can_fields.remaining == 0
        if can_fields.begin:
            try:
                first = csp_v1.decode_first_frame_header(frame)
            except CspV1CodecError:
                return _Header(source=can_fields.source, destination=can_fields.destination)
            header = _Header(
                source=first.source,
                destination=first.destination,
                destination_port=first.destination_port,
                source_port=first.source_port,
            )
            if last:
                known.pop(key, None)
            else:
                known[key] = header
            return header
        remembered = known.get(key)
        if last:
            known.pop(key, None)
        if remembered is None:
            return _Header(source=can_fields.source, destination=can_fields.destination)
        return remembered

    def _corrupt(self, rule: _ActiveRule, frame: CanFrame) -> tuple[CanFrame, str]:
        spec = rule.spec
        data = bytearray(frame.data)
        if not data:
            return frame, "byte=- (empty frame)"
        index = spec.byte if spec.byte is not None else int(rule.rng.random() * len(data))
        if index >= len(data):
            # Reported, not raised: the rule fired and said so, the frame was too short.
            return frame, f"byte={index} skipped: the frame has {len(data)} data bytes"
        mask = spec.mask if spec.mask is not None else DEFAULT_CORRUPT_MASK
        before = data[index]
        data[index] = before ^ mask
        corrupted = replace(frame, data=bytes(data))
        return corrupted, f"byte={index} mask=0x{mask:02X} {before:02x}->{data[index]:02x}"

    def _emit(
        self,
        rule: _ActiveRule,
        match: int,
        direction: str,
        frame: CanFrame,
        header: _Header,
        detail: str,
    ) -> None:
        event = BusFaultEvent(
            time=self._now(),
            rule=rule.name,
            type=rule.spec.type,
            direction=direction.upper(),
            match=match,
            can_id=frame.can_id,
            data=frame.data,
            source=header.source,
            destination=header.destination,
            destination_port=header.destination_port,
            source_port=header.source_port,
            detail=detail,
        )
        self._events.append(event)
        if self._on_event is not None:
            self._on_event(event)


def _require_can(frame: Frame) -> CanFrame:
    if not isinstance(frame, CspCanFrame | CspV1CanFrame):
        raise TransportError("bus faults apply to CAN frames; this link carries whole packets")
    return frame


__all__ = [
    "BusFaultEvent",
    "FaultInjectingTransport",
    "format_bus_fault",
    "format_bus_fault_trace_line",
]
