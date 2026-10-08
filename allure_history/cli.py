"""Command-line entry point: allure-history RESULTS_DIR [RESULTS_DIR ...]"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import __version__
from .analysis import build_execution_history, build_history
from .loader import _url, load_run, load_runs
from .notes import (EXPORT_GLOB, NOTES_FILE, NotesError, format_created, load_notes,
                    merge_into, new_note, parse_notes, read_raw, resolve_test, visible,
                    write_notes)
from .runs import detect_runs
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
               "--help'; to add, list or merge notes, 'allure-history note --help'. (A "
               "results directory literally named 'snapshot' or 'note' can be passed as "
               "./snapshot or ./note.)",
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
    p.add_argument("--runs", type=non_negative, default=15, metavar="N",
                   help="per-execution mode: how many of the most recent runs to list in the "
                        "terminal (0 = all, default 15); the HTML lists every run")
    p.add_argument("--all", action="store_true",
                   help="print all tests in the terminal table, not just flaky ones")
    p.add_argument("--title", default="Test History", help="title for the HTML report")
    p.add_argument("--notes", type=Path, metavar="FILE",
                   help=f"notes file to show in the HTML and JSON output (default: "
                        f"{NOTES_FILE} in the current directory, if it exists). Note "
                        f"exports downloaded from a report (allure-notes-export*.json) next "
                        f"to it or in the current directory are merged into it first")
    p.add_argument("--no-merge-exports", action="store_true",
                   help="don't merge allure-notes-export*.json files into the notes file")
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


def parse_note_args(argv) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="allure-history note",
        description=f"Add, list or merge notes on tests and executions. Notes live in a "
                    f"JSON file (default {NOTES_FILE}) that every report built with it "
                    f"shows; commit it so the whole team sees them.",
    )
    sub = p.add_subparsers(dest="action", metavar="{add,list,merge}")
    sub.required = True
    notes_help = f"notes file (default {NOTES_FILE})"
    add = sub.add_parser("add", help="add a note to a test or to one of its executions")
    add.add_argument("paths", nargs="*", type=Path,
                     help="allure-results directories or snapshots to look the test up in "
                          "(without them, --test is taken as the exact test key)")
    add.add_argument("--notes", type=Path, default=Path(NOTES_FILE), metavar="FILE",
                     help=notes_help + "; created if missing")
    add.add_argument("--test", required=True,
                     help="the test: its key (historyId), full name, or a unique part of "
                          "its name")
    add.add_argument("--execution", type=non_negative, metavar="START_MS",
                     help="start time (epoch ms) of the execution the note is about, as in "
                          "the JSON output's 'start'; omit for a note on the test in general")
    add.add_argument("--text", required=True, help="the note ('-' reads it from stdin)")
    add.add_argument("--author", default="", help="who wrote it")
    ls = sub.add_parser("list", help="print the notes in a notes file")
    ls.add_argument("--notes", type=Path, default=Path(NOTES_FILE), metavar="FILE",
                    help=notes_help)
    ls.add_argument("--test", help="only notes on tests whose key is this, or whose name "
                                   "contains it")
    merge = sub.add_parser(
        "merge", help="merge other notes files into the notes file",
        description="Merge notes files (e.g. allure-notes-export-*.json downloaded from a "
                    "report) into the notes file: notes are matched by id and, for each, "
                    "the most recently updated version wins, including deletions. Merging "
                    "the same file again changes nothing. Report runs do this "
                    "automatically for export files next to the notes file.")
    merge.add_argument("incoming", nargs="+", type=Path, help="notes files to merge in")
    merge.add_argument("--notes", type=Path, default=Path(NOTES_FILE), metavar="FILE",
                       help=notes_help + "; created if missing")
    return p.parse_args(argv)


def _find_execution(runs, key: str, execution: int):
    """The execution's exact start (given exactly, or to the second, which is all the
    HTML report knows; then the last one in that second) and the start of its run, as
    the report detects runs by default: by pytest process for one folder, else each
    folder is a run."""
    candidates = []
    for run in runs:
        tr = run.tests.get(key)
        for a in tr.attempts if tr else []:
            if a.start is not None and a.start // 1000 == execution // 1000:
                candidates.append((a.start != execution, -a.start, run, a))
    if not candidates:
        return None, None
    _, _, run, attempt = min(candidates, key=lambda c: c[:2])
    if len(runs) > 1:
        return attempt.start, run.start
    for detected in detect_runs(runs):
        if any(e.attempt is attempt for e in detected.executions):
            return attempt.start, detected.start
    return attempt.start, None


def note_add(args) -> int:
    try:
        entries = read_raw(args.notes)  # kept as they are, so nothing in the file is lost
    except NotesError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    text = sys.stdin.read() if args.text == "-" else args.text
    execution, run_start = args.execution, None
    if args.paths:
        runs = load_runs(args.paths)
        if not runs:
            print("error: no Allure results found in the given paths", file=sys.stderr)
            return 1
        tests = {}
        for run in runs:
            for key, tr in run.tests.items():
                tests[key] = tr.name  # latest run's name wins, as in the report
        try:
            key, name = resolve_test(args.test, tests)
        except NotesError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if execution is not None:
            execution, run_start = _find_execution(runs, key, execution)
            if execution is None:
                print(f"error: {name} has no execution that started at {args.execution} "
                      f"(see 'start' in the JSON output)", file=sys.stderr)
                return 1
    else:
        key = args.test
        known = {n.test: n.name for n in parse_notes(entries, str(args.notes))}
        name = known.get(key, "")
        print(f"note: no results given, so {key!r} is used as the test key as is",
              file=sys.stderr)
    note = new_note(key, name, text, execution=execution, run=run_start, author=args.author)
    if not note["text"]:
        print("error: the note text is empty", file=sys.stderr)
        return 1
    try:
        write_notes(args.notes, entries + [note])
    except OSError as e:
        print(f"error: cannot write {args.notes}: {e}", file=sys.stderr)
        return 1
    where = "the test" if execution is None else f"the execution started at {execution}"
    print(f"added note {note['id']} on {name or key} ({where}) to {args.notes}",
          file=sys.stderr)
    return 0


def note_list(args) -> int:
    try:
        notes = visible(load_notes(args.notes))
    except NotesError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.test:
        q = args.test.lower()
        notes = [n for n in notes if n.test == args.test or q in n.name.lower()]
    for n in sorted(notes, key=lambda n: (n.created, n.id)):
        bits = [format_created(n.created) or "?", n.author, n.name or n.test]
        if n.execution is not None:
            bits.append(f"execution {n.execution}")
        print("  ".join(b for b in bits if b))
        for line in n.text.split("\n"):
            print(f"    {line}")
    print(f"{len(notes)} note{'' if len(notes) == 1 else 's'} in {args.notes}", file=sys.stderr)
    return 0


def _counts(counts: dict, unchanged: bool = False) -> str:
    keys = ("added", "updated", "deleted") + (("unchanged",) if unchanged else ())
    return ", ".join(f"{counts[k]} {k}" for k in keys)


def note_merge(args) -> int:
    try:
        incoming = [n for p in args.incoming for n in parse_notes(read_raw(p), str(p))]
        counts = merge_into(args.notes, incoming)
    except NotesError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error: cannot write {args.notes}: {e}", file=sys.stderr)
        return 1
    print(f"merged into {args.notes}: {_counts(counts, unchanged=True)}", file=sys.stderr)
    return 0


def note_main(argv) -> int:
    args = parse_note_args(argv)
    return {"add": note_add, "list": note_list, "merge": note_merge}[args.action](args)


def _merge_exports(path: Path) -> None:
    """Merge notes the report downloaded (allure-notes-export*.json, next to the notes
    file or in the current directory) into the notes file, creating it if needed. The
    exports are left in place: merging again changes nothing."""
    seen, found = {path.resolve()}, []
    for folder in (path.parent, Path(".")):
        for f in sorted(folder.glob(EXPORT_GLOB)):
            if f.is_file() and f.resolve() not in seen:
                seen.add(f.resolve())
                found.append(f)
    incoming, used = [], 0
    for f in found:
        try:
            incoming += parse_notes(read_raw(f), str(f))
            used += 1
        except NotesError as e:
            print(f"warning: {e}; skipping it", file=sys.stderr)
    if not used:
        return
    exports = f"{used} note export{'' if used == 1 else 's'}"
    created = not path.exists()
    try:
        counts = merge_into(path, incoming)
    except NotesError as e:
        print(f"warning: {e}; not merging {exports} into it", file=sys.stderr)
        return
    except OSError as e:
        print(f"warning: cannot write {path}: {e}; not merging {exports}", file=sys.stderr)
        return
    if created:
        print(f"created {path} from {exports} ({_counts(counts)}); the export files can be "
              f"deleted", file=sys.stderr)
    elif counts["added"] + counts["updated"] + counts["deleted"]:
        print(f"merged {exports} into {path} ({_counts(counts)}); the export files can be "
              f"deleted", file=sys.stderr)
    else:
        print(f"note: {exports} already merged into {path}; the export files can be deleted",
              file=sys.stderr)


def _notes_for_report(args) -> tuple[list, Path]:
    """Notes (and tombstones) to embed, and the notes file they come from."""
    path, discovered = args.notes, args.notes is None
    if discovered:
        path = Path(NOTES_FILE)
    if not args.no_merge_exports:
        _merge_exports(path)
    if discovered:
        if not path.is_file():
            return [], path
        print(f"note: using notes from {path} (pass --notes FILE to use another)",
              file=sys.stderr)
    elif not path.exists():
        print(f"warning: notes file {path} does not exist yet; notes added in the report "
              f"can be saved into it", file=sys.stderr)
        return [], path
    try:
        return load_notes(path), path
    except NotesError as e:
        print(f"warning: {e}; showing no notes", file=sys.stderr)
        return [], path


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
    if argv[:1] == ["note"]:
        return note_main(argv[1:])
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
        history.detected_runs = detect_runs(runs)
    else:
        if args.last > 0:
            runs = runs[-args.last:]
        history = build_history(runs, min_runs=args.min_runs)
    history.notes, notes_path = _notes_for_report(args)
    # Identifies this project's notes in the browser, where notes added in the report
    # wait to be exported (reports opened from disk all share one storage area).
    notes_key = f"{notes_path.resolve().parent.name}/{notes_path.name}"

    for path, render in ((args.html, lambda: render_html(history, args.title,
                                                         notes_path.name, notes_key)),
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

    print(render_text(history, top=args.top, flaky_only=not args.all, runs_shown=args.runs))
    if args.fail_on_flaky and history.flaky:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
