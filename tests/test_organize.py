from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from allure_history.analysis import build_execution_history, build_history
from allure_history.cli import main
from allure_history.loader import file_and_class, load_runs
from allure_history.render import render_csv, render_html, render_json, test_group

from test_history import page_data


def labels(**kw):
    return [{"name": k, "value": v} for k, v in kw.items()]


class FileAndClassTests(unittest.TestCase):
    def test_allure_pytest_labels(self):
        self.assertEqual(file_and_class({
            "fullName": "src.test.common.test_file.TestPayments#test_pay",
            "labels": labels(parentSuite="src.test.common", suite="test_file",
                             subSuite="TestPayments", package="src.test.common.test_file")}),
            ("src/test/common/test_file.py", "TestPayments"))
        # module-level test: no subSuite
        self.assertEqual(file_and_class({
            "fullName": "src.test.common.test_file#test_module_level",
            "labels": labels(package="src.test.common.test_file", suite="test_file")}),
            ("src/test/common/test_file.py", ""))
        # lowercase class names come from the label, not guessed
        self.assertEqual(file_and_class({
            "fullName": "src.test.common.test_file.test_class#test_name",
            "labels": labels(package="src.test.common.test_file", subSuite="test_class")}),
            ("src/test/common/test_file.py", "test_class"))

    def test_title_path_wins(self):
        # allure-pytest 2.14+: exact file (dots in folder names kept) and nested classes
        self.assertEqual(file_and_class({
            "fullName": "src.test.v1.2.test_ver#test_version",
            "titlePath": ["src", "test", "v1.2", "test_ver.py"],
            "labels": labels(package="src.test.v1.2.test_ver")}),
            ("src/test/v1.2/test_ver.py", ""))
        self.assertEqual(file_and_class({
            "fullName": "src.t.test_auth.TestOuter.TestInner#test_nested",
            "titlePath": ["src", "t", "test_auth.py", "TestOuter", "TestInner"],
            "labels": labels(subSuite="TestOuter > TestInner")}),
            ("src/t/test_auth.py", "TestOuter.TestInner"))
        # junk titlePath falls through to labels
        for junk in (None, "x", [], [1, None], ["no", "python", "file"]):
            self.assertEqual(file_and_class({"titlePath": junk, "fullName": "p.m#t",
                                             "labels": labels(package="p.m")}), ("p/m.py", ""))

    def test_custom_sub_suite_does_not_replace_class(self):
        # @allure.sub_suite("Smoke suite") overrides the subSuite label
        self.assertEqual(file_and_class({
            "fullName": "src.test.common.test_auth.TestCustomSub#test_custom",
            "labels": labels(package="src.test.common.test_auth", subSuite="Smoke suite")}),
            ("src/test/common/test_auth.py", "TestCustomSub"))

    def test_empty_package_label_does_not_hide_a_later_one(self):
        self.assertEqual(file_and_class({
            "fullName": "a.b#t",
            "labels": [{"name": "package", "value": ""}, {"name": "package", "value": "a.b"}]}),
            ("a/b.py", ""))
        self.assertEqual(file_and_class({"fullName": "t", "labels": labels(package="...")}), ("", ""))

    def test_fallbacks_without_labels(self):
        cases = {
            "pkg.mod.TestThing#test_x": ("pkg/mod.py", "TestThing"),
            "pkg.mod#test_x": ("pkg/mod.py", ""),
            "tests/test_a.py::TestA::test_x": ("tests/test_a.py", "TestA"),
            "tests/test_a.py::test_x": ("tests/test_a.py", ""),
            "tests/test_a.py::TestA::TestInner::test_x": ("tests/test_a.py", "TestA.TestInner"),
            "test_x": ("", ""),
            "": ("", ""),
        }
        for full, expected in cases.items():
            with self.subTest(full=full):
                self.assertEqual(file_and_class({"fullName": full}), expected)

    def test_garbage_labels(self):
        for junk in (None, "x", [None, 1, "a"], [{"name": "package"}], [{"name": "package", "value": []}],
                     [{"name": "subSuite", "value": {"a": 1}}]):
            with self.subTest(labels=junk):
                file_and_class({"fullName": "pkg.mod#t", "labels": junk})  # must not raise


class OrganizeOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, run, name, status, start, package="src.tests.test_api", cls="TestApi"):
        d = self.root / run
        d.mkdir(parents=True, exist_ok=True)
        full = f"{package}.{cls}#{name}" if cls else f"{package}#{name}"
        lab = labels(package=package, **({"subSuite": cls} if cls else {}))
        (d / f"{name}-{start}-result.json").write_text(json.dumps({
            "historyId": full, "fullName": full, "status": status, "start": start,
            "stop": start + 10, "labels": lab}))

    def history(self):
        with redirect_stderr(io.StringIO()):
            return build_history(load_runs([self.root]))

    def test_groups_partition_tests(self):
        rows = {"flaky": ["passed", "failed"], "never_fails": ["passed", "skipped"],
                "never_passes": ["failed", "broken"], "always_skipped": ["skipped", "skipped"],
                "failed_and_skipped": ["skipped", "failed"]}
        for name, statuses in rows.items():
            for i, st in enumerate(statuses):
                self.write(f"r{i}", name, st, i * 100)
        groups = {t.name.split("#")[1]: test_group(t) for t in self.history().tests}
        self.assertEqual(groups, {"flaky": "flaky", "never_fails": "passed",
                                  "never_passes": "failed", "always_skipped": "skipped",
                                  "failed_and_skipped": "failed"})

    def test_file_and_class_in_outputs(self):
        self.write("r0", "t1", "passed", 0)
        self.write("r1", "t1", "failed", 100)
        self.write("r0", "t2", "passed", 1, package="src.tests.test_ui", cls="")
        h = self.history()
        data = json.loads(render_json(h))
        by = {t["name"]: t for t in data["tests"]}
        self.assertEqual((by["src.tests.test_api.TestApi#t1"]["file"],
                          by["src.tests.test_api.TestApi#t1"]["class"]),
                         ("src/tests/test_api.py", "TestApi"))
        self.assertEqual(by["src.tests.test_ui#t2"]["class"], "")
        rows = list(csv.reader(io.StringIO(render_csv(h))))
        self.assertEqual(rows[0][:3], ["test", "file", "class"])
        self.assertIn(["src.tests.test_api.TestApi#t1", "src/tests/test_api.py", "TestApi"],
                      [r[:3] for r in rows[1:]])
        page = render_html(h)
        pd = page_data(page)
        t1 = next(t for t in pd["tests"] if t["n"].endswith("#t1"))
        self.assertEqual((pd["msgs"][t1["fi"]], t1["cl"], t1["g"]), ("src/tests/test_api.py", "TestApi", "flaky"))
        for box in ('id="flakyOnly"', 'id="passOnly"', 'id="failOnly"', 'id="skipOnly"',
                    'id="groupFiles"', 'id="collapseAll"'):
            self.assertIn(box, page)
        self.assertIn('Flaky only <span class="muted">(1)</span>', page)
        self.assertIn('Only passed <span class="muted">(1)</span>', page)

    def test_snapshot_keeps_file_and_class(self):
        self.write("r0", "t1", "failed", 0)
        hist = self.root.parent / (self.root.name + "-hist")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["snapshot", str(self.root / "r0"), "-o", str(hist), "--name", "x"]), 0)
            h = build_execution_history(load_runs([hist]))
        self.assertEqual((h.tests[0].file, h.tests[0].cls), ("src/tests/test_api.py", "TestApi"))

    def test_hostile_file_and_class_are_escaped(self):
        self.write("r0", "t", "failed", 0, package="pkg.</script><img src=x onerror=alert(1)>",
                   cls="<svg onload=alert(2)>")
        page = render_html(self.history())
        self.assertEqual(page.count("</script>"), 2)
        self.assertNotIn("<svg onload", page)


if __name__ == "__main__":
    unittest.main()
