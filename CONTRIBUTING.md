# Contributing

## Tooling

`uv` is the only dependency and environment-management tool for this project.
Do not use `pip`, Poetry, Pipenv, or a second lockfile workflow. Python is
pinned for tooling by `.python-version`. Commit `uv.lock`; CI installs from it
with `uv sync --extra dev --locked`.

```sh
uv sync --extra dev
uv run ruff check .
uv run ruff format .   # formatting; Black is not used
uv run mypy src
uv run pytest
```

CI runs the same commands, plus `uv run ruff format --check .`.

## Examples

The example setups and scenarios live in `src/cubesat_testbed/examples/`, not
in a top-level `configs/` directory, because they are shipped in the wheel:
`run --example` executes them in place and `init` copies them out, so an
installed package has something to run. Each example is a directory holding
exactly `setup.toml`, `scenario.yaml` and `README.md`; adding one means adding
that directory plus an entry in `EXAMPLES` in
`src/cubesat_testbed/examples/__init__.py`. `tests/test_examples.py` then runs
it and requires it to pass, unless it needs a real bus.

## Branch workflow

Do not develop directly on `main`. Create a dedicated branch per feature or
maintenance task, land it as one or more focused, logically grouped commits,
and run the full check list above before merging:

```sh
uv sync --extra dev --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
```

See [`docs/roadmap.md`](docs/roadmap.md) for the current implementation phase
and the recommended feature-branch names for upcoming work.
