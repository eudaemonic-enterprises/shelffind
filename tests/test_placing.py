# Copyright 2026 Eudaemonic Enterprises Inc.
"""Where a file is placed, case by case: the orders of events in which a text could be shown
for a file that does not hold it, a statement outlive the measure that refutes it, or a
reading be lost or made twice."""
import hashlib
import os
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import invariants
from shelffind import fuse, indexer, link, migrate, store
from shelffind.search import search

from test_evidence import Evidence, names
from test_identity import LARGE, OTHER
from test_migrate import Earlier

THIRD = "Charts of the western shoals. " * 4000
THIRD = THIRD[:len(LARGE)]


class Placing(Evidence):
    def sums(self, path):
        return indexer.whole_checksums(path, {"size": path.stat().st_size, "mtime": path.stat().st_mtime_ns})

    def say(self, rel, work, keys=(), source="library", **more):
        record = {"id": work, "keys": list(keys), "title": f"Work {work}", **more}
        with self.db:
            return link.say(self.db, self.ids()[rel], source, record, set())

    def unsay(self, rel, source="library"):
        with self.db:
            return link.say(self.db, self.ids()[rel], source, None, set())

    def measure(self, rel, local=True):
        found = self.sums(self.root / rel)
        with self.db:
            return indexer.joined(self.db, self.ids()[rel], found if local else found[:1], local=local)

    def blank(self):
        """As a scan leaves its document: every page still to recognise."""
        with self.db:
            self.db.execute("UPDATE pages SET needs_ocr=1")
            self.db.execute("UPDATE docs SET state='needs-ocr' WHERE id IN (SELECT doc FROM pages)")

    def recognising(self):
        return patch("shelffind.extract.recognize", side_effect=lambda source, fmt, work, page, lang: source.read_text()[:60])

    def sound(self):
        self.assertEqual(invariants.structure(self.db), [])
        self.assertEqual(invariants.actual(self.db), invariants.canonical(self.db))

    def shown(self, query):
        """The addresses of the one document found, the copies with it."""
        found = search(self.db, query)
        self.assertEqual(len(found), 1)
        return sorted(str(Path(path).relative_to(self.root)) for path in (found[0]["path"], *found[0]["copies"]))

    def state(self, rel):
        return self.db.execute("SELECT d.state FROM files f JOIN docs d ON d.id=f.doc WHERE f.rel=?", (rel,)).fetchone()[0]


