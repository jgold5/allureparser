"""Render a History as text, CSV, JSON or a self-contained HTML page."""

from __future__ import annotations

import csv
import html
import io
import json
from datetime import datetime, timezone

from .analysis import History, TestHistory
from .loader import first_line
from .notes import NOTES_FILE, visible

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


def _duration(ms) -> str:
    if not isinstance(ms, (int, float)) or ms < 0:
        return ""
    s = int(ms // 1000)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _run_summary(r) -> dict:
    """Counts and noteworthy tests (failed/broken, or retried) of one detected run."""
    final = r.final()
    failures, retried = [], []
    for execs in final.values():
        last = execs[-1]
        statuses = [e.attempt.status for e in execs]
        if last.attempt.status in ("failed", "broken"):
            failures.append((last, statuses))
        elif len(execs) > 1:
            retried.append((last, statuses))
    failures.sort(key=lambda x: x[0].name)
    retried.sort(key=lambda x: x[0].name)
    duration = r.stop - r.start if r.start is not None and r.stop is not None else None
    return {"final": final, "failures": failures, "retried": retried, "duration": duration,
            "counts": r.counts()}


def _runs_text(h: History, shown: int) -> list[str]:
    runs = h.detected_runs
    if not runs:
        return []
    picked = runs[-shown:] if shown else runs
    lines = ["", f"Runs found in the results: {len(runs)} (by pytest process; "
                 f"parallel workers count as one run)",
             f"{'run':>5}  {'started (UTC)':<16}  {'duration':>8}  {'tests':>5}  {'pass':>5}  "
             f"{'fail':>5}  {'broken':>6}  {'skip':>5}  {'retried':>7}  workers"]
    for r in picked:
        s = _run_summary(r)
        c = s["counts"]
        started = _run_time(r.start).replace(" UTC", "") if r.start else "?"
        lines.append(f"{'#' + str(r.index):>5}  {started:<16}  {_duration(s['duration']):>8}  "
                     f"{len(s['final']):>5}  {c['passed']:>5}  {c['failed']:>5}  "
                     f"{c['broken']:>6}  {c['skipped']:>5}  {len(s['retried']):>7}  "
                     f"{r.workers}{' on ' + r.host if r.host else ''}")
    if shown and len(runs) > shown:
        lines.append(f"... {len(runs) - shown} earlier runs (use --runs 0 to list all, or see "
                     f"the HTML report for each run's failures)")
    return lines


def render_text(h: History, top: int = 20, flaky_only: bool = True, runs_shown: int = 15) -> str:
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
        return "\n".join(lines + _runs_text(h, runs_shown))

    shown = rows[:top] if top else rows
    noted: dict[str, int] = {}
    for n in visible(h.notes):
        noted[n.test] = noted.get(n.test, 0) + 1

    def name(t: TestHistory) -> str:
        n = noted.get(t.key, 0)
        return f"{t.name}  ({n} note{'' if n == 1 else 's'})" if n else t.name

    if h.per_execution:
        hist = {id(t): strip(t, limit=60).lstrip(".") for t in shown}
        width = max(max(len(s) for s in hist.values()), len("history"))
        lines.append(f"{'flips':>5}  {'rate':>5}  {'execs':>5}  {'history':>{width}}  test")
        for t in shown:
            lines.append(f"{t.flips:>5}  {t.flip_rate:>5.0%}  {t.runs_present:>5}  "
                         f"{hist[id(t)]:>{width}}  {name(t)}")
    else:
        width = max(len(strip(t)) for t in shown)
        lines.append(f"{'flips':>5}  {'rate':>5}  {'retry':>5}  {'history':<{width}}  test")
        for t in shown:
            lines.append(
                f"{t.flips:>5}  {t.flip_rate:>5.0%}  {t.in_run_flaky:>5}  "
                f"{strip(t):<{width}}  {name(t)}"
            )
    if top and len(rows) > top:
        lines.append(f"... and {len(rows) - top} more (use --top 0 to show all)")
    return "\n".join(lines + _runs_text(h, runs_shown))


# ---------------------------------------------------------------- csv


def _csv_text(value: str) -> str:
    """Stop spreadsheets from treating names like '=HYPERLINK(...)' as formulas."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def render_csv(h: History) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["test", "file", "class", "flaky", "flips", "flip_rate", "in_run_flaky", "passed", "failed",
         "broken", "skipped", "runs_present", "last_status"]
        + [_csv_text(r.label) for r in h.runs]
    )
    for t in h.tests:
        w.writerow(
            [_csv_text(t.name), _csv_text(t.file), _csv_text(t.cls), int(t.is_flaky), t.flips, f"{t.flip_rate:.3f}", t.in_run_flaky,
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
                "file": t.file,
                "class": t.cls,
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


def _runs_dict(h: History) -> list[dict]:
    out = []
    for r in h.detected_runs:
        s = _run_summary(r)
        out.append({
            "index": r.index, "start": r.start, "stop": r.stop, "duration_ms": s["duration"],
            "host": r.host, "workers": r.workers, "tests": len(s["final"]),
            "counts": s["counts"], "retried": len(s["retried"]),
            "failures": [{"name": e.name, "status": e.attempt.status,
                          "message": first_line(e.attempt.message),
                          "location": e.attempt.location, "attempts": st}
                         for e, st in s["failures"]],
            "retried_tests": [{"name": e.name, "status": e.attempt.status, "attempts": st}
                              for e, st in s["retried"]],
        })
    return out


def render_json(h: History) -> str:
    data = to_dict(h)
    if h.per_execution:
        data["runs_detected"] = _runs_dict(h)
    data["notes"] = [n.to_dict() for n in visible(h.notes)]
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------- html

_CSS = """
:root {
  --bg: #ffffff; --fg: #1f2328; --muted: #656d76; --border: #d0d7de; --head: #f6f8fa;
  --row-hover: #f3f6fa; --mark: #fff3a3;
  --passed: #2da44e; --failed: #cf222e; --broken: #d4a72c; --skipped: #8c959f;
  --unknown: #8250df; --none: transparent; --cell-fg: #ffffff; --note: #1f2328;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0d1117; --fg: #e6edf3; --muted: #8d96a0; --border: #30363d; --head: #161b22;
    --row-hover: #1c2129; --mark: #6b5a14;
    --passed: #238636; --failed: #da3633; --broken: #bb8009; --skipped: #484f58;
    --unknown: #8957e5; --note: #f0f6fc;
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
.controls .muted { color: var(--muted); font-size: 12px; }
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
  max-width: min(560px, 45vw); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  border-right: 1px solid var(--border); }
.name .tp { color: var(--muted); font-size: 12px; margin-left: 8px; }
.name mark { background: var(--mark); color: inherit; border-radius: 2px; padding: 0 1px; }
tr.grp td { background: var(--head); cursor: pointer; border-bottom: 1px solid var(--border);
  line-height: 22px; padding: 0; }
tr.grp:hover td { background: var(--row-hover); }
tr.grp .gin { position: sticky; left: 0; display: inline-flex; gap: 8px; align-items: baseline;
  padding: 0 10px; white-space: nowrap; }
tr.grp .caret { color: var(--muted); width: 10px; display: inline-block; }
tr.grp .gfile { font-weight: 600; font-size: 13px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
tr.grp .gmeta { color: var(--muted); font-size: 12px; }
.tbtn { border: 1px solid var(--border); background: var(--bg); color: var(--fg);
  border-radius: 6px; padding: 4px 10px; cursor: pointer; font: inherit; font-size: 13px; }
#hint { margin-left: 6px; font-size: 12px; }
#dbody .fullname { color: var(--muted); font-size: 12px; margin: -6px 0 12px;
  overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
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
td.c { cursor: pointer; }
td.c.none { cursor: default; }
td.c.sel { outline: 2px solid var(--fg); outline-offset: -2px; }
td.name { cursor: pointer; }
#detail { position: fixed; top: 0; right: 0; bottom: 0; width: min(460px, 100vw);
  background: var(--bg); border-left: 1px solid var(--border); z-index: 10;
  box-shadow: -6px 0 24px rgba(0, 0, 0, .18); display: flex; flex-direction: column; }
#detail[hidden] { display: none; }
.dhead { display: flex; gap: 6px; align-items: center; padding: 10px 12px;
  border-bottom: 1px solid var(--border); }
.dhead button { border: 1px solid var(--border); background: var(--head); color: var(--fg);
  border-radius: 6px; min-width: 32px; height: 30px; cursor: pointer; font-size: 15px; }
.dhead button:disabled { opacity: .4; cursor: default; }
#dclose { margin-left: auto; }
#dpos { font-size: 12px; }
#dbody { padding: 14px 16px; overflow: auto; }
#dbody h3 { font-size: 15px; margin: 8px 0 12px; overflow-wrap: anywhere; font-weight: 600; }
#dbody dl { display: grid; grid-template-columns: max-content 1fr; gap: 6px 12px; margin: 0 0 14px; }
#dbody dt { color: var(--muted); font-size: 12px; padding-top: 1px; }
#dbody dd { margin: 0; overflow-wrap: anywhere; }
#dbody pre { background: var(--head); border: 1px solid var(--border); border-radius: 6px;
  padding: 10px; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; margin: 0;
  max-height: 50vh; overflow: auto; }
