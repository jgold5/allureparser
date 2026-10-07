"""Load Allure result directories, one directory per CI run."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

STATUSES = ("passed", "failed", "broken", "skipped", "unknown")


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
    if result.get("historyId"):
        return str(result["historyId"])
    base = result.get("fullName") or result.get("name") or result.get("uuid") or "?"
    params = _format_params(result)
    return f"{base}{params}"


def display_name(result: dict) -> str:
    base = result.get("fullName") or result.get("name") or "?"
    return f"{base}{_format_params(result)}"


def _format_params(result: dict) -> str:
    params = [
        p for p in result.get("parameters") or []
        if isinstance(p, dict) and not p.get("excluded") and p.get("mode") != "hidden"
    ]
    if not params:
        return ""
    return "[" + ", ".join(f"{p.get('name')}={p.get('value')}" for p in params) + "]"


def _first_line(text: Optional[str], limit: int = 300) -> str:
    if not text:
        return ""
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:limit]


def is_results_dir(path: Path) -> bool:
    return path.is_dir() and any(path.glob("*-result.json"))


def discover_run_dirs(paths: list[Path]) -> list[Path]:
    """Each path is either a results dir itself, or a parent of per-run results dirs."""
    found: list[Path] = []
    for p in paths:
        if is_results_dir(p):
            found.append(p)
        elif p.is_dir():
            children = sorted(c for c in p.iterdir() if is_results_dir(c))
            if not children:
                print(f"warning: no *-result.json files under {p}", file=sys.stderr)
            found.extend(children)
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


def load_run(path: Path) -> Run:
    executor = {}
    executor_file = path / "executor.json"
    if executor_file.is_file():
        try:
            executor = json.loads(executor_file.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError) as e:
            print(f"warning: could not read {executor_file}: {e}", file=sys.stderr)

    order = executor.get("buildOrder")
    try:
        order = int(order) if order is not None else None
    except (TypeError, ValueError):
        order = None

    run = Run(
        id=path.name,
        label=str(executor.get("buildName") or path.name),
        path=path,
        order=order,
        url=executor.get("buildUrl") or executor.get("reportUrl"),
    )

    grouped: dict[str, tuple[str, list[Attempt]]] = {}
    for f in sorted(path.glob("*-result.json")):
        try:
            result = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            print(f"warning: skipping unreadable {f}: {e}", file=sys.stderr)
            continue
        if not isinstance(result, dict):
            continue
        status = result.get("status") or "unknown"
        if status not in STATUSES:
            status = "unknown"
        attempt = Attempt(
            status=status,
            start=result.get("start"),
            stop=result.get("stop"),
            message=_first_line((result.get("statusDetails") or {}).get("message")),
        )
        key = identity_key(result)
        grouped.setdefault(key, (display_name(result), []))[1].append(attempt)
        if isinstance(attempt.start, (int, float)):
            run.start = attempt.start if run.start is None else min(run.start, attempt.start)

    for key, (name, attempts) in grouped.items():
        # Retries share a historyId; order them chronologically so the last one is final,
        # matching how Allure picks the displayed result.
        attempts.sort(key=lambda a: (a.stop or a.start or 0))
        run.tests[key] = TestRun(key=key, name=name, attempts=attempts)
    return run


def load_runs(paths: list[Path]) -> list[Run]:
    runs = [load_run(d) for d in discover_run_dirs(paths)]
    return sort_runs(runs)


def sort_runs(runs: list[Run]) -> list[Run]:
    """Oldest first. Prefer executor buildOrder, then earliest test start, then name."""
    if runs and all(r.order is not None for r in runs):
        return sorted(runs, key=lambda r: (r.order, r.id))
    return sorted(runs, key=lambda r: (r.start is None, r.start or 0, r.id))
