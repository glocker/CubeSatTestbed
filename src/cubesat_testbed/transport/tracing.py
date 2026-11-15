"""Wire-level frame tracing at the transport boundary.

:class:`TracingTransportAdapter` wraps any :class:`TransportAdapter` and writes
one decoded line per frame that crosses it, in either direction. It sits at the
transport boundary rather than inside the scenario runner on purpose:

- every frame is traced regardless of who produced it, including the ones an
  :class:`~cubesat_testbed.modules.obc_peer.ObcPeerModule` sends straight to the
  transport without going through the runner;
- on SocketCAN this is the only place an outgoing frame is visible at all, since
  the adapter does not receive its own messages back from the bus.

On the in-memory bus a self-sent frame is therefore traced twice -- once as
``TX`` when it is put on the bus, once as ``RX`` when the runner's promiscuous
monitor stream reads it back and decodes it. That is not duplication to be
filtered out: it is exactly the round trip that makes a scenario assertion an
observation of the wire rather than of module state.

A packet too long for one CAN frame is traced fragment by fragment: every frame
gets its own line carrying its CFP2 fields (``cfp=begin|more|end``, packet
counter ``pc``, frame counter ``fc``), the first one also names the route, and
the line of the last frame adds the reassembled packet (``frames=N``, the whole
``payload`` and the same annotation a single-frame packet gets). Reassembly here
is a passive observer with no size limit, kept separately for TX and RX because
an in-memory frame is traced once in each direction; whether a node *accepted*
the packet, or dropped it as too large, lost or late, is the runner's business
and is reported as ``DROP`` lines.

A packet carried whole (serial KISS, ZeroMQ) is one line: ``link=serial`` or
``link=zmq`` with the exact bytes the link carried as ``data`` when it went over
the link, or ``link=local`` with ``data=-`` when it was delivered between two
simulated nodes, then the header
fields, the ``payload`` and the same annotation a CAN packet gets. Frames the
link itself rejects (a serial CRC32 mismatch, an oversized message) are reported as ``DROP`` lines
by the runner, like a lost CAN fragment.

Tracing is observability only. It never alters delivery, ordering or virtual
time, and it never raises: a frame the codec cannot decode is reported as such,
because unexpected traffic is often the reason a trace was turned on.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TextIO

from cubesat_testbed.protocol.csp_v2 import (
    CspCanFrame,
    CspCanIdFields,
    CspReassembler,
    CspV2CodecError,
    decode_can_id,
    decode_first_frame_header,
)
from cubesat_testbed.protocol.csp_v2 import decode_frame as _decode_frame
from cubesat_testbed.protocol.csp_v2_packet import CspPacketFrame, CspPacketView
from cubesat_testbed.transport.base import (
    EndpointId,
    Frame,
    PacketSender,
    TransportAdapter,
    TransportDrop,
    TransportEnvelope,
)

FrameAnnotator = Callable[[CspPacketView], str | None]
"""Describe a decoded frame in terms of the configured route it matches.

Returns a trailing annotation for the trace line (for example
``telemetry eps.telemetry.battery_percent=95.0``), or ``None`` to leave the
frame described by its header fields alone. Must not raise.
"""

VirtualTimeSource = Callable[[], int]
"""Return the current virtual time, in microseconds, for the trace timestamp."""


@dataclass(frozen=True, slots=True)
class FrameRecord:
    """One frame as the trace sees it, decoded into fields instead of text.

    The ``--trace`` line and the HTML report are both rendered from this, so what the
    report shows is exactly what the trace would print. ``kind`` is ``"packet"`` for
    a packet a link carries whole, ``"frame"`` for a CAN frame that is a whole packet,
    ``"fragment"`` for one CAN frame of a multi-frame packet and ``"undecodable"``
    for a frame the codec rejected. Header fields are ``None`` where the frame does
    not carry them (a continuation fragment has only ``dst`` and ``sender``).
    """

    direction: str
    kind: str
    time: int | None = None
    link: str | None = None
    can_id: int | None = None
    dlc: int | None = None
    cfp: str | None = None
    packet_count: int | None = None
    frame_count: int | None = None
    priority: int | None = None
    source: int | None = None
    destination: int | None = None
    destination_port: int | None = None
    source_port: int | None = None
    flags: int | None = None
    sender: int | None = None
    data: bytes = b""
    payload: bytes | None = None
    frames: int | None = None
    """Frames in the reassembled packet, set on the fragment that completes it."""
    annotation: str | None = None
    undecodable: str | None = None

    @property
    def has_header(self) -> bool:
        """Whether the full CSP header (source, ports, flags) is known for this frame."""

        return self.source is not None and self.destination_port is not None


FrameObserver = Callable[[FrameRecord, TransportEnvelope], None]
"""Called with every traced frame, after it has crossed the transport boundary.

