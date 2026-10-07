"""Command-line entry point: allure-history RESULTS_DIR [RESULTS_DIR ...]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analysis import build_history
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
    )
    p.add_argument(
        "paths", nargs="+", type=Path,
        help="allure-results directories (one per CI run), snapshot files, or directories "
             "containing either at any depth",
    )
    p.add_argument("--html", type=Path, help="write the interactive HTML matrix to this file")
    p.add_argument("--csv", type=Path, help="write the matrix as CSV to this file")
    p.add_argument("--json", type=Path, help="write the full history as JSON to this file")
    p.add_argument("--last", type=non_negative, default=0, metavar="N",
                   help="only use the N most recent runs")
    p.add_argument("--min-runs", type=non_negative, default=1, metavar="N",
                   help="ignore tests that appear in fewer than N runs (default 1)")
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
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        print(f"error: invalid snapshot name {name!r}", file=sys.stderr)
        return 1
    path = args.out / f"{name}{SUFFIX}"
    if path.exists() and not args.force:
        print(f"error: {path} already exists (use --force to overwrite, or --name)",
              file=sys.stderr)
        return 1
    size = write_snapshot(run, path)
    print(f"wrote {path} ({len(run.tests)} tests, {size / 1024:.1f} KB)", file=sys.stderr)
    if args.keep:
        for old in prune(args.out, args.keep):
            print(f"deleted {old}", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["snapshot"]:
        return snapshot_main(argv[1:])
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
