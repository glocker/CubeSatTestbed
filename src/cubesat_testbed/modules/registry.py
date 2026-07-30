"""Module-type extension point.

Maps each built-in ``module_type`` string (matching
:class:`cubesat_testbed.config.parser.ModuleType`'s values) to the dataclass
that owns its tunable parameters. ``cubesat_testbed.config.parser`` uses this
registry to validate a node's ``[nodes.<name>.params]`` TOML table by
attempting to construct the target config dataclass and surfacing whatever
error it raises; it does not duplicate that dataclass's own validation rules.

This is also where a third-party module type would register itself so it can
be configured the same way as the built-in ones.
"""

from __future__ import annotations

from cubesat_testbed.modules.eps import GenericEpsConfig
from cubesat_testbed.modules.payload import SimplePayloadConfig

MODULE_PARAM_CONFIGS: dict[str, type] = {
    "generic_eps": GenericEpsConfig,
    "simple_payload": SimplePayloadConfig,
}
"""module_type value -> config dataclass accepting node.params as keywords.

``obc_peer`` is intentionally absent: its configuration is driven by rules and
setup command mappings, not by a flat scalar-parameter table.
"""

__all__ = ["MODULE_PARAM_CONFIGS"]