Observability only, like the trace itself: it must not raise and must not change
delivery, ordering or virtual time.
"""

TRACE_PREFIX = "trace"
"""Leading token of every trace line, so a trace is greppable out of a log."""


class TracingTransportAdapter(TransportAdapter):
    """A transport adapter that logs every frame passing through it.

    ``stream`` receives one line per frame and should be ``stderr`` (or a file)
    rather than ``stdout``, so a trace composes with the CLI's machine-readable
    ``--json`` output instead of corrupting it. It may be ``None`` when only an
    ``observer`` wants the frames (the HTML report): the frames are still decoded
    the same way, there is just nowhere to print them.
    """

    def __init__(
        self,
        inner: TransportAdapter,
        *,
        stream: TextIO | None = None,
        now: VirtualTimeSource | None = None,
        annotate: FrameAnnotator | None = None,
        observer: FrameObserver | None = None,
    ) -> None:
        self._inner = inner
        self._stream = stream
        self._now = now
        self._annotate = annotate
        self._observer = observer
        self._reassemblers = {
            "TX": CspReassembler(max_packet_bytes=None),
            "RX": CspReassembler(max_packet_bytes=None),
        }

    @property
    def inner(self) -> TransportAdapter:
        """The wrapped adapter that actually carries the frames."""

        return self._inner

    def send(
        self,
        frame: Frame,
        *,
        source: EndpointId | None = None,
    ) -> TransportEnvelope:
        """Send through the wrapped adapter, tracing the frame as ``TX``.

        The frame is traced only after the wrapped adapter accepted it, so a
        send that failed does not appear in the trace as if it had reached the
        bus.
        """

        envelope = self._inner.send(frame, source=source)
        self._trace("TX", envelope)
        return envelope

    def receive(
        self,
        *,
        endpoint: EndpointId | None = None,
        timeout: float | None = None,
    ) -> TransportEnvelope | None:
        """Receive through the wrapped adapter, tracing any frame as ``RX``."""

        envelope = self._inner.receive(endpoint=endpoint, timeout=timeout)
        if envelope is not None:
            self._trace("RX", envelope)
        return envelope

    def new_sender(self, *, max_packet_bytes: int = 256) -> PacketSender:
        """Ask the wrapped adapter: how a packet becomes frames depends on the link."""

        return self._inner.new_sender(max_packet_bytes=max_packet_bytes)

    def payload_limit(self, max_packet_bytes: int) -> int:
        """Ask the wrapped adapter: what fits a node's buffer depends on the link."""

        return self._inner.payload_limit(max_packet_bytes)

    def take_drops(self) -> tuple[TransportDrop, ...]:
        """Forward what the wrapped adapter discarded."""

        return self._inner.take_drops()

    def close(self) -> None:
        """Close the wrapped adapter; the trace stream is not owned here."""

        self._inner.close()

    def _trace(self, direction: str, envelope: TransportEnvelope) -> None:
        record = describe_frame(
            direction,
            envelope,
            now=self._now_value(),
            annotate=self._annotate,
            reassembler=self._reassemblers[direction],
        )
        if self._stream is not None:
            self._stream.write(format_frame_record(record))
            self._stream.write("\n")
            self._stream.flush()
        if self._observer is not None:
            self._observer(record, envelope)

    def _now_value(self) -> int | None:
        return None if self._now is None else self._now()


def format_trace_line(
    direction: str,
    envelope: TransportEnvelope,
    *,
    now: int | None = None,
    annotate: FrameAnnotator | None = None,
    reassembler: CspReassembler | None = None,
) -> str:
    """Format one frame as a single ``key=value`` trace line.

    ``reassembler`` is fed every fragment of a multi-frame packet so the line of
    its last frame can show the whole packet; without one, fragments are
    described by their CFP2 fields alone.

    ``data`` is the full CAN data field as a bus analyzer or ``candump`` would
    show it; ``payload`` is just the CSP application bytes that follow the
    4-byte CSP v2 header extension. Both are kept, since comparing the first
    against a capture and reading the second as an application payload are two
    different debugging jobs.
    """

    return format_frame_record(
        describe_frame(direction, envelope, now=now, annotate=annotate, reassembler=reassembler)
    )


