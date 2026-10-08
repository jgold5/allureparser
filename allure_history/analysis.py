"""Build the test x run matrix and score flakiness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from pathlib import Path

from .loader import Attempt, Run, TestRun, first_line

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
    location: str = ""
    when: Optional[int] = None  # start time of the (final) execution
    stop: Optional[int] = None

    @property
    def retried(self) -> bool:
        return len(self.attempts) > 1

    @property
    def flaky_in_run(self) -> bool:
        """Retries within the run produced both a pass and a fail."""
        classes = {outcome(s) for s in self.attempts} - {None}
        return len(classes) > 1


@dataclass
class FailureReason:
    message: str   # first line of the failure message
    location: str
    count: int
    last_run: int  # index of the most recent run with this failure


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
    last_failure: str = ""  # first line of the most recent failure message
    # Distinct failure causes across runs, most frequent first. A flaky test failing the
    # same way every time usually has one root cause; many different ones point elsewhere.
    failure_reasons: list[FailureReason] = field(default_factory=list)

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
    runs: list[Run]  # in per-execution mode: one column per execution position
    tests: list[TestHistory]
    per_execution: bool = False
    executions: int = 0  # total test executions (per-execution mode)
    detected_runs: list = field(default_factory=list)  # runs.DetectedRun, per-execution mode

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
            t.last_failure = first_line(c.message)
            break
    reasons: dict[tuple[str, str], FailureReason] = {}
    for i, c in enumerate(t.cells):
        if c.status in ("failed", "broken"):
            key = (first_line(c.message), c.location)
            r = reasons.setdefault(key, FailureReason(key[0], key[1], 0, i))
            r.count += 1
            r.last_run = i
    t.failure_reasons = sorted(reasons.values(), key=lambda r: (-r.count, -r.last_run))


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
                    location=tr.location,
                    when=tr.attempts[-1].start,
                    stop=tr.attempts[-1].stop,
                ))
        th = TestHistory(key=key, name=name, cells=cells)
        _score(th)
        if th.runs_present >= min_runs:
            tests.append(th)

    tests.sort(key=rank_key)
    return History(runs=runs, tests=tests)


def build_execution_history(runs: list[Run], last: int = 0, min_executions: int = 1) -> History:
    """History by test execution instead of by run: every result file is one execution,
    and each test's executions are ordered by start time, wherever they came from. Use
    this for a folder holding results of many runs mixed together. Retries are just more
    executions, so fail-then-pass on retry counts as a flip."""
    names: dict[str, str] = {}
    execs: dict[str, dict] = {}
    for run in runs:
        for key, tr in run.tests.items():
            names[key] = tr.name
            seen = execs.setdefault(key, {})
            for a in tr.attempts:
                # The same execution can arrive twice (e.g. a snapshot and its raw folder)
                seen.setdefault((a.status, a.start, a.stop, a.message, a.location), a)

    timelines: dict[str, list[Attempt]] = {}
    for key, seen in execs.items():
        attempts = sorted(seen.values(), key=lambda a: (a.start is not None, a.start or 0,
                                                       a.stop is not None, a.stop or 0))
        if last > 0:
            attempts = attempts[-last:]
        if len(attempts) >= min_executions:
            timelines[key] = attempts

    width = max((len(a) for a in timelines.values()), default=0)
    # Columns are positions counted back from each test's latest execution, so the
    # newest result of every test lines up in the rightmost column.
    columns = [Run(id=f"exec-{width - 1 - i}", path=Path("."),
                   label="latest" if i == width - 1 else f"\u2212{width - 1 - i}")
               for i in range(width)]
    tests = []
    for key, attempts in timelines.items():
        cells = [Cell(status=None) for _ in range(width - len(attempts))]
        cells += [Cell(status=a.status, attempts=[a.status], message=a.message,
                       location=a.location, when=a.start, stop=a.stop) for a in attempts]
        th = TestHistory(key=key, name=names[key], cells=cells)
        _score(th)
        tests.append(th)
    tests.sort(key=rank_key)
    return History(runs=columns, tests=tests, per_execution=True,
                   executions=sum(len(a) for a in timelines.values()))
