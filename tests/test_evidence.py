# Copyright 2026 Eudaemonic Enterprises Inc.
"""A text is attached to a file only on evidence: the cases in which a likeness could mislead."""
import os
import shutil
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from shelffind import history, indexer, store
from shelffind.search import search

from test_identity import LARGE, OTHER, Reading


def names(db, query):
    return sorted(hit["path"].rsplit("/", 2)[-2] + "/" + hit["name"] for hit in search(db, query))


class Likeness(Reading):
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

    def test_a_known_name_is_looked_up_before_a_stamp_whose_file_is_gone(self):
        alpha, blue = self.write("alpha.txt"), self.write("blue.txt", OTHER)
        self.build()
        alpha.unlink()
        copy = self.root / "copies" / "blue.txt"
        copy.parent.mkdir()
        shutil.copy2(blue, copy)                            # the same bytes and date as blue.txt
        self.read.clear()
        self.build()
        self.assertEqual(names(self.db, "southern capes"), ["Reading Room/blue.txt", "copies/blue.txt"][:1])
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
        self.build()
        self.assertEqual(search(self.db, "northern harbours"), [])
        found = search(self.db, "southern capes")
        self.assertEqual((len(found), len(found[0]["copies"])), (1, 1))
        self.assertEqual(self.db.execute("SELECT count(*) FROM files WHERE maybe IS NOT NULL").fetchone()[0], 0)
        self.assertEqual(self.build(), (0, 0))

    def test_a_document_read_before_fingerprints_existed_is_never_guessed_from_its_size(self):
        old = self.write("old.txt")
        self.build()
        self.db.execute("DELETE FROM doc_keys WHERE key LIKE 'fp:%'")       # as an index converted from the earlier one
        self.db.commit()
        old.unlink()
        self.write("new.txt", OTHER, when=1_800_000_000)
        self.read.clear()
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(self.read, ["new.txt"])
        self.assertEqual(names(self.db, "southern capes"), ["Reading Room/new.txt"])
        self.assertEqual(search(self.db, "northern harbours"), [])

    def test_such_a_document_gets_its_fingerprint_at_the_next_reading_of_its_folder(self):
        path = self.write("old.txt")
        self.build()
        self.db.execute("DELETE FROM doc_keys WHERE key LIKE 'fp:%'")
        self.db.commit()
        self.build()
        self.assertEqual(self.db.execute("SELECT count(*) FROM doc_keys WHERE key LIKE 'fp:%'").fetchone()[0], 1)
        os.utime(path, (1_800_000_000, 1_800_000_000))      # so that a mere new date reads nothing again
        self.read.clear()
        self.assertEqual(self.build(), (0, 0))
        self.assertEqual(self.read, [])
        self.assertEqual(names(self.db, "northern harbours"), ["Reading Room/old.txt"])

    def test_a_file_that_changes_while_it_is_compared_proves_nothing(self):
        self.write("a/book.txt")
        self.build()
        other = self.write("b/other.txt", OTHER, when=1_800_000_000)
        real = indexer.fingerprint

        def racing(path, fmt):
            if path == other:               # rewritten, between the look at its date and its reading,
                other.write_text(LARGE)     # into the very content of a document already known
            return real(path, fmt)

        with patch("shelffind.indexer.fingerprint", side_effect=racing):
            self.assertEqual(self.build(), (0, 0))          # nothing is concluded from such a reading
        self.assertEqual(names(self.db, "northern harbours"), ["a/book.txt"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM doc_keys WHERE key LIKE 'fp:%'").fetchone()[0], 1)
        self.assertEqual(self.build(), (0, 0))              # the next inventory sees the file as it is now,
        found = search(self.db, "northern harbours")        # a second copy of the first document
        self.assertEqual((len(found), found[0]["copies"]), (1, [str(other)]))

    def test_the_same_bytes_under_two_formats_are_read_by_each_reader(self):
        self.write("notes.txt")
        self.write("notes.md")
        self.assertEqual(self.build(), (2, 0))
        self.assertEqual(sorted(self.read), ["notes.md", "notes.txt"])
        self.assertEqual(self.documents(), 2)
        path = self.root / "notes.txt"
        self.assertNotEqual(indexer.fingerprint(path, "txt"), indexer.fingerprint(path, "md"))
        self.assertEqual(indexer.fingerprint(path, "txt"), indexer.fingerprint(self.root / "notes.md", "txt"))

    def test_a_failed_reading_leaves_no_fingerprint_behind(self):
        self.write("first.txt")
        with patch("shelffind.extract.passages", side_effect=RuntimeError("the reader stumbled")):
            self.assertEqual(self.build(), (1, 1))
        self.assertEqual(self.db.execute("SELECT count(*) FROM doc_keys WHERE key LIKE 'fp:%'").fetchone()[0], 0)
        self.write("second.txt", when=1_800_000_000)        # a healthy copy under another date
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(names(self.db, "northern harbours"), ["Reading Room/second.txt"])
        self.assertEqual(self.build(retry=True), (1, 0))    # the first is now known by its content
        self.assertEqual(len(search(self.db, "northern harbours")[0]["copies"]), 1)


class Ambiguity(Reading):
    """A size and a second that several documents share prove nothing: content decides."""

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
        self.db.execute("DELETE FROM doc_keys WHERE key LIKE 'fp:%'")       # read before fingerprints existed
        self.db.commit()
        old.unlink()
        new = self.write("new.txt", OTHER)
        os.utime(new, ns=(1_700_000_000 * 10**9 + 77, 1_700_000_000 * 10**9 + 77))    # the same second
        self.read.clear()
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(self.read, ["new.txt"])
        self.assertEqual((search(self.db, "northern harbours"), names(self.db, "southern capes")),
                         ([], ["Reading Room/new.txt"]))
        # No fingerprint was lent to the pages of the file that left.
        self.assertEqual(self.db.execute("SELECT count(*) FROM doc_keys k JOIN docs d ON d.id=k.doc "
                                         "WHERE k.key LIKE 'fp:%' AND d.gone IS NOT NULL").fetchone()[0], 0)

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

    def test_two_checksums_of_one_kind_prove_two_documents(self):
        from shelffind import link
        first, second = self.write("a/book.txt"), self.write("b/book.txt", OTHER)       # one name, size and date
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(self.documents(), 1)               # the inventory took them for copies
        ids = dict(self.db.execute("SELECT rel,id FROM files"))
        with self.db:
            for rel, sha in (("a/book.txt", "aa"), ("b/book.txt", "bb")):
                link.attach(self.db, ids[rel], "library", {"id": rel, "keys": ["sha256:" + sha * 32], "text": False},
                            False, set())
        self.assertEqual(self.documents(), 2)
        shown = names(self.db, "northern harbours") + names(self.db, "southern capes")
        self.assertEqual(len(shown), 1)                     # only the file that was read shows a text: its own
        self.build()
        self.assertEqual((names(self.db, "northern harbours"), names(self.db, "southern capes")),
                         (["a/book.txt"], ["b/book.txt"]))
        self.assertEqual((first.read_text()[:4], second.read_text()[:4]), ("Tide", "Wind"))

    def test_a_true_copy_set_apart_by_a_checksum_joins_its_document_again(self):
        from shelffind import link
        self.write("a/book.txt")
        self.write("b/book.txt")
        self.build()
        ids = dict(self.db.execute("SELECT rel,id FROM files"))
        record = {"id": "W1", "keys": ["sha256:" + "aa" * 32], "title": "Tide Tables", "text": False}
        with self.db:
            for rel in ("a/book.txt", "b/book.txt"):
                link.attach(self.db, ids[rel], "library", record, False, set())
        self.read.clear()
        self.assertEqual(self.build(), (0, 0))              # compared by fingerprint, not read again
        self.assertEqual((self.read, self.documents()), ([], 1))
        found = search(self.db, "northern harbours")
        self.assertEqual((len(found), len(found[0]["copies"]), found[0]["title"]), (1, 1, "Tide Tables"))

    def test_a_page_recognised_in_either_copy_survives_their_merging(self):
        with self.db:
            one = store.new_document(self.db, "stamp:9:1:pdf", 9, "pdf")
            two = store.new_document(self.db, "stamp:9:2:pdf", 9, "pdf")
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

    @unittest.skipIf(os.geteuid() == 0, "The administrator reads every file")
    def test_a_fingerprint_that_cannot_be_taken_is_reported(self):
        path = self.write("old.txt")
        self.build()
        self.db.execute("DELETE FROM doc_keys WHERE key LIKE 'fp:%'")
        self.db.commit()
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        said = []
        done = indexer.build(self.db, self.data, store.roots(self.db), progress=said.append)
        self.assertEqual(done, (0, 1))
        self.assertTrue(any("Could not fingerprint old.txt" in line for line in said))


class Robustness(Reading):
    @unittest.skipIf(os.geteuid() == 0, "The administrator reads every file")
    def test_a_copy_that_cannot_be_read_does_not_hide_one_that_can(self):
        first, second = self.write("a/book.txt"), self.write("b/book.txt")
        hidden = min(first, second, key=lambda p: self.root.joinpath(p).as_posix())
        hidden.chmod(0)
        self.addCleanup(hidden.chmod, 0o600)
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(len(search(self.db, "northern harbours")), 1)

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


class Lost(Reading):
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

    def test_what_a_source_said_survives_a_merge_in_preference_to_its_silence(self):
        with self.db:
            one = store.new_document(self.db, "stamp:1:1:txt", 1, "txt")
            two = store.new_document(self.db, "stamp:1:2:txt", 1, "txt")
            other = store.new_document(self.db, "stamp:2:2:txt", 2, "txt")
            self.db.execute("INSERT INTO records(doc,source,ref,text) VALUES(?,'library','',0)", (one,))
            self.db.execute("INSERT INTO records(doc,source,ref,title,text) VALUES(?,'library','W1','Tides',1)", (two,))
            self.db.execute("UPDATE docs SET title='Tides' WHERE id=?", (two,))
            kept = store.merge(self.db, one, two)
            self.assertIsNone(store.merge(self.db, kept, other))            # another size: another document
        self.assertEqual(self.db.execute("SELECT ref FROM records WHERE doc=?", (kept,)).fetchall()[0][0], "W1")
        self.assertEqual(self.db.execute("SELECT title FROM docs WHERE id=?", (kept,)).fetchone()[0], "Tides")
        self.assertEqual(self.documents(), 2)


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
        self.assertEqual([p.name for p in self.data.iterdir() if p.name.startswith("history.jsonl.")], [])
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
