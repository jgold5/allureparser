"""Render a History as text, CSV, JSON or a self-contained HTML page."""

from __future__ import annotations

import csv
import html
import io
import json
from datetime import datetime, timezone

from .analysis import History, TestHistory

GLYPH = {"passed": "P", "failed": "F", "broken": "B", "skipped": "S", "unknown": "?", None: "."}


def _run_time(ms) -> str:
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        return ""


def strip(t: TestHistory, limit: int = 0) -> str:
    """Compact one-char-per-run history, e.g. 'PPFP.B'. Lowercase marks a retried run.
    With a limit, only the most recent entries are shown, after a leading '…'."""
    out = []
    for c in t.cells:
        g = GLYPH.get(c.status, "?")
        out.append(g.lower() if c.retried and c.status else g)
    text = "".join(out)
    if limit and len(text) > limit:
        text = "\u2026" + text[-(limit - 1):]
    return text


# ---------------------------------------------------------------- text


def render_text(h: History, top: int = 20, flaky_only: bool = True) -> str:
    rows = h.flaky if flaky_only else h.tests
    if h.per_execution:
        lines = [
            f"{h.executions} test executions, {len(h.tests)} tests, {len(h.flaky)} flaky "
            f"(each test's executions in time order, latest on the right)",
            "Legend: P passed  F failed  B broken  S skipped",
            "",
        ]
    else:
        lines = [
            f"{len(h.runs)} runs, {len(h.tests)} tests, {len(h.flaky)} flaky",
            "Legend: P passed  F failed  B broken  S skipped  . not run  "
            "(lowercase = retried in that run)",
            "",
        ]
    if not rows:
        lines.append("No flaky tests found." if flaky_only else "No tests found.")
        return "\n".join(lines)

    shown = rows[:top] if top else rows
    if h.per_execution:
        hist = {id(t): strip(t, limit=60).lstrip(".") for t in shown}
        width = max(max(len(s) for s in hist.values()), len("history"))
        lines.append(f"{'flips':>5}  {'rate':>5}  {'execs':>5}  {'history':>{width}}  test")
        for t in shown:
            lines.append(f"{t.flips:>5}  {t.flip_rate:>5.0%}  {t.runs_present:>5}  "
                         f"{hist[id(t)]:>{width}}  {t.name}")
    else:
        width = max(len(strip(t)) for t in shown)
        lines.append(f"{'flips':>5}  {'rate':>5}  {'retry':>5}  {'history':<{width}}  test")
        for t in shown:
            lines.append(
                f"{t.flips:>5}  {t.flip_rate:>5.0%}  {t.in_run_flaky:>5}  "
                f"{strip(t):<{width}}  {t.name}"
            )
    if top and len(rows) > top:
        lines.append(f"... and {len(rows) - top} more (use --top 0 to show all)")
    return "\n".join(lines)


# ---------------------------------------------------------------- csv


