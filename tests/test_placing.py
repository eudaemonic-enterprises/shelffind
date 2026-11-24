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

    def test_a_statement_about_a_file_read_by_an_earlier_version_is_dropped_when_a_measure_refutes_it(self):
        one = self.write("a/book.txt")
        self.build()
        self.unmeasured()                                   # read by an earlier version: no word places it
        small = self.write("b/small.txt", "A short note.")
        self.build(names_only=True)
        self.assertTrue(self.say("a/book.txt", "W9", self.sums(small), title="Taken For A Note"))
        self.assertEqual(names(self.db, "taken for a note"), ["a/book.txt"])
        self.measure("b/small.txt")                         # the note is measured: a file of another size is not it
        self.assertEqual(names(self.db, "taken for a note"), [])
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 0)
        self.assertEqual(names(self.db, "northern harbours"), ["a/book.txt"])
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
        held = sorted(tuple(row) for row in self.db.execute("SELECT key,basis FROM doc_keys"))
        self.assertEqual(held, sorted((key, "content") for key in self.sums(one)))
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
        self.assertEqual([tuple(row) for row in self.db.execute("SELECT key,format,size,basis FROM doc_keys")],
                         [(right, "txt", len(LARGE), "source")])
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


class Words2(Placing):
    """Files that only the words of sources hold together."""

    def test_a_file_said_to_be_a_work_whose_files_are_gone_starts_that_work_anew(self):
        one = self.write("a/book.txt")
        self.build(names_only=True)
        md5, sha = self.sums(one)
        self.assertTrue(self.say("a/book.txt", "W1", [sha, md5], title="Library Card", text=True))
        one.unlink()
        self.write("b/book.txt", OTHER)                     # another file of that size, which is not that work
        self.build(names_only=True)
        self.assertEqual(self.db.execute("SELECT count(*) FROM records WHERE file IS NULL").fetchone()[0], 1)
        self.assertTrue(self.say("b/book.txt", "W1", [md5], source="catalogue", title="Catalogue Card"))
        self.assertEqual((names(self.db, "catalogue card"), names(self.db, "library card")), (["b/book.txt"], []))
        self.assertEqual([tuple(row) for row in self.db.execute("SELECT key,format,size,basis FROM doc_keys")],
                         [(md5, "txt", len(LARGE), "source")])
        self.sound()

    def test_what_was_said_of_a_file_without_naming_its_bytes_is_forgotten_with_it(self):
        one, two = self.write("a/book.txt"), self.write("b/book.txt")
        self.build(names_only=True)
        self.measure("a/book.txt")
        self.assertTrue(self.say("b/book.txt", "W1", self.sums(one)))       # the second is said to be the first,
        self.assertTrue(self.say("b/book.txt", "C1", [], source="catalogue", title="Loose Card"))   # and bears a card
        self.assertEqual(self.shown("loose card"), ["a/book.txt", "b/book.txt"])
        two.unlink()
        self.build(names_only=True)
        self.assertEqual(sorted(row["source"] for row in self.db.execute("SELECT source FROM records")), ["library"])
        self.assertEqual(names(self.db, "loose card"), [])
        self.sound()

    def test_a_source_that_repeats_itself_moves_no_file(self):
        for folder in "abc":
            self.write(f"{folder}/book.txt")
        self.build(names_only=True)
        key = ["md5:" + "aa" * 16]
        for folder in "abc":
            self.assertTrue(self.say(f"{folder}/book.txt", "W1", key, title="First Title"))
        docs = self.docs()
        with patch("shelffind.store.place", side_effect=AssertionError("a file was placed again")):
            for folder in "abc":
                self.assertTrue(self.say(f"{folder}/book.txt", "W1", key, title="Second Title"))
        self.assertEqual((self.docs(), self.documents()), (docs, 1))
        self.assertEqual((len(names(self.db, "second title")), names(self.db, "first title")), (1, []))
        self.sound()

    def test_files_held_together_through_one_file_part_when_it_says_less(self):
        for folder in "abc":
            self.write(f"{folder}/book.txt")
        self.build(names_only=True)
        first, second = "md5:" + "aa" * 16, "sha256:" + "bb" * 32
        self.assertTrue(self.say("a/book.txt", "W1", [first]))
        self.assertTrue(self.say("b/book.txt", "W1", [first, second]))
        self.assertTrue(self.say("c/book.txt", "W1", [second]))
        self.assertEqual(self.documents(), 1)               # the second holds the three together
        self.assertTrue(self.say("b/book.txt", "W1", [first]))
        docs = self.docs()
        self.assertEqual((docs["a/book.txt"] == docs["b/book.txt"], docs["b/book.txt"] == docs["c/book.txt"]), (True, False))
        self.sound()


