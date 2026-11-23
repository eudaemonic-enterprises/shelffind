# Copyright 2026 Eudaemonic Enterprises Inc.
"""A text is attached to a file only on the evidence of its content: the cases in which a
likeness of name, size or date could mislead, and those in which two kinds of evidence meet."""
import fcntl
import os
import shutil
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from shelffind import history, indexer, link, store
from shelffind.search import search

from test_identity import LARGE, OTHER, Reading


def names(db, query):
    return sorted(hit["path"].rsplit("/", 2)[-2] + "/" + hit["name"] for hit in search(db, query))


class Evidence(Reading):
    def unmeasured(self):
        """As in an index converted from the earlier one: the text is there, and nothing was
        ever measured of the files."""
        with self.db:
            self.db.execute("DELETE FROM doc_keys")
            self.db.execute("UPDATE files SET sum=NULL")

    def ids(self):
        return dict(self.db.execute("SELECT rel,id FROM files"))

    def docs(self):
        return dict(self.db.execute("SELECT rel,doc FROM files"))

    def measured(self):
        """The checksums by which documents are known from a measure."""
        return self.db.execute("SELECT count(*) FROM doc_keys WHERE basis='content'").fetchone()[0]


class Likeness(Evidence):
    def test_an_edit_that_keeps_the_size_and_the_second_is_read_again(self):
        first, second = self.write("a/book.txt"), self.write("b/book.txt")
        os.utime(first, ns=(100, 100))
        os.utime(second, ns=(100, 100))
        self.assertEqual(self.build(), (1, 0))
        second.write_text(OTHER)
        os.utime(second, ns=(900, 900))                     # the same second, the same size
        self.read.clear()
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(self.read, ["book.txt"])
        self.assertEqual(names(self.db, "northern harbours"), ["a/book.txt"])
        self.assertEqual(names(self.db, "southern capes"), ["b/book.txt"])
        self.assertEqual(self.build(), (0, 0))

    def test_a_copy_joins_the_document_whose_content_it_has_whatever_left_meanwhile(self):
        alpha, blue = self.write("alpha.txt"), self.write("blue.txt", OTHER)
        self.build()
        alpha.unlink()
        copy = self.root / "copies" / "blue.txt"
        copy.parent.mkdir()
        shutil.copy2(blue, copy)                            # the same bytes and date as blue.txt
        self.read.clear()
        self.assertEqual(self.build(), (0, 0))
        self.assertEqual(self.read, [])
        self.assertEqual(len(search(self.db, "southern capes")[0]["copies"]), 1)
        self.assertEqual(search(self.db, "northern harbours"), [])

    def test_a_file_of_the_size_of_a_lost_document_is_compared_in_every_folder(self):
        old = self.write("old.txt")
        annex = self.base / "Annex"
        annex.mkdir()
        self.run_cli("roots", "add", str(annex))
        self.build()
        old.unlink()
        for folder in (self.root, annex):                   # another text of the same size, in two folders
            (folder / "new.txt").write_text(OTHER)
            os.utime(folder / "new.txt", (1_800_000_000, 1_800_000_000))
        self.read.clear()
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(self.read, ["new.txt"])            # read once for the two
        self.assertEqual(search(self.db, "northern harbours"), [])
        found = search(self.db, "southern capes")
        self.assertEqual((len(found), len(found[0]["copies"])), (1, 1))
        self.assertEqual(self.build(), (0, 0))

    def test_a_document_read_before_checksums_were_kept_is_never_guessed_from_its_size(self):
        old = self.write("old.txt")
        self.build()
        self.unmeasured()
        old.unlink()
        self.write("new.txt", OTHER, when=1_800_000_000)
        self.read.clear()
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(self.read, ["new.txt"])
        self.assertEqual(names(self.db, "southern capes"), ["Reading Room/new.txt"])
        self.assertEqual(search(self.db, "northern harbours"), [])

    def test_such_a_document_gets_its_checksums_at_the_next_reading_of_its_folder(self):
        path = self.write("old.txt")
        self.build()
        self.unmeasured()
        self.assertEqual(self.build(), (0, 0))
        self.assertEqual(self.measured(), 2)                # its MD5 and its SHA-256
        self.assertEqual(self.db.execute("SELECT count(*) FROM files WHERE sum IS NULL").fetchone()[0], 0)
        os.utime(path, (1_800_000_000, 1_800_000_000))      # so that a mere new date reads nothing again
        self.read.clear()
        self.assertEqual(self.build(), (0, 0))
        self.assertEqual(self.read, [])
        self.assertEqual(names(self.db, "northern harbours"), ["Reading Room/old.txt"])

    def test_a_file_that_changes_while_it_is_compared_proves_nothing(self):
        self.write("a/book.txt")
        self.build()
        other = self.write("b/other.txt", OTHER, when=1_800_000_000)
        real, looks = indexer.check_stamp, []

        def racing(path, row, same=None):
            if path == other:
                looks.append(path)
                if len(looks) == 2:         # rewritten, between the look at its date and the end of its reading,
                    other.write_text(LARGE)     # into the very content of a document already known
            return real(path, row, same)

        with patch("shelffind.indexer.check_stamp", side_effect=racing):
            self.assertEqual(self.build(), (0, 1))          # nothing is concluded from such a reading, and it is said
        self.assertEqual(names(self.db, "northern harbours"), ["a/book.txt"])
        self.assertEqual(self.measured(), 2)
        self.assertEqual(self.build(), (0, 0))              # the next inventory sees the file as it is now,
        found = search(self.db, "northern harbours")        # a second copy of the first document
        self.assertEqual((len(found), found[0]["copies"]), (1, [str(other)]))

    def test_the_same_bytes_under_two_formats_are_read_by_each_reader(self):
        self.write("notes.txt")
        self.write("notes.md")
        self.assertEqual(self.build(), (2, 0))
        self.assertEqual(sorted(self.read), ["notes.md", "notes.txt"])
        self.assertEqual(self.documents(), 2)
        known = sorted(row["key"].rsplit("@", 1) for row in self.db.execute("SELECT key FROM doc_keys WHERE key LIKE 'md5:%'"))
        self.assertEqual((known[0][0], [fmt for _, fmt in known]), (known[1][0], ["md", "txt"]))     # one checksum, two readers

    def test_a_failed_reading_does_not_keep_a_copy_from_being_read(self):
        self.write("first.txt")
        with patch("shelffind.extract.passages", side_effect=RuntimeError("the reader stumbled")):
            self.assertEqual(self.build(), (1, 1))
        self.assertEqual(self.db.execute("SELECT state FROM docs").fetchone()[0], "error")
        self.write("second.txt", when=1_800_000_000)        # a copy under another date
        self.read.clear()
        self.assertEqual(self.build(), (1, 0))              # the two are one document, which waits no longer
        self.assertEqual(len(self.read), 1)
        found = search(self.db, "northern harbours")
        self.assertEqual((len(found), len(found[0]["copies"]), self.documents()), (1, 1, 1))

    def test_a_date_changed_again_and_again_on_the_same_bytes_stops_nothing(self):
        path = self.write("book.txt")
        self.build()
        self.read.clear()
        for moment in (200, 300, 400):                      # the same second, three times over
            os.utime(path, ns=(1_700_000_000 * 10**9 + moment, 1_700_000_000 * 10**9 + moment))
            self.assertEqual(self.build(), (0, 0))
        self.write("other.txt", OTHER)
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual((self.read, self.documents()), (["other.txt"], 2))
        self.assertEqual(names(self.db, "northern harbours"), ["Reading Room/book.txt"])


