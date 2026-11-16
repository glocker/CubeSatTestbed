"""The HTML report: structure, determinism, evidence links and the CLI flag."""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

import cubesat_testbed.scenario.runner as runner_module
from cubesat_testbed.config import load_scenario, load_testbed_config, parse_scenario
from cubesat_testbed.main import main
from cubesat_testbed.report import RunRecorder, describe_step, format_virtual_time, render_report
from cubesat_testbed.scenario import run_scenario
from tests.example_paths import DEFAULT_SCENARIO, DEFAULT_SETUP
from tests.test_multi_frame_runtime import SCENARIO as MULTI_SCENARIO
from tests.test_multi_frame_runtime import _setup_text as multi_setup_text

FAILING = """
name: Deliberately failing scenario
steps:
  - action: inject_fault
    type: state_override
    target: eps.model.battery_percent
    value: 25
    duration: 5s
  - action: wait
    virtual_time: 3s
  - action: assert
    signal: eps.telemetry.battery_percent
    op: "=="
    value: 25.0
    timeout: 1s
  - action: assert
    signal: eps.telemetry.battery_percent
    op: "=="
    value: 99.0
    timeout: 1s
"""

_VOID = {"meta", "link", "br", "hr", "img", "input", "col", "area", "base", "source", "wbr"}


def _record(setup_path: Path = DEFAULT_SETUP, scenario_text: str | None = None):  # type: ignore[no-untyped-def]
    setup = load_testbed_config(setup_path)
    scenario = (
        load_scenario(DEFAULT_SCENARIO, setup=setup)
        if scenario_text is None
        else parse_scenario(scenario_text, setup=setup)
    )
    recorder = RunRecorder()
    result = run_scenario(scenario, setup, output=io.StringIO(), recorder=recorder)
    return recorder, result


class _Rows(HTMLParser):
    """Collects the report's timeline rows, anchors and script, and checks tag balance."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.rows: dict[str, dict[str, str]] = {}
        self.links: list[str] = []
        self.script = ""
        self._in_script = False
        self._row: str | None = None
        self._row_text: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {k: (v or "") for k, v in attrs}
        if tag not in _VOID:
            self.stack.append(tag)
        if tag == "script":
            self._in_script = True
        if tag == "a" and attributes.get("href", "").startswith("#"):
            self.links.append(attributes["href"][1:])
        if tag == "tr" and "data-kind" in attributes:
            self._row = attributes.get("id", f"anon{len(self.rows)}")
            self.rows[self._row] = {**attributes, "text": ""}
            self._row_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False
        if tag in _VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}> (open: {self.stack[-3:]})")
        else:
            self.stack.pop()
        if tag in {"td", "th"} and self._row is not None:
            self._row_text.append(" ")  # cells read as separate words, as on screen
        if tag == "tr" and self._row is not None:
            self.rows[self._row]["text"] = " ".join("".join(self._row_text).split())
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self.script += data
        if self._row is not None:
            self._row_text.append(data)


def _parse(report: str) -> _Rows:
    parser = _Rows()
    parser.feed(report)
    parser.close()
    return parser


# --- structure -------------------------------------------------------------------------


def test_a_passing_run_renders_a_well_formed_self_contained_page() -> None:
    recorder, result = _record()

    report = render_report(recorder, result)
    parsed = _parse(report)

    assert report.startswith("<!DOCTYPE html>") and report.endswith("</html>\n")
    assert parsed.errors == [] and parsed.stack == []
    assert ">PASS</span>" in report and "EPS Low Battery Protection Test" in report
    # Nothing is fetched at view time: no external URL, stylesheet link, import or font.
    assert not re.search(r"https?://", report)
    assert "<link" not in report and "@import" not in report and "url(" not in report
    assert "src=" not in report


def test_the_report_is_byte_identical_across_repeats() -> None:
    reports = []
    for _ in range(2):
        recorder, result = _record()
        reports.append(render_report(recorder, result))

    assert reports[0] == reports[1]
    assert not re.search(r"\b20\d\d-\d\d-\d\d", reports[0])  # no wall-clock date sneaks in


def test_recording_does_not_change_the_run() -> None:
    setup = load_testbed_config(DEFAULT_SETUP)
    scenario = load_scenario(DEFAULT_SCENARIO, setup=setup)

    plain = run_scenario(scenario, setup, output=io.StringIO())
    recorded = run_scenario(scenario, setup, output=io.StringIO(), recorder=RunRecorder())

    assert recorded == plain


# --- the point of the feature: an assertion leads to its frame -------------------------


def test_a_failed_assertion_links_to_the_frame_carrying_the_value_it_failed_on() -> None:
    recorder, result = _record(scenario_text=FAILING)
    parsed = _parse(render_report(recorder, result))

    assert not result.passed
    failed = [a for a in recorder.assertions if not a.result.passed]
    assert len(failed) == 1 and failed[0].evidence_frame is not None
    row = parsed.rows[f"f{failed[0].evidence_frame}"]
    # The linked row is the RX of the telemetry frame whose decoded value was 25.0, not 99.0.
    assert row["data-dir"] == "RX"
    assert "telemetry eps.telemetry.battery_percent=25.0" in row["text"]
    assert f"f{failed[0].evidence_frame}" in parsed.links


def test_a_passing_assertion_links_to_the_frame_that_satisfied_it() -> None:
    recorder, result = _record()
    (assertion,) = recorder.assertions

    assert result.passed and assertion.evidence_frame is not None
    parsed = _parse(render_report(recorder, result))
    row = parsed.rows[f"f{assertion.evidence_frame}"]
    assert "telemetry payload.telemetry.power_status='offline'" in row["text"]


def test_every_evidence_link_points_at_a_real_row() -> None:
    recorder, result = _record(scenario_text=FAILING)
    parsed = _parse(render_report(recorder, result))

    assert parsed.links and all(target in parsed.rows for target in parsed.links)


def test_a_signal_never_seen_on_the_bus_has_no_link_and_says_so() -> None:
    scenario = """
