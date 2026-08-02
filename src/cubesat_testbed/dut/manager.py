"""DUT/node mode orchestration.

The manager maps each configured node to one of the supported modes:
``simulated``, ``software`` or ``hardware``. Switching the DUT is a config
change, not a code change: nothing downstream (the runtime builder, the
runner) re-derives this mapping itself, it consults
:func:`resolve_participants` once.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cubesat_testbed.config import NodeMode, TestbedConfig


class ParticipantKind(str, Enum):
    """Mirrors :class:`cubesat_testbed.config.NodeMode`'s values.

    A distinct enum from ``NodeMode`` on purpose: this one is the DUT
    manager's own vocabulary for "who owns this node's behavior", read by
    callers that should not need to import the config schema layer just to
    ask "is this node simulated".
    """

    SIMULATED = "simulated"
    SOFTWARE = "software"
    HARDWARE = "hardware"


@dataclass(frozen=True, slots=True)
class NodeParticipant:
    """One configured node's DUT role."""

    name: str
    kind: ParticipantKind
    address: int
    module_type: str | None
    """Registered module-type name, set only when ``kind is SIMULATED``.

    A plain string, like ``NodeConfig.module_type`` it mirrors, because the
    module registry rather than a closed enum decides which types exist.
    """

    @property
    def is_simulated(self) -> bool:
        """Whether this testbed process owns a running model for this node."""

        return self.kind is ParticipantKind.SIMULATED

    @property
    def is_external(self) -> bool:
        """Whether this node is reached only through the transport.

        True for both ``software`` and ``hardware``: neither is simulated
        locally, so both are addressed and observed purely as CSP-over-CAN
        traffic on the configured transport.
        """

        return not self.is_simulated


def resolve_participants(setup: TestbedConfig) -> dict[str, NodeParticipant]:
    """Map each configured node to its :class:`NodeParticipant`.

    This is the single place that decides which nodes the runtime should
    build a simulated module for and which it should only ever reach over the
    bus. Setup validation already guarantees ``hardware`` nodes only occur
    with ``transport.type = "socketcan"``
    (:meth:`TestbedConfig._validate_transport_mode_combination`); ``software``
    nodes are valid on either transport, since an in-memory "external" peer is
    a normal way to test against a hand-driven or scripted bus participant
    without a real CAN interface.
    """

    return {
        node_name: NodeParticipant(
            name=node_name,
            kind=ParticipantKind(node.mode.value),
            address=node.address,
            module_type=node.module_type,
        )
        for node_name, node in setup.nodes.items()
    }


def _validate_node_mode_matches_participant_kind() -> None:
    # NodeMode and ParticipantKind must stay in lockstep; this assertion fails
    # loudly at import time (not silently at some later lookup) if either enum
    # is extended without updating the other.
    if {mode.value for mode in NodeMode} != {kind.value for kind in ParticipantKind}:
        raise AssertionError("NodeMode and ParticipantKind have diverged")


_validate_node_mode_matches_participant_kind()


__all__ = ["NodeParticipant", "ParticipantKind", "resolve_participants"]
