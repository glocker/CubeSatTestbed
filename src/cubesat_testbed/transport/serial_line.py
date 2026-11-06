"""Serial line access for the KISS transport, on the standard library alone.

:class:`SerialLine` is the small surface the KISS adapter needs: write bytes, read
whatever has arrived. :class:`TermiosSerialLine` implements it over a POSIX file
descriptor with ``termios`` in raw mode, which is what a real ``/dev/ttyUSB0`` and
a pty pair (used in CI, and to talk to a libcsp process on the same machine) both
are. No ``pyserial`` is needed; the project targets Linux, like the SocketCAN
adapter.
"""

from __future__ import annotations

import os
import select
import termios
from typing import Protocol

from cubesat_testbed.transport.base import TransportError

_BAUD_CONSTANTS = {
    rate: getattr(termios, f"B{rate}")
    for rate in (
        1200,
        2400,
        4800,
        9600,
        19200,
        38400,
        57600,
        115200,
        230400,
        460800,
        500000,
        576000,
        921600,
        1000000,
        1152000,
        1500000,
        2000000,
        2500000,
        3000000,
        3500000,
        4000000,
    )
    if hasattr(termios, f"B{rate}")
}


class SerialLine(Protocol):
    """The byte pipe a KISS adapter talks over."""

    def write(self, data: bytes) -> None:
        """Write all of ``data``."""

    def read(self, timeout: float = 0.0) -> bytes:
        """Return bytes that have arrived, waiting up to ``timeout`` seconds for some."""

    def close(self) -> None:
        """Release the line; safe to call more than once."""


class TermiosSerialLine:
    """A raw 8N1 serial line on a file descriptor."""

    def __init__(self, fd: int, *, owns_fd: bool = True) -> None:
        self._fd = fd
        self._owns_fd = owns_fd
        self._closed = False

    @classmethod
    def open(cls, device: str, *, baudrate: int = 115200) -> TermiosSerialLine:
        """Open ``device`` (a tty or the slave side of a pty) raw at ``baudrate``."""

        if baudrate not in _BAUD_CONSTANTS:
            supported = ", ".join(str(rate) for rate in sorted(_BAUD_CONSTANTS))
            raise TransportError(f"unsupported baudrate {baudrate}; supported: {supported}")
        try:
            fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise TransportError(f"failed to open serial device {device!r}: {exc}") from exc
        try:
            attributes = termios.tcgetattr(fd)
            # iflag, oflag, cflag, lflag, ispeed, ospeed, cc: raw bytes, 8N1, no flow control.
            attributes[0] = 0
            attributes[1] = 0
            attributes[2] = (
                (
                    attributes[2]
                    & ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.CRTSCTS)
                )
                | termios.CS8
                | termios.CREAD
                | termios.CLOCAL
            )
            attributes[3] = 0
            attributes[4] = _BAUD_CONSTANTS[baudrate]
            attributes[5] = _BAUD_CONSTANTS[baudrate]
            attributes[6][termios.VMIN] = 0
            attributes[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attributes)
        except (termios.error, OSError) as exc:
            os.close(fd)
            raise TransportError(f"failed to configure serial device {device!r}: {exc}") from exc
        return cls(fd)

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        try:
            while view:
                try:
                    written = os.write(self._fd, view)
                except BlockingIOError:
                    select.select([], [self._fd], [], 1.0)
                    continue
                view = view[written:]
        except OSError as exc:
            raise TransportError(f"failed to write to the serial line: {exc}") from exc

    def read(self, timeout: float = 0.0) -> bytes:
        try:
            ready, _, _ = select.select([self._fd], [], [], max(timeout, 0.0))
            if not ready:
                return b""
            return os.read(self._fd, 4096)
        except BlockingIOError:
            return b""
        except OSError as exc:
            raise TransportError(f"failed to read from the serial line: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_fd:
            try:
                os.close(self._fd)
            except OSError as exc:
                raise TransportError(f"failed to close the serial line: {exc}") from exc


__all__ = ["SerialLine", "TermiosSerialLine"]
