from __future__ import annotations

import json
from pathlib import Path

import pytest

import cubesat_testbed
from cubesat_testbed.config import (
    InMemoryTransportConfig,
    load_scenario,
    load_testbed_config,
)
from cubesat_testbed.examples import (
    DEFAULT_EXAMPLE_NAME,
    Example,
    ExampleError,
    available_examples,
    get_example,
)
from cubesat_testbed.main import main
from cubesat_testbed.scenario import run_scenario_files

EXAMPLE_FILENAMES = ("setup.toml", "scenario.yaml", "README.md")


def _example_ids() -> list[str]:
    return [example.name for example in available_examples()]


@pytest.fixture(params=available_examples(), ids=_example_ids())
def example(request: pytest.FixtureRequest) -> Example:
    return request.param  # type: ignore[no-any-return]


def test_examples_ship_inside_the_installed_package(example: Example) -> None:
    """The examples must live in the package, or a pip install has nothing to run.

    This is the regression guard for the whole feature: moving these files back
    out to a repository directory would silently make the published wheel inert
    again, and every other test here would still pass.
    """

    package_root = Path(cubesat_testbed.__file__).resolve().parent
    for path in example.files():
        assert path.is_relative_to(package_root)
        assert path.is_file()


def test_example_directory_holds_exactly_the_expected_files(example: Example) -> None:
    assert sorted(path.name for path in example.directory.iterdir()) == sorted(EXAMPLE_FILENAMES)
    assert tuple(path.name for path in example.files()) == EXAMPLE_FILENAMES


def test_example_setup_and_scenario_validate_together(example: Example) -> None:
    setup = load_testbed_config(example.setup_path)
    scenario = load_scenario(example.scenario_path, setup=setup)

    assert scenario.steps


def test_in_memory_examples_pass_when_run(example: Example) -> None:
    """Every example that does not need a CAN bus must actually pass."""

    setup = load_testbed_config(example.setup_path)
    if not isinstance(setup.transport, InMemoryTransportConfig):
        pytest.skip(f"example {example.name!r} needs a real bus")

    result = run_scenario_files(example.setup_path, example.scenario_path)

    assert result.assertions
    assert result.passed


def test_example_readme_names_the_example(example: Example) -> None:
    assert example.name in example.readme_path.read_text(encoding="utf-8")


def test_get_example_rejects_an_unknown_name() -> None:
    with pytest.raises(ExampleError) as exc_info:
        get_example("nope")

    message = str(exc_info.value)
    assert "unknown example 'nope'" in message
    for known in available_examples():
        assert known.name in message


def test_copy_to_writes_the_example_verbatim(tmp_path: Path) -> None:
    example = get_example(DEFAULT_EXAMPLE_NAME)
    target = tmp_path / "fresh"

    written = example.copy_to(target)

    assert tuple(path.name for path in written) == EXAMPLE_FILENAMES
    for source, copy in zip(example.files(), written, strict=True):
        assert copy.parent == target
        assert copy.read_bytes() == source.read_bytes()


def test_copied_example_runs_from_its_new_directory(tmp_path: Path) -> None:
    get_example(DEFAULT_EXAMPLE_NAME).copy_to(tmp_path)

    result = run_scenario_files(tmp_path / "setup.toml", tmp_path / "scenario.yaml")

    assert result.passed


def test_copy_to_refuses_to_clobber_and_changes_nothing(tmp_path: Path) -> None:
    example = get_example(DEFAULT_EXAMPLE_NAME)
    (tmp_path / "scenario.yaml").write_text("mine\n", encoding="utf-8")

    with pytest.raises(ExampleError) as exc_info:
        example.copy_to(tmp_path)

    assert "--force" in str(exc_info.value)
    # The refusal is checked before anything is written, so the untouched files
    # of a half-populated directory stay untouched too.
    assert (tmp_path / "scenario.yaml").read_text(encoding="utf-8") == "mine\n"
    assert not (tmp_path / "setup.toml").exists()


def test_copy_to_force_overwrites(tmp_path: Path) -> None:
    example = get_example(DEFAULT_EXAMPLE_NAME)
    (tmp_path / "scenario.yaml").write_text("mine\n", encoding="utf-8")

    example.copy_to(tmp_path, force=True)

    assert (tmp_path / "scenario.yaml").read_bytes() == example.scenario_path.read_bytes()