class Ambiguity(Evidence):
    """A name, a size and a second that several files share prove nothing: content decides."""

    def test_a_copy_of_an_edited_file_gets_the_edited_text(self):
        first, second = self.write("a/book.txt"), self.write("x/book.txt")
        for path in (first, second):
            os.utime(path, ns=(100, 100))
        self.build()
        second.write_text(OTHER)
        os.utime(second, ns=(900, 900))
        self.build()
        third = self.root / "z" / "book.txt"
        third.parent.mkdir()
        shutil.copy2(second, third)                         # the same name, size and date as the edited file
        self.read.clear()
        self.assertEqual(self.build(), (0, 0))
        self.assertEqual(self.read, [])
        self.assertEqual(names(self.db, "northern harbours"), ["a/book.txt"])
        found = search(self.db, "southern capes")
        self.assertEqual((len(found), sorted([found[0]["path"], *found[0]["copies"]])), (1, sorted([str(second), str(third)])))

    def test_every_edit_within_one_second_is_read_for_itself(self):
        path = self.write("book.txt", "alpha " * 12000)
        seen = []
        for moment, word in ((100, "alpha"), (900, "bravo"), (400, "gamma"), (900, "delta")):
            path.write_text(f"{word} " * 12000)
            os.utime(path, ns=(moment, moment))
            self.build()
            seen.append([word for word in ("alpha", "bravo", "gamma", "delta") if search(self.db, word)])
        self.assertEqual(seen, [["alpha"], ["bravo"], ["gamma"], ["delta"]])

    def test_a_renamed_file_is_not_given_the_text_of_one_that_left_in_the_same_second(self):
        alpha, blue = self.write("alpha.txt"), self.write("blue.txt", OTHER)
        self.build()
        alpha.unlink()
        blue.rename(self.root / "green.txt")
        self.read.clear()
        self.assertEqual(self.build(), (0, 0))
        self.assertEqual(self.read, [])
        self.assertEqual(names(self.db, "southern capes"), ["Reading Room/green.txt"])
        self.assertEqual(search(self.db, "northern harbours"), [])

    def test_a_file_that_replaces_another_of_its_size_and_second_is_read(self):
        old = self.write("old.txt")
        self.build()
        self.unmeasured()                                    # read before fingerprints existed
        old.unlink()
        new = self.write("new.txt", OTHER)
        os.utime(new, ns=(1_700_000_000 * 10**9 + 77, 1_700_000_000 * 10**9 + 77))    # the same second
        self.read.clear()
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(self.read, ["new.txt"])
        self.assertEqual((search(self.db, "northern harbours"), names(self.db, "southern capes")),
                         ([], ["Reading Room/new.txt"]))
        # No checksum was lent to the pages of the file that left.
        self.assertEqual(self.db.execute("SELECT count(*) FROM doc_keys k JOIN docs d ON d.id=k.doc "
                                         "WHERE d.gone IS NOT NULL").fetchone()[0], 0)

    def test_a_file_moved_to_a_folder_listed_earlier_keeps_its_text(self):
        annex = self.base / "Zannex"
        annex.mkdir()
        self.run_cli("roots", "add", str(annex))
        (annex / "book.txt").write_text(LARGE)
        self.build()
        (annex / "book.txt").unlink()
        self.write("moved.txt", when=1_800_000_000)         # written anew in the folder that is listed first
        self.read.clear()
        self.assertEqual(self.build(), (0, 0))
        self.assertEqual((self.read, names(self.db, "northern harbours")), ([], ["Reading Room/moved.txt"]))

    @unittest.skipIf(os.geteuid() == 0, "The administrator reads every file")
    def test_a_file_that_could_not_be_compared_shares_nothing_with_its_namesake(self):
        self.write("old.txt")
        self.build()
        hidden = self.write("a/book.txt", when=1_800_000_000)       # the bytes of old.txt, out of reach for now
        hidden.chmod(0)
        self.addCleanup(hidden.chmod, 0o600)
        self.write("b/book.txt", OTHER, when=1_800_000_000)         # its namesake: same size, same date, another text
        self.read.clear()
        self.assertEqual(self.build(), (1, 1))              # one problem, said once
        self.assertEqual(self.read, ["book.txt"])
        self.assertEqual(names(self.db, "southern capes"), ["b/book.txt"])
        self.assertEqual(names(self.db, "northern harbours"), ["Reading Room/old.txt"])
        hidden.chmod(0o600)
        self.assertEqual(self.build(), (0, 0))              # within reach again: it is what old.txt is
        self.assertEqual(names(self.db, "southern capes"), ["b/book.txt"])
        found = search(self.db, "northern harbours")
        self.assertEqual({found[0]["path"], *found[0]["copies"]}, {str(self.root / "old.txt"), str(hidden)})

    def test_a_page_recognised_in_either_copy_survives_their_merging(self):
        with self.db:
            one, two = store.new_document(self.db, 9, "pdf"), store.new_document(self.db, 9, "pdf")
            for doc, pages in ((one, [(1, "", 1), (2, "second page", 0)]), (two, [(1, "first page", 0), (2, "", 1)])):
                self.db.executemany("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,?,NULL,?,?)",
                                    [(doc, page, store.deflate(body), blank) for page, body, blank in pages])
                self.db.execute("UPDATE docs SET state='needs-ocr' WHERE id=?", (doc,))
            kept = store.merge(self.db, one, two)
        rows = [(row[0], store.inflate(row[1]), row[2]) for row in self.db.execute(
            "SELECT page,ztext,needs_ocr FROM pages WHERE doc=? ORDER BY page", (kept,))]
        self.assertEqual(rows, [(1, "first page", 0), (2, "second page", 0)])
        self.assertEqual(self.db.execute("SELECT state FROM docs WHERE id=?", (kept,)).fetchone()[0], "indexed")
        self.assertEqual(self.db.execute("SELECT count(*) FROM passages WHERE passages MATCH 'first'").fetchone()[0], 1)

    def test_the_files_of_a_document_merged_into_another_are_its_copies(self):
        self.write("a/book.txt")
        self.write("b/book.txt")
        self.build(names_only=True)
        with self.db:                                       # as an earlier version left them: each read for itself
            for doc in self.docs().values():
                self.db.execute("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,1,NULL,?,0)",
                                (doc, store.deflate("Tide tables of the northern harbours.")))
            self.db.execute("UPDATE docs SET state='indexed'")
            self.db.execute("UPDATE files SET read=1")
        self.assertEqual(self.build(), (0, 0))              # their checksums show one document
        self.assertEqual(self.documents(), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM files WHERE read=1").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM pages").fetchone()[0], 1)

    def test_a_copy_that_waits_for_its_recognition_keeps_the_document_waiting(self):
        with self.db:
            failed, waiting = store.new_document(self.db, 9, "pdf"), store.new_document(self.db, 9, "pdf")
            for doc, state, pages in ((failed, "ocr-error", [(1, "first page", 0), (2, "", 1)]),
                                      (waiting, "needs-ocr", [(1, "", 1), (2, "", 1)])):
                self.db.executemany("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,?,NULL,?,?)",
                                    [(doc, page, store.deflate(body), blank) for page, body, blank in pages])
                self.db.execute("UPDATE docs SET state=?,error='the recogniser stumbled' WHERE id=?", (state, doc))
            kept = store.merge(self.db, failed, waiting)
        self.assertEqual(self.db.execute("SELECT state,error FROM docs WHERE id=?", (kept,)).fetchone()[:], ("needs-ocr", None))
        self.assertEqual(self.db.execute("SELECT count(*) FROM pages WHERE doc=? AND needs_ocr=0", (kept,)).fetchone()[0], 1)

    @unittest.skipIf(os.geteuid() == 0, "The administrator reads every file")
    def test_a_file_that_was_read_and_cannot_be_measured_is_reported(self):
        path = self.write("old.txt")
        self.build()
        self.unmeasured()
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        said = []
        done = indexer.build(self.db, self.data, store.roots(self.db), progress=said.append)
        self.assertEqual(done, (0, 1))
        self.assertTrue(any("Could not compare old.txt" in line for line in said))


