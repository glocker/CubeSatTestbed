"""Module-type extension point.

A ``module_type`` string in setup TOML (``[nodes.<node>] module_type = ...``)
is resolved here, not by a closed enum: this registry maps each name to the
dataclass owning its tunable ``[nodes.<node>.params]`` and to the factory
:func:`cubesat_testbed.scenario.runner.build_runtime` calls to construct it.
Both the built-in modules and a third-party one go through
:func:`register_module`; there is no second, privileged path for the modules
that ship in this package.

``cubesat_testbed.config.parser`` validates a node's ``module_type`` against
the registered names and its ``params`` table by attempting to construct the
target config dataclass, surfacing whatever error it raises rather than
duplicating that dataclass's own validation rules.

Registration happens on import, so a third-party module type must be imported
before the setup config is parsed -- from Python by importing the package, or
from the CLI with ``cubesat-testbed run --module-import PACKAGE``. See
``docs/writing-a-module.md`` for the full walkthrough.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeAlias, TypeVar, cast

from cubesat_testbed.modules.base import SimulatedModule, _validate_identifier

if TYPE_CHECKING:
    from cubesat_testbed.config.parser import NodeConfig, TestbedConfig
    from cubesat_testbed.engine import DiscreteEventEngine
    from cubesat_testbed.fault_injection import FaultInjectionEngine
    from cubesat_testbed.modules.obc_peer import ObcPeerRule
    from cubesat_testbed.transport.base import TransportAdapter

_ModuleT = TypeVar("_ModuleT", bound=SimulatedModule)
_ConfigT = TypeVar("_ConfigT")

BUILD_ORDER_INDEPENDENT = 0
"""Build/tick order for a module that reads no other module's state."""

BUILD_ORDER_CONSUMER = 10
"""Build/tick order for a module built from another module's instance."""

BUILD_ORDER_SUPERVISOR = 20
"""Build/tick order for a module observing the others, such as the OBC Peer."""


class ModuleRegistryError(ValueError):
    """Raised for an unknown, duplicate or malformed module-type registration."""


@dataclass(frozen=True, slots=True)
class ModuleBuildContext:
    """Everything a module factory may need to construct one configured node.

    A factory receives this instead of a long argument list so that adding a
    runtime object later does not break every registered factory, including
    third-party ones outside this repository.
    """

    setup: TestbedConfig
    node_name: str
    node: NodeConfig
    engine: DiscreteEventEngine
    fault_engine: FaultInjectionEngine
    transport: TransportAdapter
    modules: Mapping[str, SimulatedModule]
    """Modules already built for this runtime, keyed by node name.

    Only modules whose registration has a lower ``build_order`` are guaranteed
    to be present; that ordering is the contract a factory reading another
    module relies on.
    """

    obc_rules: Mapping[str, tuple[ObcPeerRule, ...]] | None = None
    """Rule-set override passed to ``build_runtime``, keyed by OBC node name.

    Only the ``obc_peer`` factory reads this; every other factory ignores it.
    """

    @property
    def address(self) -> int:
        """CSP address configured for this node, used as its transport endpoint."""

        return self.node.address

    @property
    def params(self) -> dict[str, Any]:
        """The node's ``[nodes.<node>.params]`` table.

        Already validated against this module type's config dataclass by
        ``NodeConfig``, which is why the factory can splat it in directly.
        """

        return cast("dict[str, Any]", self.node.params)

    def module_config(self, config_cls: Callable[..., _ConfigT]) -> _ConfigT:
        """Build this node's module config: its params plus name and endpoint.

        Typed as a callable rather than ``type[...]`` because the config
        dataclass's own keyword signature is what matters here, and ``params``
        is a dynamic table that no static signature can describe.

        ``name`` and ``endpoint`` come from the node itself and are rejected
        inside ``params`` by config validation, so they can never collide here.
        """

        return config_cls(name=self.node_name, endpoint=self.node.address, **self.params)

    def first_module_of_type(self, module_type: type[_ModuleT]) -> _ModuleT | None:
        """Return the first already-built module of ``module_type``, if any.

        Product v1 wires cross-module references (EPS to the payload whose
        rail it switches) by type rather than by name, so a two-node setup
        needs no extra configuration. Returns ``None`` when no such module is
        configured, which every built-in caller treats as "nothing attached"
        rather than an error.
        """

        for module in self.modules.values():
            if isinstance(module, module_type):
                return module
        return None


ModuleFactory: TypeAlias = Callable[[ModuleBuildContext], SimulatedModule]


@dataclass(frozen=True, slots=True)
class ModuleRegistration:
    """One registered ``module_type`` and everything the runtime needs for it."""

    module_type: str
    factory: ModuleFactory
    config_cls: type | None = None
    """Dataclass accepting ``params`` as keywords, or ``None`` to reject params.

    ``obc_peer`` is the built-in with ``None``: its configuration is driven by
    rules and command mappings, not by a flat scalar-parameter table.
    """

    build_order: int = BUILD_ORDER_INDEPENDENT
    """Lower builds first -- and, in the same order, ticks first.

    Construction order and tick order are the same number because they express
    the same dependency: a module built from another's instance also has to
    observe that module's state after it has advanced. Nodes sharing a
    ``build_order`` keep their setup-file order. The three ``BUILD_ORDER_*``
    constants are spaced so a third-party module can slot between them.
    """

    summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "module_type", _validate_identifier("module_type", self.module_type)
        )
        if not callable(self.factory):
            raise ModuleRegistryError(f"module_type {self.module_type!r} factory must be callable")
        if self.config_cls is not None and not isinstance(self.config_cls, type):
            raise ModuleRegistryError(
                f"module_type {self.module_type!r} config_cls must be a class or None"
            )


