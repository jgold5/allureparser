"""Compact per-run snapshots, so raw allure-results (traces, steps, attachments) can be
discarded after each CI run while the cross-run history is kept.

A snapshot is gzipped JSON holding only what the history needs: each test's identity,
name, and for every attempt its status, timing, failure message (capped) and failure
location. Stack traces, steps, labels and attachments are not kept.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .loader import (STATUSES, Attempt, Run, TestRun, _dict, _order, _text, _time, _url,
                     clean_message, sort_runs)

FORMAT = "allure-history-snapshot"
VERSION = 2  # 2 added full (capped) messages and failure locations; 1 is still readable
SUFFIX = ".snapshot.json.gz"


def is_snapshot(path: Path) -> bool:
    return path.is_file() and path.name.endswith(SUFFIX)


def to_dict(run: Run) -> dict:
    return {
        "format": FORMAT,
        "version": VERSION,
        "label": run.label,
        "order": run.order,
        "url": run.url,
        "start": run.start,
        "tests": [
            {"k": t.key, "n": t.name,
             "a": [[a.status, a.start, a.stop, a.message, a.location, a.session]
                   for a in t.attempts]}
            for t in run.tests.values()
        ],
    }


def default_name(run: Run) -> str:
    """File name stem: build order, else build name, else start time, else directory."""
    if run.order is not None:
        stem = f"build-{run.order}"
    elif run.label != run.id:  # label came from executor.json buildName
        stem = run.label
    elif run.start is not None:
        stem = datetime.fromtimestamp(run.start / 1000, tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    else:
        stem = run.id
    return re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.") or "run"


def write_snapshot(run: Run, path: Path) -> int:
    """Write atomically; returns the size in bytes."""
    data = json.dumps(to_dict(run), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    blob = gzip.compress(data, mtime=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, path)
    return len(blob)


def read_snapshot(path: Path) -> Optional[Run]:
    """Load a snapshot as a Run, or None (with a warning) if it is not a valid one."""
    try:
        data = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    except (OSError, EOFError, ValueError, RecursionError) as e:  # bad gzip/UTF-8/JSON
        print(f"warning: skipping unreadable snapshot {path}: {e}", file=sys.stderr)
        return None
    data = _dict(data)
    if data.get("format") != FORMAT:
        print(f"warning: skipping {path}: not an allure-history snapshot", file=sys.stderr)
        return None
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version > VERSION:
        print(f"warning: skipping {path}: unsupported snapshot version {version!r}; "
              f"upgrade allure-history", file=sys.stderr)
        return None

    run_id = path.name[: -len(SUFFIX)]
    run = Run(id=run_id, label=_text(data.get("label")) or run_id, path=path,
              order=_order(data.get("order")), url=_url(data.get("url")),
              start=_time(data.get("start")))
    tests = data.get("tests")
    for entry in tests if isinstance(tests, list) else []:
        entry = _dict(entry)
        key = _text(entry.get("k"))
        raw_attempts = entry.get("a")
        if not key or not isinstance(raw_attempts, list):
            continue
        attempts = []
        for a in raw_attempts:
            if not isinstance(a, list) or not a:
                continue
            a = (a + [None] * 6)[:6]  # session (6th) was added later; older files lack it
            status = a[0] if a[0] in STATUSES else "unknown"
            attempts.append(Attempt(status=status, start=_time(a[1]), stop=_time(a[2]),
                                    message=clean_message(a[3]), location=_text(a[4])[:300],
                                    session=_text(a[5])[:200]))
        if attempts:
            run.tests[key] = TestRun(key=key, name=_text(entry.get("n")) or key,
                                     attempts=attempts)
    return run


def prune(directory: Path, keep: int, protect: Optional[Path] = None) -> tuple[list[Path], bool]:
    """Delete all but the `keep` newest snapshots directly inside `directory`. `protect`
    (the snapshot just written) is never deleted, even if it sorts as older. Returns the
    deleted paths and whether `protect` was spared that way."""
    runs = [r for r in (read_snapshot(p) for p in sorted(directory.glob("*" + SUFFIX))) if r]
    ordered = sort_runs(runs)
    doomed = ordered[:-keep] if keep > 0 else []
    spared = False
    if protect is not None:
        before = len(doomed)
        doomed = [r for r in doomed if r.path.resolve() != protect.resolve()]
        spared = len(doomed) < before
    for r in doomed:
        r.path.unlink()
    return [r.path for r in doomed], spared