def describe_frame(
    direction: str,
    envelope: TransportEnvelope,
    *,
    now: int | None = None,
    annotate: FrameAnnotator | None = None,
    reassembler: CspReassembler | None = None,
) -> FrameRecord:
    """Decode one frame into a :class:`FrameRecord`; never raises on a bad frame."""

    frame = envelope.frame
    if isinstance(frame, CspPacketFrame):
        fields = frame.fields
        return FrameRecord(
            direction=direction,
            kind="packet",
            time=now,
            link=frame.link or "local",
            priority=fields.priority,
            source=fields.source,
            destination=fields.destination,
            destination_port=fields.destination_port,
            source_port=fields.source_port,
            flags=fields.flags,
            data=frame.wire,
            payload=frame.payload,
            annotation=None if annotate is None else annotate(frame),
        )

    can_fields = decode_can_id(frame.can_id) if frame.is_extended_id else None
    base = {
        "direction": direction,
        "time": now,
        "can_id": frame.can_id,
        "dlc": frame.dlc,
        "data": frame.data,
    }
    if can_fields is not None and not (can_fields.begin and can_fields.end):
        return _describe_fragment(
            frame, can_fields, base, now=now, annotate=annotate, reassembler=reassembler
        )

    try:
        packet = _decode_frame(frame)
    except CspV2CodecError as exc:
        return FrameRecord(kind="undecodable", undecodable=str(exc), **base)  # type: ignore[arg-type]

    fields = packet.fields
    return FrameRecord(
        kind="frame",
        priority=fields.priority,
        source=fields.source,
        destination=fields.destination,
        destination_port=fields.destination_port,
        source_port=fields.source_port,
        flags=fields.flags,
        payload=packet.payload,
        annotation=None if annotate is None else annotate(packet),
        **base,  # type: ignore[arg-type]
    )


def _describe_fragment(
    frame: CspCanFrame,
    can_fields: CspCanIdFields,
    base: Mapping[str, object],
    *,
    now: int | None,
    annotate: FrameAnnotator | None,
    reassembler: CspReassembler | None,
) -> FrameRecord:
    """Describe one frame of a multi-frame packet; see the module docstring."""

    position = "begin" if can_fields.begin else "end" if can_fields.end else "more"
    cfp = {
        "cfp": position,
        "packet_count": can_fields.packet_count,
        "frame_count": can_fields.frame_count,
    }
    header: dict[str, object] = {}
    if can_fields.begin:
        try:
            first = decode_first_frame_header(frame)
        except CspV2CodecError as exc:
            return FrameRecord(kind="undecodable", undecodable=str(exc), **base, **cfp)  # type: ignore[arg-type]
        header = {
            "priority": first.priority,
            "source": first.source,
            "destination": first.destination,
            "destination_port": first.destination_port,
            "source_port": first.source_port,
            "flags": first.flags,
        }
    else:
        header = {"destination": can_fields.destination, "sender": can_fields.sender}

    frames: int | None = None
    payload: bytes | None = None
    annotation: str | None = None
    if reassembler is not None:
        for event in reassembler.feed(frame, now=0 if now is None else now):
            packet = event.packet
            if event.kind == "complete" and packet is not None:
                frames = packet.frame_count
                payload = packet.payload
                annotation = None if annotate is None else annotate(packet)
    return FrameRecord(
        kind="fragment",
        frames=frames,
        payload=payload,
        annotation=annotation,
        **base,  # type: ignore[arg-type]
        **cfp,  # type: ignore[arg-type]
        **header,  # type: ignore[arg-type]
    )


def format_frame_record(record: FrameRecord) -> str:
    """Render a :class:`FrameRecord` as the single ``key=value`` ``--trace`` line."""

    parts = [TRACE_PREFIX]
    if record.time is not None:
        parts.append(f"t={record.time}")
    parts.append(record.direction)

    if record.kind == "packet":
        parts.append(f"link={record.link}")
    else:
        parts.append(f"can_id=0x{record.can_id:08X}")
        parts.append(f"dlc={record.dlc}")
    if record.cfp is not None:
        parts.append(f"cfp={record.cfp}")
        parts.append(f"pc={record.packet_count}")
        parts.append(f"fc={record.frame_count}")

    if record.kind == "undecodable":
        parts.append(f"data={_hex(record.data)}")
        parts.append(f"undecodable={record.undecodable!r}")
        return " ".join(parts)

    if record.has_header:
        parts.extend(
            (
                f"pri={record.priority}",
                f"src={record.source}",
                f"dst={record.destination}",
                f"dport={record.destination_port}",
                f"sport={record.source_port}",
                f"flags=0x{record.flags:02X}",
            )
        )
    else:  # a continuation fragment knows only where it is going and who sent it
        parts.append(f"dst={record.destination}")
        parts.append(f"sender={record.sender}")
    parts.append(f"data={_hex(record.data)}")
    if record.frames is not None:
        parts.append(f"frames={record.frames}")
    if record.payload is not None:
        parts.append(f"payload={_hex(record.payload)}")
    if record.annotation:
        parts.append(record.annotation)
    return " ".join(parts)


def _hex(data: bytes) -> str:
    """Render frame bytes as lowercase hex, with a dash for no bytes at all."""

    return data.hex() if data else "-"


__all__ = [
    "TRACE_PREFIX",
    "FrameAnnotator",
    "FrameObserver",
    "FrameRecord",
    "TracingTransportAdapter",
    "VirtualTimeSource",
    "describe_frame",
    "format_frame_record",
    "format_trace_line",
]