def _csv_text(value: str) -> str:
    """Stop spreadsheets from treating names like '=HYPERLINK(...)' as formulas."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def render_csv(h: History) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["test", "flaky", "flips", "flip_rate", "in_run_flaky", "passed", "failed",
         "broken", "skipped", "runs_present", "last_status"]
        + [_csv_text(r.label) for r in h.runs]
    )
    for t in h.tests:
        w.writerow(
            [_csv_text(t.name), int(t.is_flaky), t.flips, f"{t.flip_rate:.3f}", t.in_run_flaky,
             t.counts["passed"], t.counts["failed"], t.counts["broken"], t.counts["skipped"],
             t.runs_present, t.last_status or ""]
            + [c.status or "" for c in t.cells]
        )
    return buf.getvalue()


# ---------------------------------------------------------------- json


def to_dict(h: History) -> dict:
    return {
        "mode": "per-execution" if h.per_execution else "per-run",
        "runs": [
            {"id": r.id, "label": r.label, "order": r.order, "url": r.url,
             "start": r.start, "path": str(r.path), "tests": len(r.tests)}
            for r in h.runs
        ],
        "tests": [
            {
                "key": t.key,
                "name": t.name,
                "flaky": t.is_flaky,
                "flips": t.flips,
                "flip_rate": round(t.flip_rate, 4),
                "in_run_flaky": t.in_run_flaky,
                "counts": t.counts,
                "last_status": t.last_status,
                "last_failure": t.last_failure,
                "failure_reasons": [
                    {"message": r.message, "location": r.location, "count": r.count,
                     "last_run": h.runs[r.last_run].label}
                    for r in t.failure_reasons
                ],
                "cells": [
                    {"status": c.status, "attempts": c.attempts, "message": c.message,
                     "location": c.location, "start": c.when}
                    for c in t.cells
                ],
            }
            for t in h.tests
        ],
    }


def render_json(h: History) -> str:
    return json.dumps(to_dict(h), indent=2)


# ---------------------------------------------------------------- html

_CSS = """
:root {
  --bg: #ffffff; --fg: #1f2328; --muted: #656d76; --border: #d0d7de; --head: #f6f8fa;
  --row-hover: #f3f6fa;
  --passed: #2da44e; --failed: #cf222e; --broken: #d4a72c; --skipped: #8c959f;
  --unknown: #8250df; --none: transparent; --cell-fg: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0d1117; --fg: #e6edf3; --muted: #8d96a0; --border: #30363d; --head: #161b22;
    --row-hover: #1c2129;
    --passed: #238636; --failed: #da3633; --broken: #bb8009; --skipped: #484f58;
    --unknown: #8957e5;
  }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
  font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; }
