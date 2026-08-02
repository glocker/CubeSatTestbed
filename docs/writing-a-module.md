# Writing a module

The core idea of this project is a universal *engine*, not universal subsystem
*models*. No `generic_eps` will ever match your EPS board, so the first serious
thing you will do here is write your own module. This document is how.

Everything below is the same path the modules shipped in this package take.
There is no privileged internal API: `thermal_rc` is registered by the same
public `register_module` call your module makes, and `build_runtime` cannot
tell the difference.

A module you write gets, for free:

- a `module_type` name usable in setup TOML;
- per-node tuning through `[nodes.<node>.params]`, validated before a run
  starts;
- telemetry encoded onto the CSP bus and decoded back for assertions;
- named commands routed to it over that bus;
- the passive Fault Injection Engine's state overrides, signal overrides and
  named faults;
- OBC Peer rules that react to its telemetry.

The worked example is `src/cubesat_testbed/modules/thermal.py` plus the
`thermal-heater` example (`cubesat-testbed init --example thermal-heater`).
Read them alongside this document; everything here is visible there.

## 1. What a module is

A module is an **isolated finite state machine over virtual time**. It owns
some state, advances that state when told to, answers commands, and reports
telemetry. That is the whole contract.

What a module must never do:

- **call `sleep()` or read the wall clock.** Virtual time is the source of
  truth. A module that waits on real time destroys the determinism the whole
  test framework rests on.
- **run its own loop or thread.** The runner drives every module from the
  discrete-event engine.
- **reach into another module's Python state at runtime.** Cross-module
  behaviour goes over the bus (see §7), or through an explicit reference
  handed to your factory at construction (see §5).
- **be non-deterministic.** No `random` without a seeded generator you own, no
  iteration over unordered sets in a way that reaches output, no dependence on
  dictionary insertion order you did not fix yourself. The same setup and
  scenario must produce byte-identical results on every machine, every run.

There is no abstract base class to inherit from with abstract methods to fill
in. The runner discovers what your module can do by duck typing, so you
implement only the parts that make sense:

| Method | Called when | Required |
| --- | --- | --- |
| `tick(ticks: int = 1)` | once per virtual second, to advance the model | if your model changes over time |
| `telemetry(*, now=None) -> dict[str, object]` | before telemetry frames are encoded | if the node publishes telemetry |
| `emit_telemetry(engine, *, names=None, delay=0, source=None)` | never by the runner; by Python-API callers scheduling samples straight onto the engine | no |
| `handle_command(command, *, payload=None, source=None, target=None) -> bool` | when a command frame is routed to the node | if the node accepts commands |

Subclass `SimulatedModule` (`modules/base.py`) to get the helpers the rest of
this document uses — `model_path`, `telemetry_path`, `resolve_model_value`,
`resolve_signal_value`, `command_targets_module`, `schedule_telemetry_sample`.
It is a helper base, not a framework: it has no `__init_subclass__` magic and
does not register anything.

## 2. Config object and `params`

Physical parameters belong in a frozen dataclass, not in your module's body.
That dataclass *is* the `[nodes.<node>.params]` schema: the config layer
validates a node's params by attempting to construct it and surfacing whatever
it raises. You write your validation once, and setup files get it for free.

```python
from dataclasses import dataclass

from cubesat_testbed.modules.base import (
    ModuleError,
    _coerce_non_negative_float_value,
    _coerce_positive_float_value,
    _validate_identifier,
    _validate_optional_endpoint,
)


@dataclass(frozen=True, slots=True)
class MyEpsConfig:
    name: str = "eps"
    endpoint: int | None = None
    battery_capacity_wh: float = 20.0
    shunt_limit_w: float = 12.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_identifier("module name", self.name))
        object.__setattr__(
            self, "endpoint", _validate_optional_endpoint("module endpoint", self.endpoint)
        )
        object.__setattr__(
            self,
            "battery_capacity_wh",
            _coerce_positive_float_value("battery_capacity_wh", self.battery_capacity_wh),
        )
        if self.shunt_limit_w > self.battery_capacity_wh:
            raise ModuleError("shunt_limit_w must not exceed battery_capacity_wh")
```

Rules that matter:

- **`name` and `endpoint` are mandatory fields** with those exact names. The
  runtime passes the node's own name and CSP address into them, and setup
  validation rejects a `params` table that tries to set either.
- **Validate in `__post_init__` and raise `ModuleError` or `ValueError`.** Both
  are turned into a setup-validation failure that names your parameter, before
  any scenario runs. An invalid config should be impossible to construct, not
  merely unlikely to be used.
- **Validate relationships, not just ranges.** The pair check above, and
  `ThermalRcConfig`'s refusal of an integration step longer than the node's
  time constant, are the interesting ones: they catch configs that are
  individually plausible and jointly meaningless.
