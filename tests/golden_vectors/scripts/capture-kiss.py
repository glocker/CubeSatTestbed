#!/usr/bin/env python3
"""Capture the KISS frames libcsp writes to a serial line, for golden vectors.

Usage: capture-kiss.py OUTPUT FRAMES -- COMMAND [ARG...]

Opens a pty pair, replaces the literal ``{tty}`` in COMMAND's arguments with the
slave device path, runs COMMAND (the repository-owned ``csp_client -k {tty}``),
and records what libcsp's KISS interface writes to the line. This is the serial
counterpart of ``candump -n N vcan0``: the libcsp binary is the source of truth
and nothing in the capture is synthesised here.

OUTPUT gets one line per KISS frame: the exact bytes on the wire, FEND to FEND
inclusive, as space-separated upper-case hex (``C0 00 ... C0``). The script
stops after FRAMES frames and fails if COMMAND exits, or ``--timeout`` seconds
pass, before that many arrive.

Standard library only: the vector container is a plain python image.
"""

from __future__ import annotations

import argparse
import os
import pty
import select
import subprocess
import sys
import time

FEND = 0xC0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output")
    parser.add_argument("frames", type=int)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.frames < 1:
        parser.error("need FRAMES >= 1 and a COMMAND after --")

    master, slave = pty.openpty()
    tty_path = os.ttyname(slave)
    process = subprocess.Popen(
        [part.replace("{tty}", tty_path) for part in command],
        stdout=subprocess.DEVNULL,
    )

    frames: list[bytes] = []
    current = bytearray()
    deadline = time.monotonic() + args.timeout
    try:
        while len(frames) < args.frames and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if not ready:
                if process.poll() is not None and not select.select([master], [], [], 0.2)[0]:
                    break
                continue
            for byte in os.read(master, 4096):
                if byte == FEND:
                    if current:
                        current.append(FEND)
                        frames.append(bytes(current))
                        current = bytearray()
                    else:
                        current = bytearray([FEND])
                elif current:
                    current.append(byte)
    finally:
        process.wait()
        os.close(master)
        os.close(slave)

    if len(frames) != args.frames:
        print(f"expected {args.frames} KISS frame(s), captured {len(frames)}", file=sys.stderr)
        return 1
    with open(args.output, "w", encoding="utf-8") as handle:
        handle.writelines(frame.hex(" ").upper() + "\n" for frame in frames)
    return 0


if __name__ == "__main__":
    sys.exit(main())