_REGISTRY: dict[str, ModuleRegistration] = {}


def register_module(
    module_type: str,
    factory: ModuleFactory,
    *,
    config_cls: type | None = None,
    build_order: int = BUILD_ORDER_INDEPENDENT,
    summary: str = "",
    replace: bool = False,
) -> ModuleRegistration:
    """Register ``module_type`` so setup configs can name it.

    Call this at import time of the package that defines the module. Pass
    ``replace=True`` to deliberately take over a name that is already
    registered -- the way to substitute your own EPS model for the built-in
    ``generic_eps`` without touching existing setup files.
    """

    registration = ModuleRegistration(
        module_type=module_type,
        factory=factory,
        config_cls=config_cls,
        build_order=build_order,
        summary=summary,
    )
    if not replace and registration.module_type in _REGISTRY:
        raise ModuleRegistryError(
            f"module_type {registration.module_type!r} is already registered; "
            "pass replace=True to override it deliberately"
        )
    _REGISTRY[registration.module_type] = registration
    return registration


def unregister_module(module_type: str) -> None:
    """Remove a registered module type.

    Mainly for tests, which must not leak a registration into the next test.
    """

    try:
        del _REGISTRY[module_type]
    except KeyError:
        raise ModuleRegistryError(f"module_type {module_type!r} is not registered") from None


def module_registration(module_type: str) -> ModuleRegistration:
    """Return the registration for ``module_type`` or raise with the known names."""

    try:
        return _REGISTRY[module_type]
    except KeyError:
        raise ModuleRegistryError(
            f"unknown module_type {module_type!r}; registered module types: "
            f"{', '.join(registered_module_types())}. A module type defined outside this "
            "package must be imported before the setup config is parsed -- from the CLI "
            "with 'run --module-import PACKAGE'."
        ) from None


def registered_module_types() -> tuple[str, ...]:
    """Return every registered ``module_type`` name, sorted for stable messages."""

    return tuple(sorted(_REGISTRY))


def registrations_in_build_order() -> tuple[ModuleRegistration, ...]:
    """Return every registration sorted by ``build_order``, registration order last."""

    return tuple(sorted(_REGISTRY.values(), key=lambda entry: entry.build_order))


class _ParamConfigsView(Mapping[str, type]):
    """Live read-only ``module_type -> config dataclass`` view of the registry.

    Kept because it is this package's documented 1.0 name for that mapping.
    Module types registered with ``config_cls=None`` (``obc_peer``) are absent,
    matching the 1.0 behaviour where a missing key means "does not take params".
    """

    def __getitem__(self, key: str) -> type:
        config_cls = _REGISTRY[key].config_cls
        if config_cls is None:
            raise KeyError(key)
        return config_cls

    def __iter__(self) -> Iterator[str]:
        return (
            module_type for module_type, entry in _REGISTRY.items() if entry.config_cls is not None
        )

    def __len__(self) -> int:
        return sum(1 for entry in _REGISTRY.values() if entry.config_cls is not None)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({dict(self)!r})"


MODULE_PARAM_CONFIGS: Mapping[str, type] = _ParamConfigsView()
"""module_type -> config dataclass accepting node.params as keywords.

A compatibility view over the registry; :func:`register_module` is what
actually adds an entry.
"""


def _register_built_in_modules() -> None:
    """Register the module types shipped in this package.

    Done here rather than in each module file so that importing this registry
    is enough to resolve every built-in name -- config parsing imports only
    this module. A third-party package instead calls ``register_module`` at
    its own import time, which is why nothing below is privileged: these are
    ordinary registrations of ordinary factories.
    """

    from cubesat_testbed.modules.eps import GenericEpsConfig, build_generic_eps
    from cubesat_testbed.modules.obc_peer import build_obc_peer
    from cubesat_testbed.modules.payload import SimplePayloadConfig, build_simple_payload
    from cubesat_testbed.modules.thermal import ThermalRcConfig, build_thermal_rc

    register_module(
        "simple_payload",
        build_simple_payload,
        config_cls=SimplePayloadConfig,
        build_order=BUILD_ORDER_INDEPENDENT,
        summary="basic power draw and command/data-volume behavior",
    )
    register_module(
        "thermal_rc",
        build_thermal_rc,
        config_cls=ThermalRcConfig,
        build_order=BUILD_ORDER_INDEPENDENT,
        summary="single-node RC thermal model with a commandable heater",
    )
    register_module(
        "generic_eps",
        build_generic_eps,
        config_cls=GenericEpsConfig,
        build_order=BUILD_ORDER_CONSUMER,
        summary="battery/load/power-mode behavior and EPS telemetry",
    )
    register_module(
        "obc_peer",
        build_obc_peer,
        config_cls=None,
        build_order=BUILD_ORDER_SUPERVISOR,
        summary="stateless rule-engine mock OBC",
    )


_register_built_in_modules()


__all__ = [
    "BUILD_ORDER_CONSUMER",
    "BUILD_ORDER_INDEPENDENT",
    "BUILD_ORDER_SUPERVISOR",
    "MODULE_PARAM_CONFIGS",
    "ModuleBuildContext",
    "ModuleFactory",
    "ModuleRegistration",
    "ModuleRegistryError",
    "module_registration",
    "register_module",
    "registered_module_types",
    "registrations_in_build_order",
    "unregister_module",
]
