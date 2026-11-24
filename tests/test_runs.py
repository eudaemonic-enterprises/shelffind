# Copyright 2026 Eudaemonic Enterprises Inc.
"""What a run of the index does around the reading itself: where the copies are made and what
is removed, what stops a run and what is counted, and what a run must not leave for the next."""
import errno
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shelffind import extract, indexer, link, store
from shelffind.search import search

from test_evidence import names
from test_identity import LARGE, OTHER
from test_placing import THIRD, Placing


class Copies(Placing):
    """The copies are made in a folder of ShelfFind's own, and nothing else is ever removed."""

    def test_a_folder_of_the_reader_named_like_a_copy_is_never_touched(self):
        mine = self.base / "shelffind-copy-manual-collection"       # in the folder of temporary files, and a day old
        mine.mkdir()
        (mine / "notes.txt").write_text(OTHER)
        os.utime(mine / "notes.txt", (1_600_000_000, 1_600_000_000))
        os.utime(mine, (1_600_000_000, 1_600_000_000))
        self.run_cli("roots", "add", str(mine))
        self.write("book.txt")
        with patch.dict(os.environ, {"TMPDIR": str(self.base)}), patch.object(tempfile, "tempdir", str(self.base)):
            self.assertEqual(self.build(), (2, 0))
            self.assertEqual(self.build(), (0, 0))
        self.assertEqual((mine / "notes.txt").read_text(), OTHER)
        self.assertEqual(len(search(self.db, "southern capes")), 1)

    def named(self, name):
        """A folder that the index names as one in which a run made its copies."""
        (self.data / name).mkdir()
        with self.db:
            self.db.execute("INSERT INTO meta(key,value) VALUES(?,?)", (f"copies:{name}", name))
        return self.data / name

    def test_the_copies_are_made_in_a_folder_of_the_run_and_what_an_earlier_run_left_is_removed(self):
        left = self.named("copies-leftover")                # as a run that was cut short left it
        (left / "copy-left-behind").mkdir()
        (left / "copy-left-behind" / "book.pdf").write_text("left by a run that was cut short")
        mine = self.data / "copies"                         # a folder of the reader's own, which the index does not name
        mine.mkdir()
        (mine / "notes.txt").write_text("mine")
        self.write("book.txt")
        real, held = indexer.private_copy, []

        def watching(path, row, hold, known=None):
            held.append((hold.parent.parent, hold.parent.name.startswith("copies-"), left.exists()))
            return real(path, row, hold, known)

        with patch("shelffind.indexer.private_copy", side_effect=watching):
            self.assertEqual(self.build(), (1, 0))
        self.assertEqual(held, [(self.data, True, False)])  # in the data folder, after the leftover was removed
        self.assertEqual(sorted(path.name for path in self.data.glob("copies*")), ["copies"])      # nothing is left of this run
        self.assertEqual((mine / "notes.txt").read_text(), "mine")
        self.assertEqual(self.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'copies:%'").fetchone()[0], 0)

    def test_an_inventory_makes_no_copy_and_removes_none(self):
        left = self.named("copies-at-work")
        self.write("book.txt")
        self.assertEqual(self.build(names_only=True), (0, 0))
        self.assertTrue(left.exists())

    def test_nothing_is_removed_that_is_not_a_folder_of_copies_in_the_data_folder(self):
        outside = self.base / "kept"
        outside.mkdir()
        (outside / "note.txt").write_text("mine")
        (self.data / "copies-link").symlink_to(outside)     # a link that leads out of the data folder
        papers = self.data / "papers"
        papers.mkdir()
        (papers / "note.txt").write_text("mine")
        with self.db:                                       # an index that names what no run made
            self.db.executemany("INSERT INTO meta(key,value) VALUES(?,?)",
                                [("copies:copies-link", "copies-link"), ("copies:../kept", "../kept"),
                                 ("copies:papers", "papers"), ("copies:copies-x", "papers")])
        self.write("book.txt")
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(((outside / "note.txt").read_text(), (papers / "note.txt").read_text()), ("mine", "mine"))
        self.assertTrue((self.data / "copies-link").is_symlink())

    def test_copies_that_cannot_be_removed_are_said_and_removed_by_a_later_run(self):
        self.write("book.txt")
        said = []
        with patch("shelffind.indexer.shutil.rmtree", side_effect=lambda *args, **kwargs: None):
            done = indexer.build(self.db, self.data, store.roots(self.db), progress=said.append)
        self.assertEqual((done, sum("could not be removed" in line for line in said)), ((1, 1), 1))
        self.assertEqual(self.db.execute("SELECT state FROM docs").fetchone()[0], "indexed")       # the document is read all the same
        self.assertEqual(len(list(self.data.glob("copies-*"))), 1)
        self.assertEqual(self.build(), (0, 0))
        self.assertEqual(list(self.data.glob("copies-*")), [])

    def test_a_copy_that_cannot_be_closed_stops_the_reading_and_no_document_is_said_to_have_failed(self):
        self.write("book.txt")
        real = open

        class Full:
            """A copy whose last bytes find no room when it is closed."""

            def __init__(self, target):
                self.target = target

            def write(self, block):
                return self.target.write(block)

            def close(self):
                self.target.close()
                raise OSError(errno.ENOSPC, "No space left on device")

        with patch("shelffind.indexer.open", create=True, side_effect=lambda path, mode, buffering: Full(real(path, mode, buffering=buffering))):
            self.assertEqual(self.build(), (0, 1))
        self.assertEqual(self.db.execute("SELECT state FROM docs").fetchone()[0], "pending")
        self.assertEqual(self.build(), (1, 0))              # with room again, an ordinary run reads it

    def test_a_folder_cannot_be_moved_through_the_settings_into_the_data_folder_or_into_another_folder(self):
        self.write("chapter.txt")
        self.build()
        inside = self.data / "copies-mine"
        inside.mkdir()
        (inside / "chapter.txt").write_text(OTHER)
        annex = self.base / "Annex"
        (annex / "shelf").mkdir(parents=True)
        self.run_cli("roots", "add", str(annex))
        self.addCleanup(store.declare, {})
        for target, reason in ((inside, "Choose a document directory"), (annex / "shelf", "Overlapping root")):
            store.declare({"Reading Room": {"path": str(target)}})
            said = []
            store.adopt_declared(self.db, self.data, said.append)
            self.assertTrue(any(reason in line for line in said), said)
            self.assertEqual(store.roots(self.db, "Reading Room")[0]["path"], str(self.root))
        store.declare({})
        self.assertEqual(self.build(), (0, 0))
        self.assertEqual((inside / "chapter.txt").read_text(), OTHER)
        self.assertEqual(names(self.db, "northern harbours"), ["Reading Room/chapter.txt"])

    def test_without_room_for_a_copy_the_reading_stops_and_no_document_is_said_to_have_failed(self):
        self.write("a/book.txt")
        self.write("b/other.txt", OTHER)
        real = tempfile.TemporaryDirectory

        def full(*args, **kwargs):
            if kwargs.get("prefix") == "copy-":
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(*args, **kwargs)

        with patch.object(tempfile, "TemporaryDirectory", side_effect=full):
            self.assertEqual(self.build(), (0, 1))
        self.assertEqual({row[0] for row in self.db.execute("SELECT state FROM docs")}, {"pending"})
        self.assertEqual(self.build(), (2, 0))              # with room again, an ordinary run reads them
        self.sound()

    def test_a_copy_that_cannot_be_written_is_not_a_fault_of_the_file(self):
        path = self.write("book.txt")
        row = {"size": path.stat().st_size, "mtime": path.stat().st_mtime_ns}
        with self.assertRaises(indexer.Unplaced):
            indexer.whole_checksums(path, row, self.base / "no such folder" / "copy.txt")

    def test_a_plain_text_too_large_to_be_read_is_refused_before_it_is_copied(self):
        self.write("book.txt")
        with patch.object(extract, "MAX_TEXT", 1000), \
                patch("shelffind.indexer.private_copy", side_effect=AssertionError("a copy was made")):
            self.assertEqual(self.build(), (1, 1))
        self.assertEqual(self.db.execute("SELECT state,error FROM docs").fetchone()[:], ("error", "Plain text exceeds 64 MiB"))


class Changing(Placing):
    def test_a_file_written_in_place_while_it_is_read_is_noticed_though_its_date_is_put_back(self):
        path = self.write("atlas.txt", "N" * (3 << 20))
        row = {"size": path.stat().st_size, "mtime": path.stat().st_mtime_ns}
        real = Path.open

        class Interleaved:
            """The file as its reader sees it, with a writer at work between two readings."""

            def __init__(self, source):
                self.source, self.reads = source, 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.source.close()

            def fileno(self):
                return self.source.fileno()

            def read(self, size):
                self.reads += 1
                if self.reads == 2:                         # the middle of the file is other bytes for a moment,
                    with real(path, "r+b") as writer:
                        writer.seek(1 << 20)
                        writer.write(b"S" * (1 << 20))
                block = self.source.read(size)
                if self.reads == 2:                         # then what it was, under the date it had
                    with real(path, "r+b") as writer:
                        writer.seek(1 << 20)
                        writer.write(b"N" * (1 << 20))
                    os.utime(path, ns=(row["mtime"], row["mtime"]))
                return block

        def opening(self_path, mode="r", *args, **kwargs):
            source = real(self_path, mode, *args, **kwargs)
            return Interleaved(source) if self_path == path and mode == "rb" else source

        copy = self.base / "copy.txt"
        with patch.object(Path, "open", opening), self.assertRaises(ValueError):
            indexer.whole_checksums(path, row, copy)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (b"N" * (3 << 20), row["mtime"]))
        self.assertIn(b"S", copy.read_bytes())              # what was copied never was the file


