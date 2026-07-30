"""SocketCAN transport adapter for Linux HIL paths.

The adapter uses ``python-can`` with the Linux ``socketcan`` backend and keeps the
same synchronous transport boundary as the deterministic in-memory bus. The
receive side is non-blocking: it performs one immediate SocketCAN poll and
returns ``None`` when no frame is queued.
"""

from __future__ import annotations

import math
import re
from types import TracebackType
from typing import Protocol, Self, cast

import can

from cubesat_testbed.protocol.csp_v2 import CspCanFrame, CspV2CodecError
from cubesat_testbed.transport.base import (
    EndpointId,
    TransportAdapter,
    TransportEndpointError,
    TransportEnvelope,
    TransportError,
)

_INTERFACE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class CanBusLike(Protocol):
    """Subset of ``python-can`` bus behavior used by :class:`SocketCanAdapter`."""

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        """Send one CAN message through the bus."""

    def recv(self, timeout: float | None = None) -> can.Message | None:
        """Return one received CAN message, or ``None`` when no message is available."""

    def shutdown(self) -> None:
        """Release bus resources."""


class SocketCanAdapter(TransportAdapter):
    """Linux SocketCAN adapter for CSP-over-CAN HIL tests.

    ``interface`` is the Linux CAN network interface name, for example ``vcan0``
    in Docker/local loopback setups or ``can0``/``can1`` for physical CAN
    adapters. Endpoint-specific receive queues are not available on SocketCAN;
    callers should use the default receive stream and decode/filter frames at
    the protocol layer.
    """

    def __init__(
        self,
        *,
        interface: str = "vcan0",
        receive_own_messages: bool = False,
        send_timeout: float | None = 0.0,
        bus: CanBusLike | None = None,
    ) -> None:
        self.interface = _validate_interface(interface)
        self.receive_own_messages = _validate_bool("receive_own_messages", receive_own_messages)
        self.send_timeout = _validate_optional_timeout("send_timeout", send_timeout)
        self._bus = (
            bus
            if bus is not None
            else _open_socketcan_bus(
                self.interface,
                receive_own_messages=self.receive_own_messages,
            )
        )
        self._next_sequence = 0
        self._closed = False

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def send(
        self,
        frame: CspCanFrame,
        *,
        source: EndpointId | None = None,
    ) -> TransportEnvelope:
        """Send one extended classic CAN frame through SocketCAN."""

        self._ensure_open()
        validated_source = None if source is None else _validate_endpoint(source)
        if not frame.is_extended_id:
            raise TransportError("SocketCAN CSP v2 path requires extended 29-bit CAN identifiers")

        message = _message_from_frame(frame, channel=self.interface)
        try:
            self._bus.send(message, timeout=self.send_timeout)
        except (can.CanError, OSError) as exc:
            raise TransportError(
                f"failed to send CAN frame on SocketCAN interface {self.interface!r}: {exc}"
            ) from exc
        return self._envelope(frame, source=validated_source)

    def receive(self, *, endpoint: EndpointId | None = None) -> TransportEnvelope | None:
        """Poll the default SocketCAN receive stream once without blocking."""

        self._ensure_open()
        if endpoint is not None:
            _validate_endpoint(endpoint)
            raise TransportEndpointError(
                "SocketCAN exposes only the default receive stream; "
                "endpoint-specific receive queues are not available"
            )

        try:
            message = self._bus.recv(timeout=0.0)
        except (can.CanError, OSError) as exc:
            raise TransportError(
                f"failed to receive CAN frame from SocketCAN interface {self.interface!r}: {exc}"
            ) from exc
        if message is None:
            return None

        frame = _frame_from_message(message)
        return self._envelope(frame, source=None)

    def close(self) -> None:
        """Shutdown the underlying ``python-can`` bus."""

        if self._closed:
            return
        try:
            self._bus.shutdown()
        except (can.CanError, OSError) as exc:
            raise TransportError(
                f"failed to close SocketCAN interface {self.interface!r}: {exc}"
            ) from exc
        finally:
            self._closed = True

    def shutdown(self) -> None:
        """Alias for :meth:`close`, matching ``python-can`` naming."""

        self.close()

    @property
    def closed(self) -> bool:
        """Return whether this adapter has been closed."""

        return self._closed

    def _envelope(
        self,
        frame: CspCanFrame,
        *,
        source: EndpointId | None,
    ) -> TransportEnvelope:
        envelope = TransportEnvelope(frame=frame, sequence=self._next_sequence, source=source)
        self._next_sequence += 1
        return envelope

    def _ensure_open(self) -> None:
        if self._closed:
            raise TransportError(f"SocketCAN interface {self.interface!r} is already closed")


def _open_socketcan_bus(interface: str, *, receive_own_messages: bool) -> CanBusLike:
    try:
        return cast(
            CanBusLike,
            can.Bus(
                channel=interface,
                interface="socketcan",
                receive_own_messages=receive_own_messages,
                ignore_config=True,
            ),
        )
    except (can.CanError, OSError) as exc:
        raise TransportError(f"failed to open SocketCAN interface {interface!r}: {exc}") from exc


def _message_from_frame(frame: CspCanFrame, *, channel: str) -> can.Message:
    try:
        return can.Message(
            arbitration_id=frame.can_id,
            is_extended_id=frame.is_extended_id,
            is_remote_frame=False,
            is_error_frame=False,
            channel=channel,
            dlc=frame.dlc,
            data=frame.data,
            is_fd=False,
            is_rx=False,
            check=True,
        )
    except ValueError as exc:
        raise TransportError(
            f"CSP CAN frame cannot be converted to a SocketCAN message: {exc}"
        ) from exc


def _frame_from_message(message: can.Message) -> CspCanFrame:
    if message.is_error_frame:
        raise TransportError("SocketCAN error frames are outside the CSP v2 HIL path")
    if message.is_remote_frame:
        raise TransportError("SocketCAN remote frames are outside the CSP v2 HIL path")
    if message.is_fd:
        raise TransportError("CAN FD frames are outside the classic CAN 2.0 v1 profile")
    if not message.is_extended_id:
        raise TransportError("SocketCAN CSP v2 path requires extended 29-bit CAN identifiers")

    try:
        return CspCanFrame(
            can_id=message.arbitration_id,
            data=bytes(message.data),
            is_extended_id=True,
        )
    except CspV2CodecError as exc:
        raise TransportError(
            f"received SocketCAN frame is outside the CSP v2 profile: {exc}"
        ) from exc


def _validate_interface(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("SocketCAN interface must be a string")
    if not _INTERFACE_RE.fullmatch(value):
        raise ValueError(
            "SocketCAN interface must start with a letter and contain only letters, "
            "numbers, '_' or '-'"
        )
    return value


def _validate_bool(name: str, value: bool) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a bool")
    return value


def _validate_optional_timeout(name: str, value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a non-negative number or None")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return timeout


def _validate_endpoint(endpoint: EndpointId) -> EndpointId:
    if isinstance(endpoint, bool):
        raise TransportEndpointError("endpoint id must be a non-bool string or integer")
    if isinstance(endpoint, str):
        if endpoint == "":
            raise TransportEndpointError("endpoint id must not be an empty string")
        return endpoint
    if isinstance(endpoint, int):
        if endpoint < 0:
            raise TransportEndpointError("integer endpoint id must be non-negative")
        return endpoint
    raise TransportEndpointError("endpoint id must be a string or integer")


__all__ = ["CanBusLike", "SocketCanAdapter"]
