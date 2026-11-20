"""Renders a recorded run as one self-contained HTML file.

The file needs nothing at view time: the stylesheet and the few lines of script are
inline, the signal plots are inline SVG, and there is no font, image or network
fetch. Everything is readable with scripting off; the script only adds the timeline
filters. An assertion links to the frame its value was decoded from with a plain
``#f<id>`` anchor, so that works without scripting too.

The output is a pure function of the recording: no wall-clock time, host name,
absolute path or random id goes into it, so the same run produces the same bytes.
"""

from __future__ import annotations

from html import escape
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

from cubesat_testbed.report.recorder import (
    RecordedAssertion,
    RecordedDrop,
    RecordedFault,
    RecordedFrame,
    RecordedStep,
    RunRecorder,
    format_virtual_time,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cubesat_testbed.scenario.runner import ScenarioRunResult

_PLOT_WIDTH = 720
_PLOT_HEIGHT = 170
_MARGIN_LEFT = 62
_MARGIN_RIGHT = 14
_MARGIN_TOP = 12
_MARGIN_BOTTOM = 26


def _esc(value: object) -> str:
    return escape(str(value), quote=True)


def _version() -> str:
    try:
        return metadata.version("cubesat_testbed")
    except metadata.PackageNotFoundError:  # pragma: no cover - not installed
        return "unknown"


def _hex(data: bytes | None) -> str:
    if data is None:
        return ""
    return data.hex(" ") if data else "—"


def _display(value: object) -> str:
    if isinstance(value, float):
        return repr(value)
    return str(value)


# --- node names ------------------------------------------------------------------------


def _node_names(recorder: RunRecorder) -> dict[int, str]:
    if recorder.setup is None:
        return {}
    return {node.address: name for name, node in recorder.setup.nodes.items()}


def _node_label(address: int | None, names: dict[int, str]) -> str:
    if address is None:
        return "?"
    name = names.get(address)
    return f"{name} ({address})" if name else str(address)


# --- plots -----------------------------------------------------------------------------


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _collapse(points: list[tuple[int, object]]) -> list[tuple[int, float]]:
    """Keep the first point of every run of equal values, plus the very last point.

    A held value draws as a flat step, so the points in the middle of a run add bytes
    and no information; the last point keeps the plot reaching the end of the run.
    """

    kept: list[tuple[int, float]] = []
    previous: object = object()
    for time, value in points:
        number = float(value)  # type: ignore[arg-type]
        if number != previous:
            kept.append((time, number))
            previous = number
    if points and (not kept or kept[-1][0] != points[-1][0]):
        kept.append((points[-1][0], float(points[-1][1])))  # type: ignore[arg-type]
    return kept


def _tick_label(value: float) -> str:
    text = f"{value:.4g}"
    return text


def _plot_svg(
    signal: str,
    points: list[tuple[int, float]],
    marks: list[RecordedAssertion],
    *,
    start: int,
    end: int,
) -> str:
    """A step plot of one numeric signal with a marker per assertion on that signal."""

    low = min(value for _, value in points)
    high = max(value for _, value in points)
    if low == high:
        pad = abs(low) * 0.1 or 1.0
        low, high = low - pad, high + pad
    span_x = max(end - start, 1)
    plot_w = _PLOT_WIDTH - _MARGIN_LEFT - _MARGIN_RIGHT
    plot_h = _PLOT_HEIGHT - _MARGIN_TOP - _MARGIN_BOTTOM

    def x_of(time: int) -> float:
        return _MARGIN_LEFT + (time - start) / span_x * plot_w

    def y_of(value: float) -> float:
        return _MARGIN_TOP + (1 - (value - low) / (high - low)) * plot_h

    path: list[str] = []
    previous_y = 0.0
    for index, (time, value) in enumerate(points):
        x, y = x_of(time), y_of(value)
        if index == 0:
            path.append(f"M{x:.2f},{y:.2f}")
        else:
            path.append(f"L{x:.2f},{previous_y:.2f}L{x:.2f},{y:.2f}")
        previous_y = y
    path.append(f"L{x_of(end):.2f},{previous_y:.2f}")

    parts = [
        (
            f'<svg class="plot" role="img" viewBox="0 0 {_PLOT_WIDTH} {_PLOT_HEIGHT}" '
            f'aria-label="{_esc(signal)} over virtual time">'
        ),
        f"<title>{_esc(signal)} over virtual time</title>",
        (
            f'<rect class="plot-bg" x="{_MARGIN_LEFT}" y="{_MARGIN_TOP}" '
            f'width="{plot_w}" height="{plot_h}"/>'
        ),
    ]
    for fraction in (0.0, 0.5, 1.0):
        value = low + (high - low) * (1 - fraction)
        y = _MARGIN_TOP + fraction * plot_h
        parts.append(
            f'<line class="grid" x1="{_MARGIN_LEFT}" y1="{y:.2f}" '
            f'x2="{_MARGIN_LEFT + plot_w}" y2="{y:.2f}"/>'
            f'<text class="tick" x="{_MARGIN_LEFT - 6}" y="{y + 4:.2f}" '
            f'text-anchor="end">{_esc(_tick_label(value))}</text>'
        )
    for fraction in (0.0, 0.5, 1.0):
        time = start + round(span_x * fraction)
        x = _MARGIN_LEFT + fraction * plot_w
        anchor = "start" if fraction == 0 else "end" if fraction == 1 else "middle"
        parts.append(
            f'<text class="tick" x="{x:.2f}" y="{_PLOT_HEIGHT - 8}" '
            f'text-anchor="{anchor}">{_esc(format_virtual_time(time))}</text>'
        )
    for mark in marks:
        x = x_of(min(max(mark.result.evaluated_at, start), end))
        kind = "pass" if mark.result.passed else "fail"
        parts.append(
            f'<line class="mark mark-{kind}" x1="{x:.2f}" y1="{_MARGIN_TOP}" '
            f'x2="{x:.2f}" y2="{_MARGIN_TOP + plot_h}">'
            f"<title>{_esc(mark.result.name)}: {kind.upper()} at "
            f"{_esc(format_virtual_time(mark.result.evaluated_at))}</title></line>"
        )
    parts.append(f'<path class="series" d="{" ".join(path)}"/>')
    parts.append("</svg>")
    return "".join(parts)


def _signals_section(recorder: RunRecorder, *, start: int, end: int) -> str:
    if not recorder.signals:
        return (
            '<section id="signals"><h2>Signals</h2>'
            '<p class="muted">No telemetry was decoded from the bus during this run.</p>'
            "</section>"
        )
    cards: list[str] = []
    for signal in sorted(recorder.signals):
        values = recorder.signals[signal]
        marks = [a for a in recorder.assertions if a.result.signal == signal]
        if all(_is_number(value) for _, value in values):
            points = _collapse(values)
            body = _plot_svg(signal, points, marks, start=start, end=end)
            last = _display(values[-1][1])
            note = f"{len(values)} samples, last {_esc(last)}"
        else:
            changes: list[tuple[int, object]] = []
            for time, value in values:
                if not changes or changes[-1][1] != value:
                    changes.append((time, value))
            rows = "".join(
                f"<tr><td>{_esc(format_virtual_time(time))}</td><td>{_esc(_display(value))}</td></tr>"
                for time, value in changes
            )
            body = (
                '<table class="changes"><thead><tr><th>virtual time</th><th>value</th></tr></thead>'
                f"<tbody>{rows}</tbody></table>"
            )
            note = f"{len(values)} samples, {len(changes)} value change(s)"
        cards.append(
            f'<div class="card"><h3>{_esc(signal)}</h3><p class="muted">{note}</p>{body}</div>'
        )
    legend = (
        '<p class="muted">Vertical lines mark when an assertion on the signal was evaluated: '
        '<span class="chip pass">PASS</span> <span class="chip fail">FAIL</span>.</p>'
    )
    return f'<section id="signals"><h2>Signals</h2>{legend}{"".join(cards)}</section>'


# --- assertions ------------------------------------------------------------------------


def _frame_link(frame_id: int | None) -> str:
    if frame_id is None:
        return '<span class="muted">signal never seen on the bus</span>'
    return f'<a href="#f{frame_id}">frame #{frame_id}</a>'


def _assertions_section(recorder: RunRecorder) -> str:
    if not recorder.assertions:
        return (
            '<section id="assertions"><h2>Assertions</h2>'
            '<p class="muted">The scenario made no assertions.</p></section>'
        )
    failed = [a for a in recorder.assertions if not a.result.passed]
    lines: list[str] = []
    if failed:
        items = "".join(
            f"<li><strong>{_esc(a.result.name)}</strong> "
            f"({_esc(a.result.signal)}) → {_frame_link(a.evidence_frame)}</li>"
            for a in failed
        )
        lines.append(f'<div class="failures"><p>Failed assertions:</p><ul>{items}</ul></div>')
    rows: list[str] = []
    for number, a in enumerate(recorder.assertions, start=1):
        result = a.result
        kind = "pass" if result.passed else "fail"
        rows.append(
            f'<tr class="a-{kind}"><td>{number}</td>'
            f'<td><span class="chip {kind}">{"PASS" if result.passed else "FAIL"}</span></td>'
            f"<td>{_esc(result.name)}</td>"
            f"<td><code>{_esc(result.signal)} {_esc(result.operator.value)} "
            f"{_esc(_display(result.expected))}</code></td>"
            f"<td><code>{_esc(_display(result.actual))}</code></td>"
            f"<td>{_esc(format_virtual_time(result.evaluated_at))}</td>"
            f"<td>{_frame_link(a.evidence_frame)}</td>"
            f"<td>{_esc(result.detail or '')}</td></tr>"
        )
    return (
        '<section id="assertions"><h2>Assertions</h2>'
        + "".join(lines)
        + '<table class="assertions"><thead><tr><th>#</th><th>result</th><th>name</th>'
        "<th>expected</th><th>actual</th><th>evaluated at</th><th>evidence</th><th>detail</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></section>"
    )


# --- setup -----------------------------------------------------------------------------


def _setup_section(recorder: RunRecorder) -> str:
    setup = recorder.setup
    if setup is None:
        return ""
    transport = setup.transport
    options = ", ".join(
        f"{key}={value}" for key, value in transport.model_dump().items() if key != "type"
    )
    node_rows = "".join(
        f"<tr><td>{_esc(name)}</td><td>{node.address}</td><td>{_esc(node.mode.value)}</td>"
        f"<td>{_esc(node.module_type or '—')}</td><td>{node.max_packet_bytes}</td></tr>"
        for name, node in setup.nodes.items()
    )
    return (
        '<section id="setup"><h2>Setup</h2>'
        f"<p>Transport: <code>{_esc(transport.type)}</code>"
        f"{' (' + _esc(options) + ')' if options else ''}</p>"
        "<table><thead><tr><th>node</th><th>CSP address</th><th>mode</th><th>module</th>"
        "<th>max_packet_bytes</th></tr></thead>"
        f"<tbody>{node_rows}</tbody></table></section>"
    )


# --- timeline --------------------------------------------------------------------------


def _frame_row(entry: RecordedFrame, names: dict[int, str]) -> str:
    record = entry.record
    nodes = " ".join(str(a) for a in (record.source, record.destination) if a is not None)
    ports = " ".join(str(p) for p in (record.destination_port, record.source_port) if p is not None)
    classes = ["frame", f"dir-{record.direction.lower()}"]
    if record.kind == "fragment" and record.frames is None:
        classes.append("partial")
    if record.kind == "undecodable":
        classes.append("bad")
    if record.kind == "packet":
        wire = f"{record.link}"
    else:
        wire = f"CAN 0x{record.can_id:08X}" if record.can_id is not None else ""
    if record.cfp is not None:
        if record.remaining is not None:  # CSP v1: CFP1 counts frames down and numbers packets
            wire += f" cfp={record.cfp} rem={record.remaining} id={record.identification}"
        else:
            wire += f" cfp={record.cfp} pc={record.packet_count} fc={record.frame_count}"
    if record.has_header:
        route = (
            f"{_esc(_node_label(record.source, names))} → "
            f"{_esc(_node_label(record.destination, names))}"
        )
        port_text = f"{record.destination_port} ← {record.source_port}"
        flags = f"0x{record.flags:02X}"
    else:
        route = (
            f'<span class="muted">sender {_esc(record.sender)}</span> → '
            f"{_esc(_node_label(record.destination, names))}"
            if record.kind != "undecodable"
            else ""
        )
        port_text = ""
        flags = ""
    notes: list[str] = []
    if record.frames is not None:
        notes.append(f"reassembled {record.frames} frames")
    if record.annotation:
        notes.append(_esc(record.annotation))
    if record.undecodable:
        notes.append(f"undecodable: {_esc(record.undecodable)}")
    time = record.time if record.time is not None else 0
    return (
        f'<tr id="f{entry.frame_id}" class="{" ".join(classes)}" data-kind="frame" '
        f'data-dir="{_esc(record.direction)}" data-nodes="{_esc(nodes)}" '
        f'data-ports="{_esc(ports)}" data-t="{time}">'
        f'<td><a class="idlink" href="#f{entry.frame_id}">#{entry.frame_id}</a></td>'
        f"<td>{_esc(format_virtual_time(time))}</td><td>{_esc(record.direction)}</td>"
        f"<td>{_esc(wire)}</td><td>{route}</td><td>{_esc(port_text)}</td><td>{_esc(flags)}</td>"
        f'<td class="hex">{_esc(_hex(record.data))}</td>'
        f'<td class="hex">{_esc(_hex(record.payload))}</td><td>{"; ".join(notes)}</td></tr>'
    )


def _drop_row(entry: RecordedDrop, names: dict[int, str], number: int) -> str:
    nodes = " ".join(str(a) for a in (entry.sender, entry.destination) if a is not None)
    where = f"at {_node_label(entry.destination, names)}" if entry.destination is not None else ""
    limit = f" (limit {entry.limit})" if entry.limit is not None else ""
    return (
        f'<tr id="d{number}" class="drop" data-kind="drop" data-dir="" '
        f'data-nodes="{_esc(nodes)}" data-ports="" data-t="{entry.time}">'
        f"<td>drop</td><td>{_esc(format_virtual_time(entry.time))}</td><td></td>"
        f'<td colspan="7"><span class="chip fail">DROP</span> <strong>{_esc(entry.reason)}</strong> '
        f"{_esc(where)}{_esc(limit)}: {_esc(entry.detail)}</td></tr>"
    )


def _fault_row(entry: RecordedFault, names: dict[int, str], number: int) -> str:
    event = entry.event
    nodes = " ".join(str(a) for a in (event.source, event.destination) if a is not None)
    ports = " ".join(str(a) for a in (event.destination_port, event.source_port) if a is not None)
    route = ""
    if event.source is not None and event.destination is not None:
        route = f" {_node_label(event.source, names)} → {_node_label(event.destination, names)}" + (
            f" port {event.destination_port}" if event.destination_port is not None else ""
        )
    detail = f" {event.detail}" if event.detail else ""
    return (
        f'<tr id="x{number}" class="fault" data-kind="fault" data-dir="{_esc(event.direction)}" '
        f'data-nodes="{_esc(nodes)}" data-ports="{_esc(ports)}" data-t="{event.time}">'
        f"<td>fault</td><td>{_esc(format_virtual_time(event.time))}</td>"
        f"<td>{_esc(event.direction)}</td>"
        f'<td colspan="7"><span class="chip fail">FAULT</span> <strong>{_esc(event.type)}</strong> '
        f"rule {_esc(event.rule)}, match {event.match}{_esc(route)}{_esc(detail)}, "
        f'data <span class="hex">{_esc(_hex(event.data))}</span></td></tr>'
    )


def _step_row(entry: RecordedStep) -> str:
    return (
        f'<tr class="step" data-kind="step" data-dir="" data-nodes="" data-ports="" '
        f'data-t="{entry.time}"><td>step {entry.number}</td>'
        f"<td>{_esc(format_virtual_time(entry.time))}</td><td></td>"
        f'<td colspan="7"><code>{_esc(entry.text)}</code></td></tr>'
    )


def _timeline_section(recorder: RunRecorder, names: dict[int, str]) -> str:
    rows: list[str] = []
    drops = 0
    faults = 0
    addresses: set[int] = set()
    ports: set[int] = set()
    for entry in recorder.timeline:
        if isinstance(entry, RecordedFrame):
            rows.append(_frame_row(entry, names))
            for address in (entry.record.source, entry.record.destination):
                if address is not None:
                    addresses.add(address)
            for port in (entry.record.destination_port, entry.record.source_port):
                if port is not None:
                    ports.add(port)
        elif isinstance(entry, RecordedDrop):
            drops += 1
            rows.append(_drop_row(entry, names, drops))
            for address in (entry.sender, entry.destination):
                if address is not None:
                    addresses.add(address)
        elif isinstance(entry, RecordedFault):
            faults += 1
            rows.append(_fault_row(entry, names, faults))
            for address in (entry.event.source, entry.event.destination):
                if address is not None:
                    addresses.add(address)
            for port in (entry.event.destination_port, entry.event.source_port):
                if port is not None:
                    ports.add(port)
        else:
            rows.append(_step_row(entry))
    node_options = "".join(
        f'<option value="{a}">{_esc(_node_label(a, names))}</option>' for a in sorted(addresses)
    )
    port_options = "".join(f'<option value="{p}">{p}</option>' for p in sorted(ports))
    controls = (
        '<form id="filters" class="filters" onsubmit="return false">'
        '<label>direction <select id="f-dir"><option value="">all</option>'
        '<option value="TX">TX</option><option value="RX">RX</option></select></label>'
        f'<label>node <select id="f-node"><option value="">all</option>{node_options}</select></label>'
        f'<label>port <select id="f-port"><option value="">all</option>{port_options}</select></label>'
        '<label><input type="checkbox" id="k-frame" checked> frames</label>'
        '<label><input type="checkbox" id="k-drop" checked> drops</label>'
        '<label><input type="checkbox" id="k-fault" checked> bus faults</label>'
        '<label><input type="checkbox" id="k-step" checked> steps</label>'
        '<label>search <input type="search" id="f-text" placeholder="payload, annotation…"></label>'
        '<span id="f-count" class="muted"></span></form>'
    )
    table = (
        '<table id="timeline"><thead><tr><th>#</th><th>virtual time</th><th>dir</th>'
        "<th>link / frame</th><th>route</th><th>dport ← sport</th><th>flags</th>"
        "<th>data</th><th>payload</th><th>note</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    return (
        '<section id="timeline-section"><h2>Timeline</h2>'
        '<p class="muted">Every frame that crossed the transport boundary, in order, with the '
        "scenario steps, dropped packets and injected bus faults. On the in-memory bus a frame appears twice, "
        "TX when sent and RX when read back: that round trip is what an assertion observes."
        f"</p>{controls}{table}</section>"
    )


# --- page ------------------------------------------------------------------------------

_STYLE = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1b1f24;--muted:#5b6672;--line:#d0d7de;
--card:#f6f8fa;--pass:#1a7f37;--fail:#c4272d;--accent:#0b5cad;--warn:#fff1f0}
@media (prefers-color-scheme:dark){:root{--bg:#0f1318;--fg:#e6edf3;--muted:#9aa7b4;
--line:#30363d;--card:#161b22;--pass:#56d364;--fail:#ff7b72;--accent:#79c0ff;--warn:#2d1517}}
*{box-sizing:border-box}body{margin:0;padding:1.2rem clamp(.8rem,3vw,2rem);background:var(--bg);
color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
h1{margin:0 0 .2rem;font-size:1.5rem}h2{margin:1.8rem 0 .5rem;font-size:1.15rem;
border-bottom:1px solid var(--line);padding-bottom:.25rem}h3{margin:.2rem 0;font-size:.95rem}
a{color:var(--accent)}code,.hex{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.muted{color:var(--muted)}.badge{display:inline-block;padding:.15rem .7rem;border-radius:1rem;
font-weight:700;color:#fff}.badge.pass{background:var(--pass)}.badge.fail,.badge.error{background:var(--fail)}
.chip{display:inline-block;padding:0 .45rem;border-radius:.3rem;font-weight:700;font-size:.8rem;
border:1px solid currentColor}.chip.pass{color:var(--pass)}.chip.fail{color:var(--fail)}
.summary{display:flex;flex-wrap:wrap;gap:.4rem 1.4rem;margin:.6rem 0}
.errorbox,.failures{border:1px solid var(--fail);background:var(--warn);padding:.5rem .9rem;
border-radius:.4rem;margin:.6rem 0}.failures ul{margin:.2rem 0 .2rem 1.2rem;padding:0}
table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid var(--line);
padding:.25rem .5rem;text-align:left;vertical-align:top}thead th{position:sticky;top:0;
background:var(--card)}.hex{word-break:break-all;max-width:22rem}
tr.a-fail td{background:var(--warn)}tr.partial{opacity:.7}tr.bad td{background:var(--warn)}
tr.step td{background:var(--card);font-weight:600}tr.drop td{background:var(--warn)}
tr.frame:target td,tr.frame.hit td{outline:2px solid var(--accent);background:var(--card)}
tr[hidden]{display:none}.filters{display:flex;flex-wrap:wrap;gap:.4rem 1rem;align-items:center;
margin:.4rem 0 .6rem;padding:.4rem .6rem;background:var(--card);border-radius:.4rem}
.filters select,.filters input[type=search]{font:inherit;color:inherit;background:var(--bg);
border:1px solid var(--line);border-radius:.25rem;padding:.1rem .3rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:.5rem;
padding:.5rem .8rem;margin:.6rem 0}.plot{width:100%;max-width:760px;height:auto}
.plot-bg{fill:var(--bg);stroke:var(--line)}.grid{stroke:var(--line);stroke-dasharray:3 3}
.tick{fill:var(--muted);font-size:11px}.series{fill:none;stroke:var(--accent);stroke-width:2}
.mark{stroke-width:1.5;stroke-dasharray:4 3}.mark-pass{stroke:var(--pass)}.mark-fail{stroke:var(--fail)}
table.changes{width:auto}footer{margin-top:2rem;color:var(--muted);font-size:.85rem}
"""

_SCRIPT = """
(function(){
var rows=[].slice.call(document.querySelectorAll('#timeline tbody tr'));
var dir=document.getElementById('f-dir'),node=document.getElementById('f-node'),
port=document.getElementById('f-port'),text=document.getElementById('f-text'),
count=document.getElementById('f-count');
var kinds={frame:document.getElementById('k-frame'),drop:document.getElementById('k-drop'),
fault:document.getElementById('k-fault'),
step:document.getElementById('k-step')};
if(!rows.length||!dir){return;}
function has(list,value){return (' '+list+' ').indexOf(' '+value+' ')>=0;}
function apply(){
var shown=0,needle=text.value.toLowerCase();
rows.forEach(function(row){
var kind=row.getAttribute('data-kind'),show=kinds[kind].checked;
if(show&&kind==='frame'){
if(dir.value&&row.getAttribute('data-dir')!==dir.value){show=false;}
if(show&&node.value&&!has(row.getAttribute('data-nodes'),node.value)){show=false;}
if(show&&port.value&&!has(row.getAttribute('data-ports'),port.value)){show=false;}
}else if(show&&(kind==='drop'||kind==='fault')&&node.value&&!has(row.getAttribute('data-nodes'),node.value)){show=false;}
if(show&&needle&&row.textContent.toLowerCase().indexOf(needle)<0){show=false;}
row.hidden=!show;if(show){shown++;}
});
count.textContent='showing '+shown+' of '+rows.length;
}
function reveal(id){
var row=id&&document.getElementById(id);
if(!row||!row.hidden){return;}
dir.value='';node.value='';port.value='';text.value='';
Object.keys(kinds).forEach(function(k){kinds[k].checked=true;});
apply();
}
[dir,node,port,text].forEach(function(el){el.addEventListener('input',apply);});
Object.keys(kinds).forEach(function(k){kinds[k].addEventListener('change',apply);});
window.addEventListener('hashchange',function(){reveal(location.hash.slice(1));});
document.addEventListener('click',function(e){
var a=e.target&&e.target.closest&&e.target.closest('a[href^="#"]');
if(a){reveal(a.getAttribute('href').slice(1));}
});
apply();reveal(location.hash.slice(1));
})();
"""


def render_report(
    recorder: RunRecorder,
    result: ScenarioRunResult | None,
    *,
    error: str | None = None,
) -> str:
    """Render the recording as a complete HTML document (a string, ending in a newline).

    ``result`` is ``None`` when the run aborted before producing one; ``error`` is the
    message then, and the report shows what was recorded up to that point.
    """

    names = _node_names(recorder)
    scenario_name = (
        result.scenario_name
        if result is not None
        else (recorder.scenario.name if recorder.scenario is not None else "scenario")
    )
    if error is not None:
        status, label = "error", "ERROR"
    elif result is not None and result.passed:
        status, label = "pass", "PASS"
    else:
        status, label = "fail", "FAIL"

    last_time = max(
        [
            entry.event.time if isinstance(entry, RecordedFault) else entry.time
            for entry in recorder.timeline
            if not isinstance(entry, RecordedFrame)
        ]
        + [
            entry.record.time
            for entry in recorder.timeline
            if isinstance(entry, RecordedFrame) and entry.record.time is not None
        ]
        + [time for series in recorder.signals.values() for time, _ in series]
        + [0]
    )
    start = result.started_at if result is not None else 0
    end = max(result.finished_at if result is not None else last_time, start + 1)

    passed = sum(1 for a in recorder.assertions if a.result.passed)
    failed = len(recorder.assertions) - passed
    drops = sum(1 for entry in recorder.timeline if isinstance(entry, RecordedDrop))
    bus_faults = sum(1 for entry in recorder.timeline if isinstance(entry, RecordedFault))
    summary = [
        f"<span>assertions: <strong>{passed}</strong> passed, <strong>{failed}</strong> failed</span>",
        f"<span>frames traced: <strong>{len(recorder.frames)}</strong></span>",
        f"<span>dropped packets: <strong>{drops}</strong></span>",
        *(
            [
                (
                    f"<span>bus faults: <strong>{bus_faults}</strong> "
                    f"(seed <strong>{result.bus_fault_seed}</strong>)</span>"
                )
            ]
            if result is not None and result.bus_fault_seed is not None
            else []
        ),
        (
            f"<span>virtual time: <strong>{_esc(format_virtual_time(start))}</strong> → "
            f"<strong>{_esc(format_virtual_time(end))}</strong></span>"
        ),
    ]
    description = ""
    if recorder.scenario is not None and recorder.scenario.description:
        description = f'<p class="muted">{_esc(recorder.scenario.description)}</p>'
    error_box = (
        f'<div class="errorbox"><strong>The run aborted:</strong> {_esc(error)}<br>'
        '<span class="muted">The report shows what was recorded before the error.</span></div>'
        if error is not None
        else ""
    )

    body = "".join(
        (
            f"<h1>{_esc(scenario_name)}</h1>",
            f'<p><span class="badge {status}">{label}</span></p>',
            description,
            f'<div class="summary">{"".join(summary)}</div>',
            error_box,
            _assertions_section(recorder),
            _setup_section(recorder),
            _signals_section(recorder, start=start, end=end),
            _timeline_section(recorder, names),
            (
                f"<footer>cubesat-testbed {_esc(_version())}. Times are virtual; this report "
                "contains no wall-clock time, so the same run renders the same file.</footer>"
            ),
        )
    )
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(scenario_name)} — {label} — CubeSat Testbed report</title>"
        f"<style>{_STYLE}</style></head><body>{body}<script>{_SCRIPT}</script></body></html>\n"
    )


def write_report(
    path: Path,
    recorder: RunRecorder,
    result: ScenarioRunResult | None,
    *,
    error: str | None = None,
) -> None:
    """Write :func:`render_report` to ``path`` as UTF-8, creating parent directories."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(recorder, result, error=error), encoding="utf-8")


__all__ = ["render_report", "write_report"]