class Pages(Placing):
    """Pages belong to the bytes they were read from, and to no other."""

    def test_a_wrong_word_about_an_unmeasured_file_never_lets_another_file_complete_its_pages(self):
        one = self.write("a/scan.txt")
        self.build(paths=[one])
        self.blank()
        self.unmeasured()                                   # read by an earlier version: its pages are tied to nothing
        two = self.write("b/scan.txt", OTHER)
        self.build(names_only=True)
        self.measure("b/scan.txt")                          # the second was measured, and never read
        self.assertTrue(self.say("a/scan.txt", "W2", self.sums(two)))   # a source takes the first for the second
        self.assertEqual(self.documents(), 2)
        with self.recognising():                            # recognition through the second writes nothing of the first
            done = indexer.build(self.db, self.data, store.roots(self.db), ocr=True, paths=[two], progress=lambda _: None)
        self.assertEqual(done, (0, 0))
        self.assertEqual(self.db.execute("SELECT count(*) FROM pages WHERE needs_ocr=1").fetchone()[0], 1)
        with self.recognising():
            self.assertEqual(self.build(ocr=True), (1, 0))  # through its own file, which is measured on the way
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual((names(self.db, "northern harbours"), names(self.db, "southern capes")),
                         (["a/scan.txt"], ["b/scan.txt"]))
        self.sound()

    def test_two_readings_of_an_earlier_version_become_one_only_by_the_measure_of_both_files(self):
        self.write("a/alpha.txt")
        self.write("b/bravo.txt", OTHER)
        self.write("c/bravo.txt", OTHER)
        self.build(paths=[self.root / "a/alpha.txt", self.root / "c/bravo.txt"])
        self.unmeasured()                                   # two readings of an earlier version, and a file never read
        self.measure("b/bravo.txt")
        self.assertTrue(self.say("a/alpha.txt", "W2", self.sums(self.root / "b/bravo.txt")))    # wrong of the first
        self.measure("c/bravo.txt", local=False)            # what a storage says of pages tied to nothing is not taken
        self.assertEqual(self.documents(), 3)
        self.measure("c/bravo.txt")                         # a reading in full: the third is the second, with its pages
        docs = self.docs()
        self.assertEqual((docs["b/bravo.txt"], docs["a/alpha.txt"] != docs["c/bravo.txt"]), (docs["c/bravo.txt"], True))
        self.assertEqual((self.shown("northern harbours"), self.shown("southern capes")),
                         (["a/alpha.txt"], ["b/bravo.txt", "c/bravo.txt"]))
        self.assertEqual(self.build(), (0, 0))              # the first is measured in its turn: the word about it is refuted
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 0)
        self.assertEqual(names(self.db, "northern harbours"), ["a/alpha.txt"])
        self.sound()

    def test_a_file_replaced_under_its_size_and_date_takes_nothing_of_the_pages_when_it_is_read_again(self):
        path = self.write("scan.txt")
        self.build()
        self.blank()
        stamp = path.stat().st_mtime_ns
        path.write_text(OTHER)                              # other bytes under the same size and the same instant
        os.utime(path, ns=(stamp, stamp))
        with self.recognising():
            self.assertEqual(self.build(ocr=True), (0, 0))  # read for its recognition, it is found to be another document
        self.assertEqual(self.db.execute("SELECT count(*) FROM pages WHERE needs_ocr=1").fetchone()[0], 1)
        self.assertEqual(search(self.db, "northern harbours"), [])
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(names(self.db, "southern capes"), ["Reading Room/scan.txt"])
        self.sound()

    def test_recognition_through_a_true_copy_completes_the_document(self):
        one = self.write("a/scan.txt")
        self.build(paths=[one])
        self.blank()
        two = self.write("b/scan.txt")                      # the same bytes, never measured
        self.build(names_only=True)
        self.assertEqual(self.documents(), 2)
        with self.recognising():
            done = indexer.build(self.db, self.data, store.roots(self.db), ocr=True, paths=[two], progress=lambda _: None)
        self.assertEqual(done, (1, 0))                      # measured on the way, it is the document
        self.assertEqual(names(self.db, "northern harbours"), ["a/scan.txt"])
        self.assertEqual((self.documents(), len(search(self.db, "northern harbours")[0]["copies"])), (1, 1))
        self.sound()

    def test_the_text_of_a_scan_follows_its_file_to_a_new_address(self):
        path = self.write("scan.txt")
        self.build()
        self.blank()
        with self.recognising():
            self.assertEqual(self.build(ocr=True), (1, 0))
        moved = self.root / "filed" / "scan-1999.txt"
        moved.parent.mkdir()
        moved.write_text(path.read_text())                  # filed elsewhere: a new copy with a new date
        path.unlink()
        self.read.clear()
        with patch("shelffind.extract.recognize", side_effect=AssertionError("recognised again")):
            self.assertEqual(self.build(), (0, 0))
            self.assertEqual(self.build(ocr=True), (0, 0))
        self.assertEqual((self.read, names(self.db, "tide")), ([], ["filed/scan-1999.txt"]))
        self.sound()