#dbody a, #dbody .link { color: inherit; text-decoration: underline; cursor: pointer;
  background: none; border: 0; padding: 0; font: inherit; }
#dbody .sec { font-size: 12px; color: var(--muted); font-weight: 600; margin: 14px 0 6px; }
#dbody .muted, .dhead .muted { color: var(--muted); }
@media (max-width: 600px) {
  #detail { top: auto; height: 70vh; width: 100vw; border-left: 0;
    border-top: 1px solid var(--border); }
}
.chip { display: inline-block; padding: 0 7px; border-radius: 9px; font-size: 12px;
  color: var(--cell-fg); line-height: 18px; }
.chip.passed { background: var(--passed); } .chip.failed { background: var(--failed); }
.chip.broken { background: var(--broken); } .chip.skipped { background: var(--skipped); }
/* notes: a folded top-left corner on cells (the retry dot is top-right), a count by names */
td.c.noted::before { content: ""; position: absolute; top: 0; left: 0; width: 0; height: 0;
  border-top: 9px solid var(--note); border-right: 9px solid transparent; }
.nbadge { display: inline-block; font-size: 10px; line-height: 13px; padding: 0 4px;
  border-radius: 8px; border: 1px solid var(--muted); color: var(--fg); margin-right: 6px;
  vertical-align: 1px; font-weight: 600; }
.notesbar { display: flex; gap: 4px 10px; flex-wrap: wrap; align-items: center;
  margin: -4px 0 12px; font-size: 12px; }
.notesbar .muted { color: var(--muted); }
.notesbar .link { color: inherit; text-decoration: underline; cursor: pointer; background: none;
  border: 0; padding: 0; font: inherit; }
.tbtn:disabled { opacity: .5; cursor: default; }
.note { border: 1px solid var(--border); border-radius: 6px; padding: 7px 10px; margin: 0 0 8px; }
.note.unsaved { border-style: dashed; }
.nmeta { display: flex; flex-wrap: wrap; gap: 2px 8px; align-items: baseline; font-size: 12px;
  color: var(--muted); margin-bottom: 4px; }
.nmeta b { color: var(--fg); font-weight: 600; }
.nact { margin-left: auto; display: inline-flex; gap: 10px; }
.ntext { white-space: pre-wrap; overflow-wrap: anywhere; }
.tag { font-size: 11px; line-height: 16px; border-radius: 8px; padding: 0 6px;
  border: 1px solid var(--border); background: var(--head); color: var(--fg); }
.tag.unsaved { background: var(--mark); border-color: transparent; }
#dbody textarea, #dbody input, #dbody select { font: inherit; color: var(--fg);
  background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 5px 8px; }