class Passes(Placing):
    def test_a_comparison_that_fails_after_the_reading_is_counted(self):
        self.write("a/book.txt")
        self.build(names_only=True)
        closed = self.write("b/other.txt", OTHER)           # of the size of the first, and not to be opened
        closed.chmod(0)
        self.addCleanup(closed.chmod, 0o600)
        if os.access(closed, os.R_OK):
            self.skipTest("The administrator reads every file")
        # One reading is allowed: the first document. The second file is then of the size of a
        # document known by a checksum, and its comparison fails.
        self.assertEqual(self.build(limit=1), (1, 1))

    def test_the_comparison_and_the_questions_alternate_for_as_long_as_the_sources_recognise_more(self):
        self.write("book.txt")
        answers = iter([1, 1, 1, 1, 1, 1, 0])
        real, settled = indexer.settle, []

        def settling(*args, **kwargs):
            settled.append(1)
            return real(*args, **kwargs)

        with patch("shelffind.indexer.link.link", side_effect=lambda *args, **kwargs: next(answers)), \
                patch("shelffind.indexer.settle", side_effect=settling):
            self.assertEqual(self.build(names_only=True), (0, 0))
        self.assertEqual(len(settled), 7)

    def test_a_file_only_measured_by_a_pass_is_read_by_the_same_run_when_its_document_comes_to_wait(self):
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.twin(elsewhere)                                # a storage that answers, and holds none of the files
        one = self.write("a/book.txt")
        self.build(names_only=True)
        self.measure("a/book.txt")
        told = {"a/book.txt": {"id": "W1", "keys": self.sums(one), "title": "Held Elsewhere", "text": True}}
        self.sources(lambda name, path: next((record for rel, record in told.items() if path.endswith(rel)), None))
        self.assertEqual(self.build(), (0, 0))              # the library answers for its text: nothing is read
        self.assertEqual(self.state("a/book.txt"), "covered")
        one.unlink()                                        # the file is filed elsewhere,
        self.write("b/book.txt", when=1_800_000_000)
        told.clear()                                        # and the library no longer knows the work
        self.read.clear()
        # Nothing tells what the new file holds before it is copied for its reading: it is then
        # measured, found to be the document left to the library, and not read. The library,
        # asked about it afterwards, knows nothing of it: the document waits from that moment,
        # and the same run reads it.
        self.assertEqual((self.build(), self.read), ((1, 0), ["book.txt"]))
        self.assertEqual(names(self.db, "northern harbours"), ["b/book.txt"])
        self.assertEqual(self.build(), (0, 0))
        self.sound()

    def test_the_last_page_recognised_and_the_state_of_the_document_are_kept_together(self):
        self.write("scan.txt")
        self.build()
        self.blank()
        seen = []
        self.db.set_trace_callback(seen.append)
        self.addCleanup(self.db.set_trace_callback, None)
        with self.recognising():
            self.assertEqual(self.build(ocr=True), (1, 0))
        self.db.set_trace_callback(None)
        last = max(number for number, line in enumerate(seen) if line.startswith("UPDATE pages SET ztext"))
        after = [line for line in seen[last:] if line.startswith(("COMMIT", "UPDATE docs SET state='indexed'"))]
        self.assertEqual([line.split()[0] for line in after[:2]], ["UPDATE", "COMMIT"])

    def test_a_document_left_waiting_by_what_the_run_reads_is_read_in_the_same_run(self):
        self.write("a/book.txt")
        self.write("b/other.txt", OTHER)
        self.build(names_only=True)
        with self.db:                                       # the second is left to a source for the time being
            self.db.execute("UPDATE docs SET state='covered' WHERE id=?", (self.docs()["b/other.txt"],))
        real = indexer.update_text

        def reading(db, root, row, *args, **kwargs):
            done = real(db, root, row, *args, **kwargs)
            if row["rel"] == "a/book.txt":                  # and what the first reading shows ends that
                with db:
                    db.execute("UPDATE docs SET state='pending' WHERE state='covered'")
            return done

        with patch("shelffind.indexer.update_text", side_effect=reading):
            self.assertEqual(self.build(), (2, 0))
        self.assertEqual((names(self.db, "northern harbours"), names(self.db, "southern capes")),
                         (["a/book.txt"], ["b/other.txt"]))


if __name__ == "__main__":
    unittest.main()
