from __future__ import annotations

import io
import json
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from allure_history.analysis import build_execution_history
from allure_history.cli import main
from allure_history.loader import load_runs, session_id
from allure_history.render import render_html, render_json, render_text
from allure_history.runs import detect_runs

from test_history import page_data


def result(folder: Path, test: str, status: str, start: int, host="ci-1", pid=100,
           message=""):
    """A result as allure-pytest writes it, including its host and thread labels."""
    folder.mkdir(parents=True, exist_ok=True)
    r = {"uuid": str(uuid.uuid4()), "historyId": f"h-{test}", "fullName": f"tests#{test}",
         "status": status, "start": start, "stop": start + 50, "labels": []}
    if host:
        r["labels"].append({"name": "host", "value": host})
    if pid is not None:
        r["labels"].append({"name": "thread", "value": f"{pid}-MainThread"})
    if message:
        r["statusDetails"] = {"message": message,
                              "trace": f"E   {message}\n\ntests/test_x.py:7: AssertionError"}
    (folder / f"{r['uuid']}-result.json").write_text(json.dumps(r))


class RunDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.big = Path(self.tmp.name) / "allure-results"

    def tearDown(self):
        self.tmp.cleanup()

    def runs(self, *paths):
        with redirect_stderr(io.StringIO()):
            return detect_runs(load_runs(list(paths) or [self.big]))

    def summary(self, runs):
        return [(r.workers, {k: v for k, v in r.counts().items() if v}) for r in runs]

    def test_session_id_parsing(self):
        self.assertEqual(session_id({"labels": [{"name": "host", "value": "ci"},
                                                {"name": "thread", "value": "4242-MainThread"}]}),
                         "ci:4242")
        for labels in (None, [], "x", [{"name": "host", "value": "ci"}],
                       [{"name": "thread", "value": "MainThread"}, {"name": "host", "value": "h"}],
                       [1, None, {"name": "thread", "value": []}]):
            self.assertEqual(session_id({"labels": labels}), "")

    def test_sequential_runs_are_separate(self):
        for run, (pid, statuses) in enumerate([(10, "PP"), (11, "PF"), (12, "FF")]):
            for t, s in zip("ab", statuses):
                result(self.big, t, {"P": "passed", "F": "failed"}[s], 1000 * run + ord(t), pid=pid)
        runs = self.runs()
        self.assertEqual(self.summary(runs), [(1, {"passed": 2}), (1, {"passed": 1, "failed": 1}),
                                              (1, {"failed": 2})])
        self.assertEqual([r.index for r in runs], [1, 2, 3])

    def test_parallel_workers_are_one_run(self):
        # xdist: 3 worker processes on one host, overlapping in time, disjoint tests
        for i, t in enumerate("abcdef"):
            result(self.big, t, "passed", 1000 + i * 10, pid=200 + i % 3)
        # next run, 2 workers
        for i, t in enumerate("abcdef"):
            result(self.big, t, "failed" if t == "c" else "passed", 5000 + i * 10, pid=300 + i % 2)
        runs = self.runs()
        self.assertEqual(self.summary(runs), [(3, {"passed": 6}), (2, {"passed": 5, "failed": 1})])

    def test_reused_pid_is_split_but_retry_is_not(self):
        # Same host and pid for two runs (e.g. pytest is always pid 7 in a container)
        result(self.big, "a", "failed", 100, pid=7)
        result(self.big, "a", "passed", 101, pid=7)   # immediate rerun: same run
        result(self.big, "b", "passed", 150, pid=7)
        result(self.big, "a", "passed", 900, pid=7)   # 'a' again after 'b': a new run
        result(self.big, "b", "failed", 950, pid=7)
        runs = self.runs()
        self.assertEqual(len(runs), 2)
        first = runs[0].final()
        self.assertEqual([e.attempt.status for e in first["h-a"]], ["failed", "passed"])
        self.assertEqual(self.summary(runs), [(1, {"passed": 2}), (1, {"passed": 1, "failed": 1})])

    def test_overlapping_runs_on_other_hosts_or_same_tests_stay_separate(self):
        # Two pipelines at the same time on different machines
        for t in "ab":
            result(self.big, t, "passed", 100 + ord(t), host="ci-1", pid=10)
            result(self.big, t, "failed", 105 + ord(t), host="ci-2", pid=10)
        # Two overlapping runs on one machine running the same tests
        for t in "ab":
            result(self.big, t, "passed", 5000 + ord(t), host="ci-3", pid=20)
            result(self.big, t, "passed", 5002 + ord(t), host="ci-3", pid=21)
        self.assertEqual(len(self.runs()), 4)

    def test_results_without_labels_group_by_source(self):
        other = Path(self.tmp.name) / "other"
        result(self.big, "a", "passed", 100, host=None, pid=None)
        result(self.big, "b", "failed", 900, host=None, pid=None)
        result(other, "a", "failed", 2000, host=None, pid=None)
        runs = self.runs(self.big, other)
        self.assertEqual(self.summary(runs), [(1, {"passed": 1, "failed": 1}), (1, {"failed": 1})])

    def test_snapshots_keep_sessions(self):
        for run, pid in enumerate((10, 11)):
            for t in "ab":
                result(self.big, t, "passed", 1000 * run + ord(t), pid=pid)
        hist = Path(self.tmp.name) / "hist"
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["snapshot", str(self.big), "-o", str(hist), "--name", "x"]), 0)
        self.assertEqual(len(self.runs(hist)), 2)
        self.assertEqual(len(self.runs(hist, self.big)), 2)  # same executions, not doubled

    def test_outputs(self):
        for run, pid in enumerate((10, 11, 12)):
            result(self.big, "ok", "passed", 1_700_000_000_000 + run * 3_600_000, pid=pid)
            status = "failed" if run == 1 else "passed"
            result(self.big, "<b>flaky</b>", status, 1_700_000_000_100 + run * 3_600_000, pid=pid,
                   message="AssertionError: boom" if status == "failed" else "")
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.big), "--runs", "2"]), 0)
        text = out.getvalue()
        self.assertIn("Runs found in the results: 3", text)
        self.assertIn("... 1 earlier runs", text)
        self.assertIn("2023-11-15 00:13", text)

        with redirect_stderr(io.StringIO()):
            runs = load_runs([self.big])
        h = build_execution_history(runs)
        h.detected_runs = detect_runs(runs)
        data = json.loads(render_json(h))["runs_detected"]
        self.assertEqual([r["counts"]["failed"] for r in data], [0, 1, 0])
        self.assertEqual(data[1]["failures"], [{
            "name": "tests#<b>flaky</b>", "status": "failed", "message": "AssertionError: boom",
            "location": "tests/test_x.py:7", "attempts": ["failed"]}])

        page = render_html(h)
        self.assertIn('id="runs"', page)
        self.assertIn("tests#&lt;b&gt;flaky&lt;/b&gt;", page)
        self.assertNotIn("<b>flaky</b>", page.split('<script id="history-data"')[0])
        pd = page_data(page)
        t = next(x for x in pd["tests"] if "flaky" in x["n"])
        self.assertIn("run #2", pd["msgs"][t["m"]["1"]])
        self.assertIn("Runs found in the results: 3", render_text(h, runs_shown=0))

    def test_per_run_mode_has_no_runs_section(self):
        result(self.big, "a", "passed", 100, pid=1)
        other = Path(self.tmp.name) / "other"
        result(other, "a", "passed", 200, pid=2)
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            main([str(self.big), str(other)])
        self.assertNotIn("Runs found", out.getvalue())


if __name__ == "__main__":
    unittest.main()
