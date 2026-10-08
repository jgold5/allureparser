"""Load Allure result directories, one directory per CI run."""

from __future__ import annotations

import hashlib
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


def _valid_unicode(text: str) -> str:
    # JSON can carry lone surrogates ("\ud800") that cannot be written out as UTF-8.
    return text.encode("utf-8", "replace").decode("utf-8")


def _text(value) -> str:
    """Scalar JSON value as single-line text; anything else (lists, dicts, null) is ''."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return _CONTROL.sub(" ", _valid_unicode(str(value))).strip()


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
    message: str = ""   # failure message, possibly multi-line (capped)
    location: str = ""  # where it failed, e.g. "tests/test_api.py:42"
    session: str = ""   # "host:pid" of the pytest process that ran it, if recorded


@dataclass
class TestRun:
    """All executions of one test within one CI run. The last attempt is the final status."""

    key: str
    name: str
    attempts: list[Attempt]
    file: str = ""  # e.g. "src/tests/test_api.py"
    cls: str = ""   # test class, if any

    @property
    def status(self) -> str:
        return self.attempts[-1].status

    @property
    def message(self) -> str:
        return self.attempts[-1].message

    @property
    def location(self) -> str:
        return self.attempts[-1].location


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
    # Like Allure's historyId: real values of all non-excluded parameters, including
    # masked/hidden ones (so distinct variants stay distinct), hashed so that masked
    # values never appear in the key, which is written to snapshots and JSON output.
    raw = result.get("parameters")
    params = [(_text(p.get("name")), _text(p.get("value")))
              for p in (raw if isinstance(raw, list) else [])
              if isinstance(p, dict) and p.get("excluded") is not True]
    if not params:
        return base
    digest = hashlib.sha256(json.dumps(params).encode("utf-8")).hexdigest()[:16]
    return f"{base}#{digest}"


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


MAX_MESSAGE_LINES = 20
MAX_MESSAGE_CHARS = 2000
_CONTROL_EXCEPT_NEWLINE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]+")


def first_line(text: str) -> str:
    lines = text.strip().splitlines() if isinstance(text, str) else []
    return lines[0].strip() if lines else ""


def clean_message(text) -> str:
    """Failure message kept multi-line (pytest puts assertion diffs on later lines), with
    control characters removed and size capped."""
    if not isinstance(text, str):
        return ""
    text = _valid_unicode(text).replace("\r\n", "\n").replace("\t", "    ")
    text = _CONTROL_EXCEPT_NEWLINE.sub(" ", text)
    lines = [line.rstrip() for line in text.strip().split("\n")]
    truncated = len(lines) > MAX_MESSAGE_LINES
    out = "\n".join(lines[:MAX_MESSAGE_LINES])
    if len(out) > MAX_MESSAGE_CHARS:
        out, truncated = out[:MAX_MESSAGE_CHARS], True
    # rstrip keeps this idempotent, so messages read back from snapshots are unchanged.
    return out.rstrip() + ("\n\u2026" if truncated else "")


# pytest ends every trace with "path/to/file.py:LINE: ExceptionType"
_PYTEST_LOCATION = re.compile(r"^(?P<file>\S.*?):(?P<line>\d+): [\w.]+$")


def failure_location(trace) -> str:
    if not isinstance(trace, str):
        return ""
    lines = [line.strip() for line in trace.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    m = _PYTEST_LOCATION.match(lines[-1])
    return _text(f"{m['file']}:{m['line']}")[:300] if m else ""


def session_id(result: dict) -> str:
    """'host:pid' of the process that produced a result. allure-pytest records the
    host and a 'thread' label of the form '<pid>-<thread name>' on every result; each
    pytest run (and each xdist worker) is its own process."""
    labels = result.get("labels")
    host = pid = ""
    for label in labels if isinstance(labels, list) else []:
        if not isinstance(label, dict):
            continue
        if label.get("name") == "host":
            host = _text(label.get("value"))
        elif label.get("name") == "thread":
            m = re.match(r"(\d+)-", _text(label.get("value")))
            pid = m.group(1) if m else ""
    return f"{host}:{pid}" if host and pid else ""


def file_and_class(result: dict) -> tuple[str, str]:
    """Source file and class of a test.

    allure-pytest (2.14+) writes titlePath, e.g. ['src', 'test', 'v1.2', 'test_x.py',
    'TestOuter', 'TestInner']: the exact file (dots in folder names kept) followed by
    the classes. Otherwise use the 'package' label (dotted module) and take the class
    from fullName, since the 'subSuite' label can be replaced with @allure.sub_suite.
    Without labels, guess from fullName ('pkg.module.Class#test') or a pytest node id
    ('path/test_x.py::Class::test')."""
    title = result.get("titlePath")
    if isinstance(title, list):
        parts = [_text(p) for p in title if isinstance(p, str)]
        for i, part in enumerate(parts):
            if part.endswith(".py"):
                return ("/".join(p for p in parts[:i + 1] if p),
                        ".".join(p for p in parts[i + 1:] if p))
    labels = result.get("labels")
    found = {}
    for label in labels if isinstance(labels, list) else []:
        if isinstance(label, dict) and label.get("name") in ("package", "subSuite", "testClass"):
            value = _text(label.get("value"))
            if value and value.strip("."):
                found.setdefault(label["name"], value)
    module = found.get("package", "")
    full = _text(result.get("fullName"))
    if module:
        cls = ""
        if "#" in full and full.startswith(module + "."):
            cls = full[len(module) + 1:full.rindex("#")]
        elif "#" not in full:
            cls = found.get("subSuite", "")
        return module.replace(".", "/") + ".py", cls
    if "::" in full:  # pytest node id
        parts = full.split("::")
        return parts[0], ".".join(parts[1:-1])
    cls = found.get("subSuite", "")
    if "#" in full:
        left = full.rsplit("#", 1)[0].split(".")
        if len(left) > 1 and left[-1][:1].isupper():  # last segment looks like a class
            cls = left[-1]
            left = left[:-1]
        module = ".".join(left)
    if not module and found.get("testClass"):
        module, _, cls = found["testClass"].rpartition(".")
    if not module:
        return "", cls
    return module.replace(".", "/") + ".py", cls


def is_results_dir(path: Path) -> bool:
    return path.is_dir() and any(path.glob("*-result.json"))


def _is_snapshot_file(path: Path) -> bool:
    from .snapshot import is_snapshot
    return is_snapshot(path)


def _find_results_dirs(root: Path, _seen: Optional[set] = None) -> list[Path]:
    """Results dirs and snapshot files at any depth under root, not descending into a
    results dir once found."""
    seen = set() if _seen is None else _seen
    real = root.resolve()
    if real in seen:  # symlink loop
        return []
    seen.add(real)
    if is_results_dir(root):
        return [root]
    try:
        entries = sorted(root.iterdir())
    except OSError as e:
        print(f"warning: cannot list {root}: {e}", file=sys.stderr)
        return []
    found = [e for e in entries if _is_snapshot_file(e)]
    for child in entries:
        if child.is_dir():
            found.extend(_find_results_dirs(child, seen))
    return found


def discover_run_dirs(paths: list[Path]) -> list[Path]:
    """Each path is a results dir, a snapshot file, or a directory containing per-run
    results dirs and/or snapshot files at any depth (e.g. ci/build-12/allure-results/)."""
    found: list[Path] = []
    for p in paths:
        if _is_snapshot_file(p):
            found.append(p)
        elif p.is_dir():
            dirs = _find_results_dirs(p)
            if not dirs:
                print(f"warning: no *-result.json or snapshot files under {p}", file=sys.stderr)
            found.extend(dirs)
        else:
            print(f"warning: {p} is not a directory or snapshot file, skipping",
                  file=sys.stderr)
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
        except (ValueError, OSError, RecursionError) as e:
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
    where: dict[str, tuple[str, str]] = {}
    for f in sorted(path.glob("*-result.json")):
        try:
            result = _read_json(f)
        except (ValueError, OSError, RecursionError) as e:  # bad JSON, bad UTF-8, too deep
            print(f"warning: skipping unreadable {f}: {e}", file=sys.stderr)
            continue
        if not isinstance(result, dict):
            print(f"warning: skipping {f}: not a JSON object", file=sys.stderr)
            continue
        status = result.get("status")
        if status not in STATUSES:
            status = "unknown"
        details = _dict(result.get("statusDetails"))
        attempt = Attempt(
            status=status,
            start=_time(result.get("start")),
            stop=_time(result.get("stop")),
            message=clean_message(details.get("message")),
            location=failure_location(details.get("trace")),
            session=session_id(result),
        )
        key = identity_key(result)
        grouped.setdefault(key, (display_name(result), []))[1].append(attempt)
        where.setdefault(key, file_and_class(result))
        if attempt.start is not None:
            run.start = attempt.start if run.start is None else min(run.start, attempt.start)

    for key, (name, attempts) in grouped.items():
        # Retries share a historyId. Like Allure, the attempt that started last is the
        # displayed (final) result; attempts without a start time count as oldest.
        attempts.sort(key=lambda a: (a.start is not None, a.start or 0,
                                     a.stop is not None, a.stop or 0))
        file, cls = where.get(key, ("", ""))
        run.tests[key] = TestRun(key=key, name=name, attempts=attempts, file=file, cls=cls)
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
    from .snapshot import read_snapshot
    sources = discover_run_dirs(paths)
    dirs = [s for s in sources if s.is_dir()]
    ids = dict(zip(dirs, _run_ids(dirs)))
    runs = []
    for s in sources:
        run = load_run(s, ids[s]) if s.is_dir() else read_snapshot(s)
        if run is not None:
            runs.append(run)
    return sort_runs(_drop_duplicate_runs(runs))


def _fingerprint(run: Run):
    return (run.start, tuple(sorted(
        (k, tuple((a.status, a.start) for a in t.attempts)) for k, t in run.tests.items())))


def _drop_duplicate_runs(runs: list[Run]) -> list[Run]:
    """The same run given twice (e.g. its snapshot and its raw results) counts once. The
    copy with a build order is kept, since snapshots can carry --order/--label overrides."""
    kept: dict = {}
    out = []
    for run in runs:
        if run.start is None:  # no timing data: can't tell identical-looking runs apart
            out.append(run)
            continue
        fp = _fingerprint(run)
        other = kept.get(fp)
        if other is None:
            kept[fp] = run
            continue
        keep, drop = (run, other) if other.order is None and run.order is not None else (other, run)
        kept[fp] = keep
        print(f"warning: {drop.path} is the same run as {keep.path}; using it once",
              file=sys.stderr)
    return out + list(kept.values())


def sort_runs(runs: list[Run]) -> list[Run]:
    """Oldest first. Prefer executor buildOrder, then earliest test start, then name."""
    if runs and all(r.order is not None for r in runs):
        return sorted(runs, key=lambda r: (r.order, r.id))
    return sorted(runs, key=lambda r: (r.start is None, r.start or 0, r.id))
