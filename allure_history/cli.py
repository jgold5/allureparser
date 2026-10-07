"""Command-line entry point: allure-history RESULTS_DIR [RESULTS_DIR ...]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analysis import build_history
from .loader import load_runs
from .render import render_csv, render_html, render_json, render_text


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="allure-history",
        description="Build a cross-run test history matrix from Allure results and rank "
                    "flaky tests by how often they flip between pass and fail.",
    )
    p.add_argument(
        "paths", nargs="+", type=Path,
        help="allure-results directories (one per CI run), or parent directories whose "
             "immediate subdirectories are per-run allure-results directories",
    )
    p.add_argument("--html", type=Path, help="write the interactive HTML matrix to this file")
    p.add_argument("--csv", type=Path, help="write the matrix as CSV to this file")
    p.add_argument("--json", type=Path, help="write the full history as JSON to this file")
    p.add_argument("--last", type=int, default=0, metavar="N",
                   help="only use the N most recent runs")
    p.add_argument("--min-runs", type=int, default=1, metavar="N",
                   help="ignore tests that appear in fewer than N runs (default 1)")
    p.add_argument("--top", type=int, default=20, metavar="N",
                   help="how many flaky tests to print (0 = all, default 20)")
    p.add_argument("--all", action="store_true",
                   help="print all tests in the terminal table, not just flaky ones")
    p.add_argument("--title", default="Test History", help="title for the HTML report")
    p.add_argument("--fail-on-flaky", action="store_true",
                   help="exit with status 2 if any flaky tests are found")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    runs = load_runs(args.paths)
    if not runs:
        print("error: no Allure results found in the given paths", file=sys.stderr)
        return 1
    if args.last > 0:
        runs = runs[-args.last:]

    history = build_history(runs, min_runs=args.min_runs)

    for path, render in ((args.html, lambda: render_html(history, args.title)),
                         (args.csv, lambda: render_csv(history)),
                         (args.json, lambda: render_json(history))):
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render(), encoding="utf-8", newline="")
            print(f"wrote {path}", file=sys.stderr)

    print(render_text(history, top=args.top, flaky_only=not args.all))
    if args.fail_on_flaky and history.flaky:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
