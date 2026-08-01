"""Runnable example setups and scenarios shipped inside the wheel.

The examples live in the package rather than in a repository ``configs/``
directory for one reason: after ``pip install cubesat-testbed`` there has to be
something to run. Every example is a directory next to this module holding
exactly three files -- ``setup.toml``, ``scenario.yaml`` and a ``README.md``
explaining what the pair demonstrates -- so ``run --example`` can execute one in
place and ``init`` can copy one out with a single fixed command to follow up
with.

The files are read through ``Path(__file__).parent``: this distribution is
installed unpacked, and real paths are what both the CLI's error messages and
the tests want to print and pass around.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

_EXAMPLES_ROOT = Path(__file__).resolve().parent

SETUP_FILENAME = "setup.toml"
SCENARIO_FILENAME = "scenario.yaml"
README_FILENAME = "README.md"

_EXAMPLE_FILENAMES = (SETUP_FILENAME, SCENARIO_FILENAME, README_FILENAME)


class ExampleError(ValueError):
    """Raised when an example is unknown or cannot be materialised."""


@dataclass(frozen=True, slots=True)
class Example:
    """A named setup/scenario pair shipped with the package."""

    name: str
    summary: str
    run_flags: tuple[str, ...] = ()
    """Extra ``run`` flags this example needs, so a printed command is runnable."""

    @property
    def directory(self) -> Path:
        return _EXAMPLES_ROOT / self.name

    @property
    def setup_path(self) -> Path:
        return self.directory / SETUP_FILENAME

    @property
    def scenario_path(self) -> Path:
        return self.directory / SCENARIO_FILENAME

    @property
    def readme_path(self) -> Path:
        return self.directory / README_FILENAME

    def files(self) -> tuple[Path, ...]:
        """Return the example's files in the order ``copy_to`` writes them."""

        return tuple(self.directory / filename for filename in _EXAMPLE_FILENAMES)

    def copy_to(self, target: Path, *, force: bool = False) -> tuple[Path, ...]:
        """Copy the example into ``target``, creating the directory if needed.

        Refuses to clobber existing files unless ``force`` is set, and checks
        every destination before writing any of them, so a rejected copy leaves
        the target directory exactly as it was.
        """

        destination = Path(target)
        if destination.exists() and not destination.is_dir():
            raise ExampleError(f"{destination} exists and is not a directory")

        written = tuple(destination / filename for filename in _EXAMPLE_FILENAMES)
        if not force:
            existing = [str(path) for path in written if path.exists()]
            if existing:
                raise ExampleError(
                    "refusing to overwrite existing "
                    + ", ".join(existing)
                    + "; pass --force to replace them"
                )

        destination.mkdir(parents=True, exist_ok=True)
        for source, target_file in zip(self.files(), written, strict=True):
            shutil.copyfile(source, target_file)
        return written


DEFAULT_EXAMPLE_NAME = "default"

EXAMPLES: tuple[Example, ...] = (
    Example(
        name=DEFAULT_EXAMPLE_NAME,
        summary="in-memory three-node satellite; OBC sheds the payload on a low battery",
    ),
    Example(
        name="socketcan-hil",
        summary="the same run against a real bus: payload as hardware on SocketCAN vcan0",
        run_flags=("--realtime",),
    ),
    Example(
        name="module-params",
        summary="retuning a built-in module through [nodes.<node>.params]",
    ),
)

_EXAMPLES_BY_NAME = {example.name: example for example in EXAMPLES}


def available_examples() -> tuple[Example, ...]:
    """Return every shipped example, in listing order."""

    return EXAMPLES


def get_example(name: str) -> Example:
    """Return the example called ``name``, or raise :class:`ExampleError`."""

    try:
        return _EXAMPLES_BY_NAME[name]
    except KeyError:
        known = ", ".join(example.name for example in EXAMPLES)
        raise ExampleError(f"unknown example {name!r}; available examples: {known}") from None


__all__ = [
    "DEFAULT_EXAMPLE_NAME",
    "EXAMPLES",
    "README_FILENAME",
    "SCENARIO_FILENAME",
    "SETUP_FILENAME",
    "Example",
    "ExampleError",
    "available_examples",
    "get_example",
]
