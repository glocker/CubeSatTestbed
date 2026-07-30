from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GOLDEN_VECTOR_DIR = Path(__file__).resolve().parent / "golden_vectors"
PINNED_LIBCSP_REPO = "https://github.com/libcsp/libcsp"
PINNED_LIBCSP_TAG = "v2.1"
PINNED_LIBCSP_COMMIT = "48f7fb0"
CLASSIC_CAN_MAX_DLC = 8
EXTENDED_CAN_ID_MAX = 0x1FFFFFFF

_BRACKET_CANDUMP_RE = re.compile(
    r"^"
    r"(?:\((?P<timestamp>\d+(?:\.\d+)?)\)\s+)?"
    r"(?P<interface>\S+)\s+"
    r"(?P<can_id>[0-9A-Fa-f]{3,8})\s+"
    r"\[(?P<dlc>\d{1,2})\]"
    r"(?:\s+(?P<payload>(?:[0-9A-Fa-f]{2}(?:\s+|$))*))?"
    r"$"
)
_HASH_CANDUMP_RE = re.compile(
    r"^"
    r"(?:\((?P<timestamp>\d+(?:\.\d+)?)\)\s+)?"
    r"(?P<interface>\S+)\s+"
    r"(?P<can_id>[0-9A-Fa-f]{3,8})#(?P<payload>[0-9A-Fa-f]*)"
    r"$"
)


class GoldenVectorError(ValueError):
    """Raised when a committed golden-vector fixture cannot be loaded."""


@dataclass(frozen=True)
class CandumpFrame:
    """A single CAN data frame parsed from a committed ``candump`` fixture."""

    interface: str
    can_id: int
    is_extended_id: bool
    dlc: int
    payload: bytes
    raw_line: str
    line_number: int
    timestamp: float | None = None

    @property
    def can_id_hex(self) -> str:
        return f"0x{self.can_id:08X}"

    @property
    def payload_hex(self) -> str:
        return self.payload.hex(" ").upper()


@dataclass(frozen=True)
class GoldenVector:
    """Loaded metadata and CAN frames for one golden-vector fixture pair."""

    name: str
    metadata_path: Path
    capture_path: Path
    metadata: dict[str, Any]
    frames: tuple[CandumpFrame, ...]

    @property
    def first_frame(self) -> CandumpFrame:
        if not self.frames:
            raise GoldenVectorError(f"{self.capture_path} does not contain any CAN frames")
        return self.frames[0]


def discover_golden_vector_metadata(
    vector_dir: Path = GOLDEN_VECTOR_DIR,
) -> tuple[Path, ...]:
    """Return committed golden-vector metadata files in deterministic order."""

    return tuple(sorted(vector_dir.glob("*.meta.toml")))


def load_golden_vectors(vector_dir: Path = GOLDEN_VECTOR_DIR) -> tuple[GoldenVector, ...]:
    """Discover and load all committed golden-vector fixture pairs."""

    return tuple(load_golden_vector(path) for path in discover_golden_vector_metadata(vector_dir))


def load_golden_vector(metadata_path: Path) -> GoldenVector:
    """Load one ``*.meta.toml`` file and its sibling traffic dump."""

    metadata = _load_toml(metadata_path)
    name = _required_metadata_str(metadata, "name", metadata_path)
    capture_name = _required_metadata_str(metadata, "capture", metadata_path)
    capture_path = _sibling_capture_path(metadata_path, capture_name)

    if not capture_path.is_file():
        raise GoldenVectorError(
            f"{metadata_path}: capture file {capture_name!r} does not exist next to metadata"
        )

    return GoldenVector(
        name=name,
        metadata_path=metadata_path,
        capture_path=capture_path,
        metadata=metadata,
        frames=parse_candump_file(capture_path),
    )


def parse_candump_file(path: Path) -> tuple[CandumpFrame, ...]:
    """Parse CAN data frames from a committed ``candump`` text file."""

    frames: list[CandumpFrame] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            frames.append(parse_candump_line(raw_line, line_number=line_number))
        except GoldenVectorError as exc:
            raise GoldenVectorError(f"{path}:{line_number}: {exc}") from exc

    if not frames:
        raise GoldenVectorError(f"{path}: no CAN frames found")

    return tuple(frames)