class Sixth(Placing):
    """Cases in which the placing went wrong before it was examined a sixth time."""

    def exact(self, word, size=512):
        """A text of exactly `size` bytes that says `word`."""
        return (word + " ") * (size // (len(word) + 1)) + "." * (size % (len(word) + 1))

    def test_the_ending_of_a_file_name_cannot_pass_for_the_size_a_checksum_was_said_of(self):
        one = self.write("notes.txt", self.exact("northbound"))
        two = self.write("notes.txt#512", self.exact("southbound"))    # its format reads txt#512
        self.build(names_only=True)
        self.assertTrue(self.say("notes.txt", "W2", self.sums(two)))    # a source takes the first for the second,
        self.assertTrue(self.say("notes.txt#512", "W2", self.sums(two)))    # and says the truth of the second
        self.assertEqual(self.documents(), 2)               # two formats: two documents, whatever is said
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual((names(self.db, "northbound"), search(self.db, "southbound", exact=True)), (["Reading Room/notes.txt"], []))
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 1)      # the false statement is gone
        two.unlink()
        self.write("identical.txt", self.exact("northbound"), when=1_800_000_000)
        self.assertEqual(self.build(), (0, 0))
        self.assertEqual((self.shown("northbound"), search(self.db, "southbound", exact=True)), (["identical.txt", "notes.txt"], []))
        self.sound()

    def test_a_document_without_file_keeps_its_checksums_when_its_bytes_arrive_under_another_format(self):
        old = self.write("old.txt#draft")
        self.build(names_only=True)
        self.measure("old.txt#draft")
        self.assertTrue(self.say("old.txt#draft", "W1", self.sums(old), title="Draft Card"))
        old.unlink()
        self.write("new.txt")                               # the same bytes, another format
        self.assertEqual(self.build(), (1, 0))
        kept = [tuple(row) for row in self.db.execute("SELECT format,basis,count(*) FROM doc_keys GROUP BY 1,2 ORDER BY 1")]
        self.assertEqual(kept, [("txt", "content", 2), ("txt#draft", "content", 2)])
        self.assertEqual([row["title"] for row in self.db.execute("SELECT title FROM records WHERE file IS NULL")], ["Draft Card"])
        self.sound()

    def test_pages_read_by_an_earlier_version_survive_the_proof_that_their_file_is_a_known_document(self):
        one = self.write("a/scan.txt")
        self.build(paths=[one])
        self.unmeasured()                                   # read by an earlier version: one page, recognised
        self.write("c/scan.txt")                            # a true copy, known by the MD5 of its storage
        self.build(names_only=True)
        self.measure("c/scan.txt", local=False)
        with self.db:                                       # a source answered for the text of the first meanwhile
            self.db.execute("UPDATE docs SET state='covered' WHERE id=?", (self.docs()["a/scan.txt"],))
        self.read.clear()
        self.assertEqual(self.build(), (0, 0))              # read in full, the first is that document: its pages stay
        self.assertEqual((self.read, self.documents(), self.state("c/scan.txt")), ([], 1, "indexed"))
        self.assertEqual(self.shown("northern harbours"), ["a/scan.txt", "c/scan.txt"])
        self.sound()

    def facts(self, statements_first):
        """One file whose storage tells its MD5 and which is then read; another of which two
        sources give, the one that MD5, the other its own SHA-256: the same facts in two orders."""
        one, other = self.write("a/book.txt"), self.write("g/book.txt", OTHER)
        self.build(names_only=True)
        self.measure("a/book.txt", local=False)
        say = lambda: (self.assertTrue(self.say("g/book.txt", "W1", [self.sums(one)[0]])),
                       self.assertTrue(self.say("g/book.txt", "W1", [self.sums(other)[1]], source="catalogue")))
        if statements_first:
            say()
            self.assertEqual(self.documents(), 1)           # nothing contradicts yet: the two are one document
            self.measure("a/book.txt")
        else:
            self.measure("a/book.txt")
            say()
        docs = self.docs()
        self.assertNotEqual(docs["a/book.txt"], docs["g/book.txt"])     # together, the two statements are refuted
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 2)
        self.sound()

    def test_two_statements_refuted_only_together_part_a_file_whatever_the_order_of_the_facts(self):
        self.facts(True)

    def test_the_same_facts_in_the_other_order_give_the_same_documents(self):
        self.facts(False)

    def test_a_statement_becomes_usable_again_when_the_document_that_contradicted_it_is_forgotten(self):
        annex = self.base / "Annex"
        annex.mkdir()
        self.run_cli("roots", "add", str(annex))
        one = self.write("a/book.txt")
        self.write("g/book.txt", OTHER)
        (annex / "third.txt").write_text(THIRD)
        self.build(names_only=True)
        self.measure("a/book.txt", local=False)
        third = self.db.execute("SELECT f.id FROM files f JOIN roots r ON r.id=f.root WHERE r.name='Annex'").fetchone()[0]
        with self.db:
            indexer.joined(self.db, third, self.sums(annex / "third.txt"))
        self.assertTrue(self.say("g/book.txt", "W1", [self.sums(one)[0]]))
        self.assertTrue(self.say("g/book.txt", "W1", [self.sums(annex / "third.txt")[1]], source="catalogue"))
        self.assertEqual(self.documents(), 3)               # what is said of it names two documents: it joins neither
        self.run_cli("roots", "remove", "Annex")
        docs = self.docs()
        self.assertEqual(docs["a/book.txt"], docs["g/book.txt"])        # one document is left that it can be
        self.sound()

    def test_the_same_holds_when_that_document_is_forgotten_after_its_month(self):
        one = self.write("a/book.txt")
        self.write("g/book.txt", OTHER)
        third = self.write("t/third.txt", THIRD)
        self.build(names_only=True)
        self.measure("a/book.txt", local=False)
        self.measure("t/third.txt")
        self.assertTrue(self.say("t/third.txt", "W3", self.sums(third), title="Third Work"))
        self.assertTrue(self.say("g/book.txt", "W1", [self.sums(one)[0]]))
        self.assertTrue(self.say("g/book.txt", "W1", [self.sums(third)[1]], source="catalogue"))
        third.unlink()
        self.build(names_only=True)                         # kept for a month, with what was said of its bytes
        self.assertEqual(self.documents(), 3)
        import time
        with self.db:
            store.tend_orphans(self.db, now=int(time.time()) + store.KEEP + 60)
        docs = self.docs()
        self.assertEqual((self.documents(), docs["a/book.txt"]), (1, docs["g/book.txt"]))
        self.sound()

    def test_what_was_said_of_a_file_that_is_gone_is_dropped_when_a_measure_refutes_it(self):
        one, two = self.write("a/book.txt"), self.write("b/book.txt", OTHER)
        self.build(names_only=True)
        self.measure("a/book.txt", local=False)
        self.assertTrue(self.say("a/book.txt", "W2", [self.sums(two)[1]], title="Eastern", text=True))  # the SHA-256 of the second
        one.unlink()
        self.build(names_only=True)
        self.assertEqual(self.db.execute("SELECT count(*) FROM records WHERE file IS NULL").fetchone()[0], 1)
        self.measure("b/book.txt")                          # the second is measured: that SHA-256 goes with another MD5
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 0)
        self.write("back/book.txt", when=1_800_000_000)     # the first comes back under another name
        self.build(names_only=True)
        self.measure("back/book.txt", local=False)
        self.assertEqual((names(self.db, "eastern"), self.state("back/book.txt")), ([], "pending"))
        self.assertEqual(self.build(), (2, 0))
        self.assertEqual(names(self.db, "northern harbours"), ["back/book.txt"])
        self.sound()

    def test_a_source_that_gives_its_checksums_in_another_order_moves_no_file(self):
        for folder in "abc":
            self.write(f"{folder}/book.txt")
        self.build(names_only=True)
        md5, sha = "md5:" + "aa" * 16, "sha256:" + "bb" * 32
        for folder in "abc":
            self.assertTrue(self.say(f"{folder}/book.txt", "W1", [sha, md5]))
        docs = self.docs()
        with patch("shelffind.store.place", side_effect=AssertionError("a file was placed again")):
            for folder in "abc":
                self.assertTrue(self.say(f"{folder}/book.txt", "W1", [md5, sha]))
        self.assertEqual((self.docs(), {row["keys"] for row in self.db.execute("SELECT keys FROM records")}),
                         (docs, {f"{md5} {sha}"}))
        self.sound()