class Words(Evidence):
    """A measure and the word of a source: what each may join, and which outranks the other."""

    def sha(self, path):
        row = {"size": path.stat().st_size, "mtime": path.stat().st_mtime_ns}
        return indexer.whole_checksums(path, row)[1]

    def say(self, rel, work, key=None, **more):
        """A source says that the file at `rel` is the work `work`, with the checksum `key`."""
        record = {"id": work, "keys": [key] if key else [], "title": f"Work {work}", **more}
        with self.db:
            return link.attach(self.db, self.ids()[rel], "library", record, set())

    def test_two_files_that_differ_by_one_word_are_two_documents_whatever_their_size(self):
        body = "Tide tables of the northern harbours. " * 300_000        # some eleven megabytes
        one = self.write("a/book.txt", body)
        two = self.write("b/book.txt", body[:6_000_000] + " BRAVO " + body[6_000_007:])
        self.assertEqual(one.stat().st_size, two.stat().st_size)
        self.build(names_only=True)
        with self.db:
            for rel, path in (("a/book.txt", one), ("b/book.txt", two)):
                row = self.db.execute("SELECT size,mtime FROM files WHERE rel=?", (rel,)).fetchone()
                indexer.joined(self.db, self.ids()[rel], indexer.whole_checksums(path, row))
        self.assertEqual(self.documents(), 2)
        third = self.write("c/book.txt", two.read_text(), when=1_800_000_000)
        self.assertEqual(self.build(limit=0), (0, 0))       # the third is measured, and is the second
        docs = self.docs()
        self.assertEqual((docs["c/book.txt"], docs["a/book.txt"] != docs["b/book.txt"]), (docs["b/book.txt"], True))
        self.assertTrue(third.exists())

    def test_what_was_measured_of_a_file_outranks_what_a_source_says_of_it(self):
        one, two = self.write("a/book.txt"), self.write("b/book.txt", OTHER)
        self.assertEqual(self.build(), (2, 0))              # each was measured when it was read
        self.assertFalse(self.say("a/book.txt", "W2", self.sha(two)))
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 0)
        self.assertTrue(self.say("a/book.txt", "W1", self.sha(one)))
        self.assertEqual(names(self.db, "work w1"), ["a/book.txt"])

    def test_two_documents_that_were_both_read_are_not_joined_on_a_word(self):
        self.write("a/book.txt")
        self.write("b/book.txt", OTHER)
        self.build()
        self.unmeasured()                                   # read by an earlier version: nothing measured yet
        key = "sha256:" + "aa" * 32
        self.assertTrue(self.say("a/book.txt", "W1", key))
        self.assertTrue(self.say("b/book.txt", "W1", key))  # the source takes the two for one work
        self.assertEqual(self.documents(), 2)
        self.assertEqual((names(self.db, "northern harbours"), names(self.db, "southern capes")),
                         (["a/book.txt"], ["b/book.txt"]))
        self.assertEqual(self.build(), (0, 0))              # measured in their turn, they stay what they are,
        self.assertEqual(self.documents(), 2)               # and the word that was wrong of both is gone
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM doc_keys WHERE basis='source'").fetchone()[0], 0)

    def test_a_word_joins_a_file_that_was_never_read(self):
        one = self.write("a/book.txt")
        self.write("b/book.txt")                            # a true copy, of which nothing is measured
        self.build(names_only=True)
        key = self.sha(one)
        self.assertTrue(self.say("a/book.txt", "W1", key))
        self.assertTrue(self.say("b/book.txt", "W1", key))
        self.assertEqual(self.documents(), 1)
        self.read.clear()
        self.assertEqual(self.build(), (1, 0))              # read once for the two, and measured
        self.assertEqual((len(self.read), self.documents()), (1, 1))
        self.assertEqual(len(search(self.db, "northern harbours")[0]["copies"]), 1)

    def test_a_measure_joins_what_a_word_alone_could_not(self):
        one = self.write("a/book.txt")
        self.build()
        self.unmeasured()                                   # read, and known only by the word of a source
        self.assertTrue(self.say("a/book.txt", "W1", self.sha(one)))
        self.write("b/book.txt", when=1_800_000_000)        # a true copy arrives
        self.read.clear()
        self.assertEqual(self.build(), (0, 0))              # both are measured: one document, nothing read again
        self.assertEqual((self.read, self.documents()), ([], 1))
        found = search(self.db, "northern harbours")
        self.assertEqual((len(found[0]["copies"]), found[0]["title"]), (1, "Work W1"))
        self.assertEqual(self.db.execute("SELECT count(*) FROM doc_keys WHERE basis='source'").fetchone()[0], 0)

    @unittest.skipIf(os.geteuid() == 0, "The administrator reads every file")
    def test_a_word_about_a_file_that_was_read_does_not_vouch_for_its_pages(self):
        one = self.write("a/book.txt")
        other = self.base / "elsewhere.txt"
        other.write_text(OTHER)
        self.build()
        self.unmeasured()                                   # read by an earlier version, never measured
        self.assertTrue(self.say("a/book.txt", "W2", self.sha(other)))      # a source takes it for another work
        one.chmod(0)                                        # ... and it cannot be measured for now
        self.addCleanup(one.chmod, 0o600)
        self.write("b/other.txt", OTHER)                    # the other work itself arrives
        self.read.clear()
        self.assertEqual(self.build(), (1, 1))              # it is read for itself, whatever was said of the first
        self.assertEqual(self.read, ["other.txt"])
        self.assertEqual((names(self.db, "northern harbours"), names(self.db, "southern capes")),
                         (["a/book.txt"], ["b/other.txt"]))

    def test_a_wrong_word_about_a_file_that_was_read_costs_it_nothing(self):
        self.write("a/book.txt")
        other = self.write("b/other.txt", OTHER)
        self.build()
        self.unmeasured()
        self.assertTrue(self.say("a/book.txt", "W9", self.sha(other)))      # the checksum of another file
        self.assertEqual(names(self.db, "work w9"), ["a/book.txt"])
        self.read.clear()
        self.assertEqual(self.build(), (0, 0))
        self.assertEqual(self.read, [])                     # its text is kept, the wrong record is gone,
        self.assertEqual(names(self.db, "northern harbours"), ["a/book.txt"])
        self.assertEqual([row["ref"] for row in self.db.execute("SELECT ref FROM records")], [])
        self.assertEqual(search(self.db, "work w9"), [])    # and the sources will be asked about it again
        self.assertEqual(self.db.execute("SELECT asked FROM files WHERE rel='a/book.txt'").fetchone()[0], "")

    def test_what_is_said_of_one_file_follows_that_file_and_no_other(self):
        self.write("a/book.txt")
        self.write("b/book.txt", OTHER)
        self.build(names_only=True)
        key = "sha256:" + "aa" * 32
        self.assertTrue(self.say("a/book.txt", "W1", key, title="AlphaAtlas"))
        self.assertTrue(self.say("b/book.txt", "W1", key, title="AlphaAtlas"))
        self.assertEqual(self.documents(), 1)
        self.assertEqual(len(search(self.db, "alphaatlas")[0]["copies"]), 1)
        # The source then says of the second file alone that it is another work, without a
        # checksum: the word that had brought the file is replaced, and the file leaves.
        self.assertTrue(self.say("b/book.txt", "W2", None, title="BravoAtlas", byline="Someone Else"))
        self.assertEqual(self.documents(), 2)
        self.assertEqual((names(self.db, "alphaatlas"), names(self.db, "bravoatlas")), (["a/book.txt"], ["b/book.txt"]))
        self.assertEqual(self.build(), (2, 0))
        self.assertEqual((names(self.db, "northern harbours"), names(self.db, "southern capes")),
                         (["a/book.txt"], ["b/book.txt"]))
        self.assertEqual(names(self.db, "bravoatlas"), ["b/book.txt"])      # its title stays with it
        self.assertEqual(self.db.execute("SELECT count(*) FROM records WHERE ref='W1'").fetchone()[0], 0)

    def test_what_another_source_said_of_a_file_goes_with_it_when_it_leaves(self):
        self.write("a/book.txt")
        self.write("b/book.txt", OTHER)
        self.build(names_only=True)
        key = "sha256:" + "aa" * 32
        self.assertTrue(self.say("a/book.txt", "W1", key))
        self.assertTrue(self.say("b/book.txt", "W1", key))  # one source takes the second for the first,
        with self.db:                                       # another names it, without a checksum
            link.attach(self.db, self.ids()["b/book.txt"], "catalogue", {"id": "C7", "title": "BravoCard"}, set())
        self.assertEqual(self.documents(), 1)
        self.assertEqual(self.build(), (2, 0))              # measured, the two part, and each is read in this run
        self.assertEqual((names(self.db, "northern harbours"), names(self.db, "southern capes")),
                         (["a/book.txt"], ["b/book.txt"]))
        self.assertEqual(names(self.db, "bravocard"), ["b/book.txt"])
        self.assertEqual([row["ref"] for row in self.db.execute("SELECT ref FROM records")], ["C7"])

    def test_a_file_brought_by_a_word_leaves_when_the_word_is_withdrawn(self):
        one = self.write("a/book.txt")
        self.write("b/book.txt", OTHER)
        self.build(names_only=True)
        key = self.sha(one)
        self.assertTrue(self.say("a/book.txt", "W1", key))
        self.assertTrue(self.say("b/book.txt", "W1", key))  # the source takes the second for the first
        self.assertEqual(self.documents(), 1)
        with self.db:
            link.withdraw(self.db, self.ids()["b/book.txt"], "library")     # asked again, it says so no more
        self.assertEqual(self.documents(), 2)
        self.assertEqual(self.build(), (2, 0))
        self.assertEqual((names(self.db, "northern harbours"), names(self.db, "southern capes")),
                         (["a/book.txt"], ["b/book.txt"]))

    def test_a_checksum_given_on_a_word_goes_when_the_word_is_withdrawn(self):
        self.write("a/alpha.txt")
        bravo = self.base / "bravo.txt"
        bravo.write_text(OTHER)
        self.build(names_only=True)
        key = self.sha(bravo)
        self.assertTrue(self.say("a/alpha.txt", "W2", key))     # the source takes alpha for bravo
        with self.db:
            link.withdraw(self.db, self.ids()["a/alpha.txt"], "library")    # and then says so no more
        self.assertEqual(self.db.execute("SELECT count(*) FROM doc_keys").fetchone()[0], 0)
        self.write("b/bravo.txt", OTHER)                    # the true bravo arrives
        self.assertEqual(self.build(), (2, 0))
        self.assertEqual((names(self.db, "northern harbours"), names(self.db, "southern capes")),
                         (["a/alpha.txt"], ["b/bravo.txt"]))

    def test_a_reading_outranks_the_storage_which_never_overrides_it(self):
        one = self.write("a/book.txt")
        self.build(names_only=True)
        file = self.ids()["a/book.txt"]
        row = self.db.execute("SELECT size,mtime FROM files").fetchone()
        true, stale = indexer.whole_checksums(one, row), ["md5:" + "ee" * 16]
        with self.db:
            self.assertTrue(indexer.joined(self.db, file, stale, local=False))      # a twin that held other bytes
            self.assertTrue(indexer.joined(self.db, file, true))                    # then the file is read in full
            self.assertFalse(indexer.joined(self.db, file, stale, local=False))
        self.assertEqual(self.db.execute("SELECT sum FROM files").fetchone()[0], " ".join(true))
        self.assertEqual(sorted(row["key"] for row in self.db.execute("SELECT key FROM doc_keys")),
                         sorted(key + "@txt" for key in true))

    def test_a_file_goes_to_the_document_that_comes_to_be_known_by_its_measure(self):
        self.write("a/book.txt")
        self.build()
        self.unmeasured()                                   # read by an earlier version, never measured
        two = self.write("b/book.txt", OTHER)
        self.build(names_only=True)
        key = "sha256:" + "aa" * 32
        self.assertTrue(self.say("a/book.txt", "W1", key))
        self.assertTrue(self.say("b/book.txt", "W1", key))  # a source takes the second for the first
        self.assertEqual(self.documents(), 1)
        row = {"size": two.stat().st_size, "mtime": two.stat().st_mtime_ns}
        md5 = indexer.whole_checksums(two, row)[0]
        with self.db:                                       # its storage tells the MD5 of the second file,
            indexer.joined(self.db, self.ids()["b/book.txt"], [md5], local=False)
        self.assertEqual(self.documents(), 1)               # which contradicts nothing known, and is kept with the file
        self.write("c/other.txt", OTHER, when=1_800_000_000)
        self.build(names_only=True)
        self.assertTrue(self.say("c/other.txt", "W2", md5))     # a document comes to be known by that very MD5
        self.assertEqual(self.build(names_only=True), (0, 0))
        docs = self.docs()
        self.assertEqual((docs["b/book.txt"], docs["a/book.txt"] != docs["b/book.txt"]), (docs["c/other.txt"], True))
        self.assertEqual(names(self.db, "northern harbours"), ["a/book.txt"])

    def test_what_one_folder_teaches_settles_a_file_of_another_in_the_same_run(self):
        annex = self.base / "Annex"
        annex.mkdir()
        self.run_cli("roots", "add", str(annex))
        (annex / "book.txt").write_text(LARGE)
        self.build()
        self.unmeasured()                                   # read in the folder that is gone through last
        self.write("copy.txt", when=1_800_000_000)          # a copy in the folder that is gone through first
        self.read.clear()
        self.assertEqual(self.build(), (0, 0))
        self.assertEqual((self.read, self.documents()), ([], 1))
        self.assertEqual(len(search(self.db, "northern harbours")[0]["copies"]), 1)

    def test_a_result_of_a_source_stands_for_the_file_of_its_format(self):
        from shelffind import fuse
        one, two = self.write("notes.txt"), self.write("notes.md")      # the same bytes, two readers
        self.assertEqual(self.build(), (2, 0))
        row = {"size": one.stat().st_size, "mtime": one.stat().st_mtime_ns}
        key = indexer.whole_checksums(one, row)[1]
        docs = self.docs()
        for fmt, rel in (("md", "notes.md"), ("txt", "notes.txt")):
            self.assertEqual(fuse.document(self.db, "library", {"id": "X", "keys": [key], "format": fmt}), docs[rel])
        self.assertIn(fuse.document(self.db, "library", {"id": "X", "keys": [key]}), docs.values())
        self.assertTrue(two.exists())

    def test_a_word_does_not_join_two_documents_that_other_checksums_keep_apart(self):
        one = self.write("a/book.txt")
        self.write("b/book.txt", OTHER)
        self.assertEqual(self.build(paths=[one]), (1, 0))   # the first is read and measured
        self.assertTrue(self.say("b/book.txt", "W9", "sha256:" + "ee" * 32))
        row = {"size": one.stat().st_size, "mtime": one.stat().st_mtime_ns}
        record = {"id": "C1", "keys": [indexer.whole_checksums(one, row)[0]], "title": "By Its MD5"}
        with self.db:                                       # another source gives the second the MD5 of the first
            self.assertTrue(link.attach(self.db, self.ids()["b/book.txt"], "catalogue", record, set()))
        self.assertEqual(self.documents(), 2)               # their SHA-256 differ: they are two

    def test_a_file_leaves_when_the_file_its_document_was_read_from_is_measured_otherwise(self):
        self.write("a/book.txt")
        two = self.write("b/book.txt", OTHER)
        self.build(paths=[self.root / "a/book.txt"])
        self.unmeasured()                                   # the first was read by an earlier version
        key = "sha256:" + "aa" * 32
        self.assertTrue(self.say("a/book.txt", "W1", key))
        self.assertTrue(self.say("b/book.txt", "W1", key))  # a source takes the second for the first
        row = {"size": two.stat().st_size, "mtime": two.stat().st_mtime_ns}
        with self.db:                                       # the storage tells the MD5 of the second, kept with it
            indexer.joined(self.db, self.ids()["b/book.txt"], [indexer.whole_checksums(two, row)[0]], local=False)
        self.assertEqual(self.documents(), 1)
        self.assertEqual(self.build(), (1, 0))              # the first is measured: the second is not that document
        self.assertEqual((names(self.db, "northern harbours"), names(self.db, "southern capes")),
                         (["a/book.txt"], ["b/book.txt"]))

    def test_two_checksums_of_one_kind_in_one_record_are_no_checksum(self):
        self.write("a/book.txt")
        self.build(names_only=True)
        record = {"id": "W1", "keys": ["md5:" + "aa" * 16, "md5:" + "bb" * 16], "title": "Twice Told"}
        with self.db:
            self.assertTrue(link.attach(self.db, self.ids()["a/book.txt"], "library", record, set()))
        self.assertEqual(self.db.execute("SELECT count(*) FROM doc_keys").fetchone()[0], 0)
        self.assertEqual(names(self.db, "twice told"), ["a/book.txt"])     # the title is taken, the checksums are not

    def test_a_second_source_that_says_otherwise_is_not_believed_before_a_measure(self):
        one = self.write("a/book.txt")
        self.build(names_only=True)
        self.assertTrue(self.say("a/book.txt", "W1", self.sha(one)))
        other = {"id": "Z9", "keys": ["sha256:" + "bb" * 32], "title": "Another Work"}
        with self.db:
            self.assertFalse(link.attach(self.db, self.ids()["a/book.txt"], "catalogue", other, set()))
        self.assertEqual([row["source"] for row in self.db.execute("SELECT source FROM records")], ["library"])


