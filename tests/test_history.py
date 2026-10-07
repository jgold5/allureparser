import csv
import io
import json
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from allure_history.analysis import build_history
from allure_history.cli import main
from allure_history.loader import load_runs
from allure_history.render import render_csv, render_html, render_json, render_text, strip


def write_result(run_dir: Path, name: str, status: str, start: int, params=None,
                 message: str = "", history_id: str | None = None):
    run_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "uuid": str(uuid.uuid4()),
        "historyId": history_id or f"hid-{name}-{params}",
        "fullName": f"suite.{name}",
        "name": name,
        "status": status,
        "start": start,
        "stop": start + 100,
        "parameters": params or [],
    }
    if message:
        result["statusDetails"] = {"message": message + "\nstack trace line"}
    (run_dir / f"{result['uuid']}-result.json").write_text(json.dumps(result))


def make_runs(root: Path, matrix: dict[str, list[str | None]]) -> Path:
    """matrix: test name -> status per run (None = test absent from that run)."""
    n_runs = len(next(iter(matrix.values())))
    for i in range(n_runs):
        run_dir = root / f"run-{i:02d}"
        for name, statuses in matrix.items():
            if statuses[i] is not None:
                write_result(run_dir, name, statuses[i], start=1_700_000_000_000 + i * 3_600_000,
                             message="boom" if statuses[i] in ("failed", "broken") else "")
    return root


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def history(self, matrix, **kw):
        make_runs(self.root, matrix)
        return build_history(load_runs([self.root]), **kw)

    def by_name(self, h):
        return {t.name.split(".")[-1]: t for t in h.tests}

    def test_flip_counting_and_ranking(self):
        h = self.history({
            "stable":    ["passed"] * 6,
            "broken":    ["failed"] * 6,
            "flappy":    ["passed", "failed", "passed", "failed", "passed", "failed"],
            "once":      ["passed", "passed", "passed", "failed", "passed", "passed"],
            "regressed": ["passed", "passed", "passed", "failed", "failed", "failed"],
        })
        t = self.by_name(h)
        self.assertEqual(t["stable"].flips, 0)
        self.assertEqual(t["broken"].flips, 0)
        self.assertEqual(t["flappy"].flips, 5)
        self.assertAlmostEqual(t["flappy"].flip_rate, 1.0)
        self.assertEqual(t["once"].flips, 2)
        self.assertEqual(t["regressed"].flips, 1)
        self.assertEqual([x.name.split(".")[-1] for x in h.flaky], ["flappy", "once", "regressed"])
        self.assertFalse(t["stable"].is_flaky)
        self.assertFalse(t["broken"].is_flaky)

    def test_failed_vs_broken_and_skips_are_not_flips(self):
        h = self.history({
            "red":   ["failed", "broken", "failed", "broken"],
            "skips": ["passed", "skipped", "passed", "skipped"],
            "gap":   ["passed", None, "failed", None],
        })
        t = self.by_name(h)
        self.assertEqual(t["red"].flips, 0)
        self.assertEqual(t["skips"].flips, 0)
        self.assertEqual(t["skips"].counts["skipped"], 2)
        # Absent runs are skipped over: passed -> failed is one flip
        self.assertEqual(t["gap"].flips, 1)
        self.assertEqual(t["gap"].runs_present, 2)
        self.assertEqual(strip(t["gap"]), "P.F.")

    def test_retries_within_run(self):
        run = self.root / "run-00"
        # A failed first attempt then a passing retry, same historyId
        write_result(run, "retry", "failed", start=1000, history_id="h1", message="flaky timeout")
        write_result(run, "retry", "passed", start=2000, history_id="h1")
        write_result(run, "steady", "passed", start=1000)
        h = build_history(load_runs([self.root]))
        t = self.by_name(h)
        self.assertEqual(t["retry"].cells[0].status, "passed")
        self.assertEqual(t["retry"].cells[0].attempts, ["failed", "passed"])
        self.assertEqual(t["retry"].in_run_flaky, 1)
        self.assertTrue(t["retry"].is_flaky)
        self.assertEqual(strip(t["retry"]), "p")
        self.assertFalse(t["steady"].is_flaky)

    def test_parameterized_tests_are_separate_rows(self):
        for i, (a, b) in enumerate([("passed", "passed"), ("passed", "failed")]):
            run = self.root / f"run-{i}"
            write_result(run, "param", a, start=i * 10, params=[{"name": "x", "value": "1"}],
                         history_id="p1")
            write_result(run, "param", b, start=i * 10, params=[{"name": "x", "value": "2"}],
                         history_id="p2")
        h = build_history(load_runs([self.root]))
        names = sorted(t.name for t in h.tests)
        self.assertEqual(names, ["suite.param[x=1]", "suite.param[x=2]"])
        self.assertEqual([t.name for t in h.flaky], ["suite.param[x=2]"])

    def test_run_ordering_uses_executor_build_order(self):
        # Directory names sort opposite to buildOrder
        for name, order, status in (("a", 3, "failed"), ("b", 2, "passed"), ("c", 1, "passed")):
            run = self.root / name
            write_result(run, "t", status, start=1000)
            (run / "executor.json").write_text(json.dumps(
                {"buildOrder": order, "buildName": f"#{order}", "buildUrl": f"http://ci/{order}"}))
        h = build_history(load_runs([self.root]))
        self.assertEqual([r.label for r in h.runs], ["#1", "#2", "#3"])
        self.assertEqual(strip(h.tests[0]), "PPF")

    def test_run_ordering_falls_back_to_start_time(self):
        write_result(self.root / "zzz", "t", "passed", start=1000)
        write_result(self.root / "aaa", "t", "failed", start=5000)
        h = build_history(load_runs([self.root]))
        self.assertEqual([r.id for r in h.runs], ["zzz", "aaa"])

    def test_min_runs_filter(self):
        h = self.history({"new": [None, None, "passed"], "old": ["passed"] * 3}, min_runs=2)
        self.assertEqual([t.name for t in h.tests], ["suite.old"])

    def test_bad_files_are_skipped(self):
        run = self.root / "run-0"
        write_result(run, "ok", "passed", start=1)
        (run / "garbage-result.json").write_text("{not json")
        with redirect_stderr(io.StringIO()) as err:
            h = build_history(load_runs([self.root]))
        self.assertEqual(len(h.tests), 1)
        self.assertIn("garbage-result.json", err.getvalue())

    def test_renderers(self):
        h = self.history({
            "flappy": ["passed", "failed", "passed"],
            "<script>alert(1)</script>": ["passed", "passed", "passed"],
        })
        text = render_text(h)
        self.assertIn("1 flaky", text)
        self.assertIn("PFP", text)

        rows = list(csv.reader(io.StringIO(render_csv(h))))
        self.assertEqual(rows[0][-3:], ["run-00", "run-01", "run-02"])
        self.assertEqual(rows[1][0], "suite.flappy")
        self.assertEqual(rows[1][-3:], ["passed", "failed", "passed"])

        data = json.loads(render_json(h))
        self.assertEqual(len(data["runs"]), 3)
        self.assertEqual(data["tests"][0]["flips"], 2)
        self.assertEqual(data["tests"][0]["last_failure"], "boom")

        page = render_html(h)
        self.assertIn('class="c failed"', page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_cli(self):
        make_runs(self.root, {"flappy": ["passed", "failed", "passed", "failed"],
                              "stable": ["passed"] * 4})
        out_dir = self.root / "out"
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            code = main([str(self.root), "--html", str(out_dir / "h.html"),
                         "--csv", str(out_dir / "h.csv"), "--json", str(out_dir / "h.json"),
                         "--last", "2", "--fail-on-flaky"])
        self.assertEqual(code, 2)
        self.assertIn("2 runs", out.getvalue())
        for ext in ("html", "csv", "json"):
            self.assertTrue((out_dir / f"h.{ext}").is_file())

    def test_cli_no_results(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.root)]), 1)


if __name__ == "__main__":
    unittest.main()
