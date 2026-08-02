"""Tests for the module-type extension point.

The claim under test is that a module defined outside this package is a
first-class subsystem: nameable in setup TOML, tunable through ``params``,
built into the runtime, ticked, observed over the bus and assertable -- with
no change to this package. Most tests here therefore register a toy module
that lives nowhere but in this file.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from cubesat_testbed.config import ModuleType, load_testbed_config, parse_testbed_config
from cubesat_testbed.engine import CommandPayload, VirtualTime
from cubesat_testbed.main import main
from cubesat_testbed.modules import (
    MODULE_PARAM_CONFIGS,
    GenericEpsModule,
    ModuleBuildContext,
    ModuleError,
    ModuleRegistryError,
    ObcPeerModule,
    SimplePayloadModule,
    SimulatedModule,
    ThermalRcModule,
    module_registration,
    register_module,
    registered_module_types,
    registrations_in_build_order,
    unregister_module,
)
from cubesat_testbed.modules.base import _coerce_finite_float_value, _validate_identifier
from cubesat_testbed.scenario import build_runtime, run_scenario_files

BUILT_IN_MODULE_TYPES = ("generic_eps", "obc_peer", "simple_payload", "thermal_rc")


@dataclass(frozen=True, slots=True)
class WidgetConfig:
    """A third-party module's params object, validated by its own rules."""

    name: str = "widget"
    endpoint: int | None = None
    start_count: float = 0.0
    step: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_identifier("module name", self.name))
        object.__setattr__(self, "step", _coerce_finite_float_value("step", self.step))
        if self.step <= 0.0:
            raise ModuleError("step must be positive")


class WidgetModule(SimulatedModule):
    """A module that counts, defined entirely outside cubesat_testbed."""

    def __init__(self, config: WidgetConfig) -> None:
        self.config = config
        self.count = config.start_count
        super().__init__(config.name, endpoint=config.endpoint)

    def handle_command(
        self,
        command: CommandPayload | str,
        *,
        payload: object = None,
        source: object = None,
        target: object = None,
    ) -> bool:
        self.count = 0.0
        return True

    def tick(self, ticks: int = 1) -> object:
        self.count += self.config.step * ticks
        return self.count

    def telemetry(self, *, now: VirtualTime | None = None) -> dict[str, object]:
        return {"count": self.resolve_signal_value("count", self.count, now=now)}

    # No emit_telemetry: the runner never calls it, and a module that omits it
    # must still have its telemetry encoded onto the bus.


def build_widget(context: ModuleBuildContext) -> WidgetModule:
    return WidgetModule(context.module_config(WidgetConfig))


@pytest.fixture
def widget_module_type() -> Iterator[str]:
    """Register the toy module for one test and always take it back out."""

    register_module("widget", build_widget, config_cls=WidgetConfig, summary="counts")
    try:
        yield "widget"
    finally:
        unregister_module("widget")


WIDGET_SETUP = """
[transport]
type = "in-memory"

[nodes.widget]
mode = "simulated"
module_type = "widget"
address = 7

[nodes.widget.params]
start_count = 10.0
step = 2.5

[nodes.widget.telemetry.count]
source_port = 30
destination_port = 30
offset = 0
length = 4
type = "float"
"""

WIDGET_SCENARIO = """
name: "Third-party module scenario"
steps:
  - action: "wait"
    virtual_time: "4s"
  - action: "assert"
    name: "widget_counted_on_the_bus"
    signal: "widget.telemetry.count"
    op: "=="
    value: 20.0
"""


def test_every_built_in_module_type_is_registered() -> None:
    assert registered_module_types() == BUILT_IN_MODULE_TYPES


def test_module_type_enum_lists_exactly_the_built_in_registrations() -> None:
    """The enum is documentation of what ships, so it must not drift from it.

    It is deliberately not the validation gate -- that is the registry -- but a
    built-in missing from it would make the docs and public constants lie.
    """

    assert {module_type.value for module_type in ModuleType} == set(BUILT_IN_MODULE_TYPES)


def test_registrations_are_ordered_so_dependants_build_after_their_dependencies() -> None:
    order = [entry.module_type for entry in registrations_in_build_order()]

    # The EPS reads the payload it powers, and the OBC observes both.
    assert order.index("simple_payload") < order.index("generic_eps") < order.index("obc_peer")


