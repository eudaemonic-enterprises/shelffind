# Copyright 2026 Eudaemonic Enterprises Inc.
"""Files copied ahead of the document being read: the waits of a network folder overlap, and the
index that results is the one of a reading that copies one file at a time."""
import hashlib
import os
import shutil
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from shelffind import extract, indexer, store
from shelffind.search import search
from collections import namedtuple

from support import Shelf


def dump(db):
    """What an index holds of its documents and pages, without the numbers that name them."""
    docs = []
    for doc in db.execute("SELECT id,state,size,format FROM docs ORDER BY id"):
        names = sorted(r["rel"] for r in db.execute("SELECT rel FROM files WHERE doc=?", (doc["id"],)))
        keys = sorted(r["key"] for r in db.execute("SELECT key FROM doc_keys WHERE doc=?", (doc["id"],)))
        pages = [(r["page"], store.inflate(r["ztext"])) for r in db.execute(
            "SELECT page,ztext FROM pages WHERE doc=? ORDER BY id", (doc["id"],))]
        docs.append((names, doc["state"], doc["size"], doc["format"], keys, pages))
    return sorted(docs)


class Ahead(Shelf):
    def write(self, count):
        for n in range(count):
            (self.root / f"volume{n:02d}.txt").write_text(f"lantern number {n} " * (20 + n))
        (self.root / "volume00 copy.txt").write_bytes((self.root / "volume00.txt").read_bytes())

    def other(self, name="other-state"):
        """A second index of the same folder, to read it the other way."""
        data = self.base / name
        db = store.connect(data)
        self.addCleanup(db.close)
        store.add_root(db, self.root, data)
        return db, data

    def test_copying_several_files_at_once_gives_the_index_of_copying_one(self):
        self.write(9)
        db, data = self.other()
        one = indexer.build(db, data, store.roots(db), progress=lambda _: None, workers=1)
        several = self.build(workers=4)
        self.assertEqual(one, several)
        self.assertEqual(dump(db), dump(self.db))
        self.assertEqual(self.db.execute("SELECT count(*) FROM docs WHERE state='indexed'").fetchone()[0], 9)

    def test_the_copies_overlap_only_when_asked(self):
        self.write(8)
        for workers, overlapping in ((1, False), (4, True)):
            live, widest, lock = [0], [0], threading.Lock()
            real = indexer.whole_checksums

            def slow(*args, **kwargs):
                with lock:
                    live[0] += 1
                    widest[0] = max(widest[0], live[0])
                time.sleep(0.05)
                try:
                    return real(*args, **kwargs)
                finally:
                    with lock:
                        live[0] -= 1
            with patch("shelffind.indexer.whole_checksums", slow):
                db, data = self.other(f"overlap-{workers}")
                indexer.build(db, data, store.roots(db), progress=lambda _: None, workers=workers)
            self.assertEqual(widest[0] > 1, overlapping, (workers, widest[0]))

    def test_a_file_changed_while_its_copy_waits_gets_no_text_of_another_content(self):
        self.write(5)
        victim = self.root / "volume04.txt"
        real, calls = indexer.update_text, []

        def reading(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                time.sleep(0.4)                             # the copies ahead are made meanwhile
                victim.write_text("an entirely different content, written afterwards " * 5)
                os.utime(victim, (1_700_000_000, 1_700_000_000))
            return real(*args, **kwargs)
        with patch("shelffind.indexer.update_text", reading):
            self.build(workers=4)
        waiting = self.db.execute("SELECT d.state,(SELECT count(*) FROM pages q WHERE q.doc=d.id) FROM files f "
                                  "JOIN docs d ON d.id=f.doc WHERE f.rel='volume04.txt'").fetchone()
        self.assertEqual(tuple(waiting), ("pending", 0))     # the copy of the earlier content was not kept for it
        self.build(workers=4)
        self.build(workers=4)
        text = " ".join(store.inflate(r["ztext"]) for r in self.db.execute(
            "SELECT ztext FROM pages p JOIN files f ON f.doc=p.doc WHERE f.rel='volume04.txt'"))
        self.assertIn("entirely different", text)
        self.assertNotIn("lantern number 4", text)

    def test_a_reading_that_ends_early_leaves_no_copy_and_no_thread(self):
        self.write(12)
        self.build(workers=4, limit=2)
        self.assertEqual([p.name for p in self.data.iterdir() if p.name.startswith("copies-")], [])
        time.sleep(0.2)
        self.assertEqual([t.name for t in threading.enumerate() if t.name == "shelffind-copy"], [])
        self.assertEqual(self.build(workers=4), (10, 0))

    def test_a_copy_made_ahead_keeps_the_time_of_the_last_change_that_it_saw(self):
        self.write(1)
        path = self.root / "volume00.txt"
        row = self.db.execute("SELECT id,rel,size,mtime,format FROM files WHERE rel='volume00.txt'").fetchone()
        self.build(names_only=True)
        row = self.db.execute("SELECT id,rel,size,mtime,format FROM files WHERE rel='volume00.txt'").fetchone()
        hold = self.base / "hold"
        hold.mkdir()
        folder, copy, measured, same, changed = indexer.prepare(path, row, hold, None)
        self.addCleanup(folder.cleanup)
        self.assertEqual(changed, path.stat().st_ctime_ns)
        self.assertEqual(copy.read_bytes(), path.read_bytes())
        self.assertEqual(measured[0], "md5:" + hashlib.md5(path.read_bytes()).hexdigest())

    def test_a_disk_that_holds_two_copies_only_does_not_stop_a_reading_that_one_at_a_time_finishes(self):
        for n in range(10):
            (self.root / f"v{n:02d}.txt").write_text(f"lantern {n} " * 270_000)          # about 3 MB each
        real = indexer.put

        def tight(data):
            def put(target, block):
                used = sum(f.stat().st_size for f in data.glob("copies-*/**/*") if f.is_file())
                if used + len(block) > 5_000_000:
                    raise indexer.Unplaced("No space left on device")
                return real(target, block)
            return put
        for workers in (1, 4):
            db, data = self.other(f"tight-{workers}")
            with patch("shelffind.indexer.put", tight(data)):
                self.assertEqual(indexer.build(db, data, store.roots(db), progress=lambda _: None, workers=workers), (10, 0))
            self.assertEqual(db.execute("SELECT count(*) FROM docs WHERE state='indexed'").fetchone()[0], 10)

    def test_the_copies_held_ahead_are_a_share_of_the_free_room_and_not_beyond_the_limit(self):
        usage = namedtuple("usage", "total used free")
        with patch("shelffind.indexer.shutil.disk_usage", return_value=usage(10**9, 10**9 - 4_000_000, 4_000_000)):
            ahead = indexer.Ahead(self.base, 2)
        self.addCleanup(ahead.close)
        self.assertEqual(ahead.room, 1_000_000)
        self.write(6)
        copied = []
        real = indexer.private_copy

        def counting(path, row, hold, known=None):
            copied.append(path.name)
            return real(path, row, hold, known)
        with patch("shelffind.indexer.private_copy", counting):
            self.assertEqual(self.build(workers=4, limit=1), (1, 0))
        self.assertEqual(len(copied), 1)

    def test_the_copy_of_a_file_has_no_character_that_a_shell_takes_for_an_order(self):
        for name, wanted in (("plain.pdf", "plain.pdf"), ("a b.pdf", "a_b.pdf"), ("$(touch x).ps.gz", "__touch_x_.ps.gz"),
                             ("it's;rm -rf.txt", "it_s_rm_-rf.txt"), (".hidden.pdf", "hidden.pdf"), ("Привет.djvu", "Привет.djvu"),
                             ("a`b`|c&d>e<f.txt", "a_b__c_d_e_f.txt")):
            self.assertEqual(indexer.plain_name(Path("/some/folder") / name), wanted)
        long_name = Path("/x/" + "n" * 300 + ".ps.gz")
        shortened = indexer.plain_name(long_name)
        self.assertLessEqual(len(shortened), 100)
        self.assertTrue(shortened.endswith(".ps.gz"))

    def test_a_file_name_cannot_run_a_command_through_a_converter_written_with_a_shell(self):
        marker = self.base / "marker"
        folder = self.root
        (folder / "$(touch $MARK).zzz").write_text("quiet harbour")
        extract.declare({"zzz": {"command": ["sh", "-c", "cat {path}"]}})
        self.addCleanup(extract.declare, {})
        with patch.dict(os.environ, {"MARK": str(marker)}):
            self.assertEqual(self.build(), (1, 0))
        self.assertFalse(marker.exists())
        self.assertTrue(search(self.db, "harbour", where="text"))

    def test_a_file_that_cannot_be_copied_is_said_as_before(self):
        self.write(4)
        for workers in (1, 4):
            db, data = self.other(f"failed-{workers}")
            said = []
            victim = self.root / "volume02.txt"
            real = indexer.whole_checksums

            def failing(path, *args, **kwargs):
                if Path(path) == victim:
                    raise OSError("Input/output error")
                return real(path, *args, **kwargs)
            with patch("shelffind.indexer.whole_checksums", failing):
                done = indexer.build(db, data, store.roots(db), progress=said.append, workers=workers)
            self.assertEqual(done, (4, 1), (workers, said))
            self.assertTrue(any("volume02.txt" in line and "Input/output error" in line for line in said), said)


class Pages(Shelf):
    """Pages recognised, and pages asked for one by one, several at once and in the order of the pages."""

    def scan(self, pages=6):
        (self.root / "scan.txt").write_text("a document of blank pages")
        blank = [extract.Passage(n, None, "", True) for n in range(1, pages + 1)]
        with patch("shelffind.extract.passages", return_value=iter(blank)):
            self.assertEqual(self.build(), (1, 0))
        self.assertEqual(self.db.execute("SELECT state FROM docs").fetchone()[0], "needs-ocr")

    def texts(self):
        return [store.inflate(r["ztext"]) for r in self.db.execute("SELECT ztext FROM pages ORDER BY page")]

    def test_pages_are_recognised_at_once_and_kept_in_the_order_of_the_pages(self):
        self.scan(6)
        live, widest, lock = [0], [0], threading.Lock()

        def recognise(path, fmt, work, number, languages):
            with lock:
                live[0] += 1
                widest[0] = max(widest[0], live[0])
            time.sleep(0.06 - 0.008 * number)           # the later pages come back first
            with lock:
                live[0] -= 1
            return f"recognised page {number}"
        with patch("shelffind.extract.recognize", side_effect=recognise):
            self.assertEqual(self.build(ocr=True, workers=4), (1, 0))
        self.assertGreater(widest[0], 1)
        self.assertEqual(self.texts(), [f"recognised page {n}" for n in range(1, 7)])
        self.assertEqual(self.db.execute("SELECT state FROM docs").fetchone()[0], "indexed")

    def test_an_interruption_between_recognised_pages_keeps_those_before_it(self):
        self.scan(6)

        def recognise(path, fmt, work, number, languages):
            if number == 3:
                raise KeyboardInterrupt()
            time.sleep(0.01)
            return f"recognised page {number}"
        with patch("shelffind.extract.recognize", side_effect=recognise), self.assertRaises(KeyboardInterrupt):
            self.build(ocr=True, workers=4)
        self.assertEqual(self.db.execute("SELECT page FROM pages WHERE needs_ocr=1 ORDER BY page").fetchall()[0][0], 3)
        self.assertEqual(self.texts()[:2], ["recognised page 1", "recognised page 2"])
        with patch("shelffind.extract.recognize", side_effect=lambda p, f, w, n, l: f"recognised page {n}"):
            self.assertEqual(self.build(ocr=True, workers=4), (1, 0))
        self.assertEqual(self.texts(), [f"recognised page {n}" for n in range(1, 7)])

    @unittest.skipUnless(all(shutil.which(p) for p in ("gs", "pdftoppm", "pdftotext", "tesseract", "prlimit")),
                         "Poppler, Ghostscript, Tesseract and prlimit required")
    def test_real_pages_are_recognised_at_once_by_tesseract_under_prlimit(self):
        words = ["Sapphire meadow", "Copper lantern", "Velvet harbour", "Amber orchard"]
        ps = self.base / "outlined.ps"
        ps.write_text("%!PS\n/Helvetica findfont 48 scalefont setfont\n" + "".join(
            f"40 600 moveto ({w}) true charpath fill showpage\n" for w in words))
        subprocess.run(["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
                        f"-sOutputFile={self.root / 'scan.pdf'}", str(ps)], check=True)
        self.assertEqual(self.build(workers=3), (1, 0))
        self.assertEqual(self.db.execute("SELECT state FROM docs").fetchone()[0], "needs-ocr")
        self.assertEqual(self.build(ocr=True, workers=3), (1, 0))
        self.assertEqual([" ".join(text.split()).lower() for text in self.texts()], [w.lower() for w in words])
        self.assertEqual(self.db.execute("SELECT state FROM docs").fetchone()[0], "indexed")

    def test_djvu_pages_asked_for_one_by_one_come_back_in_order(self):
        live, widest, lock = [0], [0], threading.Lock()

        def tool(argv, work):
            if argv[0] == "djvused":
                return "5\n"
            if argv[0] == "djvutxt" and argv[1].startswith("--page="):
                number = int(argv[1].split("=")[1])
                with lock:
                    live[0] += 1
                    widest[0] = max(widest[0], live[0])
                time.sleep(0.05 - 0.008 * number)
                with lock:
                    live[0] -= 1
                return f"page {number}\f"
            return "one block without a division into pages"
        with patch("shelffind.extract.run_tool", side_effect=tool):
            pages = list(extract.passages(Path("book.djvu"), "djvu", Path(".")))
        self.assertEqual([(p.page, p.text) for p in pages], [(n, f"page {n}") for n in range(1, 6)])
        self.assertGreater(widest[0], 1)

    def test_a_program_that_is_missing_is_said_as_before_and_a_page_has_an_image_of_its_own(self):
        with self.assertRaisesRegex(ValueError, "Required program is missing: no-such-program"):
            extract.run_tool(["no-such-program"], self.base)
        seen = []
        with patch("shelffind.extract.run_tool", side_effect=lambda argv, work: seen.append(argv) or ""):
            extract.recognize(Path("a.pdf"), "pdf", self.base, 7, "eng")
            extract.recognize(Path("a.pdf"), "pdf", self.base, 8, "eng")
        images = [argv[1] for argv in seen if argv[0] == "tesseract"]
        self.assertEqual(len(set(images)), 2)


if __name__ == "__main__":
    unittest.main()
