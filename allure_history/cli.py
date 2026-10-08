"""Command-line entry point: allure-history RESULTS_DIR [RESULTS_DIR ...]"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import __version__
from .analysis import build_execution_history, build_history
from .loader import _url, load_run, load_runs
from .render import render_csv, render_html, render_json, render_text
from .snapshot import SUFFIX, default_name, prune, write_snapshot


def non_negative(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or greater, got {n}")
    return n


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="allure-history",
        description="Build a cross-run test history matrix from Allure results and rank "
                    "flaky tests by how often they flip between pass and fail.",
        epilog="To save a compact per-run snapshot instead, run 'allure-history snapshot "
               "--help'. (A results directory literally named 'snapshot' can be passed "
               "as ./snapshot.)",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "paths", nargs="+", type=Path,
        help="allure-results directories (one per CI run), snapshot files, or directories "
             "containing either at any depth",
    )
    p.add_argument("--html", type=Path, help="write the interactive HTML matrix to this file")
    p.add_argument("--csv", type=Path, help="write the matrix as CSV to this file")
    p.add_argument("--json", type=Path, help="write the full history as JSON to this file")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--per-execution", action="store_true",
                      help="treat every test result as its own execution and order each "
                           "test's executions by time, ignoring run boundaries (default "
                           "when given a single results folder, e.g. one folder that "
                           "collects results from many runs)")
    mode.add_argument("--per-run", action="store_true",
                      help="treat each results folder or snapshot as one run (default when "
                           "given several)")
    p.add_argument("--last", type=non_negative, default=0, metavar="N",
                   help="only use the N most recent runs (per-execution: each test's N "
                        "most recent executions)")
    p.add_argument("--min-runs", type=non_negative, default=1, metavar="N",
                   help="ignore tests with fewer than N runs, or N executions in "
                        "per-execution mode (default 1)")
    p.add_argument("--top", type=non_negative, default=20, metavar="N",
                   help="how many flaky tests to print (0 = all, default 20)")
    p.add_argument("--all", action="store_true",
                   help="print all tests in the terminal table, not just flaky ones")
    p.add_argument("--title", default="Test History", help="title for the HTML report")
    p.add_argument("--fail-on-flaky", action="store_true",
                   help="exit with status 2 if any flaky tests are found")
    return p.parse_args(argv)


def parse_snapshot_args(argv) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="allure-history snapshot",
        description="Save a compact summary of one run's allure-results (typically a few KB "
                    "to a few hundred KB), so the raw results can be deleted afterwards.",
    )
    p.add_argument("results", type=Path, help="this run's allure-results directory")
    p.add_argument("-o", "--out", type=Path, required=True,
                   help="history directory to write the snapshot into")
    p.add_argument("--name", help="snapshot file name (default: from build order, build name "
                                  "or start time)")
    p.add_argument("--label", help="column label, e.g. '#1234' (default: executor.json "
                                   "buildName)")
    p.add_argument("--order", type=int, help="build number used to order runs (default: "
                                             "executor.json buildOrder)")
    p.add_argument("--url", help="link for the column header (http/https only)")
    p.add_argument("--keep", type=non_negative, default=0, metavar="N",
                   help="after writing, delete all but the N newest snapshots in --out")
    p.add_argument("--force", action="store_true", help="overwrite an existing snapshot")
    return p.parse_args(argv)


def snapshot_main(argv) -> int:
    args = parse_snapshot_args(argv)
    if not args.results.is_dir() or not any(args.results.glob("*-result.json")):
        print(f"error: no *-result.json files in {args.results}", file=sys.stderr)
        return 1
    run = load_run(args.results)
    if args.label:
        run.label = args.label
    if args.order is not None:
        run.order = args.order
    if args.url:
        run.url = _url(args.url)
        if run.url is None:
            print("error: --url must start with http:// or https://", file=sys.stderr)
            return 1
    name = args.name or default_name(run)
    if name.endswith(SUFFIX):
        name = name[: -len(SUFFIX)]
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", name):
        print(f"error: invalid snapshot name {name!r} (use letters, digits, '.', '_', '-')",
              file=sys.stderr)
        return 1
    if run.label == run.id and not args.label:
        # No buildName or --label: the directory name (often "allure-results" in every
        # run) would make every column look the same.
        run.label = f"#{run.order}" if run.order is not None else name
    path = args.out / f"{name}{SUFFIX}"
    if path.exists() and not args.force:
        print(f"error: {path} already exists (use --force to overwrite, or --name)",
              file=sys.stderr)
        return 1
    try:
        size = write_snapshot(run, path)
    except OSError as e:
        print(f"error: cannot write {path}: {e}", file=sys.stderr)
        return 1
    print(f"wrote {path} ({len(run.tests)} tests, {size / 1024:.1f} KB)", file=sys.stderr)
    if args.keep:
        deleted, spared = prune(args.out, args.keep, protect=path)
        for old in deleted:
            print(f"deleted {old}", file=sys.stderr)
        if spared:
            print(f"warning: {path.name} is older than the {args.keep} newest snapshots "
                  f"(check --order); kept it anyway", file=sys.stderr)
    return 0


def _safe_stdio():
    # e.g. a Windows CI runner with cp1252 output: replace characters it can't show
    # instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    _safe_stdio()
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["snapshot"]:
        return snapshot_main(argv[1:])
    args = parse_args(argv)
    runs = load_runs(args.paths)
    if not runs:
        print("error: no Allure results found in the given paths", file=sys.stderr)
        return 1
    per_execution = args.per_execution or (len(runs) == 1 and not args.per_run)
    if per_execution:
        if not args.per_execution:
            print("note: one results folder, so each test's executions are ordered by time "
                  "(use --per-run to treat it as a single run)", file=sys.stderr)
        history = build_execution_history(runs, last=args.last, min_executions=args.min_runs)
    else:
        if args.last > 0:
            runs = runs[-args.last:]
        history = build_history(runs, min_runs=args.min_runs)

    for path, render in ((args.html, lambda: render_html(history, args.title)),
                         (args.csv, lambda: render_csv(history)),
                         (args.json, lambda: render_json(history))):
        if path:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "w", encoding="utf-8", newline="") as f:  # 3.9-compatible
                    f.write(render())
            except OSError as e:
                print(f"error: cannot write {path}: {e}", file=sys.stderr)
                return 1
            print(f"wrote {path}", file=sys.stderr)

    print(render_text(history, top=args.top, flaky_only=not args.all))
    if args.fail_on_flaky and history.flaky:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