#dbody textarea { display: block; width: 100%; min-height: 64px; resize: vertical; }
.nrow { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-top: 6px; }
.nrow input { flex: 1 1 120px; min-width: 0; }
.nadd { margin-bottom: 8px; }
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
  // Group filters: every test is in exactly one group (t.g, computed in Python).
  // Checked groups are shown; with none checked, every test is.
  const boxes = [['flakyOnly', 'flaky'], ['passOnly', 'passed'], ['failOnly', 'failed'],
                 ['skipOnly', 'skipped']].map(b => [document.getElementById(b[0]), b[1]]);
  function inGroups(t) {
    const on = boxes.filter(b => b[0].checked);
    return !on.length || on.some(b => b[1] === t.g);
  }
  const count = document.getElementById('shown');
  const nCols = 4 + D.runs.length;
  // 'src.tests.test_file.TestClass#test_name[p=1]' -> short 'test_name[p=1]' shown first,
  // and 'test_file.TestClass' after it in grey, so truncation hides the least useful part.
  function splitName(n) {
    const b = n.indexOf('[');
    const base = b > 0 ? n.slice(0, b) : n, params = b > 0 ? n.slice(b) : '';
    let cut = base.lastIndexOf('#'), sep = 1;
    if (cut < 0) { cut = base.lastIndexOf('::'); sep = 2; }   // pytest node id
    if (cut < 0) { cut = base.lastIndexOf('.'); sep = 1; }
    if (cut < 0) return [n, ''];
    const path = base.slice(0, cut).split(/::|[./]/).filter(Boolean);
    return [base.slice(cut + sep) + params, path.slice(-2).join('.')];
  }
  const groupBox = document.getElementById('groupFiles');
  D.tests.forEach(t => {
    const parts = splitName(t.n);
    t.sn = parts[0];
    t.file = t.fi >= 0 ? D.msgs[t.fi] : '';
    t.cls = t.cl || '';
    const base = t.file ? t.file.split('/').pop() : parts[1];
    t.where = [base, t.cls].filter(Boolean).join(' \u203a ');  // flat view: file › class
    // Searched text: the full name, then the file path; the short name ends the name.
    t.l = (t.n + (t.file ? '  ' + t.file : '')).toLowerCase();
    t.snStart = t.n.length - t.sn.length;
    t.snEnd = t.n.length;
  });

  let sorted = D.tests.slice(), view = [], rowH = 23, measured = false, first = -1, last = -1;
  let sel = null;  // {t, c}: the cell shown in the detail panel
  let shownIn = null;  // {t, c}: what the panel shows (c is null for a test summary)
  let noteCell = new Map(), noteTest = new Map(), noteRun = new Map();  // see indexNotes
  const collapsed = new Set();  // file paths whose group is collapsed

  // ---- fuzzy search: every space-separated term must match, as a substring or as
  // letters in order; better matches (contiguous, at word starts, in the test name
  // rather than the path) score higher.
  function boundary(s, k) { return k === 0 || '._#/:[ -'.indexOf(s[k - 1]) >= 0; }
  function matchTerm(t, term) {
    const s = t.l;
    let i = s.indexOf(term, t.snStart);
    if (i >= t.snEnd) i = -1;
    if (i < 0) i = s.indexOf(term);
    if (i >= 0) {
      const pos = [];
      for (let k = 0; k < term.length; k++) pos.push(i + k);
      const inName = i >= t.snStart && i < t.snEnd;
      return [1000 + 10 * term.length + (inName ? 300 : 0) + (boundary(s, i) ? 100 : 0) - i * 0.01, pos];
    }
    // Letters in order, preferring the test-name part: try it first, then the whole text.
    for (const from of [t.snStart, 0]) {
      const pos = [];
      let k = from - 1, score = 0;
      for (const ch of term) {
        const j = s.indexOf(ch, k + 1);
        if (j < 0 || (from && j >= t.snEnd)) { pos.length = 0; break; }
        score += 1 + (j === k + 1 && pos.length ? 8 : 0) + (boundary(s, j) ? 5 : 0)
          + (j >= t.snStart && j < t.snEnd ? 3 : 0) - (pos.length ? Math.min(j - k - 1, 10) * 0.2 : 0);
        pos.push(j); k = j;
      }
      // Letters scattered across a long path aren't a real match.
      if (pos.length === term.length && pos[pos.length - 1] - pos[0] < term.length * 4) return [score, pos];
    }
    return null;
  }
  function fuzzy(t, terms) {
    let score = 0;
    const pos = [];
    for (const term of terms) {
      const m = matchTerm(t, term);
      if (!m) return null;
      score += m[0];
      pos.push.apply(pos, m[1]);
    }
    return [score, pos];
  }

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

  // ---- time helpers (UTC, like the rest of the report)
  function utc(ms, secs) {
    const iso = new Date(ms).toISOString();
    return iso.slice(0, 10) + ' ' + iso.slice(11, secs ? 19 : 16) + ' UTC';
  }
  function startMs(t, i) {
    const v = t.t && t.t[i];
    return v === null || v === undefined ? null : D.t0 + v * 1000;
  }
  function whenText(t, i) {
    const ms = startMs(t, i), u = t.u && t.u[i];
    const parts = [];
    if (ms !== null) parts.push(utc(ms, false));
    if (u) parts.push('run #' + u);
    return parts.join(' \u00b7 ');
  }
  function dur(ms) {
    if (ms === null || ms === undefined) return '';
    if (ms < 1000) return ms + ' ms';
    const s = ms / 1000;
    if (s < 60) return s.toFixed(s < 10 ? 2 : 1) + ' s';
    const m = Math.floor(s / 60);
    if (m < 60) return m + 'm ' + String(Math.round(s % 60)).padStart(2, '0') + 's';
    return Math.floor(m / 60) + 'h ' + String(m % 60).padStart(2, '0') + 'm';
  }

  function row(t) {
    const tr = document.createElement('tr');
    const name = td('name', '', t.n + (t.lf >= 0 ? '\\n\\n' + D.msgs[t.lf] : ''));
    if (t.k) {
      const b = document.createElement('span');
      b.className = 'flaky-badge'; b.textContent = 'flaky'; name.appendChild(b);
    }
    if (t.nn) {
      const b = el('span', 'nbadge', '\\u270e ' + t.nn);
      b.title = t.nn + (t.nn === 1 ? ' note' : ' notes');
      name.appendChild(b);
    }
    const tn = el('span', 'tn');
    const hits = t.hit ? new Set(t.hit.filter(k => k >= t.snStart && k < t.snEnd)) : null;
    if (hits && hits.size) {
      let run = '', marked = false;
      const flush = () => {
        if (!run) return;
        tn.appendChild(marked ? el('mark', '', run) : document.createTextNode(run));
        run = '';
      };
      for (let k = 0; k < t.sn.length; k++) {
        const m = hits.has(t.snStart + k);
        if (m !== marked) { flush(); marked = m; }
        run += t.sn[k];
      }
      flush();
    } else tn.textContent = t.sn;
    name.appendChild(tn);
    const where = grouped() ? t.cls : t.where;
    if (where) name.appendChild(el('span', 'tp', where));
    tr.appendChild(name);
    tr.appendChild(td('num', String(t.f)));
    tr.appendChild(td('num', Math.round(t.r * 100) + '%'));
    tr.appendChild(td('num', t.x + '/' + t.p));
    for (let i = 0; i < t.s.length; i++) {
      const ch = t.s[i], tip = [D.runs[i], NAME[ch]];
      let cls = 'c ' + CLS[ch];
      if (sel && sel.t === t && sel.c === i) cls += ' sel';
      const when = whenText(t, i);
      if (when) tip.push(when);
      const att = t.a && t.a[i];
      if (att) { cls += ' retry'; tip.push('attempts: ' + att); }
      const o = t.o && t.o[i];
      if (o !== undefined) tip.push('at ' + D.msgs[o]);
      const m = t.m && t.m[i];
      if (m !== undefined) tip.push(D.msgs[m]);
      const nc = noteCell.get(t.i + ':' + i);
      if (nc) { cls += ' noted'; tip.push(nc.length + (nc.length === 1 ? ' note' : ' notes')); }
      let cell;
      // Per-execution mode: '.' just pads shorter timelines, so leave it blank.
      if (ch === '.' && D.pe) cell = td(cls, '', '');
      else cell = td(cls, ch, tip.join('\\n'));
      cell.dataset.c = i;
      tr.appendChild(cell);
    }
    tr.dataset.i = t.i;
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
    for (let i = start; i < end; i++) frag.appendChild(view[i].hdr ? groupRow(view[i].hdr) : row(view[i]));
    frag.appendChild(bottomPad);
    tbody.replaceChildren(frag);
    if (!measured && end > start) {
      measured = true;
      const probe = [...tbody.rows].find(r => r.dataset.i !== undefined) || tbody.rows[1];
      const h = probe.getBoundingClientRect().height;
      if (h > 0 && Math.abs(h - rowH) > 0.5) { rowH = h; render(true); }
    }
  }

  function grouped() { return groupBox.checked; }

  function groupRow(g) {
    const tr = document.createElement('tr');
    tr.className = 'grp' + (g.open ? '' : ' closed');
    tr.dataset.file = g.file;
    const cell = document.createElement('td');
    cell.colSpan = nCols;
    const inner = el('span', 'gin');
    inner.appendChild(el('span', 'caret', g.open ? '\u25be' : '\u25b8'));
    inner.appendChild(el('span', 'gfile', g.file || '(no file recorded)'));
    const bits = [g.tests.length + (g.tests.length === 1 ? ' test' : ' tests')];
    if (g.flaky) bits.push(g.flaky + ' flaky');
    if (g.failing) bits.push(g.failing + ' never passed');
    inner.appendChild(el('span', 'gmeta', bits.join(' \u00b7 ')));
    cell.appendChild(inner);
    tr.appendChild(cell);
    return tr;
  }

  function apply(resetScroll) {
    // Setting scrollTop forces a layout, so only do it when needed.
    if (resetScroll && wrap.scrollTop) wrap.scrollTop = 0;  // new filter/sort: show top matches
    const terms = q.value.trim().toLowerCase().split(/\\s+/).filter(Boolean);
    let tests = [], hiddenMatches = 0;
    for (const t of sorted) {
      let m = null;
      if (terms.length) {
        m = fuzzy(t, terms);
        if (!m) continue;
      }
      if (!inGroups(t)) { hiddenMatches++; continue; }
      t.hit = m ? m[1] : null;
      t.score = m ? m[0] : 0;
      tests.push(t);
    }
    // While searching (and no column sort was chosen), best matches first.
    if (terms.length && sortKey === null) tests.sort((a, b) => b.score - a.score || a.i - b.i);

    if (!grouped()) {
      view = tests;
    } else {
      const groups = new Map();
      tests.forEach((t, order) => {
        let g = groups.get(t.file);
        if (!g) {
          g = {file: t.file, tests: [], flaky: 0, failing: 0, first: order};
          groups.set(t.file, g);
        }
        g.tests.push(t);
        if (t.g === 'flaky') g.flaky++;
        if (t.g === 'failed') g.failing++;
      });
      // Groups follow their best-placed test, so the overall order still wins
      // (most flaky / best match / chosen column first). Sorting by name sorts files.
      let list = [...groups.values()];
      if (sortKey === 'name') {
        list.sort((a, b) => (asc ? 1 : -1) * a.file.localeCompare(b.file));
      } else list.sort((a, b) => a.first - b.first);
      view = [];
      for (const g of list) {
        g.open = terms.length > 0 || !collapsed.has(g.file);
        view.push({hdr: g});  // file header row
        if (g.open) view.push.apply(view, g.tests);
      }
    }
    render(true);
    count.textContent = tests.length;
    hint.textContent = terms.length && hiddenMatches
      ? hiddenMatches + ' more ' + (hiddenMatches === 1 ? 'match' : 'matches') + ' in unchecked groups'
      : '';
  }
  const hint = document.getElementById('hint');

  // Typing fast re-filters at most once per frame.
  let applyPending = false;
  function applySoon() {
    if (applyPending) return;
    applyPending = true;
    requestAnimationFrame(() => { applyPending = false; apply(true); });
  }

  const KEY = {name: t => t.sn.toLowerCase(), flips: t => t.f, rate: t => t.r, fails: t => t.x};
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
  boxes.forEach(b => b[0].addEventListener('change', () => apply(true)));
  groupBox.addEventListener('change', () => apply(true));
  document.getElementById('collapseAll').addEventListener('click', () => {
    const files = new Set(D.tests.map(t => t.file));
    const allClosed = [...files].every(f => collapsed.has(f));
    collapsed.clear();
    if (!allClosed) files.forEach(f => collapsed.add(f));
    apply(false);
  });
  q.addEventListener('keydown', e => {
    if (e.key === 'Escape' && q.value) { q.value = ''; apply(true); e.stopPropagation(); }
    else if (e.key === 'Enter') {
      e.preventDefault();  // else the same keypress activates the panel's focused close button
      const t = view.find(x => !x.hdr);
      if (t) showTest(t);
    }
  });
  document.addEventListener('keydown', e => {
    if (e.key === '/' && document.activeElement !== q && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) {
      e.preventDefault(); q.focus(); q.select();
    }
  });

  // ---- detail panel: click a cell for that execution, a test name for the test
  const panel = document.getElementById('detail');
  const body = document.getElementById('dbody');
  const prevB = document.getElementById('dprev'), nextB = document.getElementById('dnext');
  const posEl = document.getElementById('dpos');

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function field(dl, label, value) {
    if (value === undefined || value === null || value === '') return;
    dl.appendChild(el('dt', '', label));
    const dd = el('dd');
    if (value instanceof Node) dd.appendChild(value); else dd.textContent = value;
    dl.appendChild(dd);
  }
  function chip(code) {
    return el('span', 'chip ' + CLS[code], NAME[code]);
  }
  function present(t) {
    const out = [];
    for (let i = 0; i < t.s.length; i++) if (t.s[i] !== '.') out.push(i);
    return out;
  }
  function runLink(t, i) {
    const u = t.u && t.u[i];
    if (u) return 'Run #' + u + ' of ' + D.nruns;
    if (D.pe) return null;
    const url = D.urls[i];
    if (url) {
      const a = el('a', '', D.runs[i]);
      a.href = url; a.target = '_blank'; a.rel = 'noopener';
      return a;
    }
    return D.runs[i];
  }

  function markSelected() {
    tbody.querySelectorAll('td.c.sel').forEach(c => c.classList.remove('sel'));
    if (!sel) return;
    const tr = tbody.querySelector('tr[data-i="' + sel.t.i + '"]');
    const c = tr && tr.querySelector('td.c[data-c="' + sel.c + '"]');
    if (c) c.classList.add('sel');
  }

  function showCell(t, i) {
    sel = {t: t, c: i};
    shownIn = {t: t, c: i};
    const code = t.s[i];
    body.replaceChildren();
    body.appendChild(chip(code));
    body.appendChild(el('h3', '', t.sn));
    body.appendChild(el('div', 'fullname', t.n));
    const dl = el('dl');
    field(dl, 'File', t.file);
    field(dl, 'Class', t.cls);
    field(dl, 'Run', runLink(t, i));
    const ms = startMs(t, i);
    field(dl, 'Started', ms !== null ? utc(ms, true) : '');
    field(dl, 'Duration', dur(t.d && t.d[i]));
    field(dl, 'Attempts', t.a && t.a[i]);
    field(dl, 'Failed at', t.o && t.o[i] !== undefined ? D.msgs[t.o[i]] : '');
    body.appendChild(dl);
    if (t.m && t.m[i] !== undefined) {
      body.appendChild(el('div', 'sec', 'Message'));
      body.appendChild(el('pre', '', D.msgs[t.m[i]]));
    }
    cellNotes(t, i);
    body.appendChild(el('div', 'sec', 'This test overall'));
    const sdl = el('dl');
    field(sdl, 'Flips', t.f + ' (' + Math.round(t.r * 100) + '% of chances)');
    field(sdl, 'Fails', t.x + ' of ' + t.p);
    body.appendChild(sdl);
    const idx = present(t), k = idx.indexOf(i);
    posEl.textContent = (D.pe ? 'Execution ' : 'Run ') + (k + 1) + ' of ' + idx.length;
    prevB.disabled = k <= 0; nextB.disabled = k >= idx.length - 1;
    prevB.hidden = nextB.hidden = false;
    openPanel();
    markSelected();
  }

  function showTest(t) {
    sel = null;
    shownIn = {t: t, c: null};
    body.replaceChildren();
    body.appendChild(el('h3', '', t.sn));
    body.appendChild(el('div', 'fullname', t.n));
    const dl = el('dl');
    field(dl, 'File', t.file);
    field(dl, 'Class', t.cls);
    field(dl, 'Flaky', t.k ? 'yes' : 'no');
    field(dl, 'Flips', t.f + ' (' + Math.round(t.r * 100) + '% of chances)');
    field(dl, 'Fails', t.x + ' of ' + t.p + (D.pe ? ' executions' : ' runs'));
    body.appendChild(dl);
    if (t.lf >= 0) body.appendChild(el('pre', '', D.msgs[t.lf]));
    const idx = present(t);
    if (idx.length) {
      const b = el('button', 'link', 'Open the latest ' + (D.pe ? 'execution' : 'run'));
      b.type = 'button';
      b.addEventListener('click', () => showCell(t, idx[idx.length - 1]));
      const p = el('p'); p.appendChild(b); body.appendChild(p);
    }
    const all = noteTest.get(t.i) || [];
    body.appendChild(el('div', 'sec', all.length ? 'Notes (' + all.length + ')' : 'Notes'));
    all.forEach(v => body.appendChild(noteItem(v, t, null, false)));
    body.appendChild(addBox(t, null));
    posEl.textContent = '';
    prevB.hidden = nextB.hidden = true;
    openPanel();
    markSelected();
  }

  function openPanel() {
    panel.hidden = false;
    document.getElementById('dclose').focus({preventScroll: true});
  }
  function closePanel() {
    panel.hidden = true; sel = null; shownIn = null; markSelected();
  }
  function step(dir) {
    if (!sel) return;
    const idx = present(sel.t), k = idx.indexOf(sel.c) + dir;
    if (k >= 0 && k < idx.length) showCell(sel.t, idx[k]);
  }

  tbody.addEventListener('click', e => {
    const g = e.target.closest('tr.grp');
    if (g) {
      const f = g.dataset.file;
      if (collapsed.has(f)) collapsed.delete(f); else collapsed.add(f);
      apply(false);
      return;
    }
    const tr = e.target.closest('tr[data-i]');
    if (!tr) return;
    const t = D.tests[+tr.dataset.i];
    const c = e.target.closest('td.c');
    if (c) { if (t.s[+c.dataset.c] !== '.') showCell(t, +c.dataset.c); }
    else if (e.target.closest('td.name')) showTest(t);
  });
  prevB.addEventListener('click', () => step(-1));
  nextB.addEventListener('click', () => step(1));
  document.getElementById('dclose').addEventListener('click', closePanel);
  document.addEventListener('keydown', e => {
    if (panel.hidden) return;
    if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName) && body.contains(e.target)) {
      if (e.key === 'Escape') e.target.blur();  // keep the draft; a second Esc closes
      return;
    }
    if (e.key === 'Escape') closePanel();
    else if (e.target === q) return;
    else if (e.key === 'ArrowLeft') { step(-1); e.preventDefault(); }
    else if (e.key === 'ArrowRight') { step(1); e.preventDefault(); }
  });

  // ---- notes: from the notes file (D.notes, embedded when the report was built,
  // tombstones of deleted notes included), plus notes added, edited or deleted here.
  // Those are "pending" in localStorage until saved into the notes file: directly where
  // the browser can write files (File System Access API), else as a downloaded export
  // that the next allure-history run merges in. Note text only goes through textContent.
  const STORE = 'allure-history-notes:' + D.nk, AUTHOR = 'allure-history-author';
  const FSA = typeof window.showSaveFilePicker === 'function';
  const saveB = document.getElementById('saveNotes');
  const modeEl = document.getElementById('nmode'), msgEl = document.getElementById('nmsg');
  const byKey = new Map(D.tests.map(t => [t.h, t]));

  // Storage can be blocked (private mode, policies): then notes last while the page is open.
  function load(key) {
    try { return JSON.parse(localStorage.getItem(key)); } catch (e) { return null; }
  }
  function keep(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch (e) { return false; }
  }

  // The rules of notes.py (parse_note, newer, merge_notes), so that saving here and
  // merging there produce the same file: union by id, the newest 'updated' wins, ties
  // go to the larger canonical JSON, and deletions are tombstones.
  function str1(v, cap) {
    if (typeof v !== 'string' && !(typeof v === 'number' && isFinite(v))) return '';
    return String(v).replace(/[\\x00-\\x1f\\x7f]+/g, ' ').trim().slice(0, cap);
  }
  function noteText(v) {
    if (typeof v !== 'string') return '';
    return v.replace(/\\r\\n/g, '\\n').replace(/\\t/g, '    ')
      .replace(/[\\x00-\\x08\\x0b-\\x1f\\x7f]+/g, ' ').trim().slice(0, 10000);
  }
  function msTime(v) {
    return typeof v === 'number' && isFinite(v) && v >= 0 && v < 1e14 ? Math.trunc(v) : null;
  }
  function parseNote(e) {
    if (!e || typeof e !== 'object' || Array.isArray(e)) return null;
    const id = str1(e.id, 100), test = str1(e.test, 1000), name = str1(e.name, 1000);
    const execution = msTime(e.execution), run = msTime(e.run), created = str1(e.created, 40);
    const updated = str1(e.updated, 40) || created;
    if (e.deleted === true) {
      if (!id) return null;
      const n = {id: id, deleted: true, updated: updated};
      if (test) n.test = test;
      if (name) n.name = name;
      if (execution !== null) n.execution = execution;
      if (run !== null) n.run = run;
      if (created) n.created = created;
      return n;
    }
    const text = noteText(e.text);
    // notes.py derives an id for hand-written notes without one; here they are left in
    // the file as they are, like any other entry that isn't a valid note.
    if (!id || !test || !text) return null;
    return {id: id, test: test, name: name, execution: execution, run: run, text: text,
            author: str1(e.author, 200), created: created, updated: updated};
  }
  function stamp(s) {
    const v = s ? Date.parse(/(Z|[+-]\\d\\d:\\d\\d)$/i.test(s) ? s : s + 'Z') : NaN;
    return isNaN(v) ? -Infinity : v;
  }
  function canonical(n) { return JSON.stringify(n, Object.keys(n).sort()); }
  function newer(a, b) {
    const x = stamp(a.updated), y = stamp(b.updated);
    if (x !== y) return x > y ? a : b;
    return canonical(b) > canonical(a) ? b : a;
  }
  function cmp(a, b) { return a < b ? -1 : a > b ? 1 : 0; }
  function parseList(entries) {
    const m = new Map();
    for (const e of entries) {
      const n = parseNote(e);
      if (n) m.set(n.id, m.has(n.id) ? newer(m.get(n.id), n) : n);
    }
    return m;
  }
  function mergeNotes(current, incoming) {
    const merged = new Map(current.map(n => [n.id, n]));
    const counts = {added: 0, updated: 0, deleted: 0, unchanged: 0};
    for (const n of incoming) {
      const before = merged.get(n.id), after = before ? newer(before, n) : n;
      merged.set(n.id, after);
      if (before && canonical(before) === canonical(after)) counts.unchanged++;
      else if (after.deleted) counts.deleted++;
      else if (!before) counts.added++;
      else counts.updated++;
    }
    const notes = [...merged.values()].sort((a, b) =>
      cmp(a.created || '', b.created || '') || cmp(a.id, b.id));
    return {notes: notes, counts: counts};
  }
  // A notes file's entries merged with notes: what gets written back.
  function mergeFile(raw, incoming) {
    const r = mergeNotes([...parseList(raw).values()], incoming);
    r.entries = r.notes.concat(raw.filter(e => !parseNote(e)));
    return r;
  }
  // For scripts (and the test comparing this merge with notes.py's)
  window.allureHistoryNotes = {parseNote: parseNote, parseList: parseList, mergeFile: mergeFile};

  const embedded = parseList(D.notes);
  const local = load(STORE) || {};
  // saved: what this page wrote into the notes file since the report was built
  const saved = parseList(Array.isArray(local.saved) ? local.saved : []);
  const pend = parseList(Array.isArray(local.pending) ? local.pending : []);
  let base;  // the notes file as far as this page knows
  function settle() {
    // Once the report is regenerated with a change in it, it is no longer pending.
    base = new Map(embedded);
    for (const [id, n] of [...saved]) {
      const e = embedded.get(id);
      if (e && stamp(e.updated) >= stamp(n.updated)) saved.delete(id); else base.set(id, n);
    }
    for (const [id, n] of [...pend]) {
      const e = base.get(id);
      if (e && stamp(e.updated) >= stamp(n.updated)) pend.delete(id);
    }
  }
  function persist() {
    if (!keep(STORE, {pending: [...pend.values()], saved: [...saved.values()]}) && pend.size) {
      say('This browser blocks storage, so unsaved notes last only while this page is open.');
    }
  }
  function say(text) { msgEl.textContent = text; }

  function runOf(t, i) { return D.pe ? (t.u && t.u[i]) || null : i; }
  function runStart(t, i) {
    const r = runOf(t, i);
    return r === null ? null : msTime(D.pe ? D.rs[r - 1] : D.rs[r]);
  }
  // Where a note goes: test row t, cell c (-1: none), run ru (null: none).
  function place(n) {
    const t = byKey.get(n.test) || null;
    const v = {n: n, t: t, c: -1, ru: null, unsaved: pend.has(n.id)};
    const k = n.run === null ? -1 : D.rs.indexOf(n.run);
    if (k >= 0) v.ru = D.pe ? k + 1 : k;
    if (t && n.execution !== null) {
      // The report knows start times only to the second, so several executions can
      // match: prefer one in the note's run, then the last (a retry's final attempt).
      const sec = Math.floor(n.execution / 1000) * 1000;
      let any = -1;
      for (let i = 0; i < t.s.length; i++) {
        if (t.s[i] === '.' || startMs(t, i) !== sec) continue;
        any = i;
        if (v.ru === null || runOf(t, i) === v.ru) v.c = i;
      }
      if (v.c < 0) v.c = any;
      // Per run, a cell is the test's result in that run, whichever attempt it was.
      if (v.c < 0 && k >= 0 && !D.pe && t.s[k] !== '.') v.c = k;
    }
    if (v.c >= 0) v.ru = runOf(t, v.c);
    return v;
  }
  function indexNotes() {
    const all = new Map(base);
    pend.forEach((n, id) => all.set(id, n));
    noteCell = new Map(); noteTest = new Map(); noteRun = new Map();
    D.tests.forEach(t => { t.nn = 0; });
    const push = (m, k, v) => { const l = m.get(k); if (l) l.push(v); else m.set(k, [v]); };
    const list = [...all.values()].filter(n => !n.deleted)
      .sort((a, b) => cmp(b.created || '', a.created || '') || cmp(a.id, b.id));  // newest first
    for (const n of list) {
      const v = place(n);
      if (v.t) {
        v.t.nn++;
        push(noteTest, v.t.i, v);
        if (v.c >= 0) push(noteCell, v.t.i + ':' + v.c, v);
      }
      if (v.ru !== null) push(noteRun, v.ru, v);
    }
    updateBar();
  }

  function nowIso(after) {
    // Strictly after the version being replaced, even if that author's clock was ahead.
    return new Date(Math.max(Date.now(), after ? stamp(after) + 1 : 0)).toISOString();
  }
  function newId() {
    const b = new Uint8Array(16);
    try { crypto.getRandomValues(b); } catch (e) { for (let k = 0; k < 16; k++) b[k] = Math.random() * 256; }
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;  // uuid4
    return Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
  }
  function change(n) {
    if (!n) return;
    pend.set(n.id, n);
    persist();
    indexNotes();
    render(true);
    refreshPanel();
  }
  function refreshPanel() {
    if (!shownIn || panel.hidden) return;
    if (shownIn.c === null) showTest(shownIn.t); else showCell(shownIn.t, shownIn.c);
  }

  function cellNotes(t, i) {
    const here = noteCell.get(t.i + ':' + i) || [];
    const ru = runOf(t, i);
    const same = ru === null ? [] : (noteRun.get(ru) || []).filter(v => !(v.t === t && v.c === i));
    const general = (noteTest.get(t.i) || []).filter(v => v.n.execution === null);
    const n = here.length + same.length + general.length;
    body.appendChild(el('div', 'sec', n ? 'Notes (' + n + ')' : 'Notes'));
    here.forEach(v => body.appendChild(noteItem(v, t, i, false)));
    same.forEach(v => body.appendChild(noteItem(v, t, i, true)));
    general.forEach(v => body.appendChild(noteItem(v, t, i, false)));
    body.appendChild(addBox(t, i));
  }

  // One note in the panel, which shows test t (and cell c, or null for the test summary).
  function noteItem(v, t, c, sameRun) {
    const n = v.n, box = el('div', 'note' + (v.unsaved ? ' unsaved' : ''));
    const meta = el('div', 'nmeta');
    if (sameRun) meta.appendChild(el('span', 'tag', 'same run'));
    let where = null;
    if (v.t !== t) where = v.t.sn + (v.c >= 0 ? ' \\u00b7 ' + (whenText(v.t, v.c) || 'execution') : '');
    else if (v.c >= 0) { if (v.c !== c) where = whenText(t, v.c) || 'execution ' + (v.c + 1); }
    else where = n.execution === null ? 'whole test' : 'execution not in this report';
    if (where !== null && (v.t !== t || v.c >= 0)) {
      const b = el('button', 'link', where);
      b.type = 'button';
      b.title = v.c >= 0 ? 'Show this execution' : 'Show this test';
      b.addEventListener('click', () => { if (v.c >= 0) showCell(v.t, v.c); else showTest(v.t); });
      meta.appendChild(b);
    } else if (where !== null) meta.appendChild(el('span', '', where));
    if (n.author) meta.appendChild(el('b', '', n.author));
    const created = stamp(n.created);
    if (created !== -Infinity) meta.appendChild(el('span', '', utc(created, false)));
    if (n.updated && n.created && n.updated !== n.created) meta.appendChild(el('span', '', '(edited)'));
    if (v.unsaved) {
      const u = el('span', 'tag unsaved', 'unsaved');
      u.title = 'Kept in this browser until you save notes';
      meta.appendChild(u);
    }
    const act = el('span', 'nact');
    const edit = el('button', 'link', 'Edit'), del = el('button', 'link', 'Delete');
    edit.type = del.type = 'button';
    const text = el('div', 'ntext', n.text);
    edit.addEventListener('click', () => {
      const editor = el('div'), ta = el('textarea'), row = el('div', 'nrow');
      ta.value = n.text;
      ta.setAttribute('aria-label', 'Note text');
      const ok = el('button', 'tbtn', 'Save'), no = el('button', 'tbtn', 'Cancel');
      ok.type = no.type = 'button';
      const done = () => {
        const value = noteText(ta.value);
        if (!value) { ta.focus(); return; }
        if (value === n.text) { refreshPanel(); return; }
        change(Object.assign({}, n, {text: value, updated: nowIso(n.updated)}));
      };
      ok.addEventListener('click', done);
      no.addEventListener('click', refreshPanel);
      ta.addEventListener('keydown', e => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); done(); }
      });
      row.appendChild(ok); row.appendChild(no);
      editor.appendChild(ta); editor.appendChild(row);
      text.replaceWith(editor);
      act.hidden = true;
      ta.focus();
    });
    del.addEventListener('click', () => {
      if (!window.confirm('Delete this note?')) return;
      change(parseNote({id: n.id, deleted: true, updated: nowIso(n.updated), test: n.test,
                        name: n.name, execution: n.execution, run: n.run, created: n.created}));
    });
    act.appendChild(edit); act.appendChild(del);
    meta.appendChild(act);
    box.appendChild(meta);
    box.appendChild(text);
    return box;
  }

  // The form for a new note on test t: on cell i, or (i null) on the test.
  function addBox(t, i) {
    const box = el('div', 'nadd'), ta = el('textarea'), row = el('div', 'nrow');
    ta.placeholder = i === null ? 'Add a note on this test\\u2026'
      : 'Add a note\\u2026 e.g. why it failed, a ticket, the fix';
    ta.setAttribute('aria-label', 'New note');
    const who = el('input'), me = load(AUTHOR);
    who.type = 'text'; who.placeholder = 'Your name (optional)';
    who.setAttribute('aria-label', 'Your name');
    who.value = typeof me === 'string' ? me : '';
    let scope = null;
    if (i !== null) {
      scope = el('select');
      scope.setAttribute('aria-label', 'What the note is about');
      [['exec', 'on this execution'], ['test', 'on the whole test']].forEach(o => {
        const opt = el('option', '', o[1]);
        opt.value = o[0];
        scope.appendChild(opt);
      });
    }
    const add = el('button', 'tbtn', 'Add note');
    add.type = 'button';
    const submit = () => {
      const text = noteText(ta.value);
      if (!text) { ta.focus(); return; }
      const author = str1(who.value, 200);
      keep(AUTHOR, author);
      const on = scope && scope.value === 'exec' ? i : null, now = nowIso();
      change(parseNote({id: newId(), test: t.h, name: t.n,
                        execution: on === null ? null : startMs(t, on),
                        run: on === null ? null : runStart(t, on),
                        text: text, author: author, created: now, updated: now}));
    };
    add.addEventListener('click', submit);
    ta.addEventListener('keydown', e => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); }
    });
    box.appendChild(ta);
    row.appendChild(who);
    if (scope) row.appendChild(scope);
    row.appendChild(add);
    box.appendChild(row);
    return box;
  }

  // ---- saving into the notes file
  let handle = null;  // the notes file (FileSystemFileHandle), once picked; kept in IndexedDB
  function idb(mode, op) {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open('allure-history', 1);
      req.onupgradeneeded = () => req.result.createObjectStore('files');
      req.onerror = () => reject(req.error);
      req.onsuccess = () => {
        const db = req.result, tx = db.transaction('files', mode), r = op(tx.objectStore('files'));
        tx.oncomplete = () => { db.close(); resolve(r.result); };
        tx.onerror = tx.onabort = () => { db.close(); reject(tx.error); };
      };
    });
  }
  function updateBar() {
    const n = pend.size;
    saveB.textContent = (FSA ? 'Save notes' : 'Export notes') + (n ? ' (' + n + ' unsaved)' : '');
    saveB.disabled = !n && !FSA;
    modeEl.replaceChildren();
    if (!FSA) {
      modeEl.textContent = 'Downloads a file; the next allure-history run merges it into ' + D.nf;
      return;
    }
    modeEl.textContent = handle ? 'Saves straight into ' + handle.name
      : 'Saves straight into your ' + D.nf + ' (pick it the first time)';
    if (handle) {
      const b = el('button', 'link', 'use another file');
      b.type = 'button';
      b.addEventListener('click', () => {
        handle = null;
        idb('readwrite', s => s.delete(STORE)).catch(() => {});
        updateBar();
      });
      modeEl.appendChild(document.createTextNode(' \\u00b7 '));
      modeEl.appendChild(b);
    }
  }
  function countsText(c) {
    const bits = ['added', 'updated', 'deleted'].filter(k => c[k]).map(k => c[k] + ' ' + k);
    return bits.length ? bits.join(', ') : 'no changes';
  }

  async function saveDirect() {
    const kept = ' Your notes are still kept in this browser.';
    let h = handle;
    if (!h) {
      try {
        h = await window.showSaveFilePicker({suggestedName: D.nf,
          types: [{description: 'Notes file', accept: {'application/json': ['.json']}}]});
      } catch (e) { say('Not saved: no file chosen.' + kept); return; }
    }
    try {
      let perm = h.queryPermission ? await h.queryPermission({mode: 'readwrite'}) : 'granted';
      if (perm !== 'granted' && h.requestPermission) perm = await h.requestPermission({mode: 'readwrite'});
      if (perm !== 'granted') { say('Not saved: no permission to write ' + h.name + '.' + kept); return; }
      // Merge with what the file holds now: teammates may have added notes since.
      const current = await (await h.getFile()).text();
      const raw = current.trim() ? JSON.parse(current) : [];
      if (!Array.isArray(raw)) throw new Error(h.name + ' is not a list of notes, so it was left alone');
      const sent = new Map(pend), all = new Map(base);
      sent.forEach((n, id) => all.set(id, n));
      const r = mergeFile(raw, [...all.values()]);
      const w = await h.createWritable();
      await w.write(JSON.stringify(r.entries, null, 2) + '\\n');
      await w.close();
      handle = h;
      idb('readwrite', s => s.put(h, STORE)).catch(() => {});
      // Show what the file has now, teammates' notes too, until the report is rebuilt.
      for (const n of r.notes) {
        const e = embedded.get(n.id);
        if (!e || canonical(e) !== canonical(n)) saved.set(n.id, n);
      }
      sent.forEach((n, id) => { if (pend.get(id) === n) pend.delete(id); });
      settle();
      persist();
      indexNotes();
      render(true);
      refreshPanel();
      say('Saved to ' + h.name + ' (' + countsText(r.counts) + ').');
    } catch (e) {
      say('Not saved: ' + ((e && e.message) || e) + '.' + kept);
    }
  }
  function download() {
    const all = new Map(base);
    pend.forEach((n, id) => all.set(id, n));
    const entries = mergeFile([], [...all.values()]).entries;
    // A new name each time, so browsers don't rename it to "... (1).json"
    const name = 'allure-notes-export-' + new Date().toISOString().slice(0, 19).replace(/[-:]/g, '') + '.json';
    const a = el('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(entries, null, 2) + '\\n'],
                                          {type: 'application/json'}));
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
    say('Downloaded ' + name + '. Put it next to ' + D.nf + '; the next allure-history run merges it in.');
  }
  let saving = false;
  saveB.addEventListener('click', async () => {
    if (saving) return;
    saving = true;
    saveB.disabled = true;
    try {
      if (FSA) await saveDirect(); else download();
    } catch (e) {
      say('Not saved: ' + ((e && e.message) || e) + '.');
    } finally {
      saving = false;
      updateBar();
    }
  });

  settle();
  if (Object.keys(local).length) persist();
  indexNotes();
  if (FSA) {
    idb('readonly', s => s.get(STORE)).then(h => {
      if (h && typeof h.getFile === 'function') { handle = h; updateBar(); }
    }, () => {});
  }

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

    run_of = {(e.key, e.attempt.start, e.attempt.status): r.index
              for r in h.detected_runs for e in r.executions}
    starts = [c.when for t in h.tests for c in t.cells if c.when is not None]
    t0 = (min(starts) // 1000) * 1000 if starts else 0
    tests = []
    for rank, t in enumerate(h.tests):
        attempts, messages, locations = {}, {}, {}
        times, durations, run_nos = [], [], []
        for i, c in enumerate(t.cells):
            if c.retried:
                attempts[i] = " → ".join(c.attempts)
            if c.message:
                messages[i] = msg(c.message)
            if c.location:
                locations[i] = msg(c.location)
            times.append((c.when - t0) // 1000 if c.when is not None else None)
            durations.append(c.stop - c.when if c.when is not None and c.stop is not None
                             and c.stop >= c.when else None)
            run_nos.append(run_of.get((t.key, c.when, c.status)) if c.status else None)
        entry = {
            "i": rank, "h": t.key, "n": t.name, "k": int(t.is_flaky), "f": t.flips,
            "r": round(t.flip_rate, 4), "x": t.counts["failed"] + t.counts["broken"],
            "p": t.runs_present, "s": "".join(_CODE.get(c.status, "?") for c in t.cells),
            "g": test_group(t), "fi": msg(t.file) if t.file else -1, "cl": t.cls,
            "lf": msg(_reasons_text(t)) if t.failure_reasons else -1,
        }
        if attempts:
            entry["a"] = attempts
        if messages:
            entry["m"] = messages
        if locations:
            entry["o"] = locations
        # Dense per-cell arrays, only when the test has any data for them
        if any(v is not None for v in times):
            entry["t"] = times      # start, seconds after t0
        if any(v is not None for v in durations):
            entry["d"] = durations  # milliseconds
        if any(run_nos):
            entry["u"] = run_nos    # detected run number (per-execution mode)
        tests.append(entry)
    # Run starts, so a note's "run" can be matched to a run: detected run N is rs[N-1]
    # in per-execution mode, else column i is rs[i].
    run_starts = [r.start for r in (h.detected_runs if h.per_execution else h.runs)]
    return {"runs": [r.label for r in h.runs], "urls": [r.url for r in h.runs],
            "tests": tests, "msgs": msgs, "pe": int(h.per_execution), "t0": t0,
            "nruns": len(h.detected_runs), "rs": run_starts,
            # Tombstones too: saving merges them into the notes file.
            "notes": [n.to_dict() for n in h.notes]}


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


def test_group(t: TestHistory) -> str:
    """The one filter group a test belongs to: flaky (mixed results), failed (never
    passed), passed (never failed) or skipped (skipped every time). Skips are ignored
    for passed/failed, like flip counting."""
    if t.is_flaky:
        return "flaky"
    if t.counts["failed"] + t.counts["broken"]:
        return "failed"
    if t.counts["passed"]:
        return "passed"
    return "skipped"


_GROUP_BOXES = [
    ("flakyOnly", "flaky", "Flaky only", "Tests with both passing and failing results"),
    ("passOnly", "passed", "Only passed", "Tests that never failed (skips ignored)"),
    ("failOnly", "failed", "Only failed", "Tests that never passed (skips ignored)"),
    ("skipOnly", "skipped", "Only skipped", "Tests that were skipped every time"),
]


def _filter_boxes(h: History) -> str:
    n = {group: 0 for _, group, _, _ in _GROUP_BOXES}
    for t in h.tests:
        n[test_group(t)] += 1
    return "".join(
        f'<label title="{tip}"><input id="{id_}" type="checkbox"'
        f'{" checked" if group == "flaky" and n["flaky"] else ""}> {label} '
        f'<span class="muted">({n[group]})</span></label>'
        for id_, group, label, tip in _GROUP_BOXES)


# Its text is filled in by the page script, which knows whether the browser can save
# into the notes file directly.
_NOTES_BAR = """<div class="notesbar">
  <button type="button" id="saveNotes" class="tbtn">Save notes</button>
  <span id="nmode" class="muted"></span>
  <span id="nmsg" role="status"></span>
</div>"""


def render_html(h: History, title: str = "Test History", notes_name: str = NOTES_FILE,
                notes_key: str = "") -> str:
    """notes_name is the notes file's name (offered when saving notes); notes_key tells
    this project's notes apart from other reports' in the browser's storage."""
    flaky = h.flaky
    data = _page_data(h) if h.tests else None
    if data is not None:
        data["nf"], data["nk"] = notes_name, f"{title}|{notes_key or notes_name}"
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
         '<span>&#8226; dot = retried within run</span><span>. = not run</span>'
         ) + '<span>&#9700; corner = has notes</span>'

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
<script id="history-data" type="application/json">{_script_json(data)}</script>
<script>{_JS}</script>"""
    else:
        table = '<div class="empty">No test results found.</div>'

    span = ""
    if h.per_execution:
        starts = [c.when for t in h.tests for c in t.cells if c.when]
        heading = ("Each row is one test&rsquo;s executions, oldest &rarr; newest; "
                   "the latest is in the rightmost column")
        first_stat = f'<div class="stat"><b>{h.executions}</b><span>executions</span></div>'
        if h.detected_runs:
            first_stat += f'<div class="stat"><b>{len(h.detected_runs)}</b><span>runs</span></div>'
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
  <input id="q" type="search" placeholder="Search tests, files&hellip;  ( / )" aria-label="Search tests"
    autocomplete="off" spellcheck="false">
  <label title="Show tests under their source file"><input id="groupFiles" type="checkbox" checked> Group by file</label>
  <button type="button" id="collapseAll" class="tbtn" title="Collapse or expand all files">Collapse all</button>
  {_filter_boxes(h)}
  <span class="legend">{legend}</span>
  <span style="color:var(--muted)"><span id="shown">{len(h.tests)}</span> shown <span id="hint"></span></span>
</div>
{_NOTES_BAR if h.tests else ''}
<!-- must come before the matrix: the page script inside it looks this panel up -->
<aside id="detail" role="dialog" aria-label="Execution details" hidden>
  <div class="dhead">
    <button type="button" id="dprev" title="Previous execution of this test (&larr;)">&larr;</button>
    <button type="button" id="dnext" title="Next execution of this test (&rarr;)">&rarr;</button>
    <span id="dpos" class="muted"></span>
    <button type="button" id="dclose" title="Close (Esc)">&times;</button>
  </div>
  <div id="dbody"></div>
</aside>
{table}
</body>
</html>
"""
