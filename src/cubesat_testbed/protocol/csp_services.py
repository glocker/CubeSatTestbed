"""libcsp's standard services as a simulated node answers them (``csp_service_handler``).

Protocol-version neutral on purpose: a service request and its reply are CSP packets whose
payload means the same in v1 and v2, and this module never touches a header or a frame.
It computes the reply *payload* only; the runner addresses it back to the requester on the
request's ports, with its priority and flags, as ``csp_sendto_reply(..., CSP_O_SAME)`` does.
"""

from __future__ import annotations

from collections.abc import Mapping

CSP_SERVICE_FLAG_CRC32 = 0x01
"""``CSP_FCRC32``, the same bit in CSP v1 and v2."""

CRC32_BYTES = 4

UINT32_MAX = 0xFFFFFFFF


class ServiceRequestError(ValueError):
    """A service request the simulated node cannot answer (reported as a drop)."""


def _crc32c_table() -> tuple[int, ...]:
    table = []
    for index in range(256):
        value = index
        for _ in range(8):
            value = (value >> 1) ^ 0x82F63B78 if value & 1 else value >> 1
        table.append(value)
    return tuple(table)


_CRC32C_TABLE = _crc32c_table()


def crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli) as libcsp's ``csp_crc32_memory`` computes it."""

    value = 0xFFFFFFFF
    for byte in data:
        value = _CRC32C_TABLE[(value ^ byte) & 0xFF] ^ (value >> 8)
    return value ^ 0xFFFFFFFF


def strip_crc32(payload: bytes, flags: int) -> bytes:
    """The request's data without its CRC32 trailer when the CRC32 flag is set.

    The trailer is not checked: a corrupted packet is the bus's business (and the bus
    fault injector's), and libcsp would have dropped it before the service handler.
    """

    if not flags & CSP_SERVICE_FLAG_CRC32:
        return payload
    if len(payload) < CRC32_BYTES:
        raise ServiceRequestError(
            f"the CRC32 flag is set but the payload has only {len(payload)} bytes"
        )
    return payload[:-CRC32_BYTES]


def append_crc32(data: bytes, flags: int) -> bytes:
    """``data`` with a CRC32 trailer when the reply carries the CRC32 flag.

    libcsp ``v2.1`` verifies a CRC32 over the header and payload first and over the payload
    alone second; the payload-only form is accepted and is the same in CSP v1 and v2.
    """

    if not flags & CSP_SERVICE_FLAG_CRC32:
        return data
    return data + crc32c(data).to_bytes(CRC32_BYTES, "big")


def service_reply_data(
    service: str, request_data: bytes, *, uptime_s: int, values: Mapping[str, int]
) -> bytes:
    """The reply data libcsp's service handler sends for ``service``.

    ``ping`` echoes the request; ``uptime`` is seconds since start and ``memfree`` and
    ``buf_free`` the configured numbers, each a 4-byte big-endian unsigned integer.
    """

    if service == "ping":
        return request_data
    if service == "uptime":
        return (uptime_s & UINT32_MAX).to_bytes(4, "big")
    if service in ("memfree", "buf_free"):
        return values[service].to_bytes(4, "big")
    raise ServiceRequestError(f"unknown service {service!r}")


def decode_uint32_reply(data: bytes) -> int:
    """The number in an ``uptime``/``memfree``/``buf_free`` reply."""

    if len(data) != 4:
        raise ServiceRequestError(f"expected a 4-byte reply, got {len(data)} bytes")
    return int.from_bytes(data, "big")


__all__ = [
    "CSP_SERVICE_FLAG_CRC32",
    "ServiceRequestError",
    "append_crc32",
    "crc32c",
    "decode_uint32_reply",
    "service_reply_data",
    "strip_crc32",
]