class Reading2(Evidence):
    """What a reading must not take on trust."""

    def test_a_file_swapped_at_the_instant_it_is_read_is_noticed(self):
        path = self.write("book.txt")
        stamp = path.stat().st_mtime_ns
        original = indexer.extract.passages

        def swapping(source, fmt, work):
            other = path.with_name("other")                 # another file takes the name: same size, same date
            other.write_text(OTHER)
            os.utime(other, ns=(stamp, stamp))
            os.replace(other, path)
            return original(source, fmt, work)

        with patch("shelffind.extract.passages", side_effect=swapping):
            self.assertEqual(self.build(), (1, 1))
        self.assertEqual(self.db.execute("SELECT count(*) FROM pages").fetchone()[0], 0)
        # What was measured was the file that is gone: the one in its place starts afresh.
        self.assertEqual(tuple(self.db.execute("SELECT sum,asked,read FROM files").fetchone()), (None, "", 0))
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(names(self.db, "southern capes"), ["Reading Room/book.txt"])
        self.assertEqual(search(self.db, "northern harbours"), [])

    def test_a_document_is_never_completed_through_a_file_that_is_not_its_copy(self):
        one = self.write("one/book.txt")
        self.build()
        with self.db:                                       # as a scan leaves it: a page still to recognise
            self.db.execute("UPDATE pages SET needs_ocr=1")
            self.db.execute("UPDATE docs SET state='needs-ocr'")
        two = self.write("two/book.txt", OTHER)             # the same name, size and date, another text
        self.build(names_only=True)
        self.assertEqual(self.documents(), 2)
        self.unmeasured()                                   # read by an earlier version: nothing measured yet
        ids = self.ids()
        record = {"id": "W1", "keys": ["sha256:" + "aa" * 32], "text": False}
        with self.db:                                       # a source takes the two for one work
            for rel in ("one/book.txt", "two/book.txt"):
                link.attach(self.db, ids[rel], "library", record, set())
        self.assertEqual(self.documents(), 1)
        recognise = lambda source, fmt, work, page, lang: source.read_text()[:60]
        with patch("shelffind.extract.recognize", side_effect=recognise):
            done = indexer.build(self.db, self.data, store.roots(self.db), ocr=True, paths=[two], progress=lambda _: None)
        self.assertEqual(done, (0, 0))                      # the second file is not the document: nothing is written
        self.assertEqual(self.documents(), 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM pages WHERE needs_ocr=1").fetchone()[0], 1)
        with patch("shelffind.extract.recognize", side_effect=recognise):
            self.assertEqual(self.build(ocr=True), (1, 0))  # through the file it was read from
        self.assertEqual(names(self.db, "northern harbours"), ["one/book.txt"])
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(names(self.db, "southern capes"), ["two/book.txt"])
        self.assertEqual(one.read_text()[:4], "Tide")

    def test_no_page_is_recognised_through_a_file_that_nothing_proves_to_be_the_one_read(self):
        one = self.write("one/book.txt")
        self.build()
        with self.db:
            self.db.execute("UPDATE pages SET needs_ocr=1")
            self.db.execute("UPDATE docs SET state='needs-ocr'")
        self.unmeasured()                                    # read by an earlier version: no fingerprint yet
        two = self.write("two/book.txt", OTHER)
        self.build(names_only=True)
        ids = self.ids()
        row = {"size": two.stat().st_size, "mtime": two.stat().st_mtime_ns}
        key = indexer.whole_checksums(two, row)[1]          # the checksum that the second file really has
        with self.db:                                       # a source takes the two for one work
            for rel in ("one/book.txt", "two/book.txt"):
                link.attach(self.db, ids[rel], "library", {"id": "W1", "keys": [key]}, set())
        self.assertEqual(self.documents(), 1)
        one.unlink()                                        # the file that was read is gone, never measured
        recognise = lambda source, fmt, work, page, lang: source.read_text()[:60]
        with patch("shelffind.extract.recognize", side_effect=recognise):
            self.assertEqual(self.build(ocr=True), (1, 1))
        self.assertEqual(self.db.execute("SELECT count(*) FROM pages WHERE needs_ocr=1").fetchone()[0], 1)
        self.assertEqual(search(self.db, "southern capes"), [])

    def test_an_inventory_opens_no_file(self):
        self.write("old.txt")
        self.build()
        self.unmeasured()
        self.write("moved.txt", when=1_800_000_000)
        with patch("shelffind.indexer.whole_checksums", side_effect=AssertionError("a file was opened")):
            self.assertEqual(self.build(names_only=True), (0, 0))
        self.assertEqual(self.documents(), 2)
        self.read.clear()
        self.assertEqual(self.build(), (0, 0))              # a reading run compares the two, and reads nothing twice
        self.assertEqual(self.read, [])
        self.assertEqual(len(search(self.db, "northern harbours")[0]["copies"]), 1)

    def test_one_folder_out_of_reach_does_not_stop_the_reading_of_another(self):
        annex = self.base / "Annex"
        annex.mkdir()
        self.run_cli("roots", "add", str(annex))
        self.write("first.txt")
        (annex / "second.txt").write_text(OTHER)
        self.build(names_only=True)
        reach, state, said = store.available, {"reading": False}, []
        gone = lambda root: reach(root) and not (state["reading"] and root["name"] == "Reading Room")
        with patch("shelffind.indexer.store.available", side_effect=gone):      # it goes away once the reading begins
            done = indexer.build(self.db, self.data, store.roots(self.db), progress=said.append,
                                 on_document=lambda *told: state.update(reading=True))
        self.assertEqual(done, (1, 1))
        self.assertEqual(names(self.db, "southern capes"), ["Annex/second.txt"])
        self.assertEqual(sum("Disconnected" in line for line in said), 1)

    def test_the_same_bytes_under_two_formats_each_keep_their_checksum(self):
        for name in ("notes.txt", "notes-a.md", "notes-b.md"):
            self.write(name, when=1_700_000_000 + len(name))
        self.build(names_only=True)
        ids = self.ids()
        record = {"id": "W1", "keys": ["sha256:" + "cd" * 32], "title": "Notes", "text": True}
        with self.db:
            for rel in ids:
                link.attach(self.db, ids[rel], "library", record, set())
        docs = self.docs()
        self.assertEqual(docs["notes-a.md"], docs["notes-b.md"])           # the two of one format are one document
        self.assertNotEqual(docs["notes.txt"], docs["notes-a.md"])

    def test_a_reading_in_full_that_fails_is_said_and_tried_again(self):
        old = self.write("old.txt")
        self.build(names_only=True)
        with self.db:                                       # a source described the work by its checksum
            row = self.db.execute("SELECT size,mtime FROM files").fetchone()
            link.attach(self.db, self.ids()["old.txt"], "library",
                        {"id": "W1", "keys": [indexer.whole_checksums(old, row)[1]], "title": "Archived Work"}, set())
        target = self.root / "refiled.txt"
        shutil.copyfile(old, target)                        # the same bytes under a new name and date
        old.unlink()
        said = []
        with patch("shelffind.indexer.whole_checksums", side_effect=OSError("the disk hiccuped")):
            done = indexer.build(self.db, self.data, store.roots(self.db), limit=0, progress=said.append)
        self.assertEqual(done, (0, 1))
        self.assertTrue(any("Could not compare refiled.txt" in line for line in said))
        self.assertEqual(self.db.execute("SELECT count(*) FROM docs WHERE title='Archived Work' AND gone IS NOT NULL")
                         .fetchone()[0], 1)                 # the work it is waits, still without a file
        self.build()                                        # the next run reads it in full, and knows it
        found = search(self.db, "archived work")
        self.assertEqual([(hit["path"], hit["title"]) for hit in found], [(str(target), "Archived Work")])
        self.assertEqual(self.documents(), 1)


class Robustness(Evidence):
    @unittest.skipIf(os.geteuid() == 0, "The administrator reads every file")
    def test_a_file_that_cannot_be_read_does_not_hide_one_that_can(self):
        first, second = self.write("a/book.txt"), self.write("b/book.txt")
        first.chmod(0)
        self.addCleanup(first.chmod, 0o600)
        self.assertEqual(self.build(), (2, 1))
        self.assertEqual(names(self.db, "northern harbours"), ["b/book.txt"])

    def test_one_damaged_document_does_not_stop_the_others(self):
        self.write("damaged.txt")
        self.write("sound.txt", OTHER)
        original = indexer.extract.passages

        def reader(path, fmt, work):
            if path.name == "damaged.txt":
                raise zlib.error("invalid stored block lengths")
            return original(path, fmt, work)

        with patch("shelffind.extract.passages", side_effect=reader):
            self.assertEqual(self.build(), (2, 1))
        self.assertEqual(names(self.db, "southern capes"), ["Reading Room/sound.txt"])
        self.assertIn("invalid stored block lengths", self.db.execute("SELECT error FROM docs WHERE state='error'").fetchone()[0])

    def test_a_folder_replaced_while_a_command_waited_is_not_read_under_its_number(self):
        self.write("kept.txt")
        stale = store.roots(self.db)[0]
        self.run_cli("roots", "remove", "Reading Room")
        annex = self.base / "Annex"
        annex.mkdir()
        (annex / "new.txt").write_text("A new folder.")
        self.run_cli("roots", "add", str(annex))
        said = []
        self.assertEqual(indexer.build(self.db, self.data, [stale], progress=said.append), (0, 1))
        self.assertTrue(any("No longer a registered folder" in text for text in said))
        self.assertEqual(self.db.execute("SELECT count(*) FROM files").fetchone()[0], 0)
        self.assertNotEqual(store.roots(self.db)[0]["id"], stale["id"])     # a number is never used twice

    def test_the_folders_are_chosen_once_no_other_build_can_change_them(self):
        chosen = []

        def folders():
            chosen.append(True)
            return store.roots(self.db)

        with (self.data / "index.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)                # another build is under way
            with self.assertRaises(ValueError):
                indexer.build(self.db, self.data, folders, progress=lambda _: None)
        self.assertEqual(chosen, [])                        # the command that was refused changed nothing
        self.assertEqual(indexer.build(self.db, self.data, folders, progress=lambda _: None), (0, 0))
        self.assertEqual(chosen, [True])

    def test_folders_are_chosen_by_their_name_first(self):
        for side in ("east", "west"):
            (self.base / side / "books").mkdir(parents=True)
            self.run_cli("roots", "add", str(self.base / side / "books"))
        self.assertEqual(store.roots(self.db, "books")[0]["path"], str(self.base / "east" / "books"))
        self.assertEqual(store.roots(self.db, "books-2")[0]["path"], str(self.base / "west" / "books"))
        self.assertEqual(store.roots(self.db, "BOOKS-2")[0]["name"], "books-2")
        with self.assertRaises(ValueError):
            store.roots(self.db, "nowhere")

    def test_a_dollar_sign_in_an_address_is_only_a_dollar_sign(self):
        priced = self.base / "receipts$NOT_SET"
        priced.mkdir()
        (priced / "bill.txt").write_text("Paid in full.")
        self.assertEqual(self.run_cli("roots", "add", str(priced))[0], 0)
        self.build()
        self.assertEqual(search(self.db, "bill")[0]["path"], str(priced / "bill.txt"))


class Storage(Evidence):
    """What the storage of a folder says of its files, through a stand-in for rclone."""

    def rclone(self, listing, sums):
        script = ('case "$1" in\n'
                  f"lsjson) cat <<'EOF'\n{listing}\nEOF\n;;\n"
                  f"hashsum) cat <<'EOF'\n{sums}\nEOF\n;;\nesac\n")
        folder = self.fake_tool("rclone", script)
        return patch.dict(os.environ, {"PATH": f"{folder}{os.pathsep}{os.environ['PATH']}"})

    def test_an_instant_is_the_same_in_whatever_time_zone_it_is_written(self):
        row = {"rel": "a/book.txt", "size": 9, "mtime": 1_700_000_000 * 10**9}
        md5 = "0123456789abcdef0123456789abcdef"
        for written in ("2023-11-14T22:13:20Z", "2023-11-15T02:13:20+04:00", "2023-11-14T22:13:20.000000000Z"):
            with self.rclone(f'[{{"Path":"a/book.txt","Size":9,"ModTime":"{written}"}}]', f"{md5}  a/book.txt"):
                self.assertEqual(indexer.stored_checksums("twin:", [row]), {"a/book.txt": "md5:" + md5}, written)

    def test_a_twin_of_another_size_or_another_date_says_nothing_of_the_file(self):
        row = {"rel": "a/book.txt", "size": 9, "mtime": 1_700_000_000 * 10**9}
        md5 = "0123456789abcdef0123456789abcdef"
        for size, written in ((8, "2023-11-14T22:13:20Z"), (9, "2023-11-14T22:13:21Z"), (9, "2023-11-14T22:13:20")):
            with self.rclone(f'[{{"Path":"a/book.txt","Size":{size},"ModTime":"{written}"}}]', f"{md5}  a/book.txt"):
                self.assertEqual(indexer.stored_checksums("twin:", [row]), {}, (size, written))

    def test_a_file_that_changes_in_the_storage_while_it_is_asked_is_not_answered_for(self):
        row = {"rel": "a/book.txt", "size": 9, "mtime": 1_700_000_000 * 10**9}
        count = self.base / "listings"
        script = ('case "$1" in\n'
                  f'lsjson) echo x >> "{count}"\n'
                  f'if [ "$(wc -l < "{count}")" = 1 ]; then when=20; else when=21; fi\n'
                  'echo "[{\\"Path\\":\\"a/book.txt\\",\\"Size\\":9,\\"ModTime\\":\\"2023-11-14T22:13:${when}Z\\"}]"\n;;\n'
                  'hashsum) echo "0123456789abcdef0123456789abcdef  a/book.txt"\n;;\nesac\n')
        folder = self.fake_tool("rclone", script)
        with patch.dict(os.environ, {"PATH": f"{folder}{os.pathsep}{os.environ['PATH']}"}):
            self.assertEqual(indexer.stored_checksums("twin:", [row]), {})      # the checksum is of the later file
        self.assertEqual(len(count.read_text().split()), 2)

    def test_a_storage_that_gives_no_answer_is_not_taken_for_one_that_knows_nothing(self):
        row = {"rel": "a/book.txt", "size": 9, "mtime": 1_700_000_000 * 10**9}
        folder = self.fake_tool("rclone", "exit 3\n")
        with patch.dict(os.environ, {"PATH": f"{folder}{os.pathsep}{os.environ['PATH']}"}):
            self.assertIsNone(indexer.stored_checksums("twin:", [row]))
        with self.rclone("not an answer", ""):
            self.assertIsNone(indexer.stored_checksums("twin:", [row]))


class Twin(Evidence):
    """A folder whose storage tells the checksums of its files."""

    def refile(self, path, name):
        """File a document elsewhere as a filing tool does: a new copy with a new date, the original removed."""
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        os.utime(target, (1_800_000_000, 1_800_000_000))
        path.unlink()
        return target

    def unread(self):
        return patch("shelffind.indexer.whole_checksums", side_effect=AssertionError("a file was opened"))

    def test_a_file_read_here_is_known_again_after_a_move_by_an_inventory_alone(self):
        questions = self.twin()
        path = self.write("Unsorted/tides.txt")
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(self.db.execute("SELECT count(*) FROM doc_keys WHERE key LIKE 'md5:%' AND basis='content'")
                         .fetchone()[0], 1)                 # asked of the storage once the file was read
        self.refile(path, "Harbours/TideTables.txt")
        with self.unread():
            self.assertEqual(self.build(names_only=True), (0, 0))
        self.assertEqual(names(self.db, "northern harbours"), ["Harbours/TideTables.txt"])
        self.assertEqual(self.documents(), 1)
        asked = len(questions())
        self.assertEqual(self.build(names_only=True), (0, 0))
        self.assertEqual(len(questions()), asked)           # nothing is asked twice

    def test_a_storage_that_gives_no_answer_is_said_once_and_costs_nothing(self):
        self.twin()
        path = self.write("book.txt")
        self.build()
        self.refile(path, "Filed/book.txt")
        said = []
        with patch.dict(os.environ, {"SHELFFIND_TEST_TWIN_SILENT": "1"}):
            done = indexer.build(self.db, self.data, store.roots(self.db), names_only=True, progress=said.append)
        self.assertEqual(done, (0, 0))
        self.assertEqual(sum("gives no answer" in line for line in said), 1)
        self.assertEqual(search(self.db, "northern harbours"), [])          # not known yet, and nothing was guessed
        self.assertEqual(self.build(names_only=True), (0, 0))               # it answers again
        self.assertEqual(names(self.db, "northern harbours"), ["Filed/book.txt"])

    def test_what_was_measured_of_a_file_goes_when_the_file_changes(self):
        import hashlib
        self.twin()
        path = self.write("book.txt")
        self.build()
        measured = lambda: self.db.execute("SELECT sum,asked FROM files").fetchone()
        self.assertEqual(measured()["sum"], "md5:" + hashlib.md5(LARGE.encode()).hexdigest())
        with self.db:
            self.db.execute("UPDATE files SET asked='library'")
        path.write_text(OTHER)                              # other bytes at the same address
        os.utime(path, (1_800_000_000, 1_800_000_000))
        self.assertEqual(self.build(names_only=True), (0, 0))
        self.assertEqual(tuple(measured()), ("md5:" + hashlib.md5(OTHER.encode()).hexdigest(), ""))
        self.assertEqual(search(self.db, "northern harbours"), [])

    def test_what_the_storage_of_one_folder_tells_settles_a_file_of_another_in_the_same_inventory(self):
        self.twin()
        annex = self.base / "Annex"
        annex.mkdir()
        self.run_cli("roots", "add", str(annex))
        store.declare({"Reading Room": {"path": str(self.root), "remote": str(self.root)},
                       "Annex": {"path": str(annex), "remote": str(annex)}})
        (annex / "book.txt").write_text(LARGE)
        with patch.dict(os.environ, {"SHELFFIND_TEST_TWIN_SILENT": "1"}):
            self.assertEqual(self.build(), (1, 0))          # read in the annex while its storage gave no answer
        self.write("copy.txt", when=1_800_000_000)          # a copy in the folder that is gone through first
        with self.unread():
            self.assertEqual(self.build(names_only=True), (0, 0))
        self.assertEqual(self.documents(), 1)
        self.assertEqual(len(search(self.db, "northern harbours")[0]["copies"]), 1)

    def test_a_twin_that_lags_behind_the_folder_says_nothing_of_a_file(self):
        lagging = self.base / "twin"
        lagging.mkdir()
        self.twin(lagging)
        old = self.write("old.txt")
        shutil.copy2(old, lagging / "old.txt")
        self.build()
        self.write("new.txt", OTHER, when=1_800_000_000)
        (lagging / "new.txt").write_text(LARGE)             # under that name the twin still holds other bytes
        os.utime(lagging / "new.txt", (1_700_000_500, 1_700_000_500))       # of the same size, at another date
        self.assertEqual(self.build(names_only=True), (0, 0))
        self.assertEqual(names(self.db, "northern harbours"), ["Reading Room/old.txt"])
        self.assertEqual(self.documents(), 2)
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(names(self.db, "southern capes"), ["Reading Room/new.txt"])


class Lost(Evidence):
    def test_a_small_file_touched_or_renamed_keeps_its_text_without_a_second_reading(self):
        note = self.write("note.txt", "A short note on the tides.")
        self.build()
        self.read.clear()
        os.utime(note, (1_800_000_000, 1_800_000_000))
        self.assertEqual(self.build(), (0, 0))
        note.rename(self.root / "renamed.txt")
        self.assertEqual(self.build(), (0, 0))
        self.assertEqual(self.read, [])
        self.assertEqual(names(self.db, "short note"), ["Reading Room/renamed.txt"])
        self.assertEqual(self.documents(), 1)

    def test_a_document_without_file_keeps_its_text_for_a_month_and_no_longer(self):
        path = self.write("book.txt")
        self.build()
        path.unlink()
        self.build()
        self.assertEqual(self.db.execute("SELECT count(*) FROM pages").fetchone()[0], 1)
        with self.db:
            store.tend_orphans(self.db, now=int(time.time()) + store.KEEP - 60)
        self.assertEqual(self.documents(), 1)
        with self.db:
            store.tend_orphans(self.db, now=int(time.time()) + store.KEEP + 60)
        self.assertEqual((self.documents(), self.db.execute("SELECT count(*) FROM pages").fetchone()[0]), (0, 0))
        self.assertEqual(self.db.execute("SELECT count(*) FROM passages WHERE passages MATCH 'harbours'").fetchone()[0], 0)

    def test_forgetting_a_folder_forgets_what_it_lost_earlier(self):
        path = self.write("book.txt")
        self.write("unread.bin", "x")
        self.build()
        path.unlink()
        self.build()
        self.assertEqual(self.documents(), 2)               # the lost document waits with its text
        self.run_cli("roots", "remove", "Reading Room")
        self.assertEqual((self.documents(), self.db.execute("SELECT count(*) FROM pages").fetchone()[0]), (0, 0))

    def test_a_document_left_by_its_last_file_does_not_outlive_the_folder_of_that_file(self):
        annex = self.base / "Annex"
        annex.mkdir()
        self.run_cli("roots", "add", str(annex))
        (annex / "kept.txt").write_text("Kept in the annex.")
        path = self.write("book.txt")
        self.build()
        with self.db:                                       # its only file turns out to be another document
            store.set_apart(self.db, self.ids()["book.txt"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM docs WHERE gone IS NOT NULL AND gone_from IS NOT NULL")
                         .fetchone()[0], 1)
        self.run_cli("roots", "remove", "Reading Room")
        (annex / "again.txt").write_text(path.read_text())  # the same bytes come back in the folder that stays
        self.read.clear()
        self.build()
        self.assertEqual(self.read, ["again.txt"])          # read for itself: what was forgotten stays forgotten
        self.assertEqual(self.db.execute("SELECT count(*) FROM docs WHERE gone IS NOT NULL").fetchone()[0], 0)

    def test_what_was_said_of_two_documents_is_said_of_the_one_they_become(self):
        with self.db:
            one, two, other = (store.new_document(self.db, size, "txt") for size in (1, 1, 2))
            self.db.execute("INSERT INTO records(doc,source,ref,title,text) VALUES(?,'library','W0','',0)", (one,))
            self.db.execute("INSERT INTO records(doc,source,ref,title,text) VALUES(?,'library','W1','Tides',1)", (two,))
            self.db.execute("UPDATE docs SET title='Tides' WHERE id=?", (two,))
            kept = store.merge(self.db, one, two)
            self.assertIsNone(store.merge(self.db, kept, other))            # another size: another document
        self.assertEqual(sorted(row["ref"] for row in self.db.execute("SELECT ref FROM records WHERE doc=?", (kept,))),
                         ["W0", "W1"])                      # both are kept, and the one with a title names the document
        self.assertEqual(self.db.execute("SELECT title FROM docs WHERE id=?", (kept,)).fetchone()[0], "Tides")
        self.assertEqual(self.documents(), 2)

    def test_a_file_parted_from_a_titled_document_is_no_longer_found_by_that_title(self):
        self.write("a/book.txt")
        self.write("b/book.txt", OTHER)
        self.build(names_only=True)
        ids = self.ids()
        with self.db:                                       # a source takes both for one work, then corrects itself
            for rel in ("a/book.txt", "b/book.txt"):
                link.attach(self.db, ids[rel], "library",
                            {"id": "W1", "keys": ["sha256:" + "aa" * 32], "title": "AlmanacMeridian"}, set())
            self.assertEqual(len(search(self.db, "almanacmeridian")[0]["copies"]), 1)
            link.attach(self.db, ids["b/book.txt"], "library", {"id": "W2", "keys": ["sha256:" + "bb" * 32]}, set())
        self.assertEqual(names(self.db, "almanacmeridian"), ["a/book.txt"])
        self.assertEqual(search(self.db, "almanacmeridian")[0]["copies"], [])


class Journal(unittest.TestCase):
    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)

    def test_the_journal_is_never_seen_half_written(self):
        hit = {"path": "/shelf/a.pdf", "source": "files", "name": "a.pdf"}
        history.record(self.data, "open", "tides", hit, when=200)
        with patch("shelffind.history.os.replace", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            history.absorb(self.data, [(100, "winds")])
        self.assertEqual(history.queries(self.data), ["tides"])             # what was there is still there
        self.assertEqual([p.name for p in self.data.iterdir() if "arriving" in p.name], [])
        self.assertTrue(history.absorb(self.data, [(100, "winds")]))
        self.assertTrue(history.absorb(self.data, [(100, "winds")]))
        self.assertEqual(history.queries(self.data), ["tides", "winds"])
        with (self.data / history.NAME).open() as journal:
            self.assertEqual(len(journal.readlines()), 2)

    def test_a_damaged_line_hides_nothing_else_and_limits_are_kept(self):
        hit = {"path": "/shelf/a.pdf", "source": "files", "name": "a.pdf"}
        history.record(self.data, "open", "tides", hit)
        with (self.data / history.NAME).open("ab") as journal:
            journal.write(b'{"t": 1, "q": "cut \xe2\x82\n')
        history.record(self.data, "open", "winds", {**hit, "path": "/shelf/b.pdf"})
        self.assertEqual(history.queries(self.data), ["winds", "tides"])
        self.assertEqual(history.documents(self.data), ["/shelf/b.pdf", "/shelf/a.pdf"])
        self.assertEqual(history.documents(self.data, 1), ["/shelf/b.pdf"])
        self.assertEqual((history.events(self.data, 0), history.documents(self.data, 0)), ([], []))
        self.assertFalse(history.absorb(self.data / "history.jsonl" / "impossible", [(1, "x")]))

    def test_a_line_cut_short_costs_only_itself_and_a_neighbouring_file_is_left_alone(self):
        history.record(self.data, "search", "tides")
        with (self.data / history.NAME).open("ab") as journal:
            journal.write(b'{"t": 5, "q"')                                  # a write that failed half-way
        (self.data / "history.jsonl.bak").write_text("a copy someone keeps\n")
        history.record(self.data, "search", "winds")
        with (self.data / history.NAME).open("a") as journal:
            journal.write('{"t": [1], "q": "odd but readable"}\n')           # an event with a time that is no time
        self.assertTrue(history.absorb(self.data, [(1, "capes")]))
        self.assertEqual(sorted(history.queries(self.data)), ["capes", "odd but readable", "tides", "winds"])
        self.assertEqual((self.data / "history.jsonl.bak").read_text(), "a copy someone keeps\n")

    def test_the_oldest_half_goes_when_the_journal_is_full(self):
        with patch.object(history, "LIMIT", 600):
            for number in range(40):
                history.record(self.data, "search", f"query number {number}")
        found = history.queries(self.data)
        self.assertEqual(found[0], "query number 39")
        self.assertLess(len(found), 40)
        self.assertGreater(len(found), 4)


if __name__ == "__main__":
    unittest.main()
