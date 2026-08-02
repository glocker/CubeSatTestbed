from __future__ import annotations

from cubesat_testbed.config import parse_testbed_config
from cubesat_testbed.dut.manager import ParticipantKind, resolve_participants


def test_resolve_participants_maps_each_node_to_its_configured_mode() -> None:
    setup = parse_testbed_config(
        """
        [transport]
        type = "socketcan"
        interface = "vcan0"

        [nodes.obc]
        mode = "simulated"
        module_type = "obc_peer"
        address = 1

        [nodes.eps]
        mode = "software"
        address = 2

        [nodes.payload]
        mode = "hardware"
        address = 3
        """
    )

    participants = resolve_participants(setup)

    assert participants["obc"].kind is ParticipantKind.SIMULATED
    assert participants["obc"].is_simulated
    assert not participants["obc"].is_external

    assert participants["eps"].kind is ParticipantKind.SOFTWARE
    assert not participants["eps"].is_simulated
    assert participants["eps"].is_external

    assert participants["payload"].kind is ParticipantKind.HARDWARE
    assert not participants["payload"].is_simulated
    assert participants["payload"].is_external


def test_resolve_participants_carries_address_and_module_type() -> None:
    setup = parse_testbed_config(
        """
        [transport]
        type = "in-memory"

        [nodes.eps]
        mode = "simulated"
        module_type = "generic_eps"
        address = 5
        """
    )

    participant = resolve_participants(setup)["eps"]

    assert participant.address == 5
    # A plain registered name, not an enum member: the module registry decides
    # which module types exist, so a third-party one arrives here unchanged.
    assert participant.module_type == "generic_eps"


def test_resolve_participants_leaves_module_type_unset_for_external_nodes() -> None:
    setup = parse_testbed_config(
        """
        [transport]
        type = "socketcan"
        interface = "vcan0"

        [nodes.payload]
        mode = "hardware"
        address = 3
        """
    )

    participant = resolve_participants(setup)["payload"]

    assert participant.module_type is None