def parse_candump_line(raw_line: str, *, line_number: int = 1) -> CandumpFrame:
    """Parse the ``candump`` formats used by can-utils for classic CAN frames."""

    line = raw_line.strip()
    bracket_match = _BRACKET_CANDUMP_RE.match(line)
    if bracket_match is not None:
        return _frame_from_bracket_match(bracket_match, raw_line=raw_line, line_number=line_number)

    hash_match = _HASH_CANDUMP_RE.match(line)
    if hash_match is not None:
        return _frame_from_hash_match(hash_match, raw_line=raw_line, line_number=line_number)

    raise GoldenVectorError(f"unsupported candump data-frame syntax: {raw_line!r}")


def parse_payload_hex(value: str) -> bytes:
    """Parse a metadata payload string such as ``"00 04 14 80 55"``."""

    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise GoldenVectorError(f"invalid payload hex string {value!r}") from exc


def parse_can_id_hex(value: str) -> int:
    """Parse a metadata CAN identifier string and enforce the 29-bit range."""

    try:
        can_id = int(value, 16)
    except ValueError as exc:
        raise GoldenVectorError(f"invalid CAN ID hex string {value!r}") from exc

    if can_id > EXTENDED_CAN_ID_MAX:
        raise GoldenVectorError(f"CAN ID {value!r} exceeds 29-bit extended CAN range")

    return can_id


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        metadata = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise GoldenVectorError(f"{path}: invalid TOML: {exc}") from exc

    return dict(metadata)


def _required_metadata_str(metadata: dict[str, Any], key: str, metadata_path: Path) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GoldenVectorError(
            f"{metadata_path}: metadata field {key!r} must be a non-empty string"
        )
    return value


def _sibling_capture_path(metadata_path: Path, capture_name: str) -> Path:
    capture = Path(capture_name)
    if capture.is_absolute() or capture.name != capture_name:
        raise GoldenVectorError(
            f"{metadata_path}: capture field must name a sibling file, got {capture_name!r}"
        )
    return metadata_path.parent / capture_name


def _frame_from_bracket_match(
    match: re.Match[str],
    *,
    raw_line: str,
    line_number: int,
) -> CandumpFrame:
    payload = _parse_spaced_payload(match.group("payload") or "")
    dlc = int(match.group("dlc"))
    _validate_classic_can_payload(payload, dlc=dlc)

    return CandumpFrame(
        interface=match.group("interface"),
        can_id=_parse_can_id_token(match.group("can_id")),
        is_extended_id=_is_extended_can_id_token(match.group("can_id")),
        dlc=dlc,
        payload=payload,
        raw_line=raw_line,
        line_number=line_number,
        timestamp=_parse_timestamp(match.group("timestamp")),
    )


def _frame_from_hash_match(
    match: re.Match[str],
    *,
    raw_line: str,
    line_number: int,
) -> CandumpFrame:
    payload_hex = match.group("payload")
    if len(payload_hex) % 2 != 0:
        raise GoldenVectorError(f"payload hex must contain full bytes: {payload_hex!r}")

    payload = parse_payload_hex(payload_hex)
    dlc = len(payload)
    _validate_classic_can_payload(payload, dlc=dlc)

    return CandumpFrame(
        interface=match.group("interface"),
        can_id=_parse_can_id_token(match.group("can_id")),
        is_extended_id=_is_extended_can_id_token(match.group("can_id")),
        dlc=dlc,
        payload=payload,
        raw_line=raw_line,
        line_number=line_number,
        timestamp=_parse_timestamp(match.group("timestamp")),
    )


def _parse_spaced_payload(value: str) -> bytes:
    value = value.strip()
    if not value:
        return b""
    return parse_payload_hex(value)


def _parse_can_id_token(value: str) -> int:
    can_id = int(value, 16)
    if can_id > EXTENDED_CAN_ID_MAX:
        raise GoldenVectorError(f"CAN ID {value!r} exceeds 29-bit extended CAN range")
    return can_id


def _is_extended_can_id_token(value: str) -> bool:
    return len(value) > 3


def _validate_classic_can_payload(payload: bytes, *, dlc: int) -> None:
    if dlc > CLASSIC_CAN_MAX_DLC:
        raise GoldenVectorError(f"classic CAN frame DLC must be <= 8, got {dlc}")
    if len(payload) != dlc:
        raise GoldenVectorError(f"payload length {len(payload)} does not match DLC {dlc}")


def _parse_timestamp(value: str | None) -> float | None:
    if value is None:
        return None
    return float(value)