.sub { color: var(--muted); margin-bottom: 16px; }
.stats { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.stat { border: 1px solid var(--border); border-radius: 6px; padding: 8px 14px; min-width: 110px; }
.stat b { display: block; font-size: 22px; }
.stat span { color: var(--muted); font-size: 12px; }
.controls { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
.controls input[type=search] { padding: 6px 10px; border: 1px solid var(--border);
  border-radius: 6px; background: var(--bg); color: var(--fg); min-width: 260px; }
.controls select { padding: 5px 8px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); color: var(--fg); }
.legend { display: flex; gap: 10px; flex-wrap: wrap; color: var(--muted); font-size: 12px; }
.legend i { display: inline-block; width: 14px; height: 14px; border-radius: 3px;
  vertical-align: -3px; margin-right: 4px; }
.wrap { overflow: auto; max-height: calc(100vh - 230px); border: 1px solid var(--border);
  border-radius: 6px; }
table { border-collapse: separate; border-spacing: 0; }
th, td { padding: 0; border-bottom: 1px solid var(--border); }
thead th { position: sticky; top: 0; background: var(--head); z-index: 2; font-weight: 600;
  font-size: 12px; }
th.run { writing-mode: vertical-rl; transform: rotate(180deg); padding: 8px 4px;
  white-space: nowrap; max-height: 160px; text-align: left; }
th.run a { color: inherit; text-decoration: none; }
th.run a:hover { text-decoration: underline; }
.name { position: sticky; left: 0; background: var(--bg); z-index: 1; padding: 0 10px;
  line-height: 22px;
  max-width: 560px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  border-right: 1px solid var(--border); }
thead .name { z-index: 3; background: var(--head); text-align: left; vertical-align: bottom;
  padding-bottom: 4px; }
.num { padding: 0 8px; line-height: 22px; text-align: right; font-variant-numeric: tabular-nums;
  white-space: nowrap; vertical-align: middle; }
thead .num { vertical-align: bottom; cursor: pointer; user-select: none; padding-bottom: 4px; }
thead .num:hover, thead .name:hover { text-decoration: underline; }
tbody tr:hover td { background: var(--row-hover); }
tbody tr:hover td.c { filter: brightness(1.1); }
td.c { width: 22px; min-width: 22px; height: 22px; text-align: center; font-size: 11px;
  font-weight: 700; color: var(--cell-fg); border-left: 1px solid var(--bg); position: relative; }
td.c.passed { background: var(--passed); } td.c.failed { background: var(--failed); }
td.c.broken { background: var(--broken); } td.c.skipped { background: var(--skipped); }
td.c.unknown { background: var(--unknown); } td.c.none { color: var(--muted); }
td.c.retry::after { content: ""; position: absolute; top: 2px; right: 2px; width: 5px;
  height: 5px; border-radius: 50%; background: var(--cell-fg); }
.flaky-badge { display: inline-block; font-size: 10px; line-height: 15px; padding: 0 5px;
  border-radius: 8px;
  background: var(--broken); color: var(--cell-fg); margin-right: 6px; vertical-align: 1px; }
.empty { padding: 24px; color: var(--muted); }
tbody tr:not(.spacer) { height: 23px; }
tr.spacer td { padding: 0; border: 0; height: 0; }
tr.spacer:hover td { background: none; }
@media (max-width: 600px) {
  .name { max-width: 45vw; }
  .controls input[type=search] { min-width: 0; width: 100%; }
}
"""

_JS = """
(function () {
  // Rows are rendered from the embedded JSON, and only those scrolled into view, so the
  // page stays fast with thousands of tests x hundreds of runs. All text goes through
  // textContent/attributes, never innerHTML.
  const D = JSON.parse(document.getElementById('history-data').textContent);
  const CLS = {P: 'passed', F: 'failed', B: 'broken', S: 'skipped', '?': 'unknown', '.': 'none'};
  const NAME = {P: 'passed', F: 'failed', B: 'broken', S: 'skipped', '?': 'unknown', '.': 'not run'};
  const OVERSCAN = 6;
  const wrap = document.querySelector('.wrap');
  const tbody = document.querySelector('tbody');
  const q = document.getElementById('q');
  const only = document.getElementById('flakyOnly');
  const count = document.getElementById('shown');
  const nCols = 4 + D.runs.length;
  D.tests.forEach(t => { t.l = t.n.toLowerCase(); });

  let sorted = D.tests.slice(), view = sorted, rowH = 23, measured = false, first = -1, last = -1;

  function spacer() {
    const tr = document.createElement('tr'), td = document.createElement('td');
    tr.className = 'spacer'; td.colSpan = nCols; tr.appendChild(td); return tr;
  }
  const topPad = spacer(), bottomPad = spacer();

  function td(cls, text, title) {
    const el = document.createElement('td');
    el.className = cls; el.textContent = text;
    if (title) el.title = title;
    return el;
  }

  function row(t) {
    const tr = document.createElement('tr');
    const name = td('name', '', t.n + (t.lf >= 0 ? '\\n\\n' + D.msgs[t.lf] : ''));
    if (t.k) {
      const b = document.createElement('span');
      b.className = 'flaky-badge'; b.textContent = 'flaky'; name.appendChild(b);
    }
    name.appendChild(document.createTextNode(t.n));
    tr.appendChild(name);
    tr.appendChild(td('num', String(t.f)));
    tr.appendChild(td('num', Math.round(t.r * 100) + '%'));
    tr.appendChild(td('num', t.x + '/' + t.p));
    for (let i = 0; i < t.s.length; i++) {
      const ch = t.s[i], tip = [D.runs[i], NAME[ch]];
      let cls = 'c ' + CLS[ch];
      const att = t.a && t.a[i];
      if (att) { cls += ' retry'; tip.push('attempts: ' + att); }
      const m = t.m && t.m[i];
      if (m !== undefined) tip.push(D.msgs[m]);
      // Per-execution mode: '.' just pads shorter timelines, so leave it blank.
      if (ch === '.' && D.pe) tr.appendChild(td(cls, '', ''));
      else tr.appendChild(td(cls, ch, tip.join('\\n')));
    }
    tr.dataset.name = t.l;
    tr.dataset.flaky = t.k ? '1' : '0';
    return tr;
  }

  // Height of the scroll area, from its CSS max-height so no layout is forced; falls back
  // to the window height if the max-height is not a fixed length.
  let viewportH = 0;
  function viewport() {
    if (!viewportH) {
      const max = parseFloat(getComputedStyle(wrap).maxHeight);
      viewportH = Math.min(window.innerHeight, isFinite(max) && max > 0 ? max : Infinity);
    }
    return viewportH;
  }

  function render(force) {
    const n = view.length;
    const visible = viewport();
    const start = Math.max(0, Math.min(n, Math.floor(wrap.scrollTop / rowH) - OVERSCAN));
    const end = Math.min(n, start + Math.ceil(visible / rowH) + 2 * OVERSCAN);
    if (!force && start === first && end === last) return;
    first = start; last = end;
    topPad.firstChild.style.height = (start * rowH) + 'px';
    bottomPad.firstChild.style.height = ((n - end) * rowH) + 'px';
    const frag = document.createDocumentFragment();
    frag.appendChild(topPad);
    for (let i = start; i < end; i++) frag.appendChild(row(view[i]));
    frag.appendChild(bottomPad);
    tbody.replaceChildren(frag);
    if (!measured && end > start) {
      measured = true;
      const h = tbody.rows[1].getBoundingClientRect().height;
      if (h > 0 && Math.abs(h - rowH) > 0.5) { rowH = h; render(true); }
    }
  }

  function apply(resetScroll) {
    // Setting scrollTop forces a layout, so only do it when needed.
    if (resetScroll && wrap.scrollTop) wrap.scrollTop = 0;  // new filter/sort: show top matches
    const term = q.value.trim().toLowerCase();
    view = sorted.filter(t => (!only.checked || t.k) && (!term || t.l.includes(term)));
    render(true);
    count.textContent = view.length;
  }

  // Typing fast re-filters at most once per frame.
  let applyPending = false;
  function applySoon() {
    if (applyPending) return;
    applyPending = true;
    requestAnimationFrame(() => { applyPending = false; apply(true); });
  }

  const KEY = {name: t => t.l, flips: t => t.f, rate: t => t.r, fails: t => t.x};
  let sortKey = null, asc = false;
  document.querySelectorAll('th[data-sort]').forEach(th => th.addEventListener('click', () => {
    const k = th.dataset.sort, get = KEY[k];
    asc = sortKey === k ? !asc : k === 'name';
    sortKey = k;
    sorted = D.tests.slice().sort((a, b) => {
      const x = get(a), y = get(b);
      const c = k === 'name' ? x.localeCompare(y) : x - y;
      return (asc ? c : -c) || a.i - b.i;  // ties keep the flakiness ranking
    });
    apply(true);
  }));

  let pending = false;
  wrap.addEventListener('scroll', () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; render(false); });
  });
  window.addEventListener('resize', () => { viewportH = 0; render(true); });
  q.addEventListener('input', applySoon);
  only.addEventListener('change', () => apply(true));
  apply(false);
})();
"""

_CODE = {"passed": "P", "failed": "F", "broken": "B", "skipped": "S", "unknown": "?", None: "."}


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _page_data(h: History) -> dict:
    """Compact matrix for the page script: one status character per run, messages
    de-duplicated into a lookup table, attempts/messages only for cells that have them."""
    msgs: list[str] = []
    msg_index: dict[str, int] = {}

    def msg(text: str) -> int:
        if text not in msg_index:
            msg_index[text] = len(msgs)
            msgs.append(text)
        return msg_index[text]

    tests = []
    for rank, t in enumerate(h.tests):
        attempts, messages = {}, {}
        for i, c in enumerate(t.cells):
            if c.retried:
                attempts[i] = " → ".join(c.attempts)
            detail = "\n".join(x for x in (_run_time(c.when) if c.when else "",
                                           f"at {c.location}" if c.location else "",
                                           c.message) if x)
            if detail:
                messages[i] = msg(detail)
        entry = {
            "i": rank, "n": t.name, "k": int(t.is_flaky), "f": t.flips,
            "r": round(t.flip_rate, 4), "x": t.counts["failed"] + t.counts["broken"],
            "p": t.runs_present, "s": "".join(_CODE.get(c.status, "?") for c in t.cells),
            "lf": msg(_reasons_text(t)) if t.failure_reasons else -1,
        }
        if attempts:
            entry["a"] = attempts
        if messages:
            entry["m"] = messages
        tests.append(entry)
    return {"runs": [r.label for r in h.runs], "tests": tests, "msgs": msgs,
            "pe": int(h.per_execution)}


def _reasons_text(t: TestHistory, limit: int = 5) -> str:
    lines = ["Failure reasons:"]
    for r in t.failure_reasons[:limit]:
        where = f"  at {r.location}" if r.location else ""
        lines.append(f"  {r.count}× {r.message or '(no message)'}{where}")
    if len(t.failure_reasons) > limit:
        lines.append(f"  … and {len(t.failure_reasons) - limit} more")
    return "\n".join(lines)


def _script_json(data) -> str:
    # Escaping "<" keeps "</script>" or "<!--" inside test names from ending the block.
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("<", "\\u003c")


def render_html(h: History, title: str = "Test History") -> str:
    flaky = h.flaky
    head_cells = []
    for r in h.runs:
        tip = r.label if h.per_execution else "\n".join(
            x for x in (r.label, _run_time(r.start), f"{len(r.tests)} tests") if x)
        label = _e(r.label)
        if r.url:
            label = f'<a href="{_e(r.url)}" target="_blank" rel="noopener">{label}</a>'
        head_cells.append(f'<th class="run" title="{_e(tip)}">{label}</th>')

    legend = "".join(
        f'<span><i style="background:var(--{s})"></i>{s}</span>'
        for s in ("passed", "failed", "broken", "skipped")
    ) + ("" if h.per_execution else
         '<span>&#8226; dot = retried within run</span><span>. = not run</span>')

    if h.tests:
        table = f"""
