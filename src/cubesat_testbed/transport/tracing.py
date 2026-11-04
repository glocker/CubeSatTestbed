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

Tracing is observability only. It never alters delivery, ordering or virtual
time, and it never raises: a frame the codec cannot decode is reported as such,
because unexpected traffic is often the reason a trace was turned on.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TextIO

from cubesat_testbed.protocol.csp_v2 import (
    CspCanFrame,
    CspCanIdFields,
    CspReassembler,
    CspV2CodecError,
    DecodedCspPacket,
    decode_can_id,
    decode_first_frame_header,
)
from cubesat_testbed.protocol.csp_v2 import decode_frame as _decode_frame
from cubesat_testbed.transport.base import EndpointId, TransportAdapter, TransportEnvelope

FrameAnnotator = Callable[[DecodedCspPacket], str | None]
"""Describe a decoded frame in terms of the configured route it matches.

Returns a trailing annotation for the trace line (for example
``telemetry eps.telemetry.battery_percent=95.0``), or ``None`` to leave the
frame described by its header fields alone. Must not raise.
"""

VirtualTimeSource = Callable[[], int]
"""Return the current virtual time, in microseconds, for the trace timestamp."""

TRACE_PREFIX = "trace"
"""Leading token of every trace line, so a trace is greppable out of a log."""


class TracingTransportAdapter(TransportAdapter):
    """A transport adapter that logs every frame passing through it.

    ``stream`` receives one line per frame and should be ``stderr`` (or a file)
    rather than ``stdout``, so a trace composes with the CLI's machine-readable
    ``--json`` output instead of corrupting it.
    """

    def __init__(
        self,
        inner: TransportAdapter,
        *,
        stream: TextIO,
        now: VirtualTimeSource | None = None,
        annotate: FrameAnnotator | None = None,
    ) -> None:
        self._inner = inner
        self._stream = stream
        self._now = now
        self._annotate = annotate
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
        frame: CspCanFrame,
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

    def close(self) -> None:
        """Close the wrapped adapter; the trace stream is not owned here."""

        self._inner.close()

    def _trace(self, direction: str, envelope: TransportEnvelope) -> None:
        self._stream.write(
            format_trace_line(
                direction,
                envelope,
                now=self._now_value(),
                annotate=self._annotate,
                reassembler=self._reassemblers[direction],
            )
        )
        self._stream.write("\n")
        self._stream.flush()

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

    frame = envelope.frame
    parts = [TRACE_PREFIX]
    if now is not None:
        parts.append(f"t={now}")
    parts.append(direction)
    parts.append(f"can_id=0x{frame.can_id:08X}")
    parts.append(f"dlc={frame.dlc}")

    can_fields = decode_can_id(frame.can_id) if frame.is_extended_id else None
    if can_fields is not None and not (can_fields.begin and can_fields.end):
        return _format_fragment_line(
            parts, frame, can_fields, now=now, annotate=annotate, reassembler=reassembler
        )

    try:
        packet = _decode_frame(frame)
    except CspV2CodecError as exc:
        parts.append(f"data={_hex(frame.data)}")
        parts.append(f"undecodable={str(exc)!r}")
        return " ".join(parts)

    fields = packet.fields
    parts.extend(
        (
            f"pri={fields.priority}",
            f"src={fields.source}",
            f"dst={fields.destination}",
            f"dport={fields.destination_port}",
            f"sport={fields.source_port}",
            f"flags=0x{fields.flags:02X}",
            f"data={_hex(frame.data)}",
            f"payload={_hex(packet.payload)}",
        )
    )

    annotation = None if annotate is None else annotate(packet)
    if annotation:
        parts.append(annotation)
    return " ".join(parts)


def _format_fragment_line(
    parts: list[str],
    frame: CspCanFrame,
    can_fields: CspCanIdFields,
    *,
    now: int | None,
    annotate: FrameAnnotator | None,
    reassembler: CspReassembler | None,
) -> str:
    """Describe one frame of a multi-frame packet; see the module docstring."""

    position = "begin" if can_fields.begin else "end" if can_fields.end else "more"
    parts.append(f"cfp={position}")
    parts.append(f"pc={can_fields.packet_count}")
    parts.append(f"fc={can_fields.frame_count}")

    if can_fields.begin:
        try:
            header = decode_first_frame_header(frame)
        except CspV2CodecError as exc:
            parts.append(f"data={_hex(frame.data)}")
            parts.append(f"undecodable={str(exc)!r}")
            return " ".join(parts)
        parts.extend(
            (
                f"pri={header.priority}",
                f"src={header.source}",
                f"dst={header.destination}",
                f"dport={header.destination_port}",
                f"sport={header.source_port}",
                f"flags=0x{header.flags:02X}",
            )
        )
    else:
        parts.append(f"dst={can_fields.destination}")
        parts.append(f"sender={can_fields.sender}")
    parts.append(f"data={_hex(frame.data)}")

    if reassembler is not None:
        for event in reassembler.feed(frame, now=0 if now is None else now):
            packet = event.packet
            if event.kind == "complete" and packet is not None:
                parts.append(f"frames={packet.frame_count}")
                parts.append(f"payload={_hex(packet.payload)}")
                annotation = None if annotate is None else annotate(packet)
                if annotation:
                    parts.append(annotation)
    return " ".join(parts)


def _hex(data: bytes) -> str:
    """Render frame bytes as lowercase hex, with a dash for no bytes at all."""

    return data.hex() if data else "-"


__all__ = [
    "TRACE_PREFIX",
    "FrameAnnotator",
    "TracingTransportAdapter",
    "VirtualTimeSource",
    "format_trace_line",
]
