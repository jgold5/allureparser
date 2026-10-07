from __future__ import annotations

import gzip
import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from allure_history.analysis import build_history
from allure_history.cli import main
from allure_history.loader import load_run, load_runs
from allure_history.render import render_json, render_text, strip
from allure_history.snapshot import SUFFIX, default_name, read_snapshot, write_snapshot

from test_history import write_result


def run_cli(*args):
    with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
        code = main([str(a) for a in args])
    return code, out.getvalue(), err.getvalue()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hist = self.root / "history"

    def tearDown(self):
        self.tmp.cleanup()

    def results(self, name, statuses: dict, start=1_700_000_000_000, executor=None):
        d = self.root / name
        for test, status in statuses.items():
            if isinstance(status, list):  # retries
                for i, s in enumerate(status):
                    write_result(d, test, s, start=start + i, history_id=f"h-{test}",
                                 message="boom" if s != "passed" else "")
            else:
                write_result(d, test, status, start=start, history_id=f"h-{test}",
                             message="boom" if status != "passed" else "",
                             params=[{"name": "pw", "value": "secret", "mode": "masked"}])
        if executor is not None:
            (d / "executor.json").write_text(json.dumps(executor))
        return d

    def test_round_trip_matches_raw(self):
        dirs = [
            self.results("r0", {"a": "passed", "b": ["failed", "passed"], "c": "broken"}, 1000,
                         {"buildOrder": 7, "buildName": "#7", "buildUrl": "https://ci/7"}),
            self.results("r1", {"a": "failed", "b": "passed", "d": "skipped"}, 2000,
                         {"buildOrder": 8, "buildName": "#8"}),
        ]
        for d in dirs:
            self.assertEqual(run_cli("snapshot", d, "-o", self.hist)[0], 0)
        self.assertEqual(sorted(p.name for p in self.hist.iterdir()),
                         ["build-7" + SUFFIX, "build-8" + SUFFIX])

        def norm(h):
            data = json.loads(render_json(h))
            for r in data["runs"]:
                r.pop("path"), r.pop("id")
            return data

        with redirect_stderr(io.StringIO()):
            raw = build_history(load_runs(dirs))
            snap = build_history(load_runs([self.hist]))
        self.assertEqual(norm(raw), norm(snap))
        self.assertEqual(render_text(raw), render_text(snap))
        self.assertEqual(snap.runs[0].url, "https://ci/7")
        b = next(t for t in snap.tests if t.name.startswith("suite.b"))
        self.assertEqual(b.cells[0].attempts, ["failed", "passed"])
        self.assertEqual(b.in_run_flaky, 1)

    def test_snapshot_contains_no_secrets_or_traces(self):
        d = self.results("r", {"t": "failed"})
        run_cli("snapshot", d, "-o", self.hist, "--name", "x")
        raw = gzip.decompress((self.hist / ("x" + SUFFIX)).read_bytes()).decode()
        self.assertNotIn("secret", raw)
        self.assertNotIn("assert False", raw)  # trace body is not kept...
        self.assertIn("tests/test_t.py:10", raw)  # ...only the failure location
        self.assertIn("boom", raw)

    def test_much_smaller_than_raw_results(self):
        d = self.root / "big"
        for i in range(500):
            write_result(d, f"test_{i}", "failed" if i % 10 == 0 else "passed", start=i,
                         message="AssertionError: x\n" + "trace line\n" * 50)
            (d / f"att-{i}-attachment.png").write_bytes(b"\x89PNG" + b"\0" * 20_000)
        code, _, err = run_cli("snapshot", d, "-o", self.hist, "--name", "big")
        self.assertEqual(code, 0, err)
        raw_bytes = sum(f.stat().st_size for f in d.iterdir())
        snap_bytes = (self.hist / ("big" + SUFFIX)).stat().st_size
        self.assertLess(snap_bytes * 500, raw_bytes)

    def test_cli_overrides(self):
        d = self.results("allure-results", {"t": "passed"}, executor={"buildOrder": 1})
        code, _, err = run_cli("snapshot", d, "-o", self.hist, "--order", "42", "--label",
                               "#42 main", "--url", "https://ci.example/42")
        self.assertEqual(code, 0, err)
        run = read_snapshot(self.hist / ("build-42" + SUFFIX))
        self.assertEqual((run.order, run.label, run.url), (42, "#42 main", "https://ci.example/42"))

    def test_refuses_overwrite_unless_forced(self):
        d = self.results("r", {"t": "passed"})
        self.assertEqual(run_cli("snapshot", d, "-o", self.hist, "--name", "n")[0], 0)
        code, _, err = run_cli("snapshot", d, "-o", self.hist, "--name", "n")
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual(run_cli("snapshot", d, "-o", self.hist, "--name", "n", "--force")[0], 0)

    def test_rejects_bad_input(self):
        d = self.results("r", {"t": "passed"})
        empty = self.root / "empty"
        empty.mkdir()
        self.assertIn("no *-result.json", run_cli("snapshot", empty, "-o", self.hist)[2])
        self.assertIn("http", run_cli("snapshot", d, "-o", self.hist, "--url", "javascript:x")[2])
        for bad in ("../escape", "a/b", "..", "."):
            code, _, err = run_cli("snapshot", d, "-o", self.hist, "--name", bad)
            self.assertEqual(code, 1, bad)
            self.assertIn("invalid snapshot name", err)
        self.assertFalse((self.root / ("escape" + SUFFIX)).exists())

    def test_keep_prunes_oldest(self):
        for i in range(5):
            d = self.results(f"r{i}", {"t": "passed"}, start=1000 + i,
                             executor={"buildOrder": i})
            code, _, err = run_cli("snapshot", d, "-o", self.hist, "--keep", "3")
            self.assertEqual(code, 0, err)
        self.assertEqual(sorted(p.name for p in self.hist.iterdir()),
                         [f"build-{i}{SUFFIX}" for i in (2, 3, 4)])
        self.assertIn("deleted", err)

    def test_keep_leaves_other_files_alone(self):
        self.hist.mkdir()
        (self.hist / "notes.txt").write_text("keep me")
        (self.hist / "other.json.gz").write_bytes(gzip.compress(b"{}"))
        for i in range(3):
            d = self.results(f"r{i}", {"t": "passed"}, executor={"buildOrder": i})
            run_cli("snapshot", d, "-o", self.hist, "--keep", "1")
        self.assertEqual(sorted(p.name for p in self.hist.iterdir()),
                         ["build-2" + SUFFIX, "notes.txt", "other.json.gz"])

    def test_default_names(self):
        d = self.results("allure-results", {"t": "passed"}, start=1_700_000_000_000)
        self.assertEqual(default_name(load_run(d)), "20231114-221320")
        (d / "executor.json").write_text(json.dumps({"buildName": "Nightly #12 / main"}))
        self.assertEqual(default_name(load_run(d)), "Nightly-12-main")
        (d / "executor.json").write_text(json.dumps({"buildName": "x", "buildOrder": 9}))
        self.assertEqual(default_name(load_run(d)), "build-9")

    def test_mixed_raw_and_snapshots(self):
        # Old runs kept as snapshots, the newest still raw.
        for i, status in enumerate(["passed", "failed"]):
            d = self.results(f"r{i}", {"t": status}, start=1000 * (i + 1),
                             executor={"buildOrder": i})
            run_cli("snapshot", d, "-o", self.hist)
        latest = self.results("latest", {"t": "passed"}, start=5000,
                              executor={"buildOrder": 2})
        with redirect_stderr(io.StringIO()):
            h = build_history(load_runs([self.hist, latest]))
        self.assertEqual(strip(h.tests[0]), "PFP")
        # A snapshot file can also be passed directly
        with redirect_stderr(io.StringIO()):
            h = build_history(load_runs([self.hist / ("build-1" + SUFFIX)]))
        self.assertEqual(strip(h.tests[0]), "F")

    def test_corrupt_snapshots_are_skipped(self):
        d = self.results("r", {"t": "passed"}, executor={"buildOrder": 1})
        run_cli("snapshot", d, "-o", self.hist)
        bad = {
            "notgzip": b"plain text",
            "truncated": gzip.compress(b'{"format": "allure-history-snapshot"')[:-4],
            "badjson": gzip.compress(b"{nope"),
            "notutf8": gzip.compress(b"\xff\xfe\x00"),
            "list": gzip.compress(b"[1,2]"),
            "otherformat": gzip.compress(b'{"format": "something-else", "version": 1}'),
            "future": gzip.compress(b'{"format": "allure-history-snapshot", "version": 99}'),
        }
        for name, blob in bad.items():
            (self.hist / (name + SUFFIX)).write_bytes(blob)
        with redirect_stderr(io.StringIO()) as err:
            h = build_history(load_runs([self.hist]))
        self.assertEqual(len(h.runs), 1)
        for name in bad:
            self.assertIn(name, err.getvalue())
        self.assertIn("upgrade allure-history", err.getvalue())

    def test_malformed_fields_inside_snapshot(self):
        junk = [None, "", 1, 1.5, True, [], {}, "x", [None], ["passed"], float("nan")]
        tests = [{"k": "ok", "n": "good", "a": [["failed", 1, 2, "m"], ["passed", 3, 4, ""]]}]
        tests += [{"k": j, "n": j, "a": j} for j in junk]
        tests += [{"k": f"k{i}", "n": "n", "a": [j, [j, j, j, j]]} for i, j in enumerate(junk)]
        data = {"format": "allure-history-snapshot", "version": 1, "label": {},
                "order": "nan", "url": "javascript:alert(1)", "start": "soon", "tests": tests}
        self.hist.mkdir()
        p = self.hist / ("weird" + SUFFIX)
        p.write_bytes(gzip.compress(json.dumps(data).encode()))
        run = read_snapshot(p)
        self.assertEqual(run.label, "weird")
        self.assertIsNone(run.url)
        self.assertIsNone(run.order)
        self.assertEqual(run.tests["ok"].status, "passed")
        h = build_history([run])
        render_text(h, top=0), render_json(h)

    def test_reads_version_1_snapshots(self):
        self.hist.mkdir()
        v1 = {"format": "allure-history-snapshot", "version": 1, "label": "#1", "order": 1,
              "url": None, "start": 5,
              "tests": [{"k": "h", "n": "pkg.t", "a": [["failed", 5, 6, "boom"]]}]}
        p = self.hist / ("old" + SUFFIX)
        p.write_bytes(gzip.compress(json.dumps(v1).encode()))
        run = read_snapshot(p)
        self.assertEqual((run.tests["h"].status, run.tests["h"].message,
                          run.tests["h"].location), ("failed", "boom", ""))

    def test_full_message_and_location_round_trip(self):
        msg = "AssertionError: assert 503 == 200\n +  where 503 = get_status()"
        d = self.results("r", {})
        write_result(d, "t", "failed", start=1, message=msg)
        run_cli("snapshot", d, "-o", self.hist, "--name", "x")
        t = next(iter(read_snapshot(self.hist / ("x" + SUFFIX)).tests.values()))
        self.assertEqual(t.message, msg)
        self.assertEqual(t.location, "tests/test_t.py:10")

    def test_write_is_deterministic_and_atomic(self):
        d = self.results("r", {"t": "failed"})
        run = load_run(d)
        a, b = self.root / "a" / ("x" + SUFFIX), self.root / "b" / ("x" + SUFFIX)
        write_snapshot(run, a)
        time.sleep(1.1)  # gzip header would otherwise embed a different mtime
        write_snapshot(run, b)
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual([p.name for p in a.parent.iterdir()], ["x" + SUFFIX])  # no .tmp left

    # ------------------------------------------------------------ audit regressions

    def test_label_defaults_when_no_executor_or_label(self):
        # Every CI run's folder is typically called "allure-results"
        for i in (1, 2):
            d = self.results(f"ci{i}/allure-results", {"t": "passed"}, start=1000 * i)
            self.assertEqual(run_cli("snapshot", d, "-o", self.hist, "--order", i)[0], 0)
        d = self.results("ci3/allure-results", {"t": "passed"}, start=3000)
        run_cli("snapshot", d, "-o", self.hist, "--name", "nightly-3")
        with redirect_stderr(io.StringIO()):
            labels = [r.label for r in load_runs([self.hist])]
        self.assertEqual(labels, ["#1", "#2", "nightly-3"])

    def test_same_run_as_raw_and_snapshot_counted_once(self):
        dirs = []
        for i, status in enumerate(["passed", "failed", "passed"]):
            dirs.append(self.results(f"b{i}", {"t": status}, start=1000 * (i + 1),
                                     executor={"buildOrder": i, "buildName": f"#{i}"}))
            run_cli("snapshot", dirs[-1], "-o", self.hist)
        with redirect_stderr(io.StringIO()) as err:
            h = build_history(load_runs([self.hist, dirs[-1]]))
        self.assertEqual([r.label for r in h.runs], ["#0", "#1", "#2"])
        self.assertIn("same run", err.getvalue())
        # Runs without any timing data are never merged, even if they look identical
        for i in range(2):
            self.write_untimed(f"u{i}")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(len(load_runs([self.root / "u0", self.root / "u1"])), 2)

    def write_untimed(self, name):
        d = self.root / name
        d.mkdir()
        (d / "a-result.json").write_text(json.dumps(
            {"historyId": "h", "fullName": "t", "status": "passed"}))

    def test_keep_never_deletes_the_snapshot_just_written(self):
        for order in (10, 11, 12):
            run_cli("snapshot", self.results(f"r{order}", {"t": "passed"}, start=order),
                    "-o", self.hist, "--order", order)
        code, _, err = run_cli("snapshot", self.results("late", {"t": "passed"}, start=99),
                               "-o", self.hist, "--order", 5, "--keep", 2)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in self.hist.iterdir()),
                         sorted(f"build-{n}{SUFFIX}" for n in (5, 11, 12)))
        self.assertIn("older than the 2 newest", err)

    def test_name_validation_and_unwritable_output(self):
        d = self.results("r", {"t": "passed"})
        for bad in ("nul\x00byte", "ctrl\x01", "space name", "-leading-dot/..", ".hidden"):
            code, _, err = run_cli("snapshot", d, "-o", self.hist, f"--name={bad}")
            self.assertEqual(code, 1, bad)
            self.assertIn("invalid snapshot name", err)
        blocker = self.root / "a-file"
        blocker.write_text("x")
        code, _, err = run_cli("snapshot", d, "-o", blocker)
        self.assertEqual(code, 1)
        self.assertIn("cannot write", err)
        code, _, err = run_cli(d, "--html", blocker / "out.html")
        self.assertEqual(code, 1)
        self.assertIn("cannot write", err)

    def test_message_cleanup_is_idempotent_through_snapshots(self):
        from allure_history.loader import clean_message
        samples = ["x" * 1999 + "   tail", "a\n" * 30, "  lead\ttab  \n\n\nend  ",
                   "y" * 3000, "z\n" * 19 + "w" * 2100]
        for m in samples:
            self.assertEqual(clean_message(clean_message(m)), clean_message(m), repr(m[:20]))
        d = self.results("r", {})
        write_result(d, "t", "failed", start=1, message=samples[0])
        run_cli("snapshot", d, "-o", self.hist, "--name", "x")
        with redirect_stderr(io.StringIO()):
            raw = load_run(d).tests.popitem()[1].message
        self.assertEqual(read_snapshot(self.hist / ("x" + SUFFIX)).tests.popitem()[1].message, raw)


class AuditRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lone_surrogates_do_not_crash(self):
        d = self.root / "r"
        d.mkdir()
        (d / "a-result.json").write_text(
            '{"historyId": "h", "fullName": "t\\ud800x", "status": "failed", "start": 1,'
            ' "statusDetails": {"message": "bad \\udfff here", "trace": "f.py:1: E"},'
            ' "parameters": [{"name": "p\\ud800", "value": "v\\udc00"}]}')
        out = self.root / "out"
        code, stdout, err = run_cli(d, "--html", out / "h.html", "--csv", out / "h.csv",
                                    "--json", out / "h.json", "--all")
        self.assertEqual(code, 0, err)
        self.assertIn("t?x", stdout)
        self.assertEqual(run_cli("snapshot", d, "-o", self.root / "hist")[0], 0)

    def test_non_utf8_console(self):
        import os
        import subprocess
        import sys
        d = self.root / "r"
        write_result(d, "t", "failed", start=1, params=[{"name": "city", "value": "日本"}])
        write_result(self.root / "s", "t", "passed", start=2,
                     params=[{"name": "city", "value": "日本"}])
        env = dict(os.environ, PYTHONIOENCODING="cp1252",
                   PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        p = subprocess.run([sys.executable, "-m", "allure_history", str(self.root)],
                           capture_output=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr.decode("cp1252", "replace"))
        self.assertIn(b"suite.t[city=??]", p.stdout)

    def test_deeply_nested_json_is_skipped(self):
        d = self.root / "r"
        write_result(d, "ok", "passed", start=1)
        (d / "deep-result.json").write_text("[" * 100_000 + "]" * 100_000)
        (d / "executor.json").write_text("[" * 100_000 + "]" * 100_000)
        with redirect_stderr(io.StringIO()) as err:
            runs = load_runs([d])
        self.assertEqual(list(runs[0].tests), [next(iter(runs[0].tests))])
        self.assertIn("deep-result.json", err.getvalue())
        hist = self.root / "hist"
        hist.mkdir()
        (hist / ("deep" + SUFFIX)).write_bytes(gzip.compress(b"[" * 100_000 + b"]" * 100_000))
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(read_snapshot(hist / ("deep" + SUFFIX)))

    def test_masked_variants_stay_distinct_without_history_id(self):
        for i in range(2):
            d = self.root / f"r{i}"
            d.mkdir()
            for j, (pw, status) in enumerate((("hunter2", "passed"), ("letmein", "failed"))):
                (d / f"{j}-result.json").write_text(json.dumps({
                    "fullName": "pkg.test_login", "status": status, "start": i * 10 + j,
                    "parameters": [{"name": "pw", "value": pw, "mode": "masked"}]}))
        with redirect_stderr(io.StringIO()):
            h = build_history(load_runs([self.root]))
        self.assertEqual(len(h.tests), 2)
        self.assertEqual(h.flaky, [])
        self.assertEqual({t.name for t in h.tests}, {"pkg.test_login[pw=******]"})
        out = render_json(h)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("letmein", out)

    def test_csv_formula_injection_neutralized(self):
        from allure_history.render import render_csv
        import csv as csvmod
        for i, status in enumerate(["passed", "failed"]):
            d = self.root / f"r{i}"
            d.mkdir()
            (d / "a-result.json").write_text(json.dumps({
                "historyId": "h", "fullName": '=HYPERLINK("http://evil","x")',
                "status": status, "start": i}))
            (d / "executor.json").write_text(json.dumps({"buildName": "+cmd", "buildOrder": i}))
        with redirect_stderr(io.StringIO()):
            rows = list(csvmod.reader(io.StringIO(render_csv(build_history(load_runs([self.root]))))))
        self.assertEqual(rows[1][0], "'=HYPERLINK(\"http://evil\",\"x\")")
        self.assertEqual(rows[0][-1], "'+cmd")

    def test_help_mentions_snapshot(self):
        with redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            main(["--help"])
        self.assertIn("allure-history snapshot --help", " ".join(out.getvalue().split()))


if __name__ == "__main__":
    unittest.main()
