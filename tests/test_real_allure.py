"""End-to-end checks against results written by the real allure-pytest plugin, plus a
small fuzz test over malformed result files.

The integration test is skipped unless pytest, allure-pytest and pytest-rerunfailures
are importable by the interpreter running the tests:

    pip install pytest allure-pytest pytest-rerunfailures
"""

import importlib.util
import io
import json
import random
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from allure_history.analysis import build_history
from allure_history.cli import main
from allure_history.loader import load_runs
from allure_history.render import render_csv, render_html, render_json, render_text

# Final outcome per run. P pass, F assertion failure, B other exception (broken),
# S skip, - test absent, R fails once then passes on rerun.
TRUTH = {
    "test_stable":             "PPPPPP",
    "test_always_fails":       "FFFFFF",
    "test_broken_then_failed": "BFBFBF",
    "test_alternating":        "PFPFPF",
    "test_one_off":            "PPPFPP",
    "test_regression":         "PPPFFF",
    "test_skip_gaps":          "PSPSPS",
    "test_added_later":        "--PFPP",
    "test_rerun_rescued":      "PRPPRP",
    "test_param[a]":           "PPPPPP",
    "test_param[b]":           "PFPPFP",
    "test_fixture_error":      "PPBPPP",
}

SUITE = textwrap.dedent('''
    import json, os, pathlib, pytest
    TRUTH = json.loads(os.environ["TRUTH"])
    RUN = int(os.environ["RUN"])
    STATE = pathlib.Path(os.environ["STATE_DIR"])

    def act(key):
        s = TRUTH[key][RUN]
        if s == "F": assert False, f"{key} failed in run {RUN}"
        if s == "B": raise RuntimeError(f"{key} broke in run {RUN}")
        if s == "S": pytest.skip("skipped")
        if s == "R":
            marker = STATE / f"{key}-{RUN}"
            if not marker.exists():
                marker.write_text("x")
                assert False, "first attempt fails"

    def test_stable(): act("test_stable")
    def test_always_fails(): act("test_always_fails")
    def test_broken_then_failed(): act("test_broken_then_failed")
    def test_alternating(): act("test_alternating")
    def test_one_off(): act("test_one_off")
    def test_regression(): act("test_regression")
    def test_skip_gaps(): act("test_skip_gaps")
    if TRUTH["test_added_later"][RUN] != "-":
        def test_added_later(): act("test_added_later")
    @pytest.mark.flaky(reruns=2)
    def test_rerun_rescued(): act("test_rerun_rescued")
    @pytest.mark.parametrize("p", ["a", "b"])
    def test_param(p): act(f"test_param[{p}]")
    @pytest.fixture
    def fx():
        if TRUTH["test_fixture_error"][RUN] == "B":
            raise RuntimeError("fixture setup exploded")
    def test_fixture_error(fx): act("test_fixture_error")
''')

STATUS = {"P": "passed", "F": "failed", "B": "broken", "S": "skipped", "R": "passed", "-": None}


def _have(*modules):
    return all(importlib.util.find_spec(m) for m in modules)


@unittest.skipUnless(_have("pytest", "allure_pytest", "pytest_rerunfailures"),
                     "needs pytest, allure-pytest and pytest-rerunfailures")
class RealAllurePytestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "test_suite.py").write_text(SUITE)
        (root / "state").mkdir()
        for run in range(6):
            out = root / "runs" / f"run-{run}"
            env = {"TRUTH": json.dumps(TRUTH), "RUN": str(run), "STATE_DIR": str(root / "state"),
                   "PATH": "", "PYTHONDONTWRITEBYTECODE": "1"}
            subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 str(root / "test_suite.py"), f"--alluredir={out}"],
                cwd=root, env=env, capture_output=True, check=False, timeout=120,
            )
            (out / "executor.json").write_text(json.dumps(
                {"buildOrder": run, "buildName": f"#{100 + run}"}))
        cls.history = build_history(load_runs([root / "runs"]))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def truth_key(name: str) -> str:
        base = name.split("#", 1)[1]
        if "[" in base:
            fn, params = base.split("[", 1)
            return f"{fn}[{params.rstrip(']').split('=', 1)[1].strip(chr(39))}]"
        return base

    def test_matrix_matches_truth(self):
        h = self.history
        self.assertEqual([r.label for r in h.runs], [f"#{100 + i}" for i in range(6)])
        got = {self.truth_key(t.name): t for t in h.tests}
        self.assertEqual(set(got), set(TRUTH))
        for key, expected in TRUTH.items():
            t = got[key]
            with self.subTest(test=key):
                self.assertEqual([c.status for c in t.cells], [STATUS[ch] for ch in expected])
                seq = ["P" if ch in "PR" else "F" for ch in expected if ch in "PRFB"]
                self.assertEqual(t.flips, sum(a != b for a, b in zip(seq, seq[1:])))
                self.assertEqual(t.in_run_flaky, expected.count("R"))
                for c, ch in zip(t.cells, expected):
                    if ch == "R":
                        self.assertEqual(c.attempts, ["failed", "passed"])

    def test_failure_details(self):
        got = {self.truth_key(t.name): t for t in self.history.tests}
        fixture = got["test_fixture_error"].cells[2]
        self.assertEqual(fixture.message, "RuntimeError: fixture setup exploded")
        self.assertRegex(fixture.location, r"test_suite\.py:\d+$")
        alt = got["test_alternating"]
        self.assertEqual([(r.message, r.count) for r in alt.failure_reasons],
                         [(f"AssertionError: test_alternating failed in run {i}", 1)
                          for i in (5, 3, 1)])
        self.assertTrue(all(r.location.startswith("test_suite.py:") for r in alt.failure_reasons))

    def test_ranking(self):
        ranked = [self.truth_key(t.name) for t in self.history.flaky]
        self.assertEqual(ranked[0], "test_alternating")  # 5 flips
        self.assertEqual(ranked[1], "test_param[b]")     # 4 flips
        self.assertEqual(ranked[-1], "test_rerun_rescued")  # 0 flips, only retry flakes
        self.assertNotIn("test_always_fails", ranked)
        self.assertNotIn("test_broken_then_failed", ranked)
        self.assertNotIn("test_skip_gaps", ranked)


class FuzzTests(unittest.TestCase):
    """Randomly corrupted result files must never crash any output format."""

    JUNK = [None, "", "x", 0, -1, 1.5, True, [], {}, [1, "a"], {"a": 1}, "\x00\n<script>",
            10**30, "1700000000000", float("nan"), float("inf")]
    SEED = {
        "uuid": "u", "historyId": "h", "fullName": "pkg.mod#test", "name": "test",
        "status": "failed", "start": 1_700_000_000_000, "stop": 1_700_000_000_500,
        "statusDetails": {"message": "boom\ntrace", "trace": "..."},
        "parameters": [{"name": "p", "value": "1"}, {"name": "s", "value": "x", "mode": "masked"}],
        "labels": [{"name": "suite", "value": "s"}],
    }
    EXECUTOR = {"name": "CI", "buildOrder": 1, "buildName": "#1", "buildUrl": "https://ci/1"}

    def mutate(self, rng, obj, depth=0):
        if isinstance(obj, dict):
            obj = dict(obj)
            for k in list(obj):
                r = rng.random()
                if r < 0.15:
                    obj[k] = rng.choice(self.JUNK)
                elif r < 0.25:
                    del obj[k]
                elif r < 0.6 and depth < 3:
                    obj[k] = self.mutate(rng, obj[k], depth + 1)
            return obj
        if isinstance(obj, list):
            return [self.mutate(rng, x, depth + 1) if rng.random() < 0.5 else rng.choice(self.JUNK)
                    for x in obj]
        return obj if rng.random() < 0.7 else rng.choice(self.JUNK)

    def test_fuzz(self):
        rng = random.Random(1234)
        for _ in range(60):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for r in range(3):
                    d = root / f"r{r}"
                    d.mkdir()
                    for i in range(6):
                        obj = self.mutate(rng, dict(self.SEED, historyId=f"h{i % 3}"))
                        raw = json.dumps(obj if rng.random() > 0.05 else rng.choice(self.JUNK))
                        if rng.random() < 0.05:
                            raw = raw[: len(raw) // 2]
                        (d / f"{i}-result.json").write_text(raw, encoding="utf-8")
                    (d / "executor.json").write_text(json.dumps(
                        self.mutate(rng, self.EXECUTOR) if rng.random() < 0.8
                        else rng.choice(self.JUNK)))
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    h = build_history(load_runs([root]))
                    render_html(h), render_csv(h), render_json(h), render_text(h, top=0)
                    self.assertEqual(main([str(root), "--top", "0"]), 0)


if __name__ == "__main__":
    unittest.main()