def test_registering_a_taken_name_is_refused_unless_replacement_is_explicit() -> None:
    with pytest.raises(ModuleRegistryError, match="'generic_eps' is already registered"):
        register_module("generic_eps", build_widget)

    # The refusal leaves the real registration in place.
    assert module_registration("generic_eps").config_cls is not WidgetConfig


def test_replace_swaps_a_built_in_without_touching_setup_files() -> None:
    """Substituting your own EPS for the built-in is a supported use, not a hack."""

    original = module_registration("generic_eps")
    register_module("generic_eps", build_widget, config_cls=WidgetConfig, replace=True)
    try:
        assert module_registration("generic_eps").factory is build_widget
    finally:
        register_module(
            original.module_type,
            original.factory,
            config_cls=original.config_cls,
            build_order=original.build_order,
            summary=original.summary,
            replace=True,
        )

    assert module_registration("generic_eps") == original


def test_unknown_module_type_names_the_registered_ones_and_the_cli_flag() -> None:
    with pytest.raises(ModuleRegistryError) as exc_info:
        module_registration("nope")

    message = str(exc_info.value)
    assert "unknown module_type 'nope'" in message
    for known in BUILT_IN_MODULE_TYPES:
        assert known in message
    assert "--module-import" in message


def test_unregistering_a_module_type_that_is_not_registered_fails() -> None:
    with pytest.raises(ModuleRegistryError, match="'nope' is not registered"):
        unregister_module("nope")


def test_param_configs_view_reflects_the_registry_and_hides_paramless_types() -> None:
    assert MODULE_PARAM_CONFIGS["thermal_rc"] is module_registration("thermal_rc").config_cls
    # obc_peer takes rules and command mappings, not a scalar params table.
    assert "obc_peer" not in MODULE_PARAM_CONFIGS
    with pytest.raises(KeyError):
        MODULE_PARAM_CONFIGS["obc_peer"]
    assert set(MODULE_PARAM_CONFIGS) == set(BUILT_IN_MODULE_TYPES) - {"obc_peer"}


def test_setup_rejects_an_unregistered_module_type() -> None:
    with pytest.raises(ValidationError, match="unknown module_type 'widget'"):
        parse_testbed_config(WIDGET_SETUP)


def test_registered_third_party_module_type_is_accepted_and_tunable(
    widget_module_type: str,
) -> None:
    setup = parse_testbed_config(WIDGET_SETUP)

    assert setup.nodes["widget"].module_type == widget_module_type
    assert setup.nodes["widget"].params == {"start_count": 10.0, "step": 2.5}


def test_third_party_params_are_validated_by_the_modules_own_config(
    widget_module_type: str,
) -> None:
    """The registry defers to the module's dataclass instead of re-checking it."""

    with pytest.raises(ValidationError, match="invalid params for module_type 'widget'"):
        parse_testbed_config(WIDGET_SETUP.replace("step = 2.5", "step = -1.0"))

    with pytest.raises(ValidationError, match="unexpected keyword argument 'nope'"):
        parse_testbed_config(WIDGET_SETUP.replace("step = 2.5", "nope = 1"))


def test_third_party_params_may_not_shadow_the_node_name_or_address(
    widget_module_type: str,
) -> None:
    with pytest.raises(ValidationError, match="params must not set endpoint, name"):
        parse_testbed_config(
            WIDGET_SETUP.replace("step = 2.5", 'step = 2.5\nname = "other"\nendpoint = 9')
        )


def test_runtime_builds_and_ticks_a_third_party_module(widget_module_type: str) -> None:
    runtime = build_runtime(parse_testbed_config(WIDGET_SETUP))
    try:
        widget = runtime.modules["widget"]

        assert isinstance(widget, WidgetModule)
        # name and endpoint came from the node, params from its table.
        assert widget.name == "widget"
        assert widget.endpoint == 7
        assert widget.count == 10.0
        assert runtime.tick_order == ("widget",)
    finally:
        runtime.transport.close()