class Proofs(Placing):
    """A complete run proves or refutes every statement, in a folder with a storage too."""

    def setUp(self):
        super().setUp()
        self.questions = self.twin()                        # a storage that holds what the folder holds

    def test_a_file_that_the_storage_cannot_be_asked_about_is_read_to_prove_what_is_said_of_it(self):
        cached = self.write("cached.txt")
        self.build(paths=[cached])
        odd = self.write("chapter\ntwo.txt", OTHER)         # a line break in its name: the storage is not asked
        self.build(names_only=True)
        self.assertTrue(self.say("chapter\ntwo.txt", "W1", self.sums(cached)))      # a source takes it for the first
        self.assertEqual(self.build(names_only=True), (0, 0))
        self.assertEqual(self.shown("northern harbours"), ["cached.txt", "chapter\ntwo.txt"])   # believed by an inventory,
        self.assertEqual(self.build(), (1, 0))              # refuted by a complete run, which reads the file
        self.assertEqual((self.shown("northern harbours"), self.shown("southern capes")), (["cached.txt"], ["chapter\ntwo.txt"]))
        self.assertTrue(odd.exists())
        self.sound()

    def test_a_statement_of_another_kind_than_the_storage_tells_is_proved_by_a_reading(self):
        self.write("covered.txt", OTHER)
        self.build(names_only=True)
        self.assertTrue(self.say("covered.txt", "W1", ["sha256:" + "ee" * 32], title="Another Work", text=True))
        self.assertEqual(self.build(names_only=True), (0, 0))
        self.assertEqual(names(self.db, "another work"), ["Reading Room/covered.txt"])
        self.assertEqual(self.build(limit=0), (0, 0))       # the MD5 of the storage proves nothing of a SHA-256
        self.assertEqual((names(self.db, "another work"), self.db.execute("SELECT count(*) FROM records").fetchone()[0]), ([], 0))
        self.assertIn("sha256:", self.db.execute("SELECT sum FROM files").fetchone()[0])
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(names(self.db, "southern capes"), ["Reading Room/covered.txt"])
        self.sound()

    def test_a_statement_that_the_storage_proves_costs_no_reading(self):
        path = self.write("book.txt")
        self.build(names_only=True)
        self.assertTrue(self.say("book.txt", "W1", self.sums(path), title="True Work"))
        with patch("shelffind.indexer.whole_checksums", side_effect=AssertionError("the file was read")):
            self.assertEqual(self.build(limit=0), (0, 0))
        self.assertEqual((names(self.db, "true work"), self.db.execute("SELECT sum FROM files").fetchone()[0]),
                         (["Reading Room/book.txt"], self.sums(path)[0]))
        self.sound()