- The `_coerce_*` helpers in `modules/base.py` are private to the package but
  stable and reused by every built-in; borrow them or write your own.

Registering with `config_cls=None` instead means the module type takes no
`params` at all — that is how `obc_peer`, configured entirely by rules and
command mappings, is registered.

## 3. State and the model/telemetry split

Keep raw mutable state in its own dataclass and expose *effective* values as
properties. The split is what makes fault injection work, and it is a real
distinction rather than a style preference:

- **`<node>.model.<field>`** is internal physical state. A `state_override`
  fault replaces it, and the model keeps integrating from the replaced value.
- **`<node>.telemetry.<field>`** is what the node reports. A `signal_override`
  fault spoofs the reported value while the model underneath carries on
  unaffected — a lying sensor, not a changed physical reality.

```python
@dataclass(slots=True)
class MyEpsState:
    battery_percent: float


class MyEpsModule(SimulatedModule):
    def __init__(self, config: MyEpsConfig, *, fault_engine=None) -> None:
        self.config = config
        self.state = MyEpsState(battery_percent=100.0)
        super().__init__(config.name, endpoint=config.endpoint, fault_engine=fault_engine)

    @property
    def battery_percent(self) -> float:
        resolved = self.resolve_model_value("battery_percent", self.state.battery_percent)
        return _coerce_percentage_value(self.model_path("battery_percent"), resolved)

    def telemetry(self, *, now=None) -> dict[str, object]:
        values = {"battery_percent": self.battery_percent}
        return {
            field: self.resolve_signal_value(field, value, now=now)
            for field, value in values.items()
        }
```

Two details are load-bearing. First, `tick` reads the *property*, not
`self.state`, so an active state override actually steers the model. Second,
the resolved value is re-validated: an override arrives from a scenario file
and is not to be trusted just because it came from the fault engine.

For named faults, define the flag names as module constants and check them
with `self.fault_engine.is_module_fault_active(self.name, flag)`. A named fault
is a *capability your module declares* — `heater_stuck_off` means something
only because the thermal module decided it does. The fault engine stays
passive: it records that the flag is set and nothing more.

Keep every threshold decision out of the module. "Temperature below 0 °C ⇒ turn
the heater on" is an OBC Peer rule in config, not an `if` in your `tick`. The
module models what the hardware does; the OBC decides what should be done.

## 4. Telemetry and the wire

The strongest guarantee in this project is that an assertion only ever sees a
value that was encoded into CSP bytes, put on the bus, and decoded back out.
Your module's `telemetry()` return values are the input to that encoding, not a
shortcut around it.

So every telemetry field you expose needs a wire layout in setup TOML:

```toml
[nodes.thermal.telemetry.temperature_c]
source_port = 22
destination_port = 22
units = "degC"
offset = 0
length = 4
type = "float"
```

The v1 signal codec is byte-aligned and scalar-only (`uint`, `int`, `float`,
big/little endian, `scale`, `offset_value`). Two consequences for module
authors:

- **A continuously varying quantity should be a `float` field.** A scaled
  integer requires the physical value to land on an exact raw integer, which a
  drifting battery percentage or temperature will not do.
- **A non-numeric value needs an `enum` table** mapping raw integers to
  labels. This is why the thermal module reports `heater_status` as `"on"` /
  `"off"` rather than a bare `bool`: the label is what the enum maps, and it
  reads correctly in an assertion and in a `--trace` line.

A field your module exposes but no setup file maps is simply never
transmitted; it costs nothing, and a signal that *is* mapped but never emitted
fails its assertion with "was never observed on the bus" rather than silently
passing.

## 5. The factory and the registry

A factory turns one configured node into one module instance. It receives a
`ModuleBuildContext` and returns the module:

```python
from cubesat_testbed.modules.registry import ModuleBuildContext


def build_my_eps(context: ModuleBuildContext) -> MyEpsModule:
    return MyEpsModule(
        context.module_config(MyEpsConfig),
        fault_engine=context.fault_engine,
    )
```

`context.module_config(MyEpsConfig)` builds your config from the node's
`params` plus its name and address. Pass `fault_engine=context.fault_engine`
too: a module without it resolves every override to the unmodified value, so
fault injection would quietly do nothing. (`build_runtime` attaches the
runtime's engine to a module that has none, so forgetting it is not fatal —
but a module built directly in a unit test will not get that safety net.)

The context also carries `setup`,
`node_name`, `node`, `engine`, `transport`, and `modules` — the modules built
so far, for the rare case where yours genuinely needs another one:

```python
payload = context.first_module_of_type(SimplePayloadModule)
```

That is how the built-in EPS finds the payload whose rail it switches. Prefer
not to: two modules coupled in Python are two modules that cannot be swapped
for real hardware independently. The loop through the bus (§7) keeps that
property.

Register at import time of the module that defines it:

