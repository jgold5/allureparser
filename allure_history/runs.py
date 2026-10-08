"""Recover individual test runs from results that were collected into one place.

A folder that accumulates results from many runs doesn't record where one run ends.
allure-pytest does record, on every result, the host and process id of the pytest
process that produced it (see loader.session_id). Runs are rebuilt from that:

1. Executions are grouped by session (host:pid), in start-time order.
2. A session is split where a test shows up again after other tests ran: pytest runs
   each test once and reruns a failure immediately, so a later repeat means the same
   pid was reused by a new run.
3. Sessions on the same host that overlap in time and ran disjoint sets of tests are
   one run: the workers of a parallel (pytest-xdist) run.

Executions with no session information are grouped by the folder or snapshot they came
from, so they still show up as one run per source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .loader import Attempt, Run


@dataclass
class Execution:
    key: str
    name: str
    attempt: Attempt


@dataclass
class DetectedRun:
    index: int = 0  # 1 = oldest
    host: str = ""
    sessions: set = field(default_factory=set)
    executions: list[Execution] = field(default_factory=list)
    start: Optional[int] = None
    stop: Optional[int] = None

    @property
    def workers(self) -> int:
        return max(1, len(self.sessions))

    def final(self) -> dict[str, list[Execution]]:
        """Executions per test in time order; the last one is the test's result."""
        out: dict[str, list[Execution]] = {}
        for e in sorted(self.executions, key=_order):
            out.setdefault(e.key, []).append(e)
        return out

    def counts(self) -> dict[str, int]:
        counts = {s: 0 for s in ("passed", "failed", "broken", "skipped", "unknown")}
        for execs in self.final().values():
            counts[execs[-1].attempt.status] += 1
        return counts


def _order(e: Execution):
    a = e.attempt
    return (a.start is not None, a.start or 0, a.stop is not None, a.stop or 0)


@dataclass
class _Segment:
    host: str
    session: str
    executions: list[Execution] = field(default_factory=list)
    keys: set = field(default_factory=set)
    start: Optional[int] = None
    stop: Optional[int] = None

    def add(self, e: Execution) -> None:
        self.executions.append(e)
        self.keys.add(e.key)
        a = e.attempt
        for t in (a.start, a.stop):
            if t is not None:
                self.start = t if self.start is None else min(self.start, t)
                self.stop = t if self.stop is None else max(self.stop, t)


def _split_session(host: str, session: str, execs: list[Execution]) -> list[_Segment]:
    segments = [_Segment(host, session)]
    previous = None
    for e in sorted(execs, key=_order):
        current = segments[-1]
        if e.key in current.keys and e.key != previous:
            current = _Segment(host, session)
            segments.append(current)
        current.add(e)
        previous = e.key
    return segments


def _overlaps(a, b) -> bool:
    return (a.start is not None and b.start is not None
            and a.start <= b.stop and b.start <= a.stop)


def detect_runs(sources: list[Run]) -> list[DetectedRun]:
    by_session: dict[tuple[str, str], list[Execution]] = {}
    seen = set()
    for src in sources:
        for key, tr in src.tests.items():
            for a in tr.attempts:
                ident = (key, a.status, a.start, a.stop, a.message, a.location)
                if ident in seen:  # same execution from a snapshot and its raw folder
                    continue
                seen.add(ident)
                if a.session:
                    host = a.session.rsplit(":", 1)[0]
                    group = (host, a.session)
                else:
                    group = ("", f"source:{src.path}")
                by_session.setdefault(group, []).append(Execution(key, tr.name, a))

    segments = []
    for (host, session), execs in by_session.items():
        if session.startswith("source:"):
            seg = _Segment(host, "")
            for e in sorted(execs, key=_order):
                seg.add(e)
            segments.append(seg)
        else:
            segments.extend(_split_session(host, session, execs))

    runs: list[DetectedRun] = []
    for seg in sorted(segments, key=lambda s: (s.start is None, s.start or 0)):
        target = None
        if seg.session:
            for run in runs:
                if (run.host == seg.host and run.sessions and _overlaps(run, seg)
                        and not seg.keys & {e.key for e in run.executions}):
                    target = run
                    break
        if target is None:
            target = DetectedRun(host=seg.host)
            runs.append(target)
        if seg.session:
            target.sessions.add(seg.session)
        target.executions.extend(seg.executions)
        for t in (seg.start, seg.stop):
            if t is not None:
                target.start = t if target.start is None else min(target.start, t)
                target.stop = t if target.stop is None else max(target.stop, t)

    runs.sort(key=lambda r: (r.start is None, r.start or 0))
    for i, run in enumerate(runs, 1):
        run.index = i
    return runs