class Coverage(Placing):
    def test_a_statement_without_checksum_names_a_work_and_never_answers_for_the_text_of_a_file(self):
        self.write("book.txt")

        def question(directory, name, op, **fields):
            return {"records": [{"n": file["n"], "id": "W1", "title": "Titled Only", "text": True} for file in fields["files"]]}

        usable = lambda directory, progress=None: ({"library": ["library"]}, {"library": True}, set())
        with patch("shelffind.link.usable", side_effect=usable), patch("shelffind.sources.question", side_effect=question), \
                patch("shelffind.sources.policy", return_value={"text": "trust", "identify": True}):
            self.assertEqual(self.build(names_only=True), (0, 0))
            self.assertEqual(self.state("book.txt"), "pending")
            self.assertEqual(self.build(), (1, 0))          # nothing can prove such a statement: the file is read
        self.assertEqual((names(self.db, "titled only"), names(self.db, "northern harbours")),
                         (["Reading Room/book.txt"], ["Reading Room/book.txt"]))
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
        other = self.write("other.txt", OTHER)              # the same size and the same date: only the file itself differs
        os.utime(other, ns=(path.stat().st_mtime_ns, path.stat().st_mtime_ns))
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

    def test_a_document_that_failed_is_not_tried_again_through_its_copies_in_the_same_run(self):
        for number in range(3):
            self.write(f"copies/{number}.txt")
        self.build(names_only=True)
        for number in range(3):
            self.measure(f"copies/{number}.txt")
        tried, copied, real = [], [], indexer.private_copy

        def failing(source, fmt, work):
            tried.append(source.name)
            raise RuntimeError("the reader stumbled")

        def copying(path, row, hold, known=None):
            copied.append(path.name)
            return real(path, row, hold, known)

        with patch("shelffind.extract.passages", side_effect=failing), patch("shelffind.indexer.private_copy", side_effect=copying):
            self.assertEqual((self.build(), len(tried), len(copied)), ((1, 1), 1, 1))
            # Once more when asked, and once only: its other files are not even copied.
            self.assertEqual((self.build(retry=True), len(tried), len(copied)), ((1, 1), 2, 2))
        self.assertEqual(self.build(retry=True), (1, 0))
        self.sound()

    def test_what_a_run_has_just_read_is_compared_at_once_with_the_files_of_its_size(self):
        self.write("a/book.txt")
        self.write("b/book.txt", OTHER)
        self.assertEqual(self.build(limit=1), (1, 0))       # one is read; the other is measured, for it could be the same
        self.assertEqual(self.db.execute("SELECT count(*) FROM files WHERE coalesce(sum,'')=''").fetchone()[0], 0)
        self.assertEqual((self.documents(), len(self.read)), (2, 1))
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

    def test_the_copies_of_a_document_that_was_just_read_are_not_even_opened(self):
        for number in range(4):
            self.write(f"copies/{number}.txt")
        self.build(names_only=True)
        for number in range(4):
            self.measure(f"copies/{number}.txt")
        self.assertEqual(self.documents(), 1)
        real, taken = indexer.private_copy, []

        def counting(path, row, hold, known=None):
            taken.append(path.name)
            return real(path, row, hold, known)

        with patch("shelffind.indexer.private_copy", side_effect=counting):
            self.assertEqual(self.build(), (1, 0))
        self.assertEqual(len(taken), 1)


