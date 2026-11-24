"""Known bug 14: signals and commands a receiver cannot tell apart are refused at load time."""

from __future__ import annotations

import io

import pytest
from pydantic import ValidationError

from cubesat_testbed.config import parse_scenario, parse_testbed_config
from cubesat_testbed.scenario import run_scenario

HEAD = """
[transport]
type = "in-memory"

[nodes.obc]
mode = "simulated"
module_type = "obc_peer"
address = 1

[nodes.eps]
mode = "simulated"
module_type = "generic_eps"
address = 2
"""


def _mapping(name: str, *, sport: int = 20, dport: int = 20, to: str | None = None) -> str:
    to_line = "" if to is None else f'to = "{to}"\n'
    return (
        f"\n[nodes.eps.telemetry.{name}]\nsource_port = {sport}\ndestination_port = {dport}\n"
        f'{to_line}offset = 0\nlength = 4\ntype = "float"\n'
    )


def test_two_signals_on_one_port_pair_are_rejected_naming_both() -> None:
    text = HEAD + _mapping("battery_percent") + _mapping("battery_voltage_v")

    with pytest.raises(ValidationError) as excinfo:
        parse_testbed_config(text)

    message = str(excinfo.value)
    assert "eps.battery_percent" in message and "eps.battery_voltage_v" in message
    assert "source_port=20 destination_port=20" in message
    assert "cannot be told apart" in message


def test_the_bug_used_to_abort_the_run_and_now_fails_before_it_starts() -> None:
    text = HEAD + _mapping("battery_percent") + _mapping("battery_voltage_v")

    with pytest.raises(ValidationError):
        run_scenario(
            parse_scenario(
                "name: s\nsteps:\n  - action: wait\n    virtual_time: 1s\n",
            ),
            parse_testbed_config(text),
            output=io.StringIO(),
        )


def test_signals_on_different_ports_are_fine() -> None:
    text = HEAD + _mapping("battery_percent") + _mapping("battery_voltage_v", sport=21, dport=21)

    assert len(parse_testbed_config(text).nodes["eps"].telemetry) == 2


def test_the_same_ports_on_different_nodes_are_fine() -> None:
    text = (
        HEAD
        + _mapping("battery_percent")
        + '\n[nodes.payload]\nmode = "simulated"\nmodule_type = "simple_payload"\naddress = 3\n'
        "\n[nodes.payload.telemetry.power_status]\nsource_port = 20\ndestination_port = 20\n"
        'offset = 0\nlength = 1\ntype = "uint"\n'
    )

    assert parse_testbed_config(text)


def test_the_same_ports_to_different_destinations_are_fine() -> None:
    text = (
        HEAD
        + '\n[nodes.payload]\nmode = "software"\naddress = 3\n'
        + _mapping("to_obc", to="obc")
        + _mapping("to_payload", to="payload")
    )

    assert len(parse_testbed_config(text).nodes["eps"].telemetry) == 2


@pytest.mark.parametrize(
    ("first", "second"),
    [(None, None), (None, "obc"), ("obc", None), ("obc", "obc")],
)
def test_a_mapping_without_to_overlaps_every_destination_and_equal_ones_overlap(
    first: str | None, second: str | None
) -> None:
    text = HEAD + _mapping("a", to=first) + _mapping("b", to=second)

    with pytest.raises(ValidationError, match="cannot be told apart"):
        parse_testbed_config(text)


COMMANDS = """
[nodes.payload]
mode = "simulated"
module_type = "simple_payload"
address = 3

[nodes.obc.commands.payload_power_off]
target = "payload"
destination_port = 10
source_port = 10
payload_hex = "00"
"""


def test_two_commands_with_the_same_bytes_are_rejected() -> None:
    twin = (
        '\n[nodes.obc.commands.payload_power_off_again]\ntarget = "payload"\n'
        'destination_port = 10\nsource_port = 10\npayload_hex = "00"\n'
    )

    with pytest.raises(ValidationError, match="cannot tell them apart") as excinfo:
        parse_testbed_config(HEAD + COMMANDS + twin)

    assert "obc.payload_power_off" in str(excinfo.value)
    assert "obc.payload_power_off_again" in str(excinfo.value)


def test_commands_that_share_ports_but_differ_in_payload_are_fine() -> None:
    other = (
        '\n[nodes.obc.commands.payload_power_on]\ntarget = "payload"\n'
        'destination_port = 10\nsource_port = 10\npayload_hex = "01"\n'
    )

    assert len(parse_testbed_config(HEAD + COMMANDS + other).nodes["obc"].commands) == 2