name: Never observed
steps:
  - action: wait
    virtual_time: 1s
  - action: assert
    signal: payload.telemetry.power_status
    op: "=="
    value: offline
    timeout: 1s
"""
    setup_text = DEFAULT_SETUP.read_text(encoding="utf-8")
    setup_text = setup_text.replace(
        'mode = "simulated"\nmodule_type = "simple_payload"', 'mode = "software"'
    )
    path = DEFAULT_SETUP.parent / "never_seen_setup.toml"
    try:
        path.write_text(setup_text, encoding="utf-8")
        recorder, result = _record(setup_path=path, scenario_text=scenario)
    finally:
        path.unlink(missing_ok=True)

    (assertion,) = recorder.assertions
    report = render_report(recorder, result)
    assert not result.passed and assertion.evidence_frame is None
    assert "signal never seen on the bus" in report


# --- other content ---------------------------------------------------------------------


def test_a_multi_frame_packet_shows_each_fragment_and_the_reassembled_one() -> None:
    from cubesat_testbed.config import parse_testbed_config

    setup = parse_testbed_config(multi_setup_text())
    scenario = parse_scenario(MULTI_SCENARIO, setup=setup)
    recorder = RunRecorder()
    result = run_scenario(scenario, setup, output=io.StringIO(), recorder=recorder)

    parsed = _parse(render_report(recorder, result))
    battery = [a for a in recorder.assertions if a.result.signal == "eps.telemetry.battery_percent"]

    assert result.passed and battery[0].evidence_frame is not None
    last = parsed.rows[f"f{battery[0].evidence_frame}"]
    first = parsed.rows[f"f{battery[0].evidence_frame - 1}"]
    assert "reassembled 2 frames" in last["text"] and "cfp=end" in last["text"]
    assert "cfp=begin" in first["text"] and "reassembled" not in first["text"]
    assert "partial" in first["class"]


def test_dropped_packets_and_scenario_steps_are_on_the_timeline() -> None:
    from cubesat_testbed.config import parse_testbed_config

    text = multi_setup_text("max_packet_bytes = 8")
    setup = parse_testbed_config(text)
    scenario = parse_scenario(MULTI_SCENARIO, setup=setup)
    recorder = RunRecorder()
    result = run_scenario(scenario, setup, output=io.StringIO(), recorder=recorder)

    report = render_report(recorder, result)
    parsed = _parse(report)
    kinds = [row["data-kind"] for row in parsed.rows.values()]

    assert result.dropped_packets
    assert kinds.count("drop") == len(result.dropped_packets)
    assert kinds.count("step") == len(scenario.steps)
    assert "overflow" in report and "max_packet_bytes=8" in report


def test_numeric_signals_get_a_plot_and_enumerations_a_change_table() -> None:
    recorder, result = _record()

    report = render_report(recorder, result)

    assert report.count('<svg class="plot"') == 1  # battery_percent is numeric
    assert 'aria-label="eps.telemetry.battery_percent over virtual time"' in report
    assert "mark-pass" in report  # the assertion on power_status is not numeric, so no mark here
    assert '<table class="changes">' in report  # power_status goes online -> offline
    assert "&#x27;online&#x27;" in report and "&#x27;offline&#x27;" in report


def test_everything_dynamic_is_escaped() -> None:
    scenario = """