def test_copy_to_rejects_a_file_target(tmp_path: Path) -> None:
    target = tmp_path / "not-a-directory"
    target.write_text("", encoding="utf-8")

    with pytest.raises(ExampleError):
        get_example(DEFAULT_EXAMPLE_NAME).copy_to(target)


def test_cli_init_writes_the_default_example(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["init", str(tmp_path)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    for filename in EXAMPLE_FILENAMES:
        assert (tmp_path / filename).is_file()
        assert f"wrote {tmp_path / filename}" in captured.out
    assert (
        f"next: cubesat-testbed run --config {tmp_path / 'setup.toml'} "
        f"--scenario {tmp_path / 'scenario.yaml'}" in captured.out
    )


def test_cli_init_defaults_to_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["init"]) == 0

    assert (tmp_path / "setup.toml").is_file()


def test_cli_init_suggests_the_flags_an_example_needs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The socketcan example is unrunnable unpaced, so its next command says so."""

    exit_code = main(["init", str(tmp_path), "--example", "socketcan-hil"])

    assert exit_code == 0
    assert "next: cubesat-testbed run --realtime --config" in capsys.readouterr().out


def test_cli_init_selects_a_named_example(tmp_path: Path) -> None:
    assert main(["init", str(tmp_path), "--example", "module-params"]) == 0

    setup = load_testbed_config(tmp_path / "setup.toml")

    assert setup.nodes["eps"].params["initial_battery_percent"] == 90.0


def test_cli_init_lists_examples_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["init", str(tmp_path), "--list"])

    assert exit_code == 0
    captured = capsys.readouterr()
    for known in available_examples():
        assert known.name in captured.out
        assert known.summary in captured.out
    assert not list(tmp_path.iterdir())


def test_cli_init_rejects_an_unknown_example(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["init", str(tmp_path), "--example", "nope"])

    assert exit_code == 2
    assert "unknown example 'nope'" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_cli_init_refuses_to_clobber(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init", str(tmp_path)]) == 0
    (tmp_path / "setup.toml").write_text("mine\n", encoding="utf-8")
    capsys.readouterr()

    exit_code = main(["init", str(tmp_path)])

    assert exit_code == 2
    assert "--force" in capsys.readouterr().err
    assert (tmp_path / "setup.toml").read_text(encoding="utf-8") == "mine\n"


def test_cli_init_force_overwrites(tmp_path: Path) -> None:
    assert main(["init", str(tmp_path)]) == 0
    (tmp_path / "setup.toml").write_text("mine\n", encoding="utf-8")

    assert main(["init", str(tmp_path), "--force"]) == 0

    expected = get_example(DEFAULT_EXAMPLE_NAME).setup_path.read_bytes()
    assert (tmp_path / "setup.toml").read_bytes() == expected


def test_cli_run_example_passes_without_any_config_paths(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["run", "--example", DEFAULT_EXAMPLE_NAME])

    assert exit_code == 0
    assert "SUMMARY scenario='EPS Low Battery Protection Test'" in capsys.readouterr().out


def test_cli_run_example_rejects_an_unknown_name(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["run", "--example", "nope"])

    assert exit_code == 2
    assert "unknown example 'nope'" in capsys.readouterr().err


def test_cli_run_rejects_example_combined_with_explicit_paths(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["run", "--example", DEFAULT_EXAMPLE_NAME, "-c", "setup.toml"])

    assert exit_code == 2
    assert "--example cannot be combined with --config/--scenario" in capsys.readouterr().err


def test_cli_run_requires_an_example_or_both_paths(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["run", "-c", "setup.toml"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "either --example NAME, or both --config and --scenario" in captured.err
    for known in available_examples():
        assert known.name in captured.err


def test_cli_run_reports_a_missing_input_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    """A bad flag combination is an execution error like any other, JSON included."""

    exit_code = main(["run", "--json"])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False
    assert payload["exit_code"] == 2
    assert payload["error"]["kind"] == "execution_error"
    assert "--example NAME" in payload["error"]["message"]