class Behind(Placing):
    """A folder whose storage has received nothing yet: its files are measured only when they
    are copied to be read, so a file may turn out, at that moment, to be another document."""

    def setUp(self):
        super().setUp()
        behind = self.base / "behind"
        behind.mkdir()
        self.twin(behind)

    def test_a_failed_reading_is_charged_to_the_document_that_the_copy_shows_the_file_to_be(self):
        self.write("b/book.txt")                            # this one is taken up first, as a document of its own
        self.build(names_only=True)
        self.write("a/book.txt")
        self.build(names_only=True)
        self.measure("a/book.txt")                          # the other is known by its checksums, and waits
        self.assertEqual(self.documents(), 2)
        with patch("shelffind.extract.passages", side_effect=RuntimeError("the reader stumbled")):
            self.assertEqual(self.build(), (1, 1))          # copied, the first is that document, whose reading fails
        self.assertEqual((self.documents(), self.state("a/book.txt"), self.state("b/book.txt")), (1, "error", "error"))
        self.assertEqual(self.build(retry=True), (1, 0))
        self.sound()

    def test_a_file_measured_by_a_run_that_wrote_no_page_is_put_to_the_sources_in_that_run(self):
        self.write("a/book.txt")
        self.assertEqual(self.build(), (1, 0))              # the document is read through its first file
        self.write("b/copy.txt")                            # a copy arrives, of which the storage says nothing yet
        asked = []

        def question(directory, name, op, **fields):
            asked.extend((Path(file["path"]).name, len(file["keys"])) for file in fields["files"])
            return {"records": []}

        usable = lambda directory, progress=None: ({"library": ["library"]}, {"library": False}, set())
        with patch("shelffind.link.usable", side_effect=usable), patch("shelffind.sources.question", side_effect=question), \
                patch("shelffind.sources.policy", return_value={"text": "ignore", "identify": True}):
            self.read.clear()
            self.assertEqual((self.build(), self.read), ((0, 0), []))   # copied, it is the document: nothing to read
            # The source was asked about it twice in that one run: by its address, then by its checksums.
            self.assertEqual([entry for entry in asked if entry[0] == "copy.txt"], [("copy.txt", 0), ("copy.txt", 2)])
            del asked[:]
            self.assertEqual((self.build(), asked), ((0, 0), []))
        self.assertEqual(self.documents(), 1)
        self.sound()

    def test_a_run_allowed_one_reading_stops_though_the_files_of_its_document_turn_out_to_differ(self):
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
        self.assertEqual((self.build(), len(self.read), self.documents()), ((1, 0), 3, 3))
        self.sound()