name: <script>alert("x")</script>
description: a <b>bold</b> & "quoted" description
steps:
  - action: wait
    virtual_time: 1s
  - action: assert
    signal: eps.telemetry.battery_percent
    op: ">="
    value: 0
    timeout: 1s
"""
    recorder, result = _record(scenario_text=scenario)

    report = render_report(recorder, result)

    assert '<script>alert("x")</script>' not in report
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in report
    assert "&lt;b&gt;bold&lt;/b&gt; &amp;" in report
    assert _parse(report).errors == []


def test_an_aborted_run_renders_an_error_page_with_what_was_recorded() -> None:
    recorder, _result = _record()

    report = render_report(recorder, None, error="boom <1>")

    assert ">ERROR</span>" in report
    assert "The run aborted:" in report and "boom &lt;1&gt;" in report
    assert _parse(report).errors == []


def test_helpers() -> None:
    assert format_virtual_time(0) == "0.000000 s"
    assert format_virtual_time(3_500_001) == "3.500001 s"
    assert format_virtual_time(-1_000_000) == "-1.000000 s"
    scenario = parse_scenario(FAILING)
    assert describe_step(scenario.steps[0]) == (
        "inject_fault type=state_override target=eps.model.battery_percent value=25 "
        "duration=5.000000 s"
    )
    assert describe_step(scenario.steps[1]) == "wait virtual_time=3.000000 s"


# --- the CLI flag ----------------------------------------------------------------------


def _cli(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_writes_the_report_and_leaves_stdout_to_the_normal_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "nested" / "report.html"

    code, stdout, stderr = _cli(["run", "--example", "default", "--html", str(out)], capsys)

    assert code == 0 and out.is_file()
    assert "SUMMARY" in stdout and "<html" not in stdout
    assert f"html report written to {out}" in stderr
    assert ">PASS</span>" in out.read_text(encoding="utf-8")


def test_cli_html_composes_with_json_and_quiet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "report.html"

    code, stdout, stderr = _cli(
        ["run", "--example", "default", "--html", str(out), "--json"], capsys
    )
    assert code == 0 and json.loads(stdout)["passed"] is True and out.is_file()

    quiet = tmp_path / "quiet.html"
    code, stdout, stderr = _cli(
        ["run", "--example", "default", "--html", str(quiet), "--quiet"], capsys
    )
    assert code == 0 and stdout == "" and quiet.is_file()
    assert "html report written" not in stderr


def test_cli_html_composes_with_trace(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "report.html"

    code, _stdout, stderr = _cli(
        ["run", "--example", "default", "--html", str(out), "--trace"], capsys
    )

    assert code == 0 and out.is_file()
    assert "trace t=0 TX " in stderr  # the trace still prints, from the same frames


def test_cli_failing_run_still_writes_a_report_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(FAILING, encoding="utf-8")
    out = tmp_path / "report.html"

    code, _stdout, _stderr = _cli(
        ["run", "-c", str(DEFAULT_SETUP), "-s", str(scenario), "--html", str(out)], capsys
    )

    assert code == 1
    assert ">FAIL</span>" in out.read_text(encoding="utf-8")


def test_cli_aborted_run_writes_a_partial_report_and_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(self: object, virtual_time: int) -> None:
        raise runner_module.ScenarioRuntimeError("simulated failure mid-run")

    monkeypatch.setattr(runner_module.ScenarioRunner, "wait", explode)
    out = tmp_path / "report.html"

    code, _stdout, stderr = _cli(["run", "--example", "default", "--html", str(out)], capsys)

    assert code == 2 and "simulated failure mid-run" in stderr
    report = out.read_text(encoding="utf-8")
    assert ">ERROR</span>" in report and "simulated failure mid-run" in report
    assert 'data-kind="step"' in report  # the steps reached before the error are there


def test_cli_unwritable_report_path_is_an_execution_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _stdout, stderr = _cli(["run", "--example", "default", "--html", str(tmp_path)], capsys)

    assert code == 2 and "could not write the HTML report" in stderr


# --- the script, run for real ----------------------------------------------------------

_NODE_HARNESS = r"""
const data = JSON.parse(process.argv[1]);
const attr = (row) => (name) => row[name];
function el(extra) { return Object.assign({value: '', checked: true, listeners: {}, hidden: false,
  addEventListener(type, fn) { this.listeners[type] = fn; }}, extra || {}); }
