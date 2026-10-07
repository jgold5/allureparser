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
    if not isinstance(ms, (int, float)):
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def strip(t: TestHistory) -> str:
    """Compact one-char-per-run history, e.g. 'PPFP.B'. Lowercase marks a retried run."""
    out = []
    for c in t.cells:
        g = GLYPH.get(c.status, "?")
        out.append(g.lower() if c.retried and c.status else g)
    return "".join(out)


# ---------------------------------------------------------------- text


def render_text(h: History, top: int = 20, flaky_only: bool = True) -> str:
    rows = h.flaky if flaky_only else h.tests
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


def render_csv(h: History) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["test", "flaky", "flips", "flip_rate", "in_run_flaky", "passed", "failed",
         "broken", "skipped", "runs_present", "last_status"]
        + [r.label for r in h.runs]
    )
    for t in h.tests:
        w.writerow(
            [t.name, int(t.is_flaky), t.flips, f"{t.flip_rate:.3f}", t.in_run_flaky,
             t.counts["passed"], t.counts["failed"], t.counts["broken"], t.counts["skipped"],
             t.runs_present, t.last_status or ""]
            + [c.status or "" for c in t.cells]
        )
    return buf.getvalue()


# ---------------------------------------------------------------- json


def to_dict(h: History) -> dict:
    return {
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
                "cells": [
                    {"status": c.status, "attempts": c.attempts, "message": c.message}
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
.name { position: sticky; left: 0; background: var(--bg); z-index: 1; padding: 4px 10px;
  max-width: 560px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  border-right: 1px solid var(--border); }
thead .name { z-index: 3; background: var(--head); text-align: left; vertical-align: bottom; }
.num { padding: 4px 8px; text-align: right; font-variant-numeric: tabular-nums;
  white-space: nowrap; vertical-align: middle; }
thead .num { vertical-align: bottom; cursor: pointer; user-select: none; }
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
.flaky-badge { display: inline-block; font-size: 10px; padding: 0 5px; border-radius: 8px;
  background: var(--broken); color: var(--cell-fg); margin-right: 6px; vertical-align: 1px; }
.empty { padding: 24px; color: var(--muted); }
@media (max-width: 600px) {
  .name { max-width: 45vw; }
  .controls input[type=search] { min-width: 0; width: 100%; }
}
"""

_JS = """
(function () {
  const tbody = document.querySelector('tbody');
  const rows = Array.from(tbody.rows);
  const q = document.getElementById('q');
  const only = document.getElementById('flakyOnly');
  const count = document.getElementById('shown');
  function apply() {
    const term = q.value.trim().toLowerCase();
    let n = 0;
    for (const r of rows) {
      const show = (!only.checked || r.dataset.flaky === '1') &&
                   (!term || r.dataset.name.includes(term));
      r.hidden = !show;
      if (show) n++;
    }
    count.textContent = n;
  }
  let sortKey = null, asc = false;
  document.querySelectorAll('th[data-sort]').forEach(th => th.addEventListener('click', () => {
    const k = th.dataset.sort;
    asc = sortKey === k ? !asc : k === 'name';
    sortKey = k;
    rows.sort((a, b) => {
      const x = a.dataset[k], y = b.dataset[k];
      const c = k === 'name' ? x.localeCompare(y) : (parseFloat(x) - parseFloat(y));
      return asc ? c : -c;
    });
    rows.forEach(r => tbody.appendChild(r));
  }));
  q.addEventListener('input', apply);
  only.addEventListener('change', apply);
  apply();
})();
"""


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def render_html(h: History, title: str = "Test History") -> str:
    flaky = h.flaky
    total_runs = len(h.runs)
    head_cells = []
    for i, r in enumerate(h.runs, 1):
        tip = "\n".join(x for x in (r.label, _run_time(r.start), f"{len(r.tests)} tests") if x)
        label = _e(r.label)
        if r.url:
            label = f'<a href="{_e(r.url)}" target="_blank" rel="noopener">{label}</a>'
        head_cells.append(f'<th class="run" title="{_e(tip)}">{label}</th>')

    body = []
    for t in h.tests:
        cells = []
        for r, c in zip(h.runs, t.cells):
            cls = c.status or "none"
            tip = [r.label, c.status or "not run"]
            if c.retried:
                cls += " retry"
                tip.append("attempts: " + " → ".join(c.attempts))
            if c.message:
                tip.append(c.message)
            cells.append(
                f'<td class="c {cls}" title="{_e(chr(10).join(tip))}">{GLYPH.get(c.status, "?")}</td>'
            )
        fails = t.counts["failed"] + t.counts["broken"]
        badge = '<span class="flaky-badge">flaky</span>' if t.is_flaky else ""
        name_tip = t.name + (f"\n\nLast failure: {t.last_failure}" if t.last_failure else "")
        body.append(
            f'<tr data-name="{_e(t.name.lower())}" data-flaky="{int(t.is_flaky)}" '
            f'data-flips="{t.flips}" data-rate="{t.flip_rate:.4f}" data-fails="{fails}" '
            f'data-retry="{t.in_run_flaky}">'
            f'<td class="name" title="{_e(name_tip)}">{badge}{_e(t.name)}</td>'
            f'<td class="num">{t.flips}</td>'
            f'<td class="num">{t.flip_rate:.0%}</td>'
            f'<td class="num">{fails}/{t.runs_present}</td>'
            + "".join(cells)
            + "</tr>"
        )

    legend = "".join(
        f'<span><i style="background:var(--{s})"></i>{s}</span>'
        for s in ("passed", "failed", "broken", "skipped")
    ) + '<span>&#8226; dot = retried within run</span><span>. = not run</span>'

    if h.tests:
        table = f"""
<div class="wrap"><table>
<thead><tr>
  <th class="name" data-sort="name">Test</th>
  <th class="num" data-sort="flips" title="Pass/fail transitions between consecutive runs">Flips</th>
  <th class="num" data-sort="rate" title="Flips / (runs with a pass or fail result - 1)">Rate</th>
  <th class="num" data-sort="fails" title="Failed or broken runs / runs present">Fails</th>
  {''.join(head_cells)}
</tr></thead>
<tbody>
{chr(10).join(body)}
</tbody></table></div>"""
    else:
        table = '<div class="empty">No test results found.</div>'

    span = ""
    starts = [r.start for r in h.runs if r.start]
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
<div class="sub">Runs oldest &rarr; newest, left to right{span}</div>
<div class="stats">
  <div class="stat"><b>{total_runs}</b><span>runs</span></div>
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
<script>{_JS}</script>
</body>
</html>
"""
