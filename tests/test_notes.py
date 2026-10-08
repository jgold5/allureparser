from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from unittest import mock

from allure_history.analysis import build_execution_history
from allure_history.loader import load_runs
from allure_history.notes import (Note, NotesError, load_notes, merge_into, merge_notes,
                                  parse_notes, resolve_test, write_notes)
from allure_history.render import render_html, render_json, render_text
from allure_history.runs import detect_runs

from test_history import page_data
from test_per_execution import run_cli
from test_runs import result


@contextmanager
def cwd(path: Path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def note(id_, text="t", updated="2026-01-01T00:00:00.000Z", **kw) -> dict:
    out = {"id": id_, "test": kw.pop("test", "h-a"), "name": "tests#a", "execution": None,
           "run": None, "text": text, "author": "", "created": "2026-01-01T00:00:00.000Z",
           "updated": updated}
    out.update(kw)
    return out


def tomb(id_, updated, **kw) -> dict:
    return dict({"id": id_, "deleted": True, "updated": updated}, **kw)


def parsed(entries) -> list[Note]:
    with redirect_stderr(io.StringIO()):
        return parse_notes(entries)


class NotesFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.file = self.root / "allure-notes.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data, name="allure-notes.json"):
        path = self.root / name
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def test_missing_file_is_empty(self):
        self.assertEqual(load_notes(self.root / "nope.json"), [])

    def test_bad_json_and_non_list_raise(self):
        for data in ("{not json", '{"notes": []}', "3", "\xff"):
            self.write(data)
            with self.assertRaises(NotesError):
                load_notes(self.file)
        self.file.write_bytes(b"\xff\xfe[]")  # not UTF-8
        with self.assertRaises(NotesError):
            load_notes(self.file)

    def test_malformed_entries_are_skipped_with_a_warning(self):
        self.write([
            note("good", "keep me", execution=1700000000123, run=1700000000000,
                 author="ann"),
            1, "text", None, [],
            {"id": "x", "test": "h-a"},                 # no text
            {"id": "y", "text": "no test"},
            {"id": "z", "test": "h-a", "text": "   "},  # blank
            {"deleted": True, "updated": "2026-01-01T00:00:00Z"},  # tombstone without id
            {"id": "w", "test": ["h"], "text": {"a": 1}},
        ])
        with redirect_stderr(io.StringIO()) as err:
            notes = load_notes(self.file)
        self.assertEqual([n.id for n in notes], ["good"])
        self.assertEqual((notes[0].execution, notes[0].run, notes[0].author),
                         (1700000000123, 1700000000000, "ann"))
        self.assertEqual(err.getvalue().count("skipping note"), 9)

    def test_field_types_are_normalized(self):
        [n] = parsed([{"id": 7, "test": "h-a\x00", "text": "  a\r\nb\tc\x07  ", "name": ["x"],
                       "execution": "123", "run": True, "author": None, "created": 5}])
        self.assertEqual((n.id, n.test, n.text, n.name), ("7", "h-a", "a\nb    c", ""))
        self.assertEqual((n.execution, n.run, n.author, n.created, n.updated),
                         (None, None, "", "5", "5"))  # updated falls back to created

    def test_tombstones_and_derived_ids(self):
        notes = parsed([tomb("gone", "2026-02-01T00:00:00Z", test="h-a"),
                        {"test": "h-a", "text": "hand-written, no id"}])
        self.assertTrue(notes[0].deleted)
        self.assertEqual(notes[0].to_dict(), {"id": "gone", "deleted": True,
                                              "updated": "2026-02-01T00:00:00Z",
                                              "test": "h-a"})
        self.assertEqual(len(notes[1].id), 32)
        self.assertEqual(notes[1].id, parsed([{"test": "h-a", "text": "hand-written, no id"}])[0].id)

    def test_duplicate_ids_keep_the_newest(self):
        with redirect_stderr(io.StringIO()) as err:
            notes = parse_notes([note("a", "old"), note("a", "new", "2026-02-01T00:00:00Z")])
        self.assertEqual([n.text for n in notes], ["new"])
        self.assertIn("more than once", err.getvalue())

    def test_resolve_test(self):
        tests = {"h1": "tests#test_login", "h2": "tests#test_login_sso", "h3": "tests#test_pay"}
        self.assertEqual(resolve_test("h3", tests), ("h3", "tests#test_pay"))
        self.assertEqual(resolve_test("tests#test_login", tests)[0], "h1")  # exact name wins
        self.assertEqual(resolve_test("SSO", tests)[0], "h2")
        with self.assertRaisesRegex(NotesError, "matches 2 tests"):
            resolve_test("login", tests)
        with self.assertRaisesRegex(NotesError, "no test matches"):
            resolve_test("nothing", tests)


class MergeTests(unittest.TestCase):
    def merge(self, current, incoming):
        merged, counts = merge_notes(parsed(current), parsed(incoming))
        return [n.to_dict() for n in merged], counts

    def test_add_update_delete_resurrect(self):
        old = note("a", "old", "2026-01-01T00:00:00Z")
        new = note("a", "new", "2026-01-02T00:00:00Z")
        out, counts = self.merge([], [old, note("b")])
        self.assertEqual(counts["added"], 2)
        out, counts = self.merge([old], [new])
        self.assertEqual((out[0]["text"], counts["updated"]), ("new", 1))
        out, counts = self.merge([new], [old])  # an older copy doesn't win
        self.assertEqual((out[0]["text"], counts["unchanged"]), ("new", 1))
        out, counts = self.merge([new], [tomb("a", "2026-01-03T00:00:00Z")])
        self.assertTrue(out[0]["deleted"])
        self.assertEqual(counts["deleted"], 1)
        back = note("a", "back", "2026-01-04T00:00:00Z")
        out, counts = self.merge([tomb("a", "2026-01-03T00:00:00Z")], [back])
        self.assertEqual((out[0]["text"], counts["updated"]), ("back", 1))
        out, _ = self.merge([tomb("a", "2026-01-05T00:00:00Z")], [back])
        self.assertTrue(out[0]["deleted"])

    def test_times_are_compared_as_instants(self):
        a = note("a", "utc", "2026-01-01T12:00:00.000Z")
        b = note("a", "later", "2026-01-01T13:30:00+01:00")  # 12:30 UTC
        self.assertEqual(self.merge([a], [b])[0][0]["text"], "later")
        c = note("a", "ms later", "2026-01-01T12:30:00.001Z")
        self.assertEqual(self.merge([b], [c])[0][0]["text"], "ms later")

    def test_commutative_and_idempotent(self):
        a = [note("a", "x", "2026-01-02T00:00:00Z"), note("b", "same time, text 1"),
             tomb("c", "2026-01-05T00:00:00Z"), note("d", "only in a")]
        b = [note("a", "y", "2026-01-01T00:00:00Z"), note("b", "same time, text 2"),
             note("c", "edited before the delete", "2026-01-04T00:00:00Z"),
             note("e", "only in b")]
        ab, _ = self.merge(a, b)
        ba, _ = self.merge(b, a)
        self.assertEqual(ab, ba)
        again, counts = self.merge(ab, b)
        self.assertEqual(again, ab)
        self.assertEqual(counts["unchanged"], len(b))
        # stable order: by created (the tombstone has none), then id
        self.assertEqual([n["id"] for n in ab], ["c", "a", "b", "d", "e"])

    def test_concurrent_edits_to_different_notes_are_both_kept(self):
        base = [note("a", "A"), note("b", "B")]
        alice = [note("a", "A by alice", "2026-01-02T00:00:00Z"), note("b", "B")]
        bob = [note("a", "A"), note("b", "B by bob", "2026-01-03T00:00:00Z")]
        out, _ = self.merge(self.merge(base, alice)[0], bob)
        self.assertEqual([n["text"] for n in out], ["A by alice", "B by bob"])

    def test_merge_into_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "sub" / "allure-notes.json"
        counts = merge_into(path, parsed([note("b", created="2026-02-01T00:00:00Z"),
                                          note("a")]))
        self.assertEqual(counts["added"], 2)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([n["id"] for n in data], ["a", "b"])  # sorted by created, id
        self.assertEqual(list(path.parent.iterdir()), [path])  # no temp file left
        # invalid entries in the file are kept, at the end
        path.write_text(json.dumps([{"junk": 1}] + data), encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            merge_into(path, parsed([note("c")]))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))[-1], {"junk": 1})
            text = path.read_text(encoding="utf-8")
            merge_into(path, parsed([note("c")]))  # nothing new: unchanged
        self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_write_is_atomic(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "allure-notes.json"
        write_notes(path, [note("a")])
        before = path.read_text(encoding="utf-8")
        with mock.patch("allure_history.notes.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_notes(path, [note("b")])
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(list(Path(tmp.name).iterdir()), [path])


class NotesCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.big = self.root / "allure-results"
        # two pytest runs (pid 10, then 11)
        for run, (pid, statuses) in enumerate([(10, "PF"), (11, "FP")]):
            for k, test in enumerate(("test_login", "test_login_sso")):
                status = {"P": "passed", "F": "failed"}[statuses[k]]
                result(self.big, test, status, 1_700_000_000_000 + run * 60_000 + k * 10,
                       pid=pid, message="boom" if status == "failed" else "")
        self.file = self.root / "allure-notes.json"

    def tearDown(self):
        self.tmp.cleanup()

    def notes(self, path=None):
        return json.loads((path or self.file).read_text(encoding="utf-8"))

    def add(self, *args):
        return run_cli("note", "add", self.big, "--notes", self.file, *args)

    def test_note_add_by_key_name_and_substring(self):
        self.assertEqual(self.add("--test", "h-test_login", "--text", "by key")[0], 0)
        self.assertEqual(self.add("--test", "tests#test_login", "--text", "by name")[0], 0)
        code, _, err = self.add("--test", "SSO", "--text", "by part", "--author", "ann")
        self.assertEqual(code, 0, err)
        notes = self.notes()
        self.assertEqual([(n["test"], n["text"]) for n in notes],
                         [("h-test_login", "by key"), ("h-test_login", "by name"),
                          ("h-test_login_sso", "by part")])
        n = notes[2]
        self.assertEqual((n["name"], n["author"], n["execution"], n["run"]),
                         ("tests#test_login_sso", "ann", None, None))
        self.assertEqual(len(n["id"]), 32)
        self.assertEqual(n["created"], n["updated"])
        self.assertRegex(n["created"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

    def test_note_add_ambiguous_or_unknown_test(self):
        code, _, err = self.add("--test", "login", "--text", "x")
        self.assertEqual(code, 1)
        self.assertIn("matches 2 tests", err)
        self.assertIn("tests#test_login_sso", err)
        self.assertEqual(self.add("--test", "nothing", "--text", "x")[0], 1)
        self.assertFalse(self.file.exists())

    def test_note_add_execution(self):
        start = 1_700_000_000_000 + 60_000  # test_login's execution in the second run
        code, _, err = self.add("--test", "h-test_login", "--execution", start, "--text", "x")
        self.assertEqual(code, 0, err)
        with redirect_stderr(io.StringIO()):
            second_run = detect_runs(load_runs([self.big]))[1]
        n = self.notes()[0]
        self.assertEqual((n["execution"], n["run"]), (start, second_run.start))
        # to the second (all the HTML report knows) is accepted too
        self.assertEqual(self.add("--test", "h-test_login", "--execution", start // 1000 * 1000,
                                  "--text", "y")[0], 0)
        self.assertEqual(self.notes()[1]["execution"], start)
        code, _, err = self.add("--test", "h-test_login", "--execution", 5, "--text", "x")
        self.assertEqual(code, 1)
        self.assertIn("no execution", err)

    def test_note_add_without_results_and_keeps_other_entries(self):
        self.file.write_text(json.dumps([{"unknown": "entry"}, note("a", test="k")]))
        code, _, err = run_cli("note", "add", "--notes", self.file, "--test", "k", "--text", "hi")
        self.assertEqual(code, 0, err)
        notes = self.notes()
        self.assertEqual(notes[0], {"unknown": "entry"})
        self.assertEqual((notes[-1]["test"], notes[-1]["name"]), ("k", "tests#a"))
        self.file.write_text("{broken")
        code, _, err = run_cli("note", "add", "--notes", self.file, "--test", "k", "--text", "x")
        self.assertEqual(code, 1)
        self.assertEqual(self.file.read_text(), "{broken")  # never overwritten

    def test_note_list(self):
        self.add("--test", "h-test_login", "--text", "first\nsecond line", "--author", "ann")
        self.add("--test", "sso", "--text", "other")
        code, out, err = run_cli("note", "list", "--notes", self.file)
        self.assertEqual(code, 0)
        self.assertIn("ann  tests#test_login", out)
        self.assertIn("    second line", out)
        self.assertIn("2 notes", err)
        code, out, _ = run_cli("note", "list", "--notes", self.file, "--test", "sso")
        self.assertNotIn("first", out)
        self.assertIn("other", out)

    def test_note_merge_command(self):
        self.file.write_text(json.dumps([note("a", "old")]))
        incoming = self.root / "in.json"
        incoming.write_text(json.dumps([note("a", "new", "2026-02-01T00:00:00Z"), note("b"),
                                        "junk"]))
        code, _, err = run_cli("note", "merge", incoming, "--notes", self.file)
        self.assertEqual(code, 0, err)
        self.assertIn("1 added, 1 updated, 0 deleted, 0 unchanged", err)
        self.assertIn("skipping note #3", err)
        self.assertEqual([n["text"] for n in self.notes()], ["new", "t"])
        incoming.write_text("[oops")
        self.assertEqual(run_cli("note", "merge", incoming, "--notes", self.file)[0], 1)

    # ------------------------------------------------------------ reports

    def report(self, *args):
        out = self.root / "out"
        code, stdout, err = run_cli(self.big, "--html", out / "h.html", "--json", out / "h.json",
                                    *args)
        self.assertEqual(code, 0, err)
        page = (out / "h.html").read_text(encoding="utf-8")
        return page, json.loads((out / "h.json").read_text(encoding="utf-8")), stdout, err

    def test_notes_are_embedded_in_html_and_json(self):
        self.file.write_text(json.dumps([note("a", "on login", test="h-test_login"),
                                         tomb("b", "2026-01-02T00:00:00Z", test="h-test_login")]))
        page, data, out, err = self.report("--notes", self.file)
        d = page_data(page)
        self.assertEqual([n["id"] for n in d["notes"]], ["a", "b"])  # tombstones too
        self.assertEqual(d["nf"], "allure-notes.json")
        self.assertTrue(d["nk"].startswith("Test History|"))
        self.assertEqual(len(d["rs"]), 2)
        self.assertEqual({t["h"] for t in d["tests"]}, {"h-test_login", "h-test_login_sso"})
        self.assertEqual([n["text"] for n in data["notes"]], ["on login"])  # no tombstones
        self.assertIn("(1 note)", out)
        self.assertLess(page.index('id="saveNotes"'), page.index("<script>"))

    def test_notes_are_embedded_safely(self):
        evil = '</script><img src=x onerror="alert(1)"><!--'
        self.file.write_text(json.dumps([note("a", evil, test="h-test_login", author=evil)]))
        page, _, _, _ = self.report("--notes", self.file)
        self.assertNotIn("<img", page)
        self.assertEqual(page.count("</script>"), 2)
        self.assertEqual(page_data(page)["notes"][0]["text"], evil)

    def test_notes_file_problems_never_stop_the_report(self):
        page, _, _, err = self.report("--notes", self.root / "missing.json")
        self.assertIn("does not exist yet", err)
        self.assertEqual(page_data(page)["notes"], [])
        self.file.write_text("{bad")
        page, _, _, err = self.report("--notes", self.file)
        self.assertIn("showing no notes", err)

    def test_notes_file_is_discovered_in_the_current_directory(self):
        self.file.write_text(json.dumps([note("a", test="h-test_login")]))
        with cwd(self.root):
            page, _, _, err = self.report()
        self.assertIn("using notes from allure-notes.json", err)
        self.assertEqual(len(page_data(page)["notes"]), 1)
        other = self.root / "elsewhere"
        other.mkdir()
        with cwd(other):
            page, _, _, err = self.report()
        self.assertNotIn("using notes", err)
        self.assertEqual(page_data(page)["notes"], [])

    def test_exports_are_merged_at_report_time(self):
        project = self.root / "project"
        project.mkdir()
        notes_file = project / "allure-notes.json"
        notes_file.write_text(json.dumps([note("a", "old", test="h-test_login")]))
        (project / "allure-notes-export-1.json").write_text(json.dumps(
            [note("a", "new", "2026-02-01T00:00:00Z", test="h-test_login")]))
        work = self.root / "work"
        work.mkdir()
        (work / "allure-notes-export-2.json").write_text(json.dumps(
            [note("b", test="h-test_login_sso"), tomb("a2", "2026-01-01T00:00:00Z")]))
        (work / "allure-notes-export-3.json").write_text("{malformed")
        with cwd(work):
            page, _, _, err = self.report("--notes", notes_file)
        self.assertIn("merged 2 note exports into", err)
        self.assertIn("(1 added, 1 updated, 1 deleted)", err)
        self.assertIn("allure-notes-export-3.json", err)  # skipped, with a warning
        self.assertEqual({n["text"] for n in page_data(page)["notes"] if "text" in n},
                         {"new", "t"})
        saved = notes_file.read_text(encoding="utf-8")
        self.assertTrue((work / "allure-notes-export-2.json").exists())  # never deleted
        with cwd(work):
            _, _, _, err = self.report("--notes", notes_file)
        self.assertIn("already merged", err)
        self.assertEqual(notes_file.read_text(encoding="utf-8"), saved)  # idempotent

    def test_notes_file_is_created_from_exports(self):
        (self.root / "allure-notes-export-x.json").write_text(json.dumps([note("a")]))
        with cwd(self.root):
            page, _, _, err = self.report()
        self.assertIn("created allure-notes.json from 1 note export (1 added", err)
        self.assertEqual([n["id"] for n in self.notes()], ["a"])
        self.assertEqual(len(page_data(page)["notes"]), 1)

    def test_no_merge_exports(self):
        (self.root / "allure-notes-export-x.json").write_text(json.dumps([note("a")]))
        with cwd(self.root):
            page, _, _, err = self.report("--no-merge-exports")
        self.assertFalse(self.file.exists())
        self.assertEqual(page_data(page)["notes"], [])

    def test_text_and_render_without_notes_file(self):
        with redirect_stderr(io.StringIO()):
            h = build_execution_history(load_runs([self.big]))
        h.notes = parsed([note("a", test="h-test_login"), note("b", test="h-test_login"),
                          tomb("c", "2026-01-01T00:00:00Z", test="h-test_login")])
        self.assertIn("(2 notes)", render_text(h))
        self.assertEqual(len(json.loads(render_json(h))["notes"]), 2)
        self.assertEqual(page_data(render_html(h))["nk"], "Test History|allure-notes.json")


if __name__ == "__main__":
    unittest.main()
