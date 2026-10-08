"""Notes on tests and on single executions, kept in a JSON file (allure-notes.json).

The HTML report is regenerated after every CI run, so notes can't live in it. They live
in a small human-readable file instead, committed to the repo or kept next to the
history, and every report embeds them. The file is a JSON list of notes:

    {"id": "3f2a...",                 unique id (uuid4 hex), never changes
     "test": "<historyId>",           the test's key, as used everywhere in this tool
     "name": "tests.test_x#test_y",   the test's full name, for humans
     "execution": 1700000000123,      start (epoch ms) of the execution it is about, or null
     "run": 1700000000000,            start (epoch ms) of that execution's run, or null
     "text": "...", "author": "...",
     "created": "2026-01-31T12:00:00.000Z", "updated": "2026-01-31T12:00:00.000Z"}

A note with an execution belongs to that one execution (and so to its run); a note
without one is about the test in general. A deleted note stays in the file as a
tombstone, {"id": ..., "deleted": true, "updated": ...}, so that merging an older copy
of the file can't bring it back. Merging keeps, for each id, the version with the
newest "updated" (see merge_notes), so files exported from several reports or edited
by several people can be combined in any order.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .loader import _CONTROL_EXCEPT_NEWLINE, _text, _time, _valid_unicode

NOTES_FILE = "allure-notes.json"
# Files the report downloads (allure-notes-export-<time>.json) where it can't save into
# the notes file directly; the next report run merges them in.
EXPORT_GLOB = "allure-notes-export*.json"
MAX_TEXT = 10_000


class NotesError(ValueError):
    """The notes file can't be read or isn't a list of notes."""


@dataclass
class Note:
    id: str
    test: str = ""
    text: str = ""
    name: str = ""
    execution: Optional[int] = None
    run: Optional[int] = None
    author: str = ""
    created: str = ""
    updated: str = ""
    deleted: bool = False

    def to_dict(self) -> dict:
        if self.deleted:  # tombstone: keep what's known about where the note was
            out = {"id": self.id, "deleted": True, "updated": self.updated}
            for k in ("test", "name", "execution", "run", "created"):
                if getattr(self, k) not in ("", None):
                    out[k] = getattr(self, k)
            return out
        return {"id": self.id, "test": self.test, "name": self.name,
                "execution": self.execution, "run": self.run, "text": self.text,
                "author": self.author, "created": self.created, "updated": self.updated}


def note_text(value) -> str:
    """Multi-line note text with control characters removed and size capped."""
    if not isinstance(value, str):
        return ""
    text = _valid_unicode(value).replace("\r\n", "\n").replace("\t", "    ")
    return _CONTROL_EXCEPT_NEWLINE.sub(" ", text).strip()[:MAX_TEXT]


def now_iso() -> str:
    # Same format as JavaScript's Date.toISOString(), which the report uses.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def stamp(value: str) -> float:
    """An ISO-8601 time as epoch seconds (no zone = UTC); -inf if missing or invalid."""
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return float("-inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def new_note(test: str, name: str, text: str, execution: Optional[int] = None,
             run: Optional[int] = None, author: str = "") -> dict:
    now = now_iso()
    return Note(id=uuid.uuid4().hex, test=test, name=name, execution=execution, run=run,
                text=note_text(text), author=_text(author)[:200], created=now,
                updated=now).to_dict()


def read_raw(path: Path) -> list:
    """The file's list of entries, unvalidated; [] if the file doesn't exist."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError, RecursionError) as e:  # bad JSON, bad UTF-8, too deep
        raise NotesError(f"cannot read notes file {path}: {e}") from None
    if not isinstance(data, list):
        raise NotesError(f"notes file {path} is not a JSON list of notes")
    return data


def parse_note(entry) -> Optional[Note]:
    """A valid note or tombstone, or None."""
    if not isinstance(entry, dict):
        return None
    note_id = _text(entry.get("id"))[:100]
    common = dict(test=_text(entry.get("test"))[:1000], name=_text(entry.get("name"))[:1000],
                  execution=_time(entry.get("execution")), run=_time(entry.get("run")),
                  created=_text(entry.get("created"))[:40])
    updated = _text(entry.get("updated"))[:40] or common["created"]
    if entry.get("deleted") is True:
        return Note(id=note_id, updated=updated, deleted=True, **common) if note_id else None
    text = note_text(entry.get("text"))
    if not common["test"] or not text:
        return None
    if not note_id:
        # Hand-written notes may lack an id; derive a stable one, so the report can
        # still tell them apart (e.g. to delete one) across regenerations.
        note_id = hashlib.sha256(json.dumps([common["test"], text, common["created"]])
                                 .encode("utf-8")).hexdigest()[:32]
    return Note(id=note_id, text=text, author=_text(entry.get("author"))[:200],
                updated=updated, **common)


