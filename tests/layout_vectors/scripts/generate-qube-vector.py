"""Write the QUBE beacon telemetry layout vector and its metadata.

The payload bytes are assembled here with ``struct`` and two hand-picked flag bytes;
the *expected values* come from the Kaitai Struct parser that ``satnogs-decoders``
generates from ``qube.ksy``, an implementation that shares nothing with this project's
signal codec. That parser is the oracle: the testbed must decode these bytes to the
same values and encode those values back to the same bytes.

``satnogs-decoders`` is not a project dependency. Run this in a throwaway environment:

    python3 -m venv /tmp/oracle && /tmp/oracle/bin/pip install satnogs-decoders==1.132.1
    /tmp/oracle/bin/python -I tests/layout_vectors/scripts/generate-qube-vector.py tests/layout_vectors
"""

from __future__ import annotations

import struct
import sys
from importlib.metadata import version
from io import BytesIO
from pathlib import Path

from kaitaistruct import KaitaiStream
from satnogsdecoders.decoder.qube import Qube

NAME = "qube_telemetry_payload"

STATUS_FLAG_BYTES = bytes([0b10110101, 0b01101011])
"""Two flag bytes chosen by hand so every bit-field width (1, 2, 3) sees both 0s and 1s."""


def payload() -> bytes:
    return b"".join(
        (
            struct.pack("<I", 10_000),  # beacon_rate [ms]
            struct.pack("<I", 1_234_567),  # uptime [s]
            STATUS_FLAG_BYTES,
            struct.pack("<bb", 21, 87),  # powerpath A temp [degC], soc [%]
            struct.pack("<bb", -7, 64),  # powerpath B
            struct.pack("<hhh", 7_412, -350, -2_594),  # powerpath A u [mV], i [mA], p [mW]
            struct.pack("<hhh", 7_398, 120, 887),  # powerpath B
            struct.pack("<i", -1_707),  # power_total [mW]
            struct.pack("<b", 18),  # temperature_obc [degC]
            struct.pack("<bbbbb", 34, -12, 5, -40, 127),  # temperature_panel x+, x-, y+, y-, z-
            struct.pack("<I", 435_525_000),  # frequency_radio [Hz]
            struct.pack("<BB", 3, 250),  # seu_rom, seu_ram
        )
    )


def expected(data: bytes) -> dict[str, object]:
    beacon = Qube.TelemetryPayload(KaitaiStream(BytesIO(data)))
    flags = beacon.status_flags
    panel = beacon.temperature_panel
    values: dict[str, object] = {
        "beacon_rate": beacon.beacon_rate,
        "uptime": beacon.uptime,
    }
    for name in (
        "blank_012",
        "aocs_active",
        "radio_2_active",
        "powerpath_b_active",
        "powerpath_a_active",
        "is_mcu_b_active",
        "blank_mode",
        "panel_xneg_active",
        "panel_xpos_active",
        "panel_yneg_active",
        "panel_ypos_active",
        "panel_zneg_active",
        "blank_15",
    ):
        values[name] = int(getattr(flags, name))  # Kaitai returns b1 as bool
    for side, temp_soc, iup in (
        ("a", beacon.powerpath_a_temp_soc, beacon.powerpath_a_iup),
        ("b", beacon.powerpath_b_temp_soc, beacon.powerpath_b_iup),
    ):
        values[f"powerpath_{side}_temp"] = temp_soc.temp
        values[f"powerpath_{side}_soc"] = temp_soc.soc
        values[f"powerpath_{side}_u"] = iup.u
        values[f"powerpath_{side}_i"] = iup.i
        values[f"powerpath_{side}_p"] = iup.p
    values["power_total"] = beacon.power_total
    values["temperature_obc"] = beacon.temperature_obc
    # temperature_panel is a struct of five s1 in qube.ksy; the testbed describes the
    # same five bytes as one array field (count = 5).
    values["temperature_panel"] = [panel.xpos, panel.xneg, panel.ypos, panel.yneg, panel.zneg]
    values["frequency_radio"] = beacon.frequency_radio
    values["seu_rom"] = beacon.seu_rom
    values["seu_ram"] = beacon.seu_ram
    return values


def toml_value(value: object) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return str(value)


def main(out_dir: str) -> None:
    target = Path(out_dir)
    data = payload()
    values = expected(data)
    (target / f"{NAME}.txt").write_text(data.hex(" ") + "\n", encoding="utf-8")
    lines = [
        f'name = "{NAME}"',
        f'capture = "{NAME}.txt"',
        'source = "satnogs-decoders qube.ksy, type telemetry_payload (42 bytes)"',
        'oracle = "satnogsdecoders.decoder.qube.Qube.TelemetryPayload (Kaitai Struct generated)"',
        f'satnogs_decoders_version = "{version("satnogs-decoders")}"',
        f'kaitaistruct_version = "{version("kaitaistruct")}"',
        (
            'command = "python -I tests/layout_vectors/scripts/generate-qube-vector.py '
            'tests/layout_vectors"'
        ),
        (
            'meaning = "QUBE (Wuerzburg, CSP v1 on the radio link) beacon housekeeping: '
            "little-endian integers, two bytes of MSB-first bit flags, five panel temperatures "
            'as an array."'
        ),
        f"payload_length = {len(data)}",
        "",
        "[expected]",
    ]
    lines.extend(f"{name} = {toml_value(value)}" for name, value in values.items())
    (target / f"{NAME}.meta.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1])