class Storage2(Placing):
    def test_what_a_storage_says_of_pages_tied_to_nothing_is_not_taken(self):
        questions = self.twin()
        path = self.write("book.txt")
        self.write("other.txt", OTHER)                      # of the same size: each would be asked about
        self.build()
        self.unmeasured()                                   # read by an earlier version
        before = len(questions())
        self.assertEqual(self.build(names_only=True), (0, 0))
        self.assertEqual(len(questions()), before)          # the storage is not even asked
        with self.db:
            self.assertFalse(indexer.joined(self.db, self.ids()["book.txt"], self.sums(path)[:1], local=False))
        self.assertEqual([row["sum"] for row in self.db.execute("SELECT sum FROM files")], [None, None])
        self.read.clear()
        self.assertEqual(self.build(), (0, 0))              # a reading in full ties the pages to the bytes
        origin = self.db.execute("SELECT d.origin FROM docs d JOIN files f ON f.doc=d.id WHERE f.rel='book.txt'").fetchone()[0]
        self.assertEqual((origin.split(), self.read), (self.sums(path), []))
        self.sound()

    def test_a_complete_run_proves_against_the_storage_what_a_source_said(self):
        questions = self.twin()
        path = self.write("book.txt")
        self.build(names_only=True)
        wrong = ["md5:" + hashlib.md5(OTHER.encode()).hexdigest()]
        self.assertTrue(self.say("book.txt", "W1", wrong, title="Wrong Work"))
        self.assertEqual(self.build(names_only=True), (0, 0))       # an inventory believes it,
        self.assertEqual((names(self.db, "wrong work"), [call for call in questions() if call[0] == "hashsum"]),
                         (["Reading Room/book.txt"], []))
        self.assertEqual(self.build(limit=0), (0, 0))       # and a complete run asks the storage, which refutes it
        self.assertEqual(names(self.db, "wrong work"), [])
        self.assertEqual(self.db.execute("SELECT sum FROM files").fetchone()[0], self.sums(path)[0])
        self.sound()

    def test_a_complete_run_proves_by_one_reading_what_a_source_said_where_no_storage_does(self):
        path = self.write("book.txt")
        self.build(names_only=True)
        wrong = ["md5:" + hashlib.md5(OTHER.encode()).hexdigest()]
        self.assertTrue(self.say("book.txt", "W1", wrong, title="Wrong Work"))
        self.assertEqual(self.build(names_only=True), (0, 0))       # an inventory believes it
        self.assertEqual(names(self.db, "wrong work"), ["Reading Room/book.txt"])
        self.assertEqual(self.build(limit=0), (0, 0))       # a complete run reads the file once, which refutes it
        self.assertEqual(names(self.db, "wrong work"), [])
        self.assertEqual(self.db.execute("SELECT sum FROM files").fetchone()[0].split(), self.sums(path))
        with patch("shelffind.indexer.whole_checksums", side_effect=AssertionError("read a second time")):
            self.assertEqual(self.build(limit=0), (0, 0))
        self.sound()

    def test_in_a_folder_with_a_storage_no_file_is_read_in_full_to_be_compared(self):
        behind = self.base / "behind"
        behind.mkdir()
        self.twin(behind)                                   # a storage that has received nothing yet
        self.write("a/book.txt")
        self.write("b/book.txt", OTHER)
        self.build(names_only=True)
        self.assertTrue(self.say("a/book.txt", "W1", ["md5:" + "ee" * 16]))     # of one of the two a source gives a checksum
        real, read = indexer.whole_checksums, []

        def reading(path, row, copy=None):
            read.append(path.parent.name)
            return real(path, row, copy)

        with patch("shelffind.indexer.whole_checksums", side_effect=reading):
            self.assertEqual(self.build(limit=0), (0, 0))
        # The statement has to be proved, and the storage does not hold the file: it is read.
        # The other file is merely of the size of a known document, and waits for the storage.
        self.assertEqual(read, ["a"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 0)      # and the statement was false
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


class Changed(Placing):
    """A file found changed keeps nothing of what was known of the file that stood there."""

    def known(self):
        one = self.write("a/book.txt")
        self.write("b/book.txt", OTHER)
        self.build(names_only=True)
        self.measure("b/book.txt")                          # a document of its size is known by a checksum
        self.assertTrue(self.say("a/book.txt", "W1", ["sha256:" + "ee" * 32], title="Said Before"))
        return one

    def kept(self):
        """What is still said of a file, and what the title said before finds."""
        return self.db.execute("SELECT count(*) FROM records WHERE file IS NOT NULL").fetchone()[0], names(self.db, "said before")

    def test_while_it_is_compared(self):
        one = self.known()
        real = indexer.whole_checksums

        def changing(path, row, copy=None):
            if path == one:
                raise ValueError(indexer.CHANGED)
            return real(path, row, copy)

        with patch("shelffind.indexer.whole_checksums", side_effect=changing):
            self.assertEqual(self.build(limit=0), (0, 1))
        self.assertEqual(self.kept(), (0, []))
        self.assertEqual(tuple(self.db.execute("SELECT sum,asked FROM files WHERE rel='a/book.txt'").fetchone()), (None, ""))
        self.sound()

    def test_between_the_inventory_and_the_answer_of_the_storage(self):
        one = self.known()
        self.twin()
        real = indexer.check_stamp

        def changing(path, row, same=None):
            if path == one:
                raise ValueError(indexer.CHANGED)
            return real(path, row, same)

        with patch("shelffind.indexer.check_stamp", side_effect=changing):
            self.assertEqual(self.build(names_only=True), (0, 0))
        self.assertEqual(self.kept(), (0, []))
        self.assertEqual(tuple(self.db.execute("SELECT sum,asked FROM files WHERE rel='a/book.txt'").fetchone()), (None, ""))
        self.sound()


class Folders(Placing):
    def test_what_a_reading_teaches_in_one_folder_sends_back_to_a_folder_gone_through_before(self):
        annex = self.base / "Annex"
        annex.mkdir()
        self.run_cli("roots", "add", str(annex))
        (annex / "read.txt").write_text(LARGE)
        self.build()
        self.unmeasured()                                   # read by an earlier version, in the second folder
        self.write("copy.txt")                              # the same bytes in the first folder, never measured
        self.read.clear()
        # Nothing is known by a checksum when the first folder is gone through. The reading in
        # full of the file of the second folder then gives its document one, and the file of
        # the first folder, of the same size, is compared in the same run.
        self.assertEqual(self.build(limit=0), (0, 0))
        self.assertEqual((self.documents(), self.db.execute("SELECT count(*) FROM files WHERE coalesce(sum,'')=''").fetchone()[0]), (1, 0))
        self.assertEqual((self.read, len(search(self.db, "northern harbours")[0]["copies"])), ([], 1))
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

    def test_a_reference_does_not_name_a_document_measured_to_be_other_bytes_than_the_result(self):
        old = self.write("old.txt")
        self.build(names_only=True)
        self.measure("old.txt")
        self.assertTrue(self.say("old.txt", "W1", self.sums(old)))      # true when it was said
        revised = ["md5:" + "ab" * 16, "sha256:" + "cd" * 32]           # the work has changed at the source since
        self.assertIsNone(fuse.document(self.db, "library", {"id": "W1", "keys": revised, "format": "txt"}))
        self.assertEqual(fuse.document(self.db, "library", {"id": "W1", "keys": [], "format": "txt"}), self.docs()["old.txt"])

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


class Practical(Placing):
    def test_a_folder_that_is_forgotten_leaves_the_documents_of_the_others_in_order(self):
        annex = self.base / "Annex"
        annex.mkdir()
        self.run_cli("roots", "add", str(annex))
        one = self.write("said.txt")
        self.write("measured.txt", OTHER)
        (annex / "said.txt").write_text(LARGE)
        (annex / "measured.txt").write_text(OTHER)
        self.build(names_only=True)
        ids = {(row["name"], row["rel"]): row["id"] for row in self.db.execute(
            "SELECT r.name,f.rel,f.id FROM files f JOIN roots r ON r.id=f.root")}
        key = self.sums(one)
        with self.db:                                       # two files held together by a word, two by a measure
            for room in ("Reading Room", "Annex"):
                link.say(self.db, ids[(room, "said.txt")], "library", {"id": "W1", "keys": key, "title": "Said Work"}, set())
                indexer.joined(self.db, ids[(room, "measured.txt")], self.sums(self.root / "measured.txt"))
                link.say(self.db, ids[(room, "measured.txt")], "library", {"id": "W2", "title": f"Card of {room}"}, set())
        self.assertEqual(self.documents(), 2)
        self.sound()
        self.run_cli("roots", "remove", "Annex")
        self.sound()
        self.assertEqual(self.documents(), 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM records WHERE file IS NULL").fetchone()[0], 1)   # with the measured bytes
        self.assertEqual(names(self.db, "said work"), ["Reading Room/said.txt"])

    def test_a_folder_that_is_forgotten_takes_with_it_what_was_said_of_its_files_alone(self):
        annex = self.base / "Annex"
        annex.mkdir()
        self.run_cli("roots", "add", str(annex))
        one = self.write("book.txt")
        (annex / "copy.txt").write_text(LARGE)
        self.build(names_only=True)
        self.measure("book.txt")
        copy = self.db.execute("SELECT f.id FROM files f JOIN roots r ON r.id=f.root WHERE r.name='Annex'").fetchone()[0]
        with self.db:                                       # the copy is said to be the book, and bears a card that names no bytes
            self.assertTrue(link.say(self.db, copy, "library", {"id": "W1", "keys": self.sums(one), "title": "Known Work"}, set()))
            self.assertTrue(link.say(self.db, copy, "catalogue", {"id": "C1", "title": "Loose Card"}, set()))
        self.assertEqual(self.documents(), 1)
        self.run_cli("roots", "remove", "Annex")
        self.assertEqual([row["source"] for row in self.db.execute("SELECT source FROM records")], ["library"])
        self.assertEqual((names(self.db, "known work"), names(self.db, "loose card")), (["Reading Room/book.txt"], []))
        self.sound()

    def test_a_djvu_document_kept_in_several_files_is_refused_with_its_remedy(self):
        from shelffind import extract
        bundled = self.base / "bundled.djvu"
        bundled.write_bytes(b"AT&TFORM\x00\x00\x10\x00DJVMDIRM\x00\x00\x00\x20\x81" + b"\x00" * 40)
        indirect = self.base / "indirect.djvu"
        indirect.write_bytes(b"AT&TFORM\x00\x00\x10\x00DJVMDIRM\x00\x00\x00\x20\x01" + b"\x00" * 40)
        self.assertEqual((extract.in_several_files(bundled), extract.in_several_files(indirect)), (False, True))
        with self.assertRaises(ValueError) as refusal:
            with extract.document(indirect, "djvu"):
                pass
        self.assertIn("djvmcvt", str(refusal.exception))


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

    def test_an_old_index_with_a_column_of_its_own_is_not_converted(self):
        self.change("ALTER TABLE pages ADD COLUMN annotation TEXT; UPDATE pages SET annotation='a note in the margin';")
        with self.assertRaises(ValueError) as refusal:
            migrate.upgrade(self.data)
        self.assertIn("columns of pages", str(refusal.exception))
        self.assertFalse((self.data / store.INDEX).exists())
        db = sqlite3.connect(self.data / store.EARLIER)
        self.assertEqual(db.execute("SELECT count(*) FROM pages WHERE annotation IS NOT NULL").fetchone()[0] > 0, True)
        db.close()

    def test_a_table_named_like_one_that_sqlite_keeps_is_a_table_of_its_own_all_the_same(self):
        self.change("CREATE TABLE passages_content (note TEXT); INSERT INTO passages_content VALUES ('the only copy');")
        with self.assertRaises(ValueError):
            migrate.upgrade(self.data)
        self.change("DROP TABLE passages_content;")
        self.assertIsNotNone(migrate.upgrade(self.data))
        self.change("CREATE TABLE passages_content (note TEXT); INSERT INTO passages_content VALUES ('written since');")
        with self.assertRaises(ValueError):
            migrate.discard_old(self.data)
        self.assertTrue((self.data / store.EARLIER).exists())

    def test_the_earlier_index_is_not_deleted_beside_a_new_index_that_this_version_cannot_use(self):
        self.assertIsNotNone(migrate.upgrade(self.data))
        new = sqlite3.connect(self.data / store.INDEX)      # as a trial of this version left it
        new.execute("PRAGMA user_version=2")
        new.close()
        with self.assertRaises(ValueError):
            store.connect(self.data)
        with self.assertRaises(ValueError):                 # not "nothing to convert"
            migrate.upgrade(self.data)
        with self.assertRaises(ValueError):
            migrate.discard_old(self.data)
        self.assertTrue((self.data / store.EARLIER).exists())
        (self.data / store.INDEX).unlink()                  # moved away, as the message says:
        for left in self.data.glob(store.INDEX + "-*"):
            left.unlink()
        self.assertIsNotNone(migrate.upgrade(self.data))    # the earlier index is converted again
        self.assertTrue(migrate.discard_old(self.data) > 0)

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
