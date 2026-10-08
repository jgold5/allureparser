from __future__ import annotations

import csv
import hashlib
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
        "historyId": history_id or hashlib.md5(f"{name}{params}".encode()).hexdigest(),
        "fullName": f"suite.{name}",
        "name": name,
        "status": status,
        "start": start,
        "stop": start + 100,
        "parameters": params or [],
    }
    if message:
        result["statusDetails"] = {
            "message": message,
            "trace": f"def test_{name}():\n>       assert False\nE       {message}\n\n"
                     f"tests/test_{name}.py:10: AssertionError",
        }
    (run_dir / f"{result['uuid']}-result.json").write_text(json.dumps(result))


def page_data(page: str) -> dict:
    """The JSON matrix embedded in the HTML report."""
    start = page.index('<script id="history-data" type="application/json">')
    body = page[page.index(">", start) + 1: page.index("</script>", start)]
    return json.loads(body)


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
            write_result(run, "t", status, start=1000 - order)  # start order is the reverse
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
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertEqual(page.count("</script>"), 2)  # data block + page script only
        data = page_data(page)
        self.assertEqual(data["runs"], ["run-00", "run-01", "run-02"])
        rows = {t["n"]: t for t in data["tests"]}
        self.assertEqual(rows["suite.flappy"]["s"], "PFP")
        self.assertEqual(rows["suite.flappy"]["k"], 1)
        flappy = rows["suite.flappy"]
        self.assertEqual(data["msgs"][flappy["m"]["1"]], "boom")
        self.assertEqual(data["msgs"][flappy["o"]["1"]], "tests/test_flappy.py:10")
        # per-cell start (seconds after t0) and duration (ms), for the detail panel
        self.assertEqual(len(flappy["t"]), 3)
        self.assertEqual(flappy["d"], [100, 100, 100])
        self.assertEqual(data["urls"], [None, None, None])
        self.assertEqual(data["msgs"][rows["suite.flappy"]["lf"]],
                         "Failure reasons:\n  1\u00d7 boom  at tests/test_flappy.py:10")
        self.assertIn("suite.<script>alert(1)</script>", rows)

    def test_html_retry_and_message_dedup(self):
        for i in range(3):
            run = self.root / f"r{i}"
            write_result(run, "t", "failed", start=i * 10, history_id="h", message="same error")
            write_result(run, "t", "passed" if i == 1 else "failed", start=i * 10 + 5,
                         history_id="h", message="same error")
        h, _ = self.load_quiet()
        data = page_data(render_html(h))
        t = data["tests"][0]
        self.assertEqual(t["s"], "FPF")
        self.assertEqual(t["a"], {"0": "failed \u2192 failed", "1": "failed \u2192 passed",
                                  "2": "failed \u2192 failed"})
        self.assertEqual(data["msgs"][t["m"]["0"]], "same error")
        self.assertEqual(data["msgs"][t["o"]["0"]], "tests/test_t.py:10")
        self.assertEqual(t["m"]["0"], t["m"]["1"])  # identical details stored once
        self.assertEqual(data["msgs"][t["lf"]],
                         "Failure reasons:\n  2\u00d7 same error  at tests/test_t.py:10")

    def test_html_panel_markup_precedes_page_script(self):
        # The page script runs where it appears and looks up the detail panel by id, so
        # the panel has to come first (otherwise nothing renders at all).
        h = self.history({"t": ["passed", "failed"]})
        page = render_html(h)
        self.assertLess(page.index('<aside id="detail"'), page.index("<script>"))
        for el in ('id="dbody"', 'id="dprev"', 'id="dnext"', 'id="dclose"', 'id="dpos"'):
            self.assertLess(page.index(el), page.index("<script>"), el)

    def test_html_empty(self):
        from allure_history.analysis import History
        page = render_html(History(runs=[], tests=[]))
        self.assertIn("No test results found.", page)
        self.assertNotIn("history-data", page)

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

    # ------------------------------------------------------------ robustness / regressions

    def load_quiet(self, *paths):
        with redirect_stderr(io.StringIO()) as err:
            h = build_history(load_runs(list(paths) or [self.root]))
        return h, err.getvalue()

    def write_raw(self, run: str, name: str, obj):
        d = self.root / run
        d.mkdir(parents=True, exist_ok=True)
        text = obj if isinstance(obj, str) else json.dumps(obj)
        (d / name).write_text(text, encoding="utf-8")

    def test_malformed_field_types_do_not_crash(self):
        bad_values = [None, "", 0, 1.5, True, [], {}, [1], {"a": 1}, "x", float("nan"), 10**30]
        for i, v in enumerate(bad_values):
            self.write_raw("run-0", f"{i:03d}-result.json", {
                "historyId": f"h{i}", "fullName": f"t{i}", "status": v, "start": v, "stop": v,
                "statusDetails": v, "parameters": v,
            })
            self.write_raw("run-0", f"{i:03d}b-result.json", {
                "historyId": f"h{i}", "fullName": f"t{i}", "status": "passed",
                "statusDetails": {"message": v}, "parameters": [v, {"name": v, "value": v}],
                "start": "1700000000000", "stop": 5,
            })
        self.write_raw("run-0", "executor.json", ["not", "a", "dict"])
        self.write_raw("run-1", "x-result.json", {"historyId": "h0", "status": "failed",
                                                  "start": float("inf")})
        self.write_raw("run-1", "executor.json", {"buildOrder": float("nan"), "buildName": {}})
        h, _ = self.load_quiet()
        self.assertEqual(len(h.runs), 2)
        render_html(h), render_csv(h), render_json(h), render_text(h, top=0)

    def test_non_object_result_and_bom_files(self):
        self.write_raw("run-0", "a-result.json", "[1, 2, 3]")
        self.write_raw("run-0", "b-result.json",
                       "\ufeff" + json.dumps({"historyId": "h", "fullName": "bom", "status": "passed"}))
        h, err = self.load_quiet()
        self.assertEqual([t.name for t in h.tests], ["bom"])
        self.assertIn("not a JSON object", err)

    def test_javascript_build_url_is_dropped(self):
        write_result(self.root / "r", "t", "passed", start=1)
        self.write_raw("r", "executor.json", {"buildName": "#1", "buildUrl": "javascript:alert(1)",
                                              "reportUrl": " JavaScript:alert(2)"})
        h, _ = self.load_quiet()
        self.assertIsNone(h.runs[0].url)
        self.assertNotIn("javascript:", render_html(h).lower())

        write_result(self.root / "s", "t", "passed", start=2)
        self.write_raw("s", "executor.json", {"buildUrl": "HTTPS://ci.example/2"})
        h, _ = self.load_quiet()
        self.assertEqual(h.runs[1].url, "HTTPS://ci.example/2")

    def test_masked_and_excluded_parameters(self):
        write_result(self.root / "r", "login", "passed", start=1, params=[
            {"name": "user", "value": "bob"},
            {"name": "password", "value": "hunter2", "mode": "masked"},
            {"name": "token", "value": "abc", "mode": "hidden"},
            {"name": "ts", "value": "123", "excluded": True},
        ])
        h, _ = self.load_quiet()
        self.assertEqual(h.tests[0].name, "suite.login[user=bob, password=******]")
        for out in (render_html(h), render_csv(h), render_json(h), render_text(h, flaky_only=False)):
            self.assertNotIn("hunter2", out)

    def test_control_characters_in_names_are_flattened(self):
        write_result(self.root / "r", "t", "failed", start=1,
                     params=[{"name": "x", "value": "line1\nline2\ttab"}])
        write_result(self.root / "s", "t", "passed", start=2,
                     params=[{"name": "x", "value": "line1\nline2\ttab"}])
        h, _ = self.load_quiet()
        self.assertEqual(h.tests[0].name, "suite.t[x=line1 line2 tab]")
        table_rows = render_text(h).splitlines()[4:]
        self.assertEqual(len(table_rows), 1)

    def test_retry_order_follows_start_time_like_allure(self):
        run = self.root / "r"
        run.mkdir()
        # Second attempt started later but its stop time is missing; it is still the final one.
        self.write_raw("r", "a-result.json", {"historyId": "h", "fullName": "t",
                                              "status": "failed", "start": 100, "stop": 200})
        self.write_raw("r", "b-result.json", {"historyId": "h", "fullName": "t",
                                              "status": "passed", "start": 300})
        # An attempt with no timing at all counts as the oldest.
        self.write_raw("r", "c-result.json", {"historyId": "h", "fullName": "t",
                                              "status": "broken"})
        h, _ = self.load_quiet()
        self.assertEqual(h.tests[0].cells[0].attempts, ["broken", "failed", "passed"])
        self.assertEqual(h.tests[0].cells[0].status, "passed")

    def test_nested_run_directories(self):
        # Typical layout of downloaded CI artifacts: <build>/allure-results/
        write_result(self.root / "build-1" / "allure-results", "t", "passed", start=1)
        write_result(self.root / "build-2" / "allure-results", "t", "failed", start=2)
        write_result(self.root / "deep" / "x" / "y" / "allure-results", "t", "passed", start=3)
        h, _ = self.load_quiet()
        self.assertEqual([r.id for r in h.runs],
                         ["build-1/allure-results", "build-2/allure-results",
                          "y/allure-results"])
        self.assertEqual(strip(h.tests[0]), "PFP")

    def test_symlink_loop_does_not_hang(self):
        write_result(self.root / "a" / "run", "t", "passed", start=1)
        try:
            (self.root / "a" / "loop").symlink_to(self.root / "a", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported")
        h, _ = self.load_quiet()
        self.assertEqual(len(h.runs), 1)

    def test_same_dir_given_twice_is_one_run(self):
        write_result(self.root / "r", "t", "passed", start=1)
        h, _ = self.load_quiet(self.root, self.root / "r", self.root / "r" / ".." / "r")
        self.assertEqual(len(h.runs), 1)

    def test_container_only_and_missing_paths_warn(self):
        self.write_raw("only-containers", "x-container.json", {"children": []})
        h, err = self.load_quiet(self.root / "only-containers", self.root / "does-not-exist")
        self.assertEqual(h.runs, [])
        self.assertIn("no *-result.json", err)
        self.assertIn("not a directory", err)

    def test_partial_build_order_falls_back_to_start_time(self):
        write_result(self.root / "a", "t", "failed", start=5000)
        self.write_raw("a", "executor.json", {"buildOrder": 1})
        write_result(self.root / "b", "t", "passed", start=1000)  # no executor.json
        h, _ = self.load_quiet()
        self.assertEqual([r.id for r in h.runs], ["b", "a"])

    def test_negative_cli_numbers_rejected(self):
        write_result(self.root / "r", "t", "passed", start=1)
        for flag in ("--top", "--last", "--min-runs"):
            with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as cm:
                main([str(self.root), flag, "-1"])
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("must be 0 or greater", err.getvalue())

    def test_text_top_limit(self):
        h = self.history({f"t{i}": ["passed", "failed"] for i in range(5)})
        text = render_text(h, top=2)
        self.assertIn("... and 3 more", text)
        self.assertEqual(sum("suite.t" in line for line in text.splitlines()), 2)
        self.assertEqual(sum("suite.t" in line for line in render_text(h, top=0).splitlines()), 5)

    def test_single_run_and_all_skipped(self):
        h = self.history({"one": ["failed"], "skip": ["skipped"]})
        self.assertEqual(h.flaky, [])
        self.assertIn("No flaky tests found.", render_text(h))
        self.assertEqual({t.flip_rate for t in h.tests}, {0.0})

    def test_unknown_status(self):
        h = self.history({"weird": ["passed", "unknown", "failed"]})
        t = h.tests[0]
        self.assertEqual(strip(t), "P?F")
        self.assertEqual(t.flips, 1)  # unknown is ignored like skipped
        self.assertEqual(page_data(render_html(h))["tests"][0]["s"], "P?F")

    def test_name_change_uses_latest_name(self):
        write_result(self.root / "r1", "old_name", "passed", start=1, history_id="same")
        write_result(self.root / "r2", "new_name", "failed", start=2, history_id="same")
        h, _ = self.load_quiet()
        self.assertEqual([t.name for t in h.tests], ["suite.new_name"])
        self.assertEqual(strip(h.tests[0]), "PF")

    def test_missing_history_id_falls_back_to_name_and_params(self):
        for i, status in enumerate(["passed", "failed"]):
            self.write_raw(f"r{i}", "a-result.json", {
                "fullName": "pkg.t", "status": status, "start": i,
                "parameters": [{"name": "x", "value": "1"}]})
            self.write_raw(f"r{i}", "b-result.json", {
                "fullName": "pkg.t", "status": "passed", "start": i,
                "parameters": [{"name": "x", "value": "2"}]})
        h, _ = self.load_quiet()
        self.assertEqual(len(h.tests), 2)
        self.assertEqual([t.name for t in h.flaky], ["pkg.t[x=1]"])

    def test_csv_round_trip_with_awkward_names(self):
        h = self.history({'comma, "quote"': ["passed", "failed"], "ünïcødé ✓": ["passed"] * 2})
        rows = list(csv.reader(io.StringIO(render_csv(h))))
        names = {r[0] for r in rows[1:]}
        self.assertEqual(names, {'suite.comma, "quote"', "suite.ünïcødé ✓"})

    # ------------------------------------------------------------ failure details

    def test_multiline_pytest_message_kept(self):
        msg = ("AssertionError: assert {'a': 1, 'b':...c': [1, 2, 3]} == {'a': 1, 'b':...}\n\n"
               "Omitting 1 identical items, use -vv to show\nDiffering items:\n"
               "{'b': 2} != {'b': 3}")
        write_result(self.root / "r", "t", "failed", start=1, message=msg)
        h, _ = self.load_quiet()
        c = h.tests[0].cells[0]
        self.assertIn("{'b': 2} != {'b': 3}", c.message)
        self.assertEqual(c.location, "tests/test_t.py:10")
        self.assertTrue(h.tests[0].last_failure.startswith("AssertionError: assert {'a'"))

    def test_message_caps_and_cleanup(self):
        from allure_history.loader import MAX_MESSAGE_CHARS, clean_message
        self.assertEqual(clean_message("a\r\nb\x00c\tz  \n"), "a\nb c    z")
        long_lines = clean_message("\n".join(f"line {i}" for i in range(100)))
        self.assertEqual(len(long_lines.splitlines()), 21)
        self.assertTrue(long_lines.endswith("\u2026"))
        self.assertLessEqual(len(clean_message("x" * 10_000)), MAX_MESSAGE_CHARS + 2)
        for junk in (None, 1, [], {}):
            self.assertEqual(clean_message(junk), "")

    def test_failure_location_parsing(self):
        from allure_history.loader import failure_location as loc
        self.assertEqual(loc("...\nE   assert 0\n\ntests/test_a.py:42: AssertionError"),
                         "tests/test_a.py:42")
        self.assertEqual(loc("x\n/home/ci/work/app/tests/test_a.py:7: KeyError\n\n"),
                         "/home/ci/work/app/tests/test_a.py:7")
        self.assertEqual(loc("x\nC:\\ci\\tests\\test_a.py:3: requests.exceptions.Timeout"),
                         "C:\\ci\\tests\\test_a.py:3")
        self.assertEqual(loc("src/pkg/helpers.py:120: Failed"), "src/pkg/helpers.py:120")
        # Not pytest format: skip tuples, Java stacks, junk
        self.assertEqual(loc("('/x/test_a.py', 20, 'Skipped: reason')"), "")
        self.assertEqual(loc("java.lang.AssertionError\n\tat com.x.FooTest.t(FooTest.java:12)"), "")
        for junk in (None, "", 5, ["a"]):
            self.assertEqual(loc(junk), "")

    def test_failure_reasons_grouped_and_ranked(self):
        statuses = ["failed", "passed", "failed", "broken", "failed", "passed"]
        messages = ["TimeoutError: read timed out", "", "TimeoutError: read timed out",
                    "ConnectionError: db down", "TimeoutError: read timed out", ""]
        for i, (st, m) in enumerate(zip(statuses, messages)):
            write_result(self.root / f"r{i}", "t", st, start=i, message=m)
        h, _ = self.load_quiet()
        reasons = [(r.message, r.location, r.count) for r in h.tests[0].failure_reasons]
        self.assertEqual(reasons, [("TimeoutError: read timed out", "tests/test_t.py:10", 3),
                                   ("ConnectionError: db down", "tests/test_t.py:10", 1)])
        data = json.loads(render_json(h))
        self.assertEqual(data["tests"][0]["failure_reasons"][0],
                         {"message": "TimeoutError: read timed out",
                          "location": "tests/test_t.py:10", "count": 3, "last_run": "r4"})
        self.assertEqual(data["tests"][0]["cells"][3]["location"], "tests/test_t.py:10")
        page = page_data(render_html(h))
        self.assertIn("3\u00d7 TimeoutError: read timed out  at tests/test_t.py:10",
                      page["msgs"][page["tests"][0]["lf"]])


if __name__ == "__main__":
    unittest.main()