def test_runtime_attaches_the_fault_engine_a_factory_forgot(widget_module_type: str) -> None:
    """Fault injection must not silently no-op because a factory omitted it.

    ``build_widget`` deliberately does not pass ``fault_engine=``, the easiest
    thing to leave out of a first module. Without the runtime attaching it,
    every override against that node would be quietly ignored and the scenario
    would pass or fail for the wrong reason.
    """

    runtime = build_runtime(parse_testbed_config(WIDGET_SETUP))
    try:
        widget = runtime.modules["widget"]

        assert widget.fault_engine is runtime.fault_engine

        runtime.fault_engine.signal_override("widget.telemetry.count", -1.0)

        assert widget.telemetry()["count"] == -1.0  # type: ignore[attr-defined]
    finally:
        runtime.transport.close()


def test_third_party_module_telemetry_is_observed_off_the_bus(
    widget_module_type: str, tmp_path: Path
) -> None:
    """The end-to-end claim: assertions see a third-party module's real frames."""

    setup_path = tmp_path / "setup.toml"
    scenario_path = tmp_path / "scenario.yaml"
    setup_path.write_text(WIDGET_SETUP, encoding="utf-8")
    scenario_path.write_text(WIDGET_SCENARIO, encoding="utf-8")

    result = run_scenario_files(setup_path, scenario_path)

    assert result.passed
    assert result.assertions[0].actual == pytest.approx(20.0)


def test_built_in_modules_are_built_through_the_same_registry_path() -> None:
    """No privileged construction path for the modules that ship here."""

    setup = load_testbed_config(
        Path(__file__).parent.parent / "src/cubesat_testbed/examples/default/setup.toml"
    )
    runtime = build_runtime(setup)
    try:
        assert isinstance(runtime.modules["payload"], SimplePayloadModule)
        assert isinstance(runtime.modules["eps"], GenericEpsModule)
        assert isinstance(runtime.modules["obc"], ObcPeerModule)
        # The EPS found the payload it powers through the build context.
        assert runtime.modules["eps"].payload is runtime.modules["payload"]  # type: ignore[attr-defined]
        assert runtime.tick_order == ("payload", "eps", "obc")
    finally:
        runtime.transport.close()


def test_thermal_module_is_registered_and_built_from_its_module_type() -> None:
    setup = load_testbed_config(
        Path(__file__).parent.parent / "src/cubesat_testbed/examples/thermal-heater/setup.toml"
    )
    runtime = build_runtime(setup)
    try:
        thermal = runtime.modules["thermal"]

        assert isinstance(thermal, ThermalRcModule)
        assert thermal.config.thermal_capacity_j_per_k == 10.0
    finally:
        runtime.transport.close()


def test_cli_module_import_makes_a_local_module_file_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented CLI path: a my_modules.py next to the setup file.

    A console script puts its own bin directory on sys.path rather than the
    caller's working directory, so this also pins that --module-import adds
    the working directory itself.
    """

    (tmp_path / "my_modules.py").write_text(
        "from tests.test_module_registry import WidgetConfig, build_widget\n"
        "from cubesat_testbed.modules import register_module\n"
        "\n"
        'register_module("widget", build_widget, config_cls=WidgetConfig)\n',
        encoding="utf-8",
    )
    (tmp_path / "setup.toml").write_text(WIDGET_SETUP, encoding="utf-8")
    (tmp_path / "scenario.yaml").write_text(WIDGET_SCENARIO, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    try:
        exit_code = main(
            [
                "run",
                "--module-import",
                "my_modules",
                "-c",
                "setup.toml",
                "-s",
                "scenario.yaml",
            ]
        )
    finally:
        unregister_module("widget")

    assert exit_code == 0


def test_cli_rejects_a_module_import_that_does_not_exist(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        ["run", "--module-import", "no_such_module_xyz", "-c", "setup.toml", "-s", "scenario.yaml"]
    )

    assert exit_code == 2
    assert "--module-import 'no_such_module_xyz' could not be imported" in capsys.readouterr().err


def test_cli_run_without_the_flag_still_reports_an_unknown_module_type(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Forgetting --module-import must produce the error that names the fix."""

    setup_path = tmp_path / "setup.toml"
    scenario_path = tmp_path / "scenario.yaml"
    setup_path.write_text(WIDGET_SETUP, encoding="utf-8")
    scenario_path.write_text(WIDGET_SCENARIO, encoding="utf-8")

    exit_code = main(["run", "-c", str(setup_path), "-s", str(scenario_path)])

    assert exit_code == 2
    captured = capsys.readouterr().err
    assert "unknown module_type 'widget'" in captured
    assert "--module-import" in captured