class Statements(Placing):
    """What a source says is kept for as long as no measure refutes it, and no longer."""

    def test_a_statement_is_refuted_by_the_two_measures_that_contradict_it_together(self):
        one, two = self.write("a/book.txt"), self.write("b/book.txt", OTHER)
        self.build(names_only=True)
        self.measure("a/book.txt", local=False)             # the storage of the first tells its MD5,
        self.assertTrue(self.say("a/book.txt", "W2", [self.sums(two)[1]]))      # a source gives it the SHA-256 of the second,
        self.assertEqual(names(self.db, "work w2"), ["a/book.txt"])
        self.measure("b/book.txt")                          # and the second is measured: the two are not one
        self.assertEqual(names(self.db, "work w2"), [])
        self.assertEqual(self.db.execute("SELECT asked FROM files WHERE rel='a/book.txt'").fetchone()[0], "")
        self.sound()

    def test_a_statement_that_names_the_checksums_of_two_documents_is_not_kept(self):
        one, two = self.write("a/book.txt"), self.write("b/book.txt", OTHER)
        self.write("c/book.txt", THIRD)
        self.build(names_only=True)
        self.measure("a/book.txt", local=False)
        self.measure("b/book.txt")
        self.assertFalse(self.say("c/book.txt", "W9", [self.sums(one)[0], self.sums(two)[1]]))
        self.assertEqual((self.documents(), self.db.execute("SELECT count(*) FROM records").fetchone()[0]), (3, 0))
        self.sound()

    def test_a_true_copy_ends_a_wrong_statement_without_stopping_the_run(self):
        one = self.write("a/book.txt")
        self.build(names_only=True)
        self.measure("a/book.txt", local=False)
        self.assertTrue(self.say("a/book.txt", "W1", ["sha256:" + "ee" * 32]))  # a SHA-256 that the file does not have
        self.write("b/book.txt")                            # a true copy, measured in full
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual((self.documents(), self.db.execute("SELECT count(*) FROM records").fetchone()[0]), (1, 0))
        self.assertEqual(sorted(self.db.execute("SELECT count(*) FROM doc_keys WHERE basis='content'").fetchone()), [2])
        self.sound()

    def test_a_word_does_not_replace_a_checksum_that_a_reading_gave(self):
        one = self.write("a/book.txt")
        copy = self.write("b/book.txt")
        self.build(names_only=True)
        self.measure("a/book.txt", local=False)
        self.measure("b/book.txt")                          # a reading of a true copy gives the document its SHA-256
        copy.unlink()
        self.build(names_only=True)
        self.assertFalse(self.say("a/book.txt", "W1", ["sha256:" + "ee" * 32]))
        held = sorted(row["key"] for row in self.db.execute("SELECT key FROM doc_keys"))
        self.assertEqual(held, sorted(f"{key}@txt" for key in self.sums(one)))
        self.sound()

    def test_a_file_leaves_when_the_last_statement_about_it_is_withdrawn(self):
        one = self.write("a/alpha.txt")
        self.write("b/bravo.txt", OTHER)
        self.build(paths=[one])
        self.build(names_only=True)
        self.assertTrue(self.say("b/bravo.txt", "W1", [self.sums(one)[1]]))     # a source takes the second for the first
        self.assertEqual(self.shown("northern harbours"), ["a/alpha.txt", "b/bravo.txt"])
        one.unlink()
        self.build(names_only=True)
        self.assertEqual(names(self.db, "northern harbours"), ["b/bravo.txt"])  # believed for as long as it is said
        self.assertTrue(self.unsay("b/bravo.txt") is False)
        self.assertEqual(search(self.db, "northern harbours"), [])
        self.assertEqual(self.state("b/bravo.txt"), "pending")
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(names(self.db, "southern capes"), ["b/bravo.txt"])
        self.sound()

    def test_sources_that_came_to_agree_are_believed_again(self):
        one = self.write("a/book.txt")
        self.build(names_only=True)
        wrong, right = "sha256:" + "ee" * 32, self.sums(one)[1]
        for source in ("library", "catalogue"):
            self.assertTrue(self.say("a/book.txt", "W1", [wrong], source=source, title="Old Title"))
        for source in ("library", "catalogue"):             # each corrects itself in its turn
            self.assertTrue(self.say("a/book.txt", "W1", [right], source=source, title="New Title"))
        self.assertEqual([row["key"] for row in self.db.execute("SELECT key FROM doc_keys")], [f"{right}@txt#{len(LARGE)}"])
        self.assertEqual((names(self.db, "new title"), names(self.db, "old title")), (["a/book.txt"], []))
        self.sound()

    def test_a_statement_refused_still_ends_what_the_source_said_before(self):
        one = self.write("a/book.txt")
        self.build(names_only=True)
        self.assertTrue(self.say("a/book.txt", "W1", [], title="Advertised Title"))
        self.measure("a/book.txt")
        self.assertFalse(self.say("a/book.txt", "W2", ["md5:" + "ee" * 16], title="Another Work"))
        self.assertEqual((names(self.db, "advertised title"), names(self.db, "another work")), ([], []))
        self.sound()

    def test_what_a_source_says_of_a_moved_file_replaces_what_it_said_at_the_old_address(self):
        one = self.write("a/book.txt")
        self.build(names_only=True)
        keys = self.sums(one)
        self.measure("a/book.txt")
        self.assertTrue(self.say("a/book.txt", "W1", keys, title="Old Title", text=True))
        self.write("b/book.txt", when=1_800_000_000)
        one.unlink()
        self.build()                                        # known again by its checksum, with what was said of its bytes
        self.assertEqual(names(self.db, "old title"), ["b/book.txt"])
        self.assertTrue(self.say("b/book.txt", "W1", keys, title="New Title", text=False))
        self.assertEqual((names(self.db, "new title"), names(self.db, "old title")), (["b/book.txt"], []))
        self.assertEqual([tuple(row) for row in self.db.execute("SELECT file IS NULL,text FROM records")], [(0, 0)])
        self.sound()

    def test_the_latest_statement_about_bytes_whose_files_are_gone_is_the_one_kept(self):
        one, two = self.write("a/book.txt"), self.write("b/book.txt")
        self.build(names_only=True)
        keys = self.sums(one)
        self.measure("a/book.txt")
        self.measure("b/book.txt")
        self.assertTrue(self.say("a/book.txt", "W1", [], title="First Card"))
        self.assertTrue(self.say("b/book.txt", "W1", keys, title="Second Card", text=True))
        one.unlink()
        two.unlink()
        self.build(names_only=True)
        self.assertEqual([tuple(row) for row in self.db.execute("SELECT title,text FROM records")], [("Second Card", 1)])
        self.assertEqual(self.db.execute("SELECT title FROM docs").fetchone()[0], "Second Card")
        self.sound()

    def test_a_work_whose_file_was_only_said_is_found_again_by_the_measure_of_its_new_file(self):
        one = self.write("a/book.txt")
        self.build(names_only=True)
        self.assertTrue(self.say("a/book.txt", "W1", self.sums(one), title="Known Work", text=True))
        self.write("b/book.txt", when=1_800_000_000)
        one.unlink()
        self.read.clear()
        self.assertEqual(self.build(), (1, 0))              # compared with what was said, then known by it
        self.assertEqual(names(self.db, "known work"), ["b/book.txt"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM docs").fetchone()[0], 1)
        self.sound()


class Collisions(Placing):
    def test_a_file_that_shares_one_checksum_with_a_document_and_differs_by_the_other_stays_apart(self):
        self.write("a/book.txt")
        self.write("b/book.txt", OTHER)
        self.build(names_only=True)
        ids = self.ids()
        with self.db:                                       # two files built to share an MD5: their SHA-256 tells them apart
            indexer.joined(self.db, ids["a/book.txt"], ["md5:" + "aa" * 16, "sha256:" + "11" * 32])
            indexer.joined(self.db, ids["b/book.txt"], ["md5:" + "aa" * 16, "sha256:" + "22" * 32])
        docs = self.docs()
        self.assertNotEqual(docs["a/book.txt"], docs["b/book.txt"])
        held = lambda rel: sorted(row["key"][:9] for row in self.db.execute("SELECT key FROM doc_keys WHERE doc=?", (docs[rel],)))
        self.assertEqual((held("a/book.txt"), held("b/book.txt")), (["md5:aaaaa", "sha256:11"], ["sha256:22"]))
        self.assertEqual(self.build(), (2, 0))              # each is read for itself
        self.assertEqual((names(self.db, "northern harbours"), names(self.db, "southern capes")),
                         (["a/book.txt"], ["b/book.txt"]))


class Reading3(Placing):
    """One reading, of the right bytes, charged to the right document."""

    def copying(self, damaged):
        """Make the readings whose numbers are in `damaged` arrive with one byte changed."""
        real, count = indexer.whole_checksums, [0]

        def reading(path, row, copy=None):
            count[0] += 1
            found = real(path, row, copy)
            if count[0] in damaged and copy is not None:
                data = bytearray(copy.read_bytes())
                data[0] ^= 1
                copy.write_bytes(bytes(data))
                return ["md5:" + hashlib.md5(data).hexdigest(), "sha256:" + hashlib.sha256(data).hexdigest()]
            return found
        return patch("shelffind.indexer.whole_checksums", side_effect=reading)

    def test_a_reading_that_contradicts_the_storage_is_made_a_second_time(self):
        path = self.write("book.txt")
        row = {"size": path.stat().st_size, "mtime": path.stat().st_mtime_ns}
        true = self.sums(path)
        hold = self.base / "hold"
        hold.mkdir()
        with self.copying({1}):                             # the first reading arrives damaged, the second is sound
            copy, found, _ = indexer.private_copy(path, row, hold, true[0])
        self.assertEqual((found, copy.read_text()), (true, LARGE))
        with self.copying({1, 2}):                          # two readings that agree against the storage: the truth here
            copy, found, _ = indexer.private_copy(path, row, hold, "md5:" + "ee" * 16)
        self.assertNotEqual(found, true)
        with self.copying({1}), self.assertRaises(OSError):  # two readings that differ, and neither is what was known
            indexer.private_copy(path, row, hold, "md5:" + "ee" * 16)

    def test_a_file_opened_under_the_name_of_another_is_not_measured(self):
        path = self.write("book.txt")
        row = {"size": path.stat().st_size, "mtime": path.stat().st_mtime_ns}
        other = self.write("other.txt", OTHER, when=1_800_000_000)
        real = Path.open

        def swapped(self_path, *args, **kwargs):            # at the instant of its opening, another file has the name
            return real(other if self_path == path else self_path, *args, **kwargs)

        with patch.object(Path, "open", swapped), self.assertRaises(ValueError):
            indexer.whole_checksums(path, row)

    def test_a_failed_reading_is_the_fault_of_the_document_that_the_file_holds(self):
        one, two = self.write("a/book.txt"), self.write("b/book.txt", OTHER)
        self.build(names_only=True)
        key = self.sums(one)[1]
        self.assertTrue(self.say("a/book.txt", "W1", [key]))
        self.assertTrue(self.say("b/book.txt", "W1", [key]))    # a source takes the second for the first
        self.assertEqual(self.documents(), 1)
        real = indexer.extract.passages

        def failing(source, fmt, work):
            if source.read_text() == OTHER:
                raise RuntimeError("the reader stumbled")
            return real(source, fmt, work)

        with patch("shelffind.extract.passages", side_effect=failing):
            self.assertEqual(self.build(), (2, 1))
        self.assertEqual((self.state("a/book.txt"), self.state("b/book.txt")), ("indexed", "error"))
        self.assertEqual(self.build(retry=True), (1, 0))
        self.assertEqual(names(self.db, "southern capes"), ["b/book.txt"])
        self.sound()

    def test_a_run_reads_no_more_documents_than_it_was_allowed(self):
        one = self.write("a/book.txt")
        self.write("b/book.txt", OTHER)
        self.write("c/book.txt", THIRD)
        self.build(names_only=True)
        key = self.sums(one)[1]
        for rel in ("a/book.txt", "b/book.txt", "c/book.txt"):  # a source takes the three for one work
            self.assertTrue(self.say(rel, "W1", [key]))
        self.assertEqual(self.documents(), 1)
        self.assertEqual((self.build(limit=1), len(self.read)), ((1, 0), 1))
        self.assertEqual((self.build(limit=1), len(self.read)), ((1, 0), 2))
        self.assertEqual((self.build(), len(self.read)), ((1, 0), 3))
        self.sound()

    def test_a_file_that_cannot_be_copied_leaves_the_document_to_its_other_files(self):
        one, two = self.write("a/book.txt"), self.write("b/book.txt")
        self.build(names_only=True)
        self.measure("a/book.txt")
        self.measure("b/book.txt")
        self.assertEqual(self.documents(), 1)
        real = indexer.private_copy

        def refusing(path, row, hold, known=None):
            if path == one:
                raise OSError("the folder does not answer")
            return real(path, row, hold, known)

        with patch("shelffind.indexer.private_copy", side_effect=refusing):
            done = indexer.build(self.db, self.data, store.roots(self.db), paths=[one], progress=lambda _: None)
            self.assertEqual((done, self.state("b/book.txt")), ((1, 1), "pending"))     # its other file was not tried
            self.assertEqual(self.build(), (1, 0))          # and reads the document
        self.assertEqual(self.shown("northern harbours"), ["a/book.txt", "b/book.txt"])
        self.sound()

    def test_identical_files_are_read_once_whether_or_not_a_storage_tells_their_checksums(self):
        for number in range(6):
            self.write(f"copies/{number}.txt")
        self.assertEqual((self.build(), len(self.read), self.documents()), ((1, 0), 1, 1))
        self.sound()


class Storage2(Placing):
    def test_what_a_storage_says_of_pages_tied_to_nothing_is_not_taken(self):
        self.twin()
        path = self.write("book.txt")
        self.build()
        self.unmeasured()                                   # read by an earlier version
        self.assertEqual(self.build(names_only=True), (0, 0))
        self.assertEqual(tuple(self.db.execute("SELECT sum FROM files").fetchone()), (None,))
        self.assertEqual(self.build(), (0, 0))              # a reading in full ties the pages to the bytes
        self.assertEqual(self.db.execute("SELECT origin FROM docs").fetchone()[0].split(), self.sums(path))
        self.assertEqual(self.read, ["book.txt"])
        self.sound()

    def test_what_the_first_batches_taught_is_used_though_the_storage_then_falls_silent(self):
        for number in range(3):
            self.write(f"{number}.txt")
        self.build(names_only=True)
        calls = []                                          # the first question is answered, the second is not
        answer = lambda remote, rows: calls.append(1) or (None if len(calls) > 1 else {row["rel"]: "md5:" + "aa" * 16 for row in rows})
        store.declare({"Reading Room": {"path": str(self.root), "remote": "twin:Room"}})
        self.addCleanup(store.declare, {})
        with patch("shelffind.indexer.ASKED", 2), patch("shelffind.indexer.stored_checksums", side_effect=answer):
            self.assertEqual(self.build(names_only=True), (0, 0))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.documents(), 2)               # the two that were answered for are one document
        self.sound()


class Results(Placing):
    def test_a_result_names_the_document_of_its_checksums_whatever_their_order(self):
        one, two = self.write("copy.txt"), self.write("copy.md")       # the same bytes, two readers
        self.build(names_only=True)
        md5, sha = self.sums(one)
        self.measure("copy.txt", local=False)               # the text file is known by its MD5 alone,
        self.measure("copy.md")                             # the other by both
        with self.db:
            self.db.execute("DELETE FROM doc_keys WHERE key=?", (md5 + "@md",))
        docs = self.docs()
        for keys in ([md5, sha], [sha, md5]):
            found = fuse.document(self.db, "library", {"id": "X", "keys": keys, "format": "md"})
            self.assertEqual(found, docs["copy.md"])
        self.assertEqual(fuse.document(self.db, "library", {"id": "X", "keys": [md5, sha], "format": "md"}, "txt"), docs["copy.txt"])
        two.unlink()
        self.build(names_only=True)
        with self.db:
            self.db.execute("DELETE FROM docs WHERE NOT EXISTS (SELECT 1 FROM files f WHERE f.doc=docs.id)")
        self.assertIsNone(fuse.document(self.db, "library", {"id": "X", "keys": [md5, sha], "format": "md"}, "md"))

    def test_the_checksums_of_a_result_outrank_a_reference_given_to_other_bytes(self):
        old, current = self.write("old.txt"), self.write("current.txt", OTHER)
        self.build(names_only=True)
        self.measure("old.txt")
        self.measure("current.txt")
        self.assertTrue(self.say("old.txt", "W1", self.sums(old)))      # the reference was given to the earlier bytes
        found = fuse.document(self.db, "library", {"id": "W1", "keys": self.sums(current), "format": "txt"})
        self.assertEqual(found, self.docs()["current.txt"])
        self.assertEqual(fuse.document(self.db, "library", {"id": "W1", "keys": [], "format": "txt"}), self.docs()["old.txt"])


class Clocks(Placing):
    def test_each_folder_is_asked_again_in_its_own_time(self):
        annex = self.base / "Annex"
        annex.mkdir()
        self.run_cli("roots", "add", str(annex))
        self.write("east.txt")
        (annex / "west.txt").write_text(OTHER)
        asked = []

        def question(directory, name, op, **fields):
            asked.append(sorted(Path(file["path"]).name for file in fields["files"]))
            return {"records": []}

        usable = lambda directory, progress=None: ({"library": ["library"]}, {"library": False}, set())
        roots = {root["name"]: root for root in store.roots(self.db)}
        run = lambda *chosen: link.link(self.db, self.data, [roots[name] for name in chosen])
        with patch("shelffind.link.usable", side_effect=usable), patch("shelffind.sources.question", side_effect=question), \
                patch("shelffind.sources.policy", return_value={"text": "ignore", "identify": True}):
            self.build(names_only=True)
            self.assertEqual(asked, [["east.txt"], ["west.txt"]])
            with self.db:                                   # a day later
                self.db.execute("UPDATE meta SET value=CAST(value AS REAL)-90000 WHERE key LIKE 'asked:%'")
            del asked[:]
            run("Reading Room")                             # one folder asked alone does not put off the other
            run("Annex")
            self.assertEqual(asked, [["east.txt"], ["west.txt"]])
            del asked[:]
            run("Reading Room", "Annex")
            self.assertEqual(asked, [])


class Conversion2(Earlier):
    """An old index is looked at again once the conversion holds it alone."""

    def change(self, statements):
        db = sqlite3.connect(self.data / store.EARLIER)
        db.executescript(statements)
        db.close()

    def test_an_index_changed_between_the_first_look_and_the_taking_is_left_alone(self):
        real = store.earlier_format
        with patch("shelffind.store.earlier_format", side_effect=lambda directory: 1 if real(directory) == 37 else real(directory)):
            self.change("CREATE TABLE annotations (note TEXT); INSERT INTO annotations VALUES ('the only copy'); PRAGMA user_version=37;")
            with self.assertRaises(ValueError):
                migrate.upgrade(self.data)
        self.assertFalse((self.data / store.INDEX).exists())
        db = sqlite3.connect(self.data / store.EARLIER)
        self.assertEqual((db.execute("PRAGMA user_version").fetchone()[0], db.execute("SELECT note FROM annotations").fetchone()[0]),
                         (37, "the only copy"))
        db.close()

    def test_a_table_this_version_does_not_know_keeps_the_old_index_from_being_converted_or_deleted(self):
        self.change("CREATE TABLE annotations (note TEXT); INSERT INTO annotations VALUES ('the only copy');")
        with self.assertRaises(ValueError) as refusal:
            migrate.upgrade(self.data)
        self.assertIn("annotations", str(refusal.exception))
        self.change("DROP TABLE annotations;")
        self.assertIsNotNone(migrate.upgrade(self.data))
        self.change("CREATE TABLE annotations (note TEXT); INSERT INTO annotations VALUES ('added since');")
        with self.assertRaises(ValueError):
            migrate.discard_old(self.data)
        self.assertTrue((self.data / store.EARLIER).exists())


if __name__ == "__main__":
    unittest.main()
