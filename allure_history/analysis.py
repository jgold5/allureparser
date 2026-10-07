"""Build the test x run matrix and score flakiness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .loader import Run, TestRun

# Outcome classes used for flip detection. failed and broken both count as "not passing":
# a test going failed -> broken is still consistently red, not flaky. skipped/unknown are
# ignored so that a skipped run between two passes does not count as a flip.
_OUTCOME = {"passed": "P", "failed": "F", "broken": "F"}


def outcome(status: Optional[str]) -> Optional[str]:
    return _OUTCOME.get(status) if status else None


@dataclass
class Cell:
    status: Optional[str]  # None when the test did not run in this run
    attempts: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def retried(self) -> bool:
        return len(self.attempts) > 1

    @property
    def flaky_in_run(self) -> bool:
        """Retries within the run produced both a pass and a fail."""
        classes = {outcome(s) for s in self.attempts} - {None}
        return len(classes) > 1


@dataclass
class TestHistory:
    key: str
    name: str
    cells: list[Cell]
    flips: int = 0
    flip_rate: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)
    in_run_flaky: int = 0
    last_status: Optional[str] = None
    last_failure: str = ""

    @property
    def runs_present(self) -> int:
        return sum(1 for c in self.cells if c.status is not None)

    @property
    def is_flaky(self) -> bool:
        return self.flips > 0 or self.in_run_flaky > 0

    @property
    def sequence(self) -> list[str]:
        return [o for o in (outcome(c.status) for c in self.cells) if o]


@dataclass
class History:
    runs: list[Run]
    tests: list[TestHistory]

    @property
    def flaky(self) -> list[TestHistory]:
        return [t for t in self.tests if t.is_flaky]


def _score(t: TestHistory) -> None:
    seq = t.sequence
    t.flips = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
    t.flip_rate = t.flips / (len(seq) - 1) if len(seq) > 1 else 0.0
    t.counts = {s: 0 for s in ("passed", "failed", "broken", "skipped", "unknown")}
    for c in t.cells:
        if c.status is not None:
            t.counts[c.status] = t.counts.get(c.status, 0) + 1
    t.in_run_flaky = sum(1 for c in t.cells if c.flaky_in_run)
    present = [c for c in t.cells if c.status is not None]
    t.last_status = present[-1].status if present else None
    for c in reversed(present):
        if c.status in ("failed", "broken") and c.message:
            t.last_failure = c.message
            break


def rank_key(t: TestHistory):
    """Most flips first; ties broken by flip rate, in-run retry flakes, failures, name."""
    fails = t.counts.get("failed", 0) + t.counts.get("broken", 0)
    return (-t.flips, -t.flip_rate, -t.in_run_flaky, -fails, t.name)


def build_history(runs: list[Run], min_runs: int = 1) -> History:
    names: dict[str, str] = {}
    for run in runs:
        for key, tr in run.tests.items():
            names[key] = tr.name  # latest run's name wins if it changed

    tests = []
    for key, name in names.items():
        cells = []
        for run in runs:
            tr: Optional[TestRun] = run.tests.get(key)
            if tr is None:
                cells.append(Cell(status=None))
            else:
                cells.append(Cell(
                    status=tr.status,
                    attempts=[a.status for a in tr.attempts],
                    message=tr.message,
                ))
        th = TestHistory(key=key, name=name, cells=cells)
        _score(th)
        if th.runs_present >= min_runs:
            tests.append(th)

    tests.sort(key=rank_key)
    return History(runs=runs, tests=tests)
