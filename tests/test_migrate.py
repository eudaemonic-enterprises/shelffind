# Copyright 2026 Eudaemonic Enterprises Inc.
"""An index of the earlier format is converted by copy: nothing is lost, the old index stays
as it was, and an interruption at any moment is repaired by doing the conversion again."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shelffind import cli, fuzzy, history, indexer, migrate, store
from shelffind.search import search

from support import run_main

# The index of version 0.2, as that version created it.
EARLIER = """
CREATE TABLE roots (
    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, mount TEXT NOT NULL,
    scanned TEXT, problem TEXT
);
CREATE TABLE files (
    id INTEGER PRIMARY KEY, root INTEGER NOT NULL REFERENCES roots ON DELETE CASCADE,
    rel TEXT NOT NULL, name TEXT NOT NULL, folded TEXT NOT NULL, format TEXT NOT NULL, size INTEGER NOT NULL,
    mtime INTEGER NOT NULL, state TEXT NOT NULL, error TEXT, UNIQUE(root, rel)
);
CREATE INDEX files_state ON files(state, id);
CREATE VIRTUAL TABLE names USING fts5(name, folder, tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER files_ad AFTER DELETE ON files BEGIN
    DELETE FROM names WHERE rowid=old.id;
END;
CREATE TABLE pages (
    id INTEGER PRIMARY KEY, file INTEGER NOT NULL REFERENCES files ON DELETE CASCADE,
    page INTEGER, section TEXT, text TEXT NOT NULL, needs_ocr INTEGER NOT NULL
);
CREATE INDEX pages_file ON pages(file);
CREATE VIRTUAL TABLE passages USING fts5(text, content='pages', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO passages(rowid,text) VALUES(new.id,new.text);
END;
CREATE TABLE words (term TEXT NOT NULL UNIQUE);
CREATE VIRTUAL TABLE spelling USING fts5(grams,content='');
CREATE TRIGGER words_ai AFTER INSERT ON words BEGIN
    INSERT INTO spelling(rowid,grams) VALUES(new.rowid,word_grams(new.term));
END;
CREATE TABLE history (query TEXT PRIMARY KEY, used INTEGER NOT NULL);
PRAGMA user_version=1;
"""
LARGE = "Tide tables of the northern harbours. " * 3000
WHEN = 1_700_000_000


class Earlier(unittest.TestCase):
    """A shelf of real files, and the index that version 0.2 would have made of it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.root = self.home / "Reading Room"
        self.data = self.home / "state"
        self.data.mkdir()
        homes = patch.dict(os.environ, {"HOME": str(self.home)})
        homes.start()
        self.addCleanup(homes.stop)
        self.old = sqlite3.connect(self.data / store.EARLIER)
        self.old.create_function("word_grams", 1, fuzzy.grams, deterministic=True)
        self.old.execute("PRAGMA journal_mode=WAL")
        self.old.executescript(EARLIER)
        self.old.execute("INSERT INTO roots(id,path,mount) VALUES(1,?,?)",
                         (str(self.root), store.mount_point(self.home)))
        # Two copies of one large document, each read by the earlier version; one of them still
        # waits for its scanned pages while the other is complete.
        self.file("Harbours/TideTables.txt", LARGE, "needs-ocr", [(1, "Tides of the northern harbours", 0), (2, "", 1)])
        self.file("Desk/TideTables.txt", LARGE, "indexed",
                  [(1, "Tides of the northern harbours", 0), (2, "The spring tide follows the new moon", 0)])
        # The same size and date under another name: another document until proved otherwise.
        self.file("Capes/WindRoses.txt", LARGE.replace("Tide", "Wind"), "pending", [])
        self.file("Capes/notes.txt", "Short notes on the wind.", "indexed", [(None, "Short notes on the wind.", 0)])
        self.file("Capes/broken.pdf", "not a PDF", "error", [], error="Syntax Error: no trailer")
        self.file("Capes/chart.png", "picture", "name-only", [])
        self.old.execute("INSERT INTO history VALUES('spring tide',?)", (WHEN * 10**9,))
        self.old.execute("INSERT INTO history VALUES('wind roses',?)", ((WHEN + 60) * 10**9,))
        self.old.commit()
        self.old.close()                # the earlier version is not running

    def earlier(self):
        """The old index opened again, as a program of the earlier version has it."""
        self.old = sqlite3.connect(self.data / store.EARLIER, timeout=0.2)
        self.old.create_function("word_grams", 1, fuzzy.grams, deterministic=True)
        self.addCleanup(self.old.close)
        return self.old

    def file(self, rel, content, state, pages, error=None):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        os.utime(path, (WHEN, WHEN))
        info = path.stat()
        suffix = path.suffix.lstrip(".")
        number = self.old.execute(
            "INSERT INTO files(root,rel,name,folded,format,size,mtime,state,error) VALUES(1,?,?,?,?,?,?,?,?)",
            (rel, path.name, fuzzy.fold(path.name), suffix, info.st_size, info.st_mtime_ns, state, error)).lastrowid
        folder = str(Path(self.root.name) / Path(rel).parent)
        self.old.execute("INSERT INTO names(rowid,name,folder) VALUES(?,?,?)",
                         (number, store.terms_for_name(path.name), store.terms_for_name(folder)))
        for page, text, blank, *section in pages:
            self.old.execute("INSERT INTO pages(file,page,section,text,needs_ocr) VALUES(?,?,?,?,?)",
                             (number, page, section[0] if section else None, text, blank))

    def converted(self):
        db = store.connect(self.data)
        self.addCleanup(db.close)
        return db

    def old_state(self):
        old = sqlite3.connect((self.data / store.EARLIER).as_uri() + "?mode=ro", uri=True)
        try:
            return (old.execute("PRAGMA user_version").fetchone()[0],
                    old.execute("SELECT count(*) FROM files").fetchone()[0],
                    old.execute("SELECT count(*) FROM pages").fetchone()[0])
        finally:
            old.close()


class Conversion(Earlier):
    def test_everything_is_carried_over(self):
        db = self.converted()
        self.assertEqual(db.execute("SELECT count(*) FROM files").fetchone()[0], 6)
        # Every file comes over as a document of its own, with the very pages read from it.
        self.assertEqual(db.execute("SELECT count(*) FROM docs").fetchone()[0], 6)
        states = dict(db.execute("SELECT f.rel,d.state FROM files f JOIN docs d ON d.id=f.doc"))
        self.assertEqual(states, {"Harbours/TideTables.txt": "needs-ocr", "Desk/TideTables.txt": "indexed",
                                  "Capes/WindRoses.txt": "pending", "Capes/notes.txt": "indexed",
                                  "Capes/broken.pdf": "error", "Capes/chart.png": "name-only"})
        self.assertEqual(db.execute("SELECT error FROM docs WHERE state='error'").fetchone()[0],
                         "Syntax Error: no trailer")
        found = search(db, "spring tide")
        self.assertEqual([(hit["path"], hit["page"], len(hit["copies"])) for hit in found],
                         [(str(self.root / "Desk/TideTables.txt"), 2, 0)])
        self.assertEqual(db.execute("SELECT count(*),sum(needs_ocr) FROM pages").fetchone()[:], (5, 1))
        # The first reading run measures the files that were read: the two copies, which hold
        # the same bytes, become one document with the pages read in full.
        with patch("shelffind.extract.passages", side_effect=AssertionError("read again")):
            indexer.build(db, self.data, store.roots(db), limit=0, progress=lambda _: None)
        found = search(db, "spring tide")
        self.assertEqual([(hit["name"], hit["page"], len(hit["copies"])) for hit in found], [("TideTables.txt", 2, 1)])
        self.assertEqual(db.execute("SELECT count(*),sum(needs_ocr) FROM pages").fetchone()[:], (3, 0))
        self.assertEqual(db.execute("SELECT count(*) FROM files WHERE read=1 AND sum IS NULL").fetchone()[0], 0)
        self.assertEqual(search(db, "wind", where="text")[0]["name"], "notes.txt")
        self.assertEqual([hit["name"] for hit in search(db, "windroses")], ["WindRoses.txt"])
        self.assertEqual(search(db, "harbuors")[0]["name"], "TideTables.txt")       # the spellings follow
        # Addresses are kept from the home folder, and mean the same again on this machine.
        self.assertEqual(db.execute("SELECT name,path FROM roots").fetchone()[:], ("Reading Room", "~/Reading Room"))
        self.assertEqual(store.roots(db)[0]["path"], str(self.root))
        self.assertEqual(history.queries(self.data), ["wind roses", "spring tide"])

    def test_the_next_reading_takes_up_where_the_old_one_stopped(self):
        db = self.converted()
        read = []
        original = indexer.extract.passages

        def counted(path, fmt, work):
            read.append(path.name)
            return original(path, fmt, work)

        with patch("shelffind.extract.passages", side_effect=counted):
            done = indexer.build(db, self.data, store.roots(db), progress=lambda _: None)
        self.assertEqual((done, read), ((1, 0), ["WindRoses.txt"]))
        self.assertEqual(db.execute("SELECT count(*) FROM docs").fetchone()[0], 5)
        self.assertEqual(len(search(db, "wind tables")), 1)

    def test_the_old_index_is_kept_and_marked(self):
        before = self.old_state()
        self.converted()
        self.assertEqual(self.old_state(), (migrate.RETIRED, *before[1:]))
        self.assertEqual(store.earlier_format(self.data), migrate.RETIRED)
        self.assertEqual(migrate.upgrade(self.data), self.data / store.INDEX)       # nothing more to do
        self.assertEqual(history.queries(self.data), ["wind roses", "spring tide"])
        self.assertGreater(migrate.discard_old(self.data), 0)
        self.assertFalse((self.data / store.EARLIER).exists())
        self.assertEqual(len(search(self.converted(), "spring tide")), 1)

    def test_nothing_is_deleted_before_the_new_index_exists(self):
        with self.assertRaises(ValueError):
            migrate.discard_old(self.data)
        self.assertTrue((self.data / store.EARLIER).exists())

    def test_an_interruption_leaves_the_old_index_as_it_was(self):
        before = self.old_state()
        for moment in ("shelffind.fuzzy.remember_text", "shelffind.migrate.os.replace"):
            with patch(moment, side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                migrate.upgrade(self.data)
            self.assertEqual(self.old_state(), before, moment)
            self.assertEqual(sorted(p.name for p in self.data.glob("index2*")), [], moment)
        (self.data / (store.INDEX + ".partial.99999")).write_bytes(b"left by a process that was killed")
        db = self.converted()
        self.assertEqual(sorted(p.name for p in self.data.glob("index2*.partial*")), [])
        self.assertEqual(len(search(db, "spring tide")), 1)

    def test_a_conversion_cut_after_the_old_index_was_marked_is_done_again(self):
        real = os.replace

        def cut(source, target):
            if str(target).endswith(store.INDEX):           # the very last step: the new index takes its name
                raise KeyboardInterrupt
            return real(source, target)

        with patch("shelffind.migrate.os.replace", side_effect=cut), self.assertRaises(KeyboardInterrupt):
            migrate.upgrade(self.data)
        # The old index is frozen, and no new index stands in its place: both programs wait.
        self.assertEqual(self.old_state()[0], migrate.RETIRED)
        self.assertEqual(sorted(p.name for p in self.data.glob("index2*")), [])
        self.assertEqual(run_main(self.data, "status", "--json")[0], 0)
        db = self.converted()                               # the next opening converts again
        self.assertEqual(db.execute("SELECT count(*) FROM files").fetchone()[0], 6)
        self.assertEqual(len(search(db, "spring tide")), 1)
        self.assertEqual(history.queries(self.data), ["wind roses", "spring tide"])
        with (self.data / history.NAME).open() as journal:
            self.assertEqual(len(journal.readlines()), 2)   # the searches were not written twice
        self.assertGreater(migrate.discard_old(self.data), 0)

    def test_an_old_index_being_written_is_not_converted_until_it_is_free(self):
        before = self.old_state()
        old = self.earlier()
        old.execute("BEGIN IMMEDIATE")                      # an earlier version is writing
        old.execute("DELETE FROM roots")                    # ... and forgets a folder, not yet for good
        with self.assertRaises(ValueError) as refused:
            migrate.upgrade(self.data, wait=0)
        self.assertIn("open in another program", str(refused.exception))
        self.assertEqual(sorted(p.name for p in self.data.glob("index2*")), [])
        old.rollback()
        old.close()
        self.assertEqual(self.old_state(), before)
        self.assertEqual(migrate.upgrade(self.data), self.data / store.INDEX)
        self.assertEqual(store.earlier_format(self.data), migrate.RETIRED)

    def test_an_old_index_that_a_panel_left_open_is_neither_converted_nor_deleted(self):
        panel = self.earlier()                              # a search panel of the earlier version, idle
        self.assertEqual(panel.execute("PRAGMA user_version").fetchone()[0], 1)
        with self.assertRaises(ValueError) as refused:
            migrate.upgrade(self.data, wait=0)
        self.assertIn("open in another program", str(refused.exception))
        self.assertEqual(sorted(p.name for p in self.data.glob("index2*")), [])
        panel.execute("INSERT INTO history VALUES('northern lights',?)", ((WHEN + 120) * 10**9,))
        panel.commit()                                      # what it writes meanwhile is not lost
        panel.close()
        self.converted()
        self.assertEqual(history.queries(self.data)[0], "northern lights")
        # A program that had the old index open before it was retired still has it open.
        panel = self.earlier()
        panel.execute("SELECT count(*) FROM history").fetchone()
        with self.assertRaises(ValueError) as kept:
            migrate.discard_old(self.data, wait=0)
        self.assertIn("open in another program", str(kept.exception))
        self.assertTrue((self.data / store.EARLIER).exists())
        panel.close()
        self.assertGreater(migrate.discard_old(self.data), 0)

    def test_an_old_index_kept_in_another_mode_is_neither_converted_nor_deleted(self):
        # Version 0.2 keeps its index in the write-ahead mode, the one in which a program that
        # still has the index open cannot go unseen.
        old = self.earlier()
        self.assertEqual(old.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete")
        old.close()
        with self.assertRaises(ValueError) as refused:
            migrate.upgrade(self.data)
        self.assertIn("journal mode", str(refused.exception))
        self.assertEqual(sorted(p.name for p in self.data.glob("index2*")), [])
        self.assertEqual(self.old_state()[0], 1)
        old = self.earlier()
        old.execute("PRAGMA journal_mode=WAL")
        old.close()
        self.converted()
        old = self.earlier()
        old.execute("PRAGMA journal_mode=DELETE")
        old.close()
        with self.assertRaises(ValueError) as kept:
            migrate.discard_old(self.data)
        self.assertIn("journal mode", str(kept.exception))
        self.assertTrue((self.data / store.EARLIER).exists())

    def test_a_file_under_the_old_name_that_is_no_index_of_ours_is_not_hidden_by_a_new_one(self):
        for version in (0, 37):
            other = self.home / f"elsewhere{version}"
            other.mkdir()
            stranger = sqlite3.connect(other / store.EARLIER)
            stranger.execute("CREATE TABLE pages(text)")
            stranger.execute("INSERT INTO pages VALUES('the only copy of a passage')")
            stranger.execute(f"PRAGMA user_version={version}")
            stranger.commit()
            stranger.close()
            with self.assertRaises(ValueError):
                store.connect(other)
            code, out, err = run_main(other, "status", "--json")
            self.assertEqual((code, out), (1, ""), version)
            self.assertIn("is not an index that this version can convert", err)
            self.assertFalse((other / store.INDEX).exists())
            self.assertEqual(run_main(other, "upgrade")[0], 1)
        empty = self.home / "empty"
        empty.mkdir()
        (empty / store.EARLIER).write_bytes(b"")            # a file with nothing in it is in nobody's way
        store.connect(empty).close()
        self.assertTrue((empty / store.INDEX).exists())

    def test_a_file_under_the_old_name_that_was_never_converted_is_not_deleted(self):
        stranger = sqlite3.connect(self.home / "stranger.sqlite3")      # format number 0, as SQLite makes it
        stranger.execute("CREATE TABLE pages(text)")
        stranger.execute("INSERT INTO pages VALUES('the only copy of a passage')")
        stranger.commit()
        stranger.close()
        other = self.home / "elsewhere"
        other.mkdir()
        store.connect(other).close()
        (other / store.EARLIER).write_bytes((self.home / "stranger.sqlite3").read_bytes())
        with self.assertRaises(ValueError):
            migrate.discard_old(other)
        self.assertTrue((other / store.EARLIER).exists())

    def test_the_searches_are_saved_once_more_before_the_old_index_is_deleted(self):
        self.converted()
        (self.data / history.NAME).unlink()                 # a folder restored without its journal
        self.assertEqual(history.queries(self.data), [])
        self.assertGreater(migrate.discard_old(self.data), 0)
        self.assertEqual(history.queries(self.data), ["wind roses", "spring tide"])

    def test_an_opening_does_not_stand_for_the_search_of_the_same_second(self):
        history.record(self.data, "open", "spring tide", {"path": "~/x.pdf", "name": "x.pdf"}, when=WHEN)
        self.converted()
        with (self.data / history.NAME).open() as journal:
            acts = sorted((json.loads(line)["act"], json.loads(line)["q"]) for line in journal)
        self.assertEqual(acts, [("open", "spring tide"), ("search", "spring tide"), ("search", "wind roses")])

    def test_an_old_index_that_cannot_be_read_is_never_taken_for_none(self):
        (self.data / store.EARLIER).write_bytes(b"this is not an index" * 400)
        for attempt in (lambda: store.connect(self.data), lambda: migrate.upgrade(self.data),
                        lambda: migrate.discard_old(self.data)):
            with self.assertRaises(ValueError):
                attempt()
        self.assertEqual(sorted(p.name for p in self.data.glob("index2*")), [])
        code, _, err = run_main(self.data, "status")
        self.assertEqual(code, 1)
        self.assertIn("The earlier index cannot be read", err)
        rows = json.loads(run_main(self.data, "doctor", "--json")[1])
        self.assertTrue(any(row["status"] == "fail" and row["label"] == "earlier index" for row in rows))

    def test_a_new_index_that_was_not_made_from_the_old_one_says_nothing_of_it(self):
        (self.data / store.INDEX).write_bytes(b"")          # an empty file under the name of the new index
        db = self.converted()                               # is no index: the old one is converted over it
        self.assertEqual(db.execute("SELECT count(*) FROM files").fetchone()[0], 6)
        self.assertEqual(store.earlier_format(self.data), migrate.RETIRED)
        db.close()
        # The new index is lost: the old one, though marked, is converted again, not replaced by nothing.
        (self.data / store.INDEX).unlink()
        db = store.connect(self.data)
        self.assertEqual(db.execute("SELECT count(*) FROM files").fetchone()[0], 6)
        db.close()
        # A new index made on its own, or made from another old index, beside this old one.
        other = self.home / "elsewhere"
        other.mkdir()
        store.connect(other).close()
        for stranger, version in ((other / store.INDEX, 1), (other / store.INDEX, migrate.RETIRED)):
            (self.data / store.INDEX).write_bytes(stranger.read_bytes())
            mark = sqlite3.connect(self.data / store.EARLIER)
            mark.execute(f"PRAGMA user_version={version}")
            mark.commit()
            mark.close()
            self.assertEqual(migrate.upgrade(self.data), self.data / store.INDEX)
            self.assertEqual(store.earlier_format(self.data), version)      # left exactly as it was
            with self.assertRaises(ValueError):
                migrate.discard_old(self.data)
            self.assertTrue((self.data / store.EARLIER).exists())

    def test_an_old_index_of_the_same_shape_and_other_words_is_not_discarded(self):
        self.converted()
        for change in ("UPDATE pages SET text='Short notes on the wing.' WHERE text='Short notes on the wind.'",
                       "UPDATE pages SET section='other.xhtml' WHERE text='Short notes on the wing.'"):
            late = sqlite3.connect(self.data / store.EARLIER)   # the same counts and lengths, another content
            late.execute(change)
            late.commit()
            late.close()
            with self.assertRaises(ValueError):
                migrate.discard_old(self.data)
            self.assertTrue((self.data / store.EARLIER).exists())
            # ... and a new index made from that other content does not vouch for this one either.
            self.assertNotEqual(migrate.origin(self.data), None)

    def test_an_old_index_that_gained_something_since_is_not_discarded(self):
        self.converted()
        late = sqlite3.connect(self.data / store.EARLIER)   # an earlier version, still running, writes on
        late.execute("INSERT INTO pages(file,page,section,text,needs_ocr) VALUES(1,3,NULL,'A page recognised late',0)")
        late.commit()
        late.close()
        with self.assertRaises(ValueError) as kept:
            migrate.discard_old(self.data)
        self.assertIn("holds something the new one was not made from", str(kept.exception))
        self.assertTrue((self.data / store.EARLIER).exists())
        code, _, err = run_main(self.data, "upgrade", "--discard-old")
        self.assertEqual(code, 1)
        self.assertIn("it is kept", err)

    def test_text_that_the_earlier_version_read_apart_stays_apart(self):
        self.earlier()
        # Two files of one name, size and date in two folders, whose pages differ: two documents.
        north = LARGE.replace("Tide", "Star")
        self.file("North/almanac.txt", north, "indexed", [(1, "An almanac of the northern lights", 0)])
        self.file("South/almanac.txt", north.replace("northern", "southern"), "indexed",
                  [(1, "An almanac of the southern cross", 0)])
        # Two copies waiting for their scanned pages, of which the second went further.
        scans = [(1, "", 1), (2, "", 1)]
        self.file("North/scan.txt", LARGE.replace("Tide", "Scan"), "needs-ocr", scans)
        self.file("South/scan.txt", LARGE.replace("Tide", "Scan"), "needs-ocr", [(1, "A recognised first page", 0), (2, "", 1)])
        # A third file of that name, size and date, which the earlier version never read, and
        # which holds what the southern almanac holds.
        self.file("West/almanac.txt", north.replace("northern", "southern"), "pending", [])
        self.old.commit()
        self.old.close()
        db = self.converted()
        self.assertEqual([hit["path"] for hit in search(db, "northern lights")], [str(self.root / "North/almanac.txt")])
        self.assertEqual([hit["path"] for hit in search(db, "southern cross")], [str(self.root / "South/almanac.txt")])
        found = search(db, "recognised first page")
        self.assertEqual((len(found), len(found[0]["copies"])), (1, 0))
        with patch("shelffind.extract.passages", side_effect=AssertionError("read again")):
            indexer.build(db, self.data, store.roots(db), progress=lambda _: None)
        # Their checksums say which files hold the same: the two scans, with the page that one
        # of them had recognised; and the unread almanac with the southern one, not the northern,
        # without its text being read again.
        found = search(db, "recognised first page")
        self.assertEqual((len(found), len(found[0]["copies"])), (1, 1))
        self.assertEqual(db.execute("SELECT count(*) FROM pages p JOIN files f ON f.doc=p.doc "
                                    "WHERE f.rel='North/scan.txt' AND p.needs_ocr=1").fetchone()[0], 1)
        self.assertEqual([hit["path"] for hit in search(db, "northern lights")], [str(self.root / "North/almanac.txt")])
        found = search(db, "southern cross")
        self.assertEqual([hit["path"] for hit in found] + found[0]["copies"],
                         [str(self.root / "South/almanac.txt"), str(self.root / "West/almanac.txt")])

    def test_every_file_keeps_the_place_of_its_own_pages(self):
        self.earlier()
        # Two books with the same words in chapters that are not named alike.
        self.file("North/book.epub", "one", "indexed", [(None, "The same chapter in both books", 0, "first.xhtml")])
        self.file("South/book.epub", "two", "indexed", [(None, "The same chapter in both books", 0, "other.xhtml")])
        self.old.commit()
        self.old.close()
        db = self.converted()
        places = dict(db.execute("SELECT f.rel,p.section FROM pages p JOIN files f ON f.doc=p.doc WHERE p.section IS NOT NULL"))
        self.assertEqual(places, {"North/book.epub": "first.xhtml", "South/book.epub": "other.xhtml"})

    def test_a_folder_with_a_dollar_sign_in_its_name_is_carried_over(self):
        self.earlier().execute("UPDATE roots SET path=?", (str(self.home / "receipts$NOT_A_VARIABLE"),))
        self.old.commit()
        self.old.close()
        db = self.converted()
        self.assertEqual(store.roots(db)[0]["path"], str(self.home / "receipts$NOT_A_VARIABLE"))
        self.assertEqual(db.execute("SELECT path FROM roots").fetchone()[0], "~/receipts$NOT_A_VARIABLE")

    def test_a_folder_that_does_not_exist_yet_has_nothing_to_convert(self):
        self.assertIsNone(migrate.upgrade(self.home / "nowhere"))
        self.assertFalse((self.home / "nowhere").exists())
        self.assertEqual(run_main(self.home / "nowhere", "upgrade")[0], 0)

    def test_a_reading_by_the_earlier_version_holds_the_conversion_back(self):
        import fcntl
        with (self.data / "index.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaises(ValueError) as refused:
                migrate.upgrade(self.data, wait=0.3)
            self.assertIn("A reading of documents is under way", str(refused.exception))
            self.assertEqual(sorted(p.name for p in self.data.glob("index2*")), [])
            self.assertEqual(self.old_state()[0], 1)
        self.assertEqual(len(search(self.converted(), "spring tide")), 1)

    def test_a_development_version_spares_the_index_in_use(self):
        with patch("shelffind.migrate.__version__", "9.9.9.dev"), \
                patch("shelffind.store.state_directory", return_value=self.data):
            with self.assertRaises(ValueError):
                migrate.upgrade(self.data)
            self.assertFalse((self.data / store.INDEX).exists())
        with patch("shelffind.migrate.__version__", "9.9.9.dev"):
            self.assertIsNotNone(migrate.upgrade(self.data))        # another folder is converted


class Commands(Earlier):
    def test_status_reports_from_the_old_index_and_converts_nothing(self):
        code, out, _ = run_main(self.data, "status", "--json", "--errors")
        self.assertEqual(code, 0)
        row = json.loads(out)[0]
        self.assertEqual((row["path"], row["files"]), (str(self.root), 6))
        self.assertEqual(row["states"], {"error": 1, "indexed": 2, "name-only": 1, "needs-ocr": 1, "pending": 1})
        self.assertEqual(row["errors"], [{"rel": "Capes/broken.pdf", "error": "Syntax Error: no trailer"}])
        self.assertFalse((self.data / store.INDEX).exists())
        self.assertIn("6 files", run_main(self.data, "status")[1])
        rows = json.loads(run_main(self.data, "doctor", "--json")[1])
        self.assertTrue(any(r["label"] == "index" and "shelffind upgrade" in r["remedy"] for r in rows))
        self.assertFalse((self.data / store.INDEX).exists())

    def test_a_small_index_is_converted_by_the_first_command_that_needs_it(self):
        code, out, err = run_main(self.data, "search", "--json", "spring tide")
        self.assertEqual((code, [hit["name"] for hit in json.loads(out)]), (0, ["TideTables.txt"]))
        self.assertIn("Converting the index", err)
        code, out, err = run_main(self.data, "status", "--json")
        self.assertEqual(json.loads(out)[0]["states"],
                         {"error": 1, "indexed": 2, "name-only": 1, "needs-ocr": 1, "pending": 1})
        self.assertEqual(err, "")
        code, out, _ = run_main(self.data, "upgrade")
        self.assertIn("Nothing to convert", out)
        code, out, _ = run_main(self.data, "upgrade", "--discard-old")
        self.assertEqual(code, 0)
        self.assertIn("deleted", out)

    def test_a_large_index_waits_for_the_upgrade_command(self):
        with patch.object(cli, "SMALL_INDEX", 0):
            code, out, err = run_main(self.data, "search", "--json", "spring tide")
            self.assertEqual((code, out), (75, ""))
            self.assertIn("shelffind upgrade", err)
            self.assertEqual(run_main(self.data, "roots", "list")[0], 75)
            self.assertFalse((self.data / store.INDEX).exists())
            code, _, err = run_main(self.data, "upgrade")
            self.assertEqual(code, 0)
            self.assertIn("The index is converted", err)
            code, out, _ = run_main(self.data, "search", "--json", "spring tide")
            self.assertEqual((code, len(json.loads(out))), (0, 1))

    def test_index_and_the_folder_commands_convert_at_any_size(self):
        with patch.object(cli, "SMALL_INDEX", 0):
            code, _, err = run_main(self.data, "index", "--names-only")
            self.assertEqual(code, 0)
            self.assertIn("Converting the index", err)
            self.assertTrue((self.data / store.INDEX).exists())


if __name__ == "__main__":
    unittest.main()