<div class="wrap"><table>
<thead><tr>
  <th class="name" data-sort="name">Test</th>
  <th class="num" data-sort="flips" title="Pass/fail transitions between consecutive runs">Flips</th>
  <th class="num" data-sort="rate" title="Flips / (runs with a pass or fail result - 1)">Rate</th>
  <th class="num" data-sort="fails" title="Failed or broken results / total results">Fails</th>
  {''.join(head_cells)}
</tr></thead>
<tbody></tbody></table></div>
<script id="history-data" type="application/json">{_script_json(_page_data(h))}</script>
<script>{_JS}</script>"""
    else:
        table = '<div class="empty">No test results found.</div>'

    span = ""
    if h.per_execution:
        starts = [c.when for t in h.tests for c in t.cells if c.when]
        heading = ("Each row is one test&rsquo;s executions, oldest &rarr; newest; "
                   "the latest is in the rightmost column")
        first_stat = f'<div class="stat"><b>{h.executions}</b><span>executions</span></div>'
    else:
        starts = [r.start for r in h.runs if r.start]
        heading = "Runs oldest &rarr; newest, left to right"
        first_stat = f'<div class="stat"><b>{len(h.runs)}</b><span>runs</span></div>'
    if starts:
        span = f" &middot; {_run_time(min(starts))} &ndash; {_run_time(max(starts))}"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>{_e(title)}</h1>
<div class="sub">{heading}{span}</div>
<div class="stats">
  {first_stat}
  <div class="stat"><b>{len(h.tests)}</b><span>tests</span></div>
  <div class="stat"><b>{len(flaky)}</b><span>flaky tests</span></div>
  <div class="stat"><b>{sum(t.flips for t in flaky)}</b><span>total flips</span></div>
</div>
<div class="controls">
  <input id="q" type="search" placeholder="Filter tests&hellip;" aria-label="Filter tests">
  <label><input id="flakyOnly" type="checkbox"{' checked' if flaky else ''}> Flaky only</label>
  <span class="legend">{legend}</span>
  <span style="color:var(--muted)"><span id="shown">{len(h.tests)}</span> shown</span>
</div>
{table}
</body>
</html>
"""