const rows = data.rows.map((r) => ({hidden: false, textContent: r.text, getAttribute: attr(r)}));
const ids = {'f-dir': el(), 'f-node': el(), 'f-port': el(), 'f-text': el(), 'f-count': el(),
  'k-frame': el(), 'k-drop': el(), 'k-fault': el(), 'k-step': el()};
for (const r of rows) {}
const byId = {};
data.rows.forEach((r, i) => { if (r.id) byId[r.id] = rows[i]; });
global.window = {addEventListener() {}};
global.location = {hash: data.hash || ''};
const docListeners = {};
global.document = {
  querySelectorAll: () => rows,
  getElementById: (id) => ids[id] || byId[id] || null,
  addEventListener(type, fn) { docListeners[type] = fn; },
};
eval(data.script);
const out = [];
for (const step of data.steps) {
  for (const [k, v] of Object.entries(step.set || {})) {
    if (k.startsWith('k-')) ids[k].checked = v; else ids[k].value = v;
  }
  if (step.fire) ids[step.fire].listeners[step.type || 'input']();
  if (step.click) {
    const href = step.click;
    docListeners.click({target: {closest: () => ({getAttribute: () => href})}});
  }
  out.push({hidden: data.rows.map((r, i) => rows[i].hidden), count: ids['f-count'].textContent,
            value: Object.fromEntries(['f-dir','f-node','f-port','f-text'].map(k => [k, ids[k].value]))});
}
console.log(JSON.stringify(out));
"""


def _run_script(parsed: _Rows, steps: list[dict[str, object]], hash_: str = "") -> list[dict]:  # type: ignore[type-arg]
    rows = [{**row, "id": key} for key, row in parsed.rows.items()]
    payload = json.dumps({"script": parsed.script, "rows": rows, "steps": steps, "hash": hash_})
    done = subprocess.run(
        ["node", "-e", _NODE_HARNESS, payload],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)  # type: ignore[no-any-return]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_timeline_filters_work() -> None:
    recorder, result = _record(scenario_text=FAILING)
    parsed = _parse(render_report(recorder, result))
    keys = list(parsed.rows)

    def shown(state: dict) -> list[str]:  # type: ignore[type-arg]
        return [k for k, h in zip(keys, state["hidden"], strict=True) if not h]

    states = _run_script(
        parsed,
        [
            {},
            {"set": {"f-dir": "TX"}, "fire": "f-dir"},
            {"set": {"f-dir": "", "k-frame": False}, "fire": "k-frame", "type": "change"},
            {"set": {"k-frame": True, "f-port": "21"}, "fire": "f-port"},
            {"set": {"f-port": "", "f-text": "99"}, "fire": "f-text"},
        ],
    )

    everything = shown(states[0])
    assert everything == keys and states[0]["count"] == f"showing {len(keys)} of {len(keys)}"
    tx = shown(states[1])
    assert tx and all(parsed.rows[k]["data-dir"] == "TX" for k in tx if k.startswith("f"))
    assert all(parsed.rows[k]["data-kind"] != "frame" for k in shown(states[2]))
    on_port = [k for k in shown(states[3]) if parsed.rows[k]["data-kind"] == "frame"]
    assert on_port and all("21" in parsed.rows[k]["data-ports"].split() for k in on_port)
    assert shown(states[4]) == [k for k in keys if "99" in parsed.rows[k]["text"].lower()]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_following_an_evidence_link_to_a_filtered_out_row_clears_the_filters() -> None:
    """Clicking works even when the hash already equals the target, which fires no event."""

    recorder, result = _record(scenario_text=FAILING)
    parsed = _parse(render_report(recorder, result))
    failed = next(a for a in recorder.assertions if not a.result.passed)
    target = f"f{failed.evidence_frame}"
    keys = list(parsed.rows)
    assert parsed.rows[target]["data-dir"] == "RX"

    states = _run_script(
        parsed,
        [
            {"set": {"f-dir": "TX"}, "fire": "f-dir"},  # hides the RX evidence row
            {"click": f"#{target}"},  # follow the assertion's link
        ],
        hash_=f"#{target}",
    )

    assert states[0]["hidden"][keys.index(target)]  # the filter really hid it
    assert not states[1]["hidden"][keys.index(target)]  # following the link brought it back
    assert states[1]["value"]["f-dir"] == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_opening_the_report_on_a_filtered_out_anchor_shows_that_row() -> None:
    recorder, result = _record(scenario_text=FAILING)
    parsed = _parse(render_report(recorder, result))
    target = next(k for k, r in parsed.rows.items() if r["data-dir"] == "RX")

    states = _run_script(parsed, [{}], hash_=f"#{target}")

    assert not states[0]["hidden"][list(parsed.rows).index(target)]


# --- the whole-packet links ------------------------------------------------------------


def test_a_report_over_a_serial_line_shows_whole_packet_frames_and_a_bad_frame() -> None:
    import os
    import tty

    from cubesat_testbed.config import parse_testbed_config
    from tests.test_kiss_transport import SCENARIO as KISS_SCENARIO
    from tests.test_kiss_transport import SETUP as KISS_SETUP
    from tests.test_kiss_transport import Pty, _eps_telemetry_frame

    line = Pty()
    os.set_blocking(line.master, False)
    try:
        tty.setraw(line.master)
        bad = bytearray(_eps_telemetry_frame(41.0))
        bad[-3] ^= 0xFF
        line.push(bytes(bad))
        line.push(_eps_telemetry_frame(42.0))
        setup = parse_testbed_config(KISS_SETUP.format(device=line.path))
        scenario = parse_scenario(KISS_SCENARIO, setup=setup)
        recorder = RunRecorder()
        result = run_scenario(scenario, setup, output=io.StringIO(), recorder=recorder)
    finally:
        line.close()

    report = render_report(recorder, result)
    parsed = _parse(report)
    texts = [row["text"] for row in parsed.rows.values()]

    assert result.passed
    assert any(" RX serial " in t and "eps (2) → eps (2)" in t for t in texts), texts
    assert any("telemetry eps.telemetry.battery_percent=42.0" in t for t in texts)
    assert any(
        row["data-kind"] == "drop" and "crc_error" in row["text"] for row in parsed.rows.values()
    )
    (assertion,) = recorder.assertions
    assert assertion.evidence_frame is not None
    assert parsed.errors == []