def _canonical(note: Note) -> str:
    # The report's page script compares notes the same way (canonical() in render._JS).
    return json.dumps(note.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def newer(a: Note, b: Note) -> Note:
    """The version that wins a merge: the newest 'updated'; on a tie, the larger
    canonical JSON, so the result doesn't depend on merge order."""
    return max(a, b, key=lambda n: (stamp(n.updated), _canonical(n)))


def parse_notes(entries: list, source: str = "notes") -> list[Note]:
    """Valid notes and tombstones, one per id; bad entries are skipped with a warning."""
    by_id: dict[str, Note] = {}
    for n, entry in enumerate(entries, 1):
        note = parse_note(entry)
        if note is None:
            print(f"warning: {source}: skipping note #{n}: it needs an 'id' and "
                  f"'deleted': true, or a 'test' and a 'text'", file=sys.stderr)
            continue
        other = by_id.get(note.id)
        if other is not None:
            print(f"warning: {source}: note id {note.id} appears more than once; "
                  f"keeping the newest", file=sys.stderr)
            note = newer(note, other)
        by_id[note.id] = note
    return list(by_id.values())


def load_notes(path: Path) -> list[Note]:
    """Notes and tombstones from a notes file ([] if it doesn't exist). Raises
    NotesError if the file as a whole is unusable."""
    return parse_notes(read_raw(path), str(path))


def visible(notes: list[Note]) -> list[Note]:
    return [n for n in notes if not n.deleted]


def sort_notes(notes: list[Note]) -> list[Note]:
    """Stable file order (oldest first), so the file diffs well in git."""
    return sorted(notes, key=lambda n: (n.created, n.id))


def merge_notes(current: list[Note], incoming: list[Note]) -> tuple[list[Note], dict]:
    """Union by id, the newer version winning (see newer): a tombstone newer than an
    edit deletes the note, an edit newer than a tombstone brings it back. Commutative
    and idempotent. Returns the merged notes and counts of what changed."""
    merged = {n.id: n for n in current}
    counts = {"added": 0, "updated": 0, "deleted": 0, "unchanged": 0}
    for note in incoming:
        before = merged.get(note.id)
        after = note if before is None else newer(before, note)
        merged[note.id] = after
        if before is not None and _canonical(before) == _canonical(after):
            counts["unchanged"] += 1
        elif after.deleted:
            counts["deleted"] += 1
        elif before is None:
            counts["added"] += 1
        else:
            counts["updated"] += 1
    return sort_notes(list(merged.values())), counts


def merge_into(path: Path, incoming: list[Note]) -> dict:
    """Merge notes into a notes file (created if missing), written only if it changes.
    Entries of the file that aren't valid notes are kept as they are, at the end.
    Returns the counts from merge_notes. Raises NotesError if the file is unusable."""
    raw = read_raw(path)
    invalid = [e for e in raw if parse_note(e) is None]
    merged, counts = merge_notes(parse_notes(raw, str(path)), incoming)
    entries = [n.to_dict() for n in merged] + invalid
    if entries != raw or not path.exists():
        write_notes(path, entries)
    return counts


def write_notes(path: Path, entries: list) -> None:
    """Write atomically, one note per block, so the file diffs well in git."""
    data = json.dumps(entries, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def resolve_test(query: str, tests: dict[str, str]) -> tuple[str, str]:
    """(key, name) of the one test matching query: its key, its full name, or a unique
    case-insensitive substring of its name. Raises NotesError if none or several match."""
    if query in tests:
        return query, tests[query]
    for found in ([k for k, n in tests.items() if n == query],
                  [k for k, n in tests.items() if query.lower() in n.lower()]):
        if len(found) == 1:
            return found[0], tests[found[0]]
        if len(found) > 1:
            names = sorted(tests[k] for k in found)
            more = f"\n  ... and {len(names) - 10} more" if len(names) > 10 else ""
            raise NotesError(f"--test {query!r} matches {len(names)} tests; be more specific "
                             f"or pass the test key:\n  " + "\n  ".join(names[:10]) + more)
    raise NotesError(f"no test matches --test {query!r}")


_ISO = re.compile(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d)")


def format_created(created: str) -> str:
    m = _ISO.match(created)
    return f"{m.group(1)} {m.group(2)} UTC" if m else created