```python
from cubesat_testbed.modules.registry import BUILD_ORDER_INDEPENDENT, register_module

register_module(
    "my_eps",
    build_my_eps,
    config_cls=MyEpsConfig,
    build_order=BUILD_ORDER_INDEPENDENT,
    summary="EPS model for our board",
)
```

`build_order` decides both construction order and per-tick order — they are
the same dependency seen twice. Three constants are provided and spaced so you
can slot between them:

| Constant | Value | For |
| --- | --- | --- |
| `BUILD_ORDER_INDEPENDENT` | 0 | reads no other module (`simple_payload`, `thermal_rc`) |
| `BUILD_ORDER_CONSUMER` | 10 | built from another module's instance (`generic_eps`) |
| `BUILD_ORDER_SUPERVISOR` | 20 | observes the others (`obc_peer`) |

Nodes sharing a `build_order` keep their setup-file order, so a run stays
reproducible from the config alone.

Passing `replace=True` takes over a name that is already registered. That is
the supported way to substitute your own EPS for the built-in `generic_eps`
across a pile of existing setup files without editing any of them.

## 6. Making the runtime see it

Registration happens on import, and `module_type` is validated when the setup
config is parsed. Your module must therefore be imported *before* the config is
read.

From the CLI:

```sh
cubesat-testbed run --module-import my_modules -c setup.toml -s scenario.yaml
```

`--module-import` is repeatable and puts the working directory on `sys.path`
first, so a plain `my_modules.py` sitting next to `setup.toml` works — you do
not have to package anything to try an idea. Once your models live in an
installed package, pass its import path instead.

Forgetting the flag is not a mysterious failure: the config error names your
`module_type`, lists the registered ones, and points at the flag.

From Python, importing is enough:

```python
import my_modules  # noqa: F401  -- registers the module type

from cubesat_testbed.scenario import run_scenario_files

result = run_scenario_files("setup.toml", "scenario.yaml")
```

## 7. Closing the loop

A module on its own is a physics model. It becomes a testbed subsystem when
its telemetry drives decisions and those decisions come back as commands. Wire
that through configuration, not Python:

```toml
[nodes.obc.commands.thermal_heater_on]
target = "thermal"
destination_port = 11
source_port = 11
payload_hex = "01"

[nodes.obc.rules.heater_on_when_cold]
signal = "thermal.telemetry.temperature_c"
op = "<"
threshold = 0.0

[[nodes.obc.rules.heater_on_when_cold.actions]]
type = "send_command"
command = "thermal_heater_on"
```

Your module's `handle_command` sees that command after it has crossed the bus
and been decoded. Two conventions worth keeping:

- **Return `False`, do not raise, when the command targets another module.**
  `command_targets_module(request)` decides that.
- **Raise `ModuleCommandError` for a command that targets you but is not
  yours.** A typo in a setup file should fail the run loudly rather than being
  ignored on the wire.

The payoff is that switching this node to real hardware is a config change —
`mode = "hardware"` and a SocketCAN transport — with nothing in the OBC's rules
or the scenario to update.

## 8. Checklist

Before you call a module done:

- [ ] The config dataclass rejects every physically meaningless value,
      including invalid *combinations*.
- [ ] `tick` is a pure function of previous state: `m.tick(25)` equals 25
      calls to `m.tick()`.
- [ ] Model state is reachable through `.model.` paths and telemetry through
      `.telemetry.` paths, with overrides applied and re-validated — and the
      factory passes `fault_engine=context.fault_engine`.
- [ ] Named fault flags are declared as constants and documented.
- [ ] Every telemetry field has a wire layout, and non-numeric ones an `enum`.
- [ ] Unknown commands aimed at your node raise; commands aimed elsewhere
      return `False`.
- [ ] A scenario asserts your telemetry *off the bus* and passes.
- [ ] No threshold logic in the module — that belongs to OBC Peer rules.

`tests/test_modules_thermal.py` is a model for the unit tests, and
`tests/test_module_registry.py` builds, registers and runs a module defined
entirely outside the package — the shortest complete example of everything
above.

## Contributing a module upstream

A module that is genuinely general (a second thermal topology, an ADCS model,
a magnetorquer) is welcome in the package. Add it under
`src/cubesat_testbed/modules/`, register it in
`registry.py`'s `_register_built_in_modules`, add its name to `ModuleType`, and
ship a packaged example that demonstrates it end to end. The built-ins are
registered from a single place only so that importing the registry resolves
every shipped name — that is the entire difference between them and yours.

## See also

- [`docs/architecture.md`](architecture.md) — where modules sit among the
  layers.
- [`configs/schema/module_schema.md`](../configs/schema/module_schema.md) —
  the full setup/scenario schema, including `params`, telemetry layouts and
  OBC rules.
- [`docs/v1-scope.md`](v1-scope.md) — what v1 deliberately leaves out, so you
  know which limits are decisions rather than gaps.
