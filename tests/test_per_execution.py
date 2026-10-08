from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from allure_history.analysis import build_execution_history
from allure_history.cli import main
from allure_history.loader import load_runs
from allure_history.render import render_csv, render_html, render_json, render_text, strip

from test_history import page_data, write_result


def run_cli(*args):
    with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
        code = main([str(a) for a in args])
    return code, out.getvalue(), err.getvalue()


class PerExecutionTests(unittest.TestCase):
    """One folder collecting results from many runs: each test's executions are ordered
    by start time and flips are counted along that sequence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.big = self.root / "allure-results"

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, test, status, start, message=""):
        write_result(self.big, test, status, start=start, history_id=f"h-{test}",
                     message=message or ("boom" if status != "passed" else ""))

    def history(self, **kw):
        with redirect_stderr(io.StringIO()):
            return build_execution_history(load_runs([self.big]), **kw)

    def by_name(self, h):
        return {t.name.split(".")[-1]: t for t in h.tests}

    def test_orders_by_time_not_file_order(self):
        # Written out of order; uuids in file names are random anyway
        for start, status in [(500, "failed"), (100, "passed"), (300, "passed"), (900, "failed"),
                              (700, "passed")]:
            self.add("t", status, start)
        t = self.history().tests[0]
        self.assertEqual(strip(t), "PPFPF")
        self.assertEqual(t.flips, 3)
        self.assertEqual([c.when for c in t.cells], [100, 300, 500, 700, 900])

    def test_retry_is_just_another_execution(self):
        self.add("t", "passed", 100)
        self.add("t", "failed", 200)   # first attempt
        self.add("t", "passed", 201)   # rerun in the same run
        self.add("t", "passed", 300)
        t = self.history().tests[0]
        self.assertEqual(strip(t), "PFPP")
        self.assertEqual(t.flips, 2)
        self.assertTrue(t.is_flaky)
        self.assertFalse(any(c.retried for c in t.cells))

    def test_shorter_timelines_are_right_aligned(self):
        for i, s in enumerate("PFPF"):
            self.add("long", {"P": "passed", "F": "failed"}[s], i)
        self.add("short", "failed", 10)
        h = self.history()
        t = self.by_name(h)
        self.assertEqual(strip(t["long"]), "PFPF")
        self.assertEqual(strip(t["short"]), "...F")
        self.assertEqual([r.label for r in h.runs], ["−3", "−2", "−1", "latest"])
        self.assertEqual(h.executions, 5)

    def test_last_and_min_executions(self):
        for i, s in enumerate("PPPPFP"):
            self.add("t", {"P": "passed", "F": "failed"}[s], i)
        self.add("once", "passed", 3)
        h = self.history(last=3)
        self.assertEqual(strip(self.by_name(h)["t"]), "PFP")
        h = self.history(min_executions=2)
        self.assertEqual([t.name for t in h.tests], ["suite.t"])

    def test_same_execution_from_snapshot_and_raw_counted_once(self):
        for i, s in enumerate(["passed", "failed", "passed"]):
            self.add("t", s, i * 10)
        hist = self.root / "hist"
        self.assertEqual(run_cli("snapshot", self.big, "-o", hist, "--name", "all")[0], 0)
        with redirect_stderr(io.StringIO()):
            both = build_execution_history(load_runs([hist, self.big]))
            raw = build_execution_history(load_runs([self.big]))
            snap = build_execution_history(load_runs([hist]))
        self.assertEqual(strip(both.tests[0]), "PFP")
        self.assertEqual(json.loads(render_json(raw))["tests"], json.loads(render_json(snap))["tests"])

    def test_cli_defaults_to_per_execution_for_one_folder(self):
        for i, s in enumerate(["passed", "failed", "passed", "failed"]):
            self.add("t", s, i)
        code, out, err = run_cli(self.big)
        self.assertEqual(code, 0)
        self.assertIn("4 test executions, 1 tests, 1 flaky", out)
        self.assertIn("PFPF", out)
        self.assertIn("--per-run", err)
        # --per-run: the whole folder is one run, so repeats look like retries
        code, out, _ = run_cli(self.big, "--per-run")
        self.assertIn("1 runs", out)
        # several folders default to per-run; --per-execution pools them
        other = self.root / "second"
        write_result(other, "t", "passed", start=100, history_id="h-t")
        code, out, _ = run_cli(self.big, other)
        self.assertIn("2 runs", out)
        code, out, _ = run_cli(self.big, other, "--per-execution")
        self.assertIn("5 test executions", out)
        self.assertIn("PFPFP", out)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main([str(self.big), "--per-run", "--per-execution"])

    def test_version_flag(self):
        from allure_history import __version__
        with redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as cm:
            main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"allure-history {__version__}")

    def test_outputs(self):
        self.add("t", "passed", 1_700_000_000_000)
        self.add("t", "failed", 1_700_000_060_000, message="AssertionError: nope")
        self.add("u", "passed", 1_700_000_000_000)
        h = self.history()
        data = json.loads(render_json(h))
        self.assertEqual(data["mode"], "per-execution")
        t = next(x for x in data["tests"] if x["name"] == "suite.t")
        self.assertEqual([c["start"] for c in t["cells"]], [1_700_000_000_000, 1_700_000_060_000])
        page = page_data(render_html(h))
        self.assertEqual(page["pe"], 1)
        pt = next(x for x in page["tests"] if x["n"] == "suite.t")
        self.assertEqual(page["msgs"][pt["m"]["1"]], "AssertionError: nope")
        self.assertEqual(page["t0"], 1_700_000_000_000)
        self.assertEqual(pt["t"], [0, 60])  # seconds after t0
        self.assertEqual(pt["d"], [100, 100])
        self.assertIn("executions", render_html(h))
        rows = render_csv(h).splitlines()
        self.assertTrue(rows[0].endswith("−1,latest"))
        self.assertIn("3 test executions", render_text(h))

    def test_long_history_is_truncated_in_terminal_only(self):
        for i in range(200):
            self.add("t", "passed" if i % 2 else "failed", i)
        h = self.history()
        line = next(l for l in render_text(h).splitlines() if l.endswith("suite.t"))
        self.assertIn("…", line)
        self.assertLess(len(line), 120)
        self.assertEqual(len(h.tests[0].cells), 200)


if __name__ == "__main__":
    unittest.main()
