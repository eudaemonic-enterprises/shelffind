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
        self.file("Harbours/TideTables.txt", LARGE, "needs-ocr", [(1, "Tides of the", 0), (2, "", 1)])
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
        self.addCleanup(self.old.close)

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
        for page, text, blank in pages:
            self.old.execute("INSERT INTO pages(file,page,section,text,needs_ocr) VALUES(?,?,NULL,?,?)",
                             (number, page, text, blank))

    def converted(self):
        db = store.connect(self.data)
        self.addCleanup(db.close)
        return db

    def old_state(self):
        with sqlite3.connect((self.data / store.EARLIER).as_uri() + "?mode=ro", uri=True) as old:
            return (old.execute("PRAGMA user_version").fetchone()[0],
                    old.execute("SELECT count(*) FROM files").fetchone()[0],
                    old.execute("SELECT count(*) FROM pages").fetchone()[0])


class Conversion(Earlier):
    def test_everything_is_carried_over(self):
        db = self.converted()
        self.assertEqual(db.execute("SELECT count(*) FROM files").fetchone()[0], 6)
        self.assertEqual(db.execute("SELECT count(*) FROM docs").fetchone()[0], 5)
        states = dict(db.execute("SELECT f.rel,d.state FROM files f JOIN docs d ON d.id=f.doc"))
        self.assertEqual(states, {"Harbours/TideTables.txt": "indexed", "Desk/TideTables.txt": "indexed",
                                  "Capes/WindRoses.txt": "pending", "Capes/notes.txt": "indexed",
                                  "Capes/broken.pdf": "error", "Capes/chart.png": "name-only"})
        self.assertEqual(db.execute("SELECT error FROM docs WHERE state='error'").fetchone()[0],
                         "Syntax Error: no trailer")
        # The two copies are one document, with the pages of the copy that was read in full.
        found = search(db, "spring tide")
        self.assertEqual([(hit["name"], hit["page"], len(hit["copies"])) for hit in found], [("TideTables.txt", 2, 1)])
        self.assertEqual(db.execute("SELECT count(*),sum(needs_ocr) FROM pages").fetchone()[:], (3, 0))
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

    def test_a_conversion_cut_after_the_new_index_appeared_is_completed(self):
        with patch("shelffind.migrate.finish", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            migrate.upgrade(self.data)
        self.assertTrue((self.data / store.INDEX).exists())
        self.assertEqual(self.old_state()[0], 1)
        self.assertEqual(history.queries(self.data), [])
        self.converted()
        self.assertEqual(self.old_state()[0], 1)            # an ordinary opening changes nothing
        self.assertEqual(run_main(self.data, "upgrade")[0], 0)
        self.assertEqual(self.old_state()[0], migrate.RETIRED)
        self.assertEqual(history.queries(self.data), ["wind roses", "spring tide"])
        run_main(self.data, "upgrade")
        with (self.data / history.NAME).open() as journal:
            self.assertEqual(len(journal.readlines()), 2)   # the searches were not written twice

    def test_a_busy_old_index_is_marked_later(self):
        self.old.execute("BEGIN IMMEDIATE")                 # an earlier version is writing
        real = sqlite3.connect
        with patch("shelffind.migrate.sqlite3.connect",
                   side_effect=lambda *a, **k: real(*a, **{**k, "timeout": 0.1})):
            self.assertEqual(migrate.upgrade(self.data, wait=0), self.data / store.INDEX)
        self.assertEqual(store.earlier_format(self.data), 1)
        self.old.rollback()
        migrate.upgrade(self.data)
        self.assertEqual(store.earlier_format(self.data), migrate.RETIRED)

    def test_a_reading_by_the_earlier_version_is_waited_for_then_passed(self):
        import fcntl
        said = []
        with (self.data / "index.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            migrate.upgrade(self.data, said.append, wait=0.3)
        self.assertTrue(any("under way" in text for text in said))
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
        self.assertEqual(json.loads(out)[0]["states"], {"error": 1, "indexed": 3, "name-only": 1, "pending": 1})
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
