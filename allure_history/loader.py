"""Load Allure result directories, one directory per CI run."""

from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

STATUSES = ("passed", "failed", "broken", "skipped", "unknown")

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value) -> str:
    """Scalar JSON value as single-line text; anything else (lists, dicts, null) is ''."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return _CONTROL.sub(" ", str(value)).strip()


def _time(value) -> Optional[int]:
    """Epoch milliseconds, or None if missing or not a plausible timestamp."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not 0 <= value < 1e14:
        return None
    return int(value)


def _url(value) -> Optional[str]:
    """Only http(s) links are kept, so a crafted executor.json can't inject javascript: URLs."""
    if isinstance(value, str) and re.match(r"https?://", value.strip(), re.IGNORECASE):
        return value.strip()
    return None


@dataclass
class Attempt:
    """A single execution of a test (one *-result.json file)."""

    status: str
    start: Optional[int]
    stop: Optional[int]
    message: str = ""


@dataclass
class TestRun:
    """All executions of one test within one CI run. The last attempt is the final status."""

    key: str
    name: str
    attempts: list[Attempt]

    @property
    def status(self) -> str:
        return self.attempts[-1].status

    @property
    def message(self) -> str:
        return self.attempts[-1].message


@dataclass
class Run:
    """One CI run, i.e. one allure-results directory."""

    id: str
    label: str
    path: Path
    order: Optional[int] = None
    url: Optional[str] = None
    start: Optional[int] = None
    tests: dict[str, TestRun] = field(default_factory=dict)


def identity_key(result: dict) -> str:
    """Stable identity of a test across runs.

    Allure's historyId already combines the full name and parameters, so it is what
    Allure itself uses to link history. Fall back to fullName + parameters.
    """
    history_id = _text(result.get("historyId"))
    if history_id:
        return history_id
    base = (_text(result.get("fullName")) or _text(result.get("name"))
            or _text(result.get("uuid")) or "?")
    return f"{base}{_format_params(result)}"


def display_name(result: dict) -> str:
    base = _text(result.get("fullName")) or _text(result.get("name")) or "?"
    return f"{base}{_format_params(result)}"


def _format_params(result: dict) -> str:
    raw = result.get("parameters")
    params = [
        p for p in (raw if isinstance(raw, list) else [])
        if isinstance(p, dict) and p.get("excluded") is not True and p.get("mode") != "hidden"
    ]
    if not params:
        return ""
    # Allure renders masked parameters (passwords, tokens) as asterisks; do the same.
    return "[" + ", ".join(
        f"{_text(p.get('name'))}={'******' if p.get('mode') == 'masked' else _text(p.get('value'))}"
        for p in params
    ) + "]"


def _first_line(text, limit: int = 300) -> str:
    if not isinstance(text, str):
        return ""
    lines = text.strip().splitlines()
    return _text(lines[0])[:limit] if lines else ""


def is_results_dir(path: Path) -> bool:
    return path.is_dir() and any(path.glob("*-result.json"))


def _find_results_dirs(root: Path, _seen: Optional[set] = None) -> list[Path]:
    """Results dirs at any depth under root, not descending into a results dir once found."""
    seen = set() if _seen is None else _seen
    real = root.resolve()
    if real in seen:  # symlink loop
        return []
    seen.add(real)
    if is_results_dir(root):
        return [root]
    found = []
    try:
        children = sorted(c for c in root.iterdir() if c.is_dir())
    except OSError as e:
        print(f"warning: cannot list {root}: {e}", file=sys.stderr)
        return []
    for child in children:
        found.extend(_find_results_dirs(child, seen))
    return found


def discover_run_dirs(paths: list[Path]) -> list[Path]:
    """Each path is either a results dir itself, or a directory containing per-run results
    dirs at any depth (e.g. ci/build-12/allure-results/)."""
    found: list[Path] = []
    for p in paths:
        if p.is_dir():
            dirs = _find_results_dirs(p)
            if not dirs:
                print(f"warning: no *-result.json files under {p}", file=sys.stderr)
            found.extend(dirs)
        else:
            print(f"warning: {p} is not a directory, skipping", file=sys.stderr)
    # De-duplicate while preserving order
    seen: set[Path] = set()
    unique = []
    for d in found:
        r = d.resolve()
        if r not in seen:
            seen.add(r)
            unique.append(d)
    return unique


def _read_json(path: Path):
    # utf-8-sig tolerates a byte-order mark, which some Windows tooling writes.
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _order(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        return int(value) if isinstance(value, (int, float, str)) else None
    except ValueError:
        return None


def load_run(path: Path, run_id: Optional[str] = None) -> Run:
    executor = {}
    executor_file = path / "executor.json"
    if executor_file.is_file():
        try:
            executor = _dict(_read_json(executor_file))
        except (ValueError, OSError) as e:
            print(f"warning: could not read {executor_file}: {e}", file=sys.stderr)

    run_id = run_id or path.name
    run = Run(
        id=run_id,
        label=_text(executor.get("buildName")) or run_id,
        path=path,
        order=_order(executor.get("buildOrder")),
        url=_url(executor.get("buildUrl")) or _url(executor.get("reportUrl")),
    )

    grouped: dict[str, tuple[str, list[Attempt]]] = {}
    for f in sorted(path.glob("*-result.json")):
        try:
            result = _read_json(f)
        except (ValueError, OSError) as e:  # JSONDecodeError and UnicodeDecodeError
            print(f"warning: skipping unreadable {f}: {e}", file=sys.stderr)
            continue
        if not isinstance(result, dict):
            print(f"warning: skipping {f}: not a JSON object", file=sys.stderr)
            continue
        status = result.get("status")
        if status not in STATUSES:
            status = "unknown"
        attempt = Attempt(
            status=status,
            start=_time(result.get("start")),
            stop=_time(result.get("stop")),
            message=_first_line(_dict(result.get("statusDetails")).get("message")),
        )
        key = identity_key(result)
        grouped.setdefault(key, (display_name(result), []))[1].append(attempt)
        if attempt.start is not None:
            run.start = attempt.start if run.start is None else min(run.start, attempt.start)

    for key, (name, attempts) in grouped.items():
        # Retries share a historyId. Like Allure, the attempt that started last is the
        # displayed (final) result; attempts without a start time count as oldest.
        attempts.sort(key=lambda a: (a.start is not None, a.start or 0,
                                     a.stop is not None, a.stop or 0))
        run.tests[key] = TestRun(key=key, name=name, attempts=attempts)
    return run


def _run_ids(dirs: list[Path]) -> list[str]:
    """Directory name, or a longer path suffix when names collide
    (e.g. build-1/allure-results and build-2/allure-results)."""
    for depth in range(1, 1 + max((len(d.resolve().parts) for d in dirs), default=1)):
        ids = ["/".join(d.resolve().parts[-depth:]) for d in dirs]
        if len(set(ids)) == len(ids):
            return ids
    return [str(d) for d in dirs]


def load_runs(paths: list[Path]) -> list[Run]:
    dirs = discover_run_dirs(paths)
    runs = [load_run(d, run_id) for d, run_id in zip(dirs, _run_ids(dirs))]
    return sort_runs(runs)


def sort_runs(runs: list[Run]) -> list[Run]:
    """Oldest first. Prefer executor buildOrder, then earliest test start, then name."""
    if runs and all(r.order is not None for r in runs):
        return sorted(runs, key=lambda r: (r.order, r.id))
    return sorted(runs, key=lambda r: (r.start is None, r.start or 0, r.id))
