# Copyright 2026 Eudaemonic Enterprises Inc.
"""Two machines share the text they read through one shared place: what one read the other
finds without reading it, a parcel damaged on the way is taken another time, the readings of
several machines add up page by page, and nothing is given to bytes that a measure contradicts,
to a document left to a source, or under a name that another data folder writes."""
import contextlib
import hashlib
import itertools
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import invariants
from shelffind import extract, history, indexer, link, machine, parcels, sharing, store
from shelffind.search import search

from support import RCLONE as STORAGE, run_main
from test_sharing import RCLONE

TEXTS = {
    "Waves/tides.txt": "Tide tables of the northern harbours. " * 400,
    "Waves/winds.txt": "Wind roses of the southern capes. " * 500,
    "Charts/shoals.txt": "Charts of the western shoals. " * 600,
    "Charts/stars.txt": "Star almanac of the eastern skies. " * 700,
}
WHEN = 1_700_000_000


def counts(**done):
    return {"sent": 0, "received": 0, "damaged": 0, "newer": 0, "skipped": 0, **done}


class Two(unittest.TestCase):
    """The machines home and office, each with its home folder, its shelf, its data folder and
    its identity, and one shared folder."""

    names = ("home", "office")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.shared = self.base / "shared"
        self.shared.mkdir()
        self.machines, self.read, self.forbidden = {}, {name: [] for name in self.names}, set()
        self.here = None
        original = extract.passages

        def counted(path, fmt, work):
            if self.here in self.forbidden:
                raise AssertionError(f"{self.here} read {path.name}")
            self.read[self.here].append(path.name)
            return original(path, fmt, work)

        watcher = patch("shelffind.extract.passages", side_effect=counted)
        watcher.start()
        self.addCleanup(watcher.stop)
        for name in self.names:
            self.add(name)

    def add(self, name, system=None):
        """A data folder `name` with its shelf, on the machine whose system identifier is that of
        `system` (by default its own)."""
        shelf = self.base / name / "Shelf"
        shelf.mkdir(parents=True)
        (self.base / f"{system or name}-id").write_text(f"{(system or name) * 8}\n")
        data = self.base / f"{name}-state"
        self.machines[name] = {"shelf": shelf, "data": data, "system": system or name}
        with self.at(name):
            db = store.connect(data)
            store.add_root(db, shelf, data)
        self.addCleanup(db.close)
        self.machines[name]["db"] = db
        self.read.setdefault(name, [])

    @contextlib.contextmanager
    def at(self, name):
        """Run what follows as on the machine of the data folder `name`."""
        before, self.here = self.here, name
        system = self.machines.get(name, {}).get("system", name)
        try:
            with patch.dict(os.environ, {"HOME": str(self.base / name)}), \
                    patch.object(machine, "SOURCES", (str(self.base / f"{system}-id"),)):
                yield self.machines.get(name)
        finally:
            self.here = before

    def write(self, rel, text=None, on=None):
        for name in on or self.names:
            path = self.machines[name]["shelf"] / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(TEXTS[rel] if text is None else text)
            os.utime(path, (WHEN, WHEN))

    def shelf(self, on=None):
        for rel in TEXTS:
            self.write(rel, on=on)

    def place(self):
        return sharing.Folder(self.shared)

    def exchange(self, name, place=None):
        machine_ = self.machines[name]
        with self.at(name):
            return parcels.Exchange(machine_["data"], machine_["db"], place or self.place())

    def build(self, name, place=None, **opts):
        """A run on the data folder `name`, sharing texts: ((done, problems), counts)."""
        machine_ = self.machines[name]
        with self.at(name):
            told = parcels.Exchange(machine_["data"], machine_["db"], place or self.place())
            done = indexer.build(machine_["db"], machine_["data"], store.roots(machine_["db"]),
                                 progress=lambda _: None, exchange=told, **opts)
        return done, told.counts

    def found(self, name, query):
        """The files found by the words of `query` exactly as typed."""
        with self.at(name):
            return sorted(Path(hit["path"]).name for hit in search(self.machines[name]["db"], query, exact=True))

    def measure(self, name):
        """Measure every file of the data folder `name` by a reading in full, as a complete run
        does when it has a reason to."""
        db = self.machines[name]["db"]
        with self.at(name):
            indexer.build(db, self.machines[name]["data"], store.roots(db), names_only=True, progress=lambda _: None)
            for row in db.execute("SELECT f.id,f.rel,f.size,f.mtime FROM files f").fetchall():
                with db:
                    indexer.joined(db, row["id"], indexer.whole_checksums(self.machines[name]["shelf"] / row["rel"], row))

    def sound(self, name):
        db = self.machines[name]["db"]
        self.assertEqual(invariants.structure(db), [])
        self.assertEqual(invariants.actual(db), invariants.canonical(db))

    def writer(self, name):
        with self.at(name):
            return machine.identity(self.machines[name]["data"])["writer"]

    def parcels_of(self, name):
        return sorted((self.shared / "texts" / self.writer(name)).glob("*.jsonl.gz"))

    def listed(self, name):
        return json.loads((self.shared / "texts" / f"{self.writer(name)}.json").read_text())

    def waiting(self, name):
        return self.machines[name]["db"].execute("SELECT count(*) FROM docs WHERE state='pending'").fetchone()[0]

    def pages(self, name, rel):
        db = self.machines[name]["db"]
        return [(row["page"], store.inflate(row["ztext"]), row["needs_ocr"]) for row in db.execute(
            "SELECT p.page,p.ztext,p.needs_ocr FROM pages p JOIN files f ON f.doc=p.doc WHERE f.rel=? ORDER BY p.id", (rel,))]

    def gift(self, writer, lines, number=1):
        """Put in the shared place, as the data folder `writer` would, a parcel of the document
        lines `lines` (dicts or raw bytes), named in its list."""
        packer = parcels.Packer(writer)
        for line in lines:
            packer.add(line if isinstance(line, bytes) else json.dumps(line).encode() + b"\n")
        data = packer.close()
        name = f"texts/{writer}/{number:06d}.jsonl.gz"
        place = self.place()
        place.put(name, data)
        try:
            listed = json.loads((self.shared / "texts" / f"{writer}.json").read_text())["parcels"]
        except OSError:
            listed = []
        listed.append({"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "documents": len(lines)})
        place.put(f"texts/{writer}.json", json.dumps({"format": 1, "writer": writer, "parcels": listed}).encode())
        return name

    def line(self, rel, pages, fmt="txt"):
        body = TEXTS[rel].encode()
        return {"md5": f"md5:{hashlib.md5(body).hexdigest()}", "sha256": f"sha256:{hashlib.sha256(body).hexdigest()}",
                "size": len(body), "format": fmt, "pages": pages}


class Texts(Two):
    def test_what_one_machine_read_the_other_finds_without_reading_it(self):
        self.shelf()
        self.assertEqual(self.build("home"), ((4, 0), counts(sent=4)))
        self.forbidden.add("office")
        self.assertEqual(self.build("office"), ((0, 0), counts(received=4)))
        self.assertEqual((self.found("office", "northern harbours"), self.found("office", "eastern skies")),
                         (["tides.txt"], ["stars.txt"]))
        self.assertEqual((self.waiting("office"), self.read["office"]), (0, []))
        self.assertEqual(self.parcels_of("office"), [])     # what it received, it does not send back
        self.assertEqual(self.build("office")[1], counts())
        self.assertEqual(self.build("home")[1], counts())
        self.sound("home")
        self.sound("office")

    def test_two_machines_that_read_in_turn_read_each_document_once(self):
        self.shelf()
        self.assertEqual(self.build("home", limit=2)[0], (2, 0))
        self.assertEqual(self.build("office")[0], (2, 0))  # the two others
        self.assertEqual(self.build("home")[0], (0, 0))
        self.assertEqual(sorted(self.read["home"] + self.read["office"]), sorted(Path(rel).name for rel in TEXTS))
        for name in ("home", "office"):
            self.assertEqual(self.waiting(name), 0)
            self.assertEqual(self.found(name, "western shoals"), ["shoals.txt"])
            self.sound(name)

    def test_each_machine_reads_in_an_order_of_its_own(self):
        names = [f"Notes/note{number:02d}.txt" for number in range(12)]   # twelve: orders alike by chance once in 479 million
        for number, rel in enumerate(names):
            self.write(rel, f"Note {number} on the harbours. " * (40 + number))
        orders = {}
        for name in ("home", "office"):
            # Every copy turns out to need no reading: each file is taken at most twice, and the run ends.
            with patch("shelffind.indexer.update_text", return_value=False) as tried, \
                    patch.object(parcels.Exchange, "run", lambda self, *steps, **kwargs: True):
                self.assertEqual(self.build(name)[0], (0, 0))
            taken = [call.args[2]["rel"] for call in tried.call_args_list]
            self.assertEqual(max(taken.count(rel) for rel in taken), 2)
            orders[name] = list(dict.fromkeys(taken))
            self.assertEqual(sorted(orders[name]), names)
        self.assertNotEqual(orders["home"], orders["office"])

    def test_pages_never_go_to_a_file_that_holds_other_bytes(self):
        self.write("Waves/tides.txt")
        twin = "Tide tables of the northern harbours. " * 400
        self.write("Waves/tides.txt", twin.replace("northern", "southern"), on=("office",))     # the same size, other bytes
        self.assertEqual(self.build("home")[0], (1, 0))
        self.assertEqual(self.build("office"), ((1, 0), counts(sent=1)))
        self.assertEqual((self.found("office", "northern"), self.found("office", "southern")), ([], ["tides.txt"]))
        self.sound("office")

    def test_a_document_that_a_source_answers_for_takes_pages_once_the_source_stops(self):
        self.shelf()
        self.build("home")
        self.measure("office")
        db = self.machines["office"]["db"]
        with db:
            db.execute("UPDATE docs SET state='covered'")
        told = self.exchange("office")
        told.run("receive", "apply")
        self.assertEqual(told.counts["received"], 0)
        with db:
            db.execute("UPDATE docs SET state='pending'")
        told.run("apply")
        self.assertEqual(told.counts["received"], 4)
        self.assertEqual(self.found("office", "southern capes"), ["winds.txt"])
        self.sound("office")

    def test_recognised_pages_travel_and_complete_the_other_reading(self):
        self.write("Waves/tides.pdf", TEXTS["Waves/tides.txt"])        # a scan, read page by page
        original = extract.passages

        def scanned(path, fmt, work):          # a scan: one page of text, the second still to recognise
            original(path, fmt, work)
            return [extract.Passage(1, None, path.read_text()), extract.Passage(2, None, "", True)]

        with patch("shelffind.extract.passages", side_effect=scanned):
            self.assertEqual(self.build("home")[0], (1, 0))
        self.forbidden.add("office")
        with patch("shelffind.extract.recognize", side_effect=AssertionError("the office recognised a page")):
            self.assertEqual(self.build("office")[1]["received"], 1)
        state = lambda name: self.machines[name]["db"].execute("SELECT state FROM docs WHERE origin IS NOT NULL").fetchone()[0]
        self.assertEqual(state("office"), "needs-ocr")
        with patch("shelffind.extract.recognize", side_effect=lambda source, fmt, work, page, lang: "Harbour charts recognised"):
            self.assertEqual(self.build("home", ocr=True), ((1, 0), counts(sent=1)))
        with patch("shelffind.extract.recognize", side_effect=AssertionError("the office recognised a page")):
            self.assertEqual(self.build("office", ocr=True), ((0, 0), counts(received=1)))
        self.assertEqual((state("office"), self.found("office", "harbour charts")), ("indexed", ["tides.pdf"]))
        self.sound("office")


class Readings(Two):
    """Readings of the same bytes add up page by page, and never take text away."""

    def scan(self, rel="Waves/tides.txt"):
        body = TEXTS[rel]
        return [[1, None, body, 0], [2, None, "", 1], [3, None, "", 1]]

    def test_a_reading_of_other_pages_never_replaces_ours(self):
        rel = "Waves/tides.txt"
        self.write(rel, on=("office",))
        self.measure("office")
        db = self.machines["office"]["db"]
        doc = db.execute("SELECT doc FROM files WHERE rel=?", (rel,)).fetchone()[0]
        keys = store.keys_of(db, doc, "content")
        ours = [(1, None, "First page", 0), (2, None, "Second page", 0), (3, None, "Late atlas", 0), (4, None, "", 1)]
        with db:
            db.executemany("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,?,?,?,?)",
                           [(doc, page, section, store.deflate(body), blank) for page, section, body, blank in ours])
            db.execute("UPDATE docs SET state='needs-ocr',origin=? WHERE id=?", (" ".join(keys), doc))
            theirs = [(1, None, "First page", 0), (2, None, "Second page", 0)]      # another layout, nothing to recognise
            self.assertFalse(store.take_pages(db, doc, keys, theirs))
        self.assertEqual([page for page, _, _ in self.pages("office", rel)], [1, 2, 3, 4])
        self.assertEqual(self.found("office", "late atlas"), ["tides.txt"])

    def test_pages_of_the_same_layout_add_up_and_nothing_recognised_is_replaced(self):
        rel = "Waves/tides.txt"
        self.write(rel, on=("office",))
        self.measure("office")
        db = self.machines["office"]["db"]
        doc = db.execute("SELECT doc FROM files WHERE rel=?", (rel,)).fetchone()[0]
        keys = store.keys_of(db, doc, "content")
        with db:
            self.assertTrue(store.take_pages(db, doc, keys, [(1, None, "One", 0), (2, None, "", 1), (3, None, "", 1)]))
            self.assertTrue(store.take_pages(db, doc, keys, [(1, None, "Other one", 0), (2, None, "", 1), (3, None, "Three", 0)]))
            self.assertTrue(store.take_pages(db, doc, keys, [(1, None, "", 1), (2, None, "Two", 0), (3, None, "", 1)]))
            self.assertFalse(store.take_pages(db, doc, keys, [(1, None, "", 1), (2, None, "Two again", 0), (3, None, "", 1)]))
        self.assertEqual(self.pages("office", rel), [(1, "One", 0), (2, "Two", 0), (3, "Three", 0)])
        self.assertEqual(db.execute("SELECT state FROM docs WHERE id=?", (doc,)).fetchone()[0], "indexed")
        self.sound("office")

    def test_offers_that_recognise_other_pages_are_both_taken_whatever_their_order(self):
        rel, body = "Waves/tides.pdf", TEXTS["Waves/tides.txt"]          # a scan, read page by page
        for order in ((0, 1), (1, 0)):
            with self.subTest(order=order):
                for path in sorted(self.shared.rglob("*"), reverse=True):
                    path.unlink() if path.is_file() else path.rmdir()
                (self.machines["office"]["data"] / parcels.KEEPING).unlink(missing_ok=True)
                db = self.machines["office"]["db"]
                self.write(rel, body, on=("office",))
                self.measure("office")
                doc = db.execute("SELECT doc FROM files WHERE rel=?", (rel,)).fetchone()[0]
                with db:                        # the office read the text as a scan of three pages, none recognised
                    db.execute("DELETE FROM pages WHERE doc=?", (doc,))
                    db.executemany("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,?,NULL,?,1)",
                                   [(doc, number, store.deflate("")) for number in (1, 2, 3)])
                    db.execute("UPDATE docs SET state='needs-ocr',origin=? WHERE id=?",
                               (" ".join(store.keys_of(db, doc, "content")), doc))
                offers = [[[1, None, body, 0], [2, None, "", 1], [3, None, "", 1]],
                          [[1, None, "", 1], [2, None, "Second", 0], [3, None, "Third", 0]]]
                md5, sha = (f"md5:{hashlib.md5(body.encode()).hexdigest()}", f"sha256:{hashlib.sha256(body.encode()).hexdigest()}")
                for writer, index in zip(("a" * 16, "b" * 16), order):
                    self.gift(writer, [{"md5": md5, "sha256": sha, "size": len(body.encode()), "format": "pdf",
                                        "pages": offers[index]}])
                told = self.exchange("office")
                told.run("receive", "apply")
                told.run("apply")                       # what the other offer adds is given at the next round
                self.assertEqual([text for _, text, _ in self.pages("office", rel)], [body, "Second", "Third"])
                told.run("receive", "apply")            # nothing more to take
                self.assertEqual(told.counts["received"], 2)


class Damage(Two):
    """What a transfer can do to a parcel, and what a data folder can lose."""

    def damaged(self, spoil):
        """Home reads and gives; the parcel is spoilt; the office takes nothing; once the parcel
        is whole again, the office takes it and reads nothing."""
        self.shelf()
        self.build("home")
        parcel = self.parcels_of("home")[0]
        whole = parcel.read_bytes()
        spoil(parcel)
        self.assertEqual(self.build("office", limit=0), ((0, 0), counts(damaged=1)))
        parcel.write_bytes(whole)
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[0], (0, 0))
        self.assertEqual(self.waiting("office"), 0)
        self.sound("office")

    def test_a_parcel_cut_on_the_way_is_taken_another_time(self):
        self.damaged(lambda parcel: parcel.write_bytes(parcel.read_bytes()[:len(parcel.read_bytes()) // 2]))

    def test_a_parcel_changed_on_the_way_is_taken_another_time(self):
        def flip(parcel):
            data = bytearray(parcel.read_bytes())
            data[len(data) // 2] ^= 0x20
            parcel.write_bytes(bytes(data))
        self.damaged(flip)

    def test_a_parcel_listed_and_missing_is_taken_once_it_is_there(self):
        self.damaged(lambda parcel: parcel.unlink())

    def test_pages_given_in_part_are_given_again_and_nothing_twice(self):
        self.shelf()
        self.build("home")
        self.measure("office")
        told = self.exchange("office")
        told.run("receive")
        real, calls = store.take_pages, []

        def stopping(db, doc, keys, pages, holding=()):
            calls.append(doc)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(db, doc, keys, pages, holding)

        with patch("shelffind.store.take_pages", side_effect=stopping), self.assertRaises(KeyboardInterrupt):
            told.run("apply")
        self.assertEqual(self.waiting("office"), 3)
        told = self.exchange("office")
        told.run("receive", "apply")
        self.assertEqual((told.counts["received"], self.waiting("office")), (3, 0))
        db = self.machines["office"]["db"]
        self.assertEqual(db.execute("SELECT count(*) FROM pages").fetchone()[0], 4)
        self.sound("office")

    def test_a_parcel_of_a_newer_format_waits_for_a_newer_version(self):
        self.shelf()
        with patch.object(parcels, "FORMAT", 2):
            self.build("home")
        self.measure("office")
        told = self.exchange("office")
        told.run("receive", "apply")
        self.assertEqual((told.counts["received"], told.counts["newer"], told.counts["damaged"]), (0, 1, 0))
        told.run("receive")                             # remembered: not fetched again
        self.assertEqual(told.counts["newer"], 2)
        with patch.object(parcels, "FORMAT", 2):
            told = self.exchange("office")
            told.run("receive", "apply")
        self.assertEqual(told.counts["received"], 4)
        self.sound("office")

    def test_a_wrong_clock_changes_nothing(self):
        self.shelf()
        with patch("time.time", return_value=86_400.0):
            self.build("home")
        self.forbidden.add("office")
        with patch("time.time", return_value=4_102_444_800.0):
            self.assertEqual(self.build("office")[1]["received"], 4)
        self.assertEqual(self.waiting("office"), 0)

    def test_a_data_folder_that_lost_its_bookkeeping_gives_nothing_twice(self):
        self.shelf()
        self.build("home")
        (self.machines["home"]["data"] / parcels.KEEPING).unlink()
        self.assertEqual(self.build("home"), ((0, 0), counts()))
        self.assertEqual(len(self.parcels_of("home")), 1)
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1]["received"], 4)
        (self.machines["office"]["data"] / parcels.KEEPING).unlink()
        self.assertEqual(self.build("office"), ((0, 0), counts()))

    def test_a_lost_or_cut_list_is_written_anew_and_a_lost_parcel_given_again(self):
        self.shelf()
        self.build("home")
        listing = self.shared / "texts" / f"{self.writer('home')}.json"
        for spoil in (lambda: listing.unlink(), lambda: listing.write_text(listing.read_text()[:20])):
            with self.subTest(spoil=spoil):
                spoil()
                self.assertEqual(self.build("home")[1]["sent"], 0)
                self.assertEqual(len(self.listed("home")["parcels"]), 1)
        self.parcels_of("home")[0].unlink()             # the parcel itself is lost: its documents are given again,
        self.assertEqual(self.build("home")[1]["sent"], 4)
        self.assertEqual([parcel["name"][-15:] for parcel in self.listed("home")["parcels"]],
                         ["000001.jsonl.gz", "000002.jsonl.gz"])  # and it stays named for a while, as it may come back
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1], counts(received=4, damaged=1))

    def test_a_list_that_names_a_parcel_wrongly_is_written_anew(self):
        self.shelf()
        self.build("home")
        listing = self.shared / "texts" / f"{self.writer('home')}.json"
        told = json.loads(listing.read_text())
        true = told["parcels"][0]["sha256"]
        told["parcels"][0]["sha256"] = "0" * 64
        listing.write_text(json.dumps(told))
        self.assertEqual(self.build("home")[1]["sent"], 0)
        self.assertEqual(self.listed("home")["parcels"][0]["sha256"], true)
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1], counts(received=4))

    def test_what_a_writing_cut_short_left_is_removed_by_its_writer_alone(self):
        self.shelf()
        self.build("home")
        mine, theirs = self.writer("home"), "f" * 16
        left = [self.shared / "texts" / f"{mine}.json.arriving.4242", self.shared / "texts" / mine / "000007.jsonl.gz.arriving.17",
                self.shared / "texts" / f"{theirs}.json.arriving.99"]
        for path in left:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"cut short")
        self.build("home")
        self.assertEqual([path.exists() for path in left], [False, False, True])

    def test_a_parcel_late_to_arrive_keeps_its_number(self):
        self.shelf()
        self.build("home")
        parcel = self.parcels_of("home")[0]
        whole = parcel.read_bytes()
        parcel.unlink()                                 # not yet carried back by a folder that synchronises,
        for path in self.machines["home"]["data"].glob(parcels.KEEPING + "*"):
            path.unlink()                               # and the bookkeeping lost
        self.write("Waves/currents.txt", "Currents of the narrow straits. " * 300)
        self.assertEqual(self.build("home")[1]["sent"], 5)
        self.assertEqual([path.name for path in self.parcels_of("home")], ["000002.jsonl.gz"])
        parcel.write_bytes(whole)                       # carried at last: its name kept its meaning
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1], counts(received=5))

    def test_a_parcel_of_its_own_that_cannot_be_read_yet_stays_named(self):
        self.shelf()
        self.build("home")
        before = self.listed("home")["parcels"]
        parcel = self.parcels_of("home")[0]
        whole = parcel.read_bytes()
        parcel.write_bytes(whole[:-5])                  # still arriving when the bookkeeping was lost
        for path in self.machines["home"]["data"].glob(parcels.KEEPING + "*"):
            path.unlink()
        self.assertTrue(self.exchange("home").run("receive"))
        self.assertEqual(self.listed("home")["parcels"], before)
        parcel.write_bytes(whole)
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1], counts(received=4))
        self.assertEqual(self.build("home")[1], counts())  # taken back, and nothing given twice

    def test_a_parcel_of_its_own_changed_in_the_shared_place_is_given_again(self):
        self.shelf()
        self.build("home")
        self.assertTrue(self.exchange("home").run("receive"))   # read back once, and found whole
        parcel = self.parcels_of("home")[0]
        spoilt, before = bytearray(parcel.read_bytes()), parcel.stat()
        spoilt[len(spoilt) // 2] ^= 0xFF
        parcel.write_bytes(bytes(spoilt))
        os.utime(parcel, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        told = self.exchange("home")
        self.assertTrue(told.run("receive", "send"))
        self.assertEqual(told.counts["sent"], 4)        # given again; the changed one is kept, for it may be put right
        self.assertEqual([path.name for path in self.parcels_of("home")], ["000001.jsonl.gz", "000002.jsonl.gz"])
        self.assertEqual(parcel.read_bytes(), bytes(spoilt))
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1], counts(received=4, damaged=1))

    def test_a_parcel_of_its_own_touched_with_the_same_bytes_stays_named(self):
        self.shelf()
        self.build("home")
        self.assertTrue(self.exchange("home").run("receive"))
        parcel = self.parcels_of("home")[0]
        before = parcel.stat()
        os.utime(parcel, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        place = Counting(self.shared)
        told = self.exchange("home", place)
        self.assertTrue(told.run("receive", "send"))
        self.assertEqual((told.counts["sent"], place.fetched, [path.name for path in self.parcels_of("home")]),
                         (0, 1, ["000001.jsonl.gz"]))
        told = self.exchange("home", place)
        self.assertTrue(told.run("receive", "send"))
        self.assertEqual(place.fetched, 1)              # known again by its size and date: not read again

    def test_a_parcel_of_its_own_of_a_newer_format_stays_named(self):
        self.shelf(on=("home",))
        with patch.object(parcels, "FORMAT", 2):        # written by a newer ShelfFind of this data folder
            self.gift(self.writer("home"), [self.line("Waves/tides.txt", [[None, None, TEXTS["Waves/tides.txt"], 0]])])
        before = self.listed("home")["parcels"]
        told = self.exchange("home")
        for _ in range(3):                              # exchange after exchange
            self.assertTrue(told.run("receive"))
            self.assertEqual(self.listed("home")["parcels"], before)
        self.assertEqual(told.counts["newer"], 1)

    def test_a_list_of_its_own_written_by_a_newer_version_is_left_to_it(self):
        self.shelf()
        self.build("home")
        listing = self.shared / "texts" / f"{self.writer('home')}.json"
        told = json.loads(listing.read_text())
        listing.write_text(json.dumps({**told, "format": 2}))
        kept = listing.read_bytes()
        self.write("Waves/currents.txt", "Currents of the narrow straits. " * 300)
        said = []
        machine_ = self.machines["home"]
        with self.at("home"):
            exchange = parcels.Exchange(machine_["data"], machine_["db"], self.place(), said.append)
            indexer.build(machine_["db"], machine_["data"], store.roots(machine_["db"]), progress=lambda _: None, exchange=exchange)
        self.assertEqual((listing.read_bytes(), len(self.parcels_of("home")), exchange.counts["sent"]), (kept, 1, 0))
        self.assertTrue(any("newer ShelfFind" in line for line in said))
        self.assertEqual(self.build("office", limit=0)[1], counts(newer=1))    # the others wait for that version too

    def test_a_parcel_that_holds_other_documents_than_its_list_says_is_not_taken(self):
        self.shelf()
        self.build("home")
        listing = self.shared / "texts" / f"{self.writer('home')}.json"
        told = json.loads(listing.read_text())
        told["parcels"][0]["documents"] = 5
        listing.write_text(json.dumps(told))
        self.assertEqual(self.build("office", limit=0)[1], counts(damaged=1))

    def test_a_reading_held_back_for_an_offer_is_given_once_its_writer_withdraws_it(self):
        self.shelf()
        self.build("home")
        office = self.machines["office"]
        with self.at("office"):                         # the office reads for itself,
            indexer.build(office["db"], office["data"], store.roots(office["db"]), progress=lambda _: None)
        self.assertTrue(self.exchange("office").run("receive", "send"))
        self.assertEqual(self.parcels_of("office"), [])  # and gives nothing that home offers already
        for parcel in self.parcels_of("home"):
            parcel.unlink()
        (self.shared / "texts" / f"{self.writer('home')}.json").write_text(
            json.dumps({"format": 1, "writer": self.writer("home"), "parcels": []}))     # home withdraws its texts
        told = self.exchange("office")
        self.assertTrue(told.run("receive", "send"))
        self.assertEqual(told.counts["sent"], 4)
        self.add("third")
        self.shelf(on=("third",))
        self.forbidden.add("third")
        self.assertEqual(self.build("third")[1], counts(received=4))

    def test_two_first_starts_of_one_data_folder_agree_on_its_writer(self):
        data, found, real = self.base / "fresh-state", [], os.urandom

        def slow(count):
            time.sleep(0.05)                            # the moment at which two starts would part
            return real(count)

        with self.at("home"), patch("shelffind.machine.os.urandom", side_effect=slow):
            starts = [threading.Thread(target=lambda: found.append(machine.identity(data)["writer"])) for _ in range(6)]
            for start in starts:
                start.start()
            for start in starts:
                start.join()
        self.assertEqual((len(found), len(set(found))), (6, 1))

    def test_the_writer_outlives_an_earlier_version_that_rewrites_the_machine(self):
        writer = self.writer("home")
        path = self.machines["home"]["data"] / machine.NAME
        told = json.loads(path.read_text())
        path.write_text(json.dumps({key: told[key] for key in ("id", "name", "system")}))     # as version 0.3 writes it
        self.assertEqual(self.writer("home"), writer)

    def test_another_shared_place_receives_everything_again(self):
        self.shelf()
        self.build("home")
        other = self.base / "other-shared"
        other.mkdir()
        self.assertEqual(self.build("home", sharing.Folder(other))[1]["sent"], 4)
        self.forbidden.add("office")
        self.assertEqual(self.build("office", sharing.Folder(other))[1]["received"], 4)

    def test_what_a_list_names_wrongly_is_left_aside(self):
        self.shelf()
        self.build("home")
        listing = self.shared / "texts" / f"{self.writer('home')}.json"
        told = json.loads(listing.read_text())
        told["parcels"] += [{"name": "../../outside.jsonl.gz", "size": 10, "sha256": "0" * 64, "documents": 1},
                            {"name": f"texts/{'f' * 16}/000001.jsonl.gz", "size": 10, "sha256": "0" * 64, "documents": 1},
                            "not a parcel", {"name": told["parcels"][0]["name"], "size": 1 << 62, "sha256": "0" * 64}]
        listing.write_text(json.dumps(told))
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1], counts(received=4, damaged=4))   # each entry left aside is said

    def test_what_cannot_be_read_is_left_aside_and_the_rest_taken(self):
        self.shelf(on=("office",))
        self.measure("office")
        good = self.line("Waves/tides.txt", [[None, None, TEXTS["Waves/tides.txt"], 0]])
        lone = json.dumps({**self.line("Waves/winds.txt", []), "pages": [[None, None, "x", 0]]}).replace('"x"', '"\\ud800"')
        deep = b"[" * 100_000 + b"]" * 100_000 + b"\n"
        large = dict(self.line("Charts/stars.txt", [[None, None, TEXTS["Charts/stars.txt"], 0]]), size=1 << 62)
        self.gift("c" * 16, [good, lone.encode() + b"\n", deep, large])
        (self.shared / "texts" / ("d" * 16 + ".json")).write_text('{"writer": "' + "d" * 16 + '", "parcels": 5}')
        (self.shared / "texts" / ("e" * 16 + ".json")).write_text("[" * 100_000)
        told = self.exchange("office")
        self.assertTrue(told.run("receive", "apply"))
        self.assertEqual((told.counts["received"], told.counts["skipped"], told.counts["damaged"]), (1, 3, 2))
        self.assertEqual(self.found("office", "northern harbours"), ["tides.txt"])

    def test_two_data_folders_of_one_machine_never_write_the_same_file(self):
        self.add("home-two", system="home")             # the same machine, another data folder
        self.write("Waves/tides.txt", on=("home",))
        self.write("Waves/winds.txt", on=("home-two",))
        self.assertNotEqual(self.writer("home"), self.writer("home-two"))
        self.assertEqual(self.build("home")[1]["sent"], 1)
        self.assertEqual(self.build("home-two")[1]["sent"], 1)
        self.shelf(on=("office",))
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1]["received"], 2)
        self.assertEqual((self.found("office", "northern harbours"), self.found("office", "southern capes")),
                         (["tides.txt"], ["winds.txt"]))

    def test_a_parcel_put_whose_list_could_not_be_written_is_named_by_the_next_exchange(self):
        self.shelf()
        real, failing = sharing.Folder.put, [True]

        def put(place, name, data):
            if name.endswith(".json") and failing:
                failing.pop()
                raise OSError("the shared place gives no answer")
            return real(place, name, data)

        with patch.object(sharing.Folder, "put", put):
            self.assertEqual(self.build("home")[1]["sent"], 0)
        stray = self.parcels_of("home")[0]
        kept = stray.read_bytes()
        self.assertEqual(self.build("home")[1]["sent"], 4)      # named now, and not sent again
        self.assertEqual((stray.read_bytes(), [path.name for path in self.parcels_of("home")]), (kept, ["000001.jsonl.gz"]))
        self.assertEqual([parcel["name"][-15:] for parcel in self.listed("home")["parcels"]], ["000001.jsonl.gz"])
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1]["received"], 4)

    def test_a_parcel_whose_writing_failed_is_given_again_under_another_number(self):
        self.shelf()
        real, failing = sharing.Folder.put, [True]

        def put(place, name, data):
            if name.endswith(".jsonl.gz") and failing:
                failing.pop()
                raise OSError("the shared place gives no answer")
            return real(place, name, data)

        with patch.object(sharing.Folder, "put", put):
            self.assertEqual(self.build("home")[1]["sent"], 0)
        self.assertEqual(self.parcels_of("home"), [])
        self.assertEqual(self.build("home")[1]["sent"], 4)
        self.assertEqual([path.name for path in self.parcels_of("home")], ["000002.jsonl.gz"])   # the first number is not given again
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1]["received"], 4)

    def test_a_newer_shared_place_is_left_as_it_is(self):
        self.shelf()
        (self.shared / ".shelffind-shared").write_text('{"format": 2}')
        said = []
        machine_ = self.machines["home"]
        with self.at("home"):
            told = parcels.Exchange(machine_["data"], machine_["db"], self.place(), said.append)
            self.assertEqual(indexer.build(machine_["db"], machine_["data"], store.roots(machine_["db"]),
                                           progress=lambda _: None, exchange=told), (4, 0))
        self.assertFalse((self.shared / "texts").exists())
        self.assertTrue(any("newer ShelfFind" in line for line in said))


class Earlier(Two):
    def test_pages_that_an_earlier_version_read_stay_on_their_machine(self):
        self.shelf()
        home = self.machines["home"]
        with self.at("home"):                           # read, and nothing shared
            indexer.build(home["db"], home["data"], store.roots(home["db"]), progress=lambda _: None)
        with home["db"]:                                # as an earlier version left them: tied to no bytes
            home["db"].execute("DELETE FROM doc_keys")
            home["db"].execute("UPDATE files SET sum=NULL,read=1")
            home["db"].execute("UPDATE docs SET origin=NULL")
        self.read["home"].clear()
        self.assertEqual(self.build("home"), ((0, 0), counts()))     # measured, tied to the bytes, and not given
        origins = [row[0] for row in home["db"].execute("SELECT origin FROM docs")]
        self.assertTrue(origins and all(origin.split()[-1] == store.INHERITED for origin in origins), origins)
        self.assertEqual((self.read["home"], self.parcels_of("home")), ([], []))
        self.sound("home")


    def test_readings_that_an_earlier_version_made_stay_on_their_machine_whenever_it_made_them(self):
        self.shelf(on=("home",))
        home = self.machines["home"]
        with self.at("home"):                           # read, then taken for what version 0.3 left: unsealed
            indexer.build(home["db"], home["data"], store.roots(home["db"]), progress=lambda _: None)
        with home["db"]:
            home["db"].execute("UPDATE docs SET origin=rtrim(replace(origin,'sealed',''))")
        self.assertEqual(self.build("home")[1], counts())      # nothing given
        self.write("Waves/currents.txt", "Currents of the narrow straits. " * 300, on=("home",))
        self.assertEqual(self.build("home")[1], counts(sent=1))   # what this version reads travels
        self.sound("home")


class Third(Two):
    """What the third reviews of the exchange found."""

    def drop_bookkeeping(self, name):
        for path in self.machines[name]["data"].glob(parcels.KEEPING + "*"):
            path.unlink()

    def test_the_fingerprint_of_a_reading_is_one_in_sql_and_in_python_and_tells_readings_apart(self):
        db = self.machines["office"]["db"]
        self.exchange("office")                         # which gives the index what the fingerprint needs
        with db:
            doc = store.new_document(db, 9, "epub")
            db.executemany("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,?,?,?,?)",
                           [(doc, None, "one.xhtml", store.deflate("One"), 0), (doc, None, "two.xhtml", store.deflate(""), 1),
                            (doc, 3, None, store.deflate(""), 1), (doc, 4, None, store.deflate("Four"), 0)])

        def both():
            rows = db.execute("SELECT id,page,section,ztext,needs_ocr FROM pages WHERE doc=? ORDER BY id", (doc,)).fetchall()
            sql = db.execute(f"SELECT reading FROM ({parcels.READING}) WHERE doc=?", (doc,)).fetchone()[0]
            self.assertEqual(sql, parcels.reading_of(rows))
            return sql

        seen = [both()]
        with db:                                        # the pages recognised elsewhere, in the same rows
            db.execute("UPDATE pages SET needs_ocr=1-needs_ocr,ztext=CASE WHEN needs_ocr=1 THEN ? ELSE ? END WHERE doc=?",
                       (store.deflate("Some"), store.deflate(""), doc))
        seen.append(both())
        with db:                                        # chapters named otherwise, with names as long
            db.execute("UPDATE pages SET section=replace(section,'one','six') WHERE doc=?", (doc,))
        seen.append(both())
        with db:                                        # a text of another length on one page
            db.execute("UPDATE pages SET ztext=? WHERE doc=? AND page=4", (store.deflate("Fourteen"), doc))
        seen.append(both())
        self.assertEqual(len(set(seen)), 4)

    def test_a_document_that_learning_leaves_without_a_file_takes_nothing_and_the_run_ends(self):
        self.write("a.txt", "Amber coast.")
        self.write("c.txt", "Olive coast.", on=("office",))
        self.build("home")
        office = self.machines["office"]
        db = office["db"]
        with self.at("office"):
            indexer.build(db, office["data"], store.roots(db), names_only=True, progress=lambda _: None)
        rows = {row["rel"]: row for row in db.execute("SELECT * FROM files")}
        amber = indexer.whole_checksums(office["shelf"] / "a.txt", rows["a.txt"])
        olive = "sha256:" + hashlib.sha256(b"Olive coast.").hexdigest()
        with db:                                        # the storage gives the MD5 of the amber, and a catalogue
            store.measure(db, rows["a.txt"]["id"], amber[:1], local=False)  # says that the olive has it too
            self.assertTrue(link.say(db, rows["c.txt"]["id"], "catalog", {"id": "guess", "keys": [amber[0], olive], "text": False}))
        (office["shelf"] / "a.txt").unlink()            # the measured file goes: the olive alone is left in the document
        with self.at("office"):
            indexer.build(db, office["data"], store.roots(db), names_only=True, progress=lambda _: None)
        self.forbidden.add("office")
        for _ in range(2):                              # the SHA-256 offered refutes the catalogue: nothing is given
            self.assertEqual(self.build("office", names_only=True)[0], (0, 0))
        self.sound("office")

    def test_a_list_that_cannot_be_fetched_stops_the_exchange_and_stays_as_it_was(self):
        self.shelf()
        self.build("home")
        listing = self.shared / "texts" / f"{self.writer('home')}.json"
        kept = listing.read_bytes()
        self.drop_bookkeeping("home")
        real = sharing.Folder.get

        def get(place, name, limit=None):
            if name.startswith("texts/") and name.endswith(".json"):
                raise ValueError("rclone: ERROR : read: input/output error")    # as a remote that fails says it
            return real(place, name, limit)

        with patch.object(sharing.Folder, "get", get):
            self.assertFalse(self.exchange("home").run("receive", "send"))
            self.assertFalse(self.exchange("office").run("receive"))
        self.assertEqual(listing.read_bytes(), kept)
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1], counts(received=4))

    def test_a_sync_of_texts_alone_writes_no_card_of_the_machine(self):
        self.shelf()
        data = self.machines["home"]["data"]
        (data / "config.toml").write_text(f'[share]\ntexts = true\nfolder = "{self.shared}"\n')
        with self.at("home"):
            self.assertEqual(run_main(data, "index")[0], 0)
            self.assertEqual(run_main(data, "sync")[0], 0)
        self.assertEqual(sorted((self.shared / "machines").glob("*")) if (self.shared / "machines").exists() else [], [])

    def test_a_card_that_cannot_be_fetched_stops_the_sync_and_stays_as_it_was(self):
        data = self.machines["home"]["data"]
        (data / "config.toml").write_text(f'[share]\nhistory = true\nfolder = "{self.shared}"\n')
        with self.at("home"):
            self.assertEqual(run_main(data, "sync")[0], 0)
        card = next((self.shared / "machines").glob("*.json"))
        kept = card.read_bytes()
        real = sharing.Folder.get

        def get(place, name, limit=None):
            if name.startswith("machines/"):
                raise ValueError("rclone: ERROR : read: input/output error")    # as a remote that fails says it
            return real(place, name, limit)

        with self.at("home"), patch.object(sharing.Folder, "get", get):
            self.assertNotEqual(run_main(data, "machine", "--name", "elsewhere")[0], 2)
            self.assertNotEqual(run_main(data, "sync")[0], 0)
        self.assertEqual(card.read_bytes(), kept)

    def test_a_parcel_of_its_own_not_yet_arrived_stays_named_when_its_documents_are_gone(self):
        self.shelf()
        self.build("home")
        parcel = self.parcels_of("home")[0]
        whole = parcel.read_bytes()
        parcel.unlink()                                 # not yet carried by a folder that synchronises,
        self.drop_bookkeeping("home")                   # the bookkeeping lost,
        with self.at("home"):                           # and the documents gone from this machine
            store.remove_root(self.machines["home"]["db"], "Shelf")
        self.assertTrue(self.exchange("home").run("receive", "send"))
        self.assertEqual(len(self.listed("home")["parcels"]), 1)       # still named
        parcel.write_bytes(whole)                       # carried at last
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1], counts(received=4))
        self.assertTrue(self.exchange("home").run("receive"))
        self.assertEqual(len(self.listed("home")["parcels"]), 1)

    def test_a_parcel_of_its_own_awaited_too_long_is_no_longer_named(self):
        self.shelf()
        self.build("home")
        self.parcels_of("home")[0].unlink()
        self.drop_bookkeeping("home")
        with self.at("home"):
            store.remove_root(self.machines["home"]["db"], "Shelf")
        self.assertTrue(self.exchange("home").run("receive"))
        later = time.time() + parcels.AWAIT + 60
        with patch("shelffind.parcels.time.time", return_value=later):
            self.assertTrue(self.exchange("home").run("receive"))
            self.assertEqual(len(self.listed("home")["parcels"]), 1)       # a clock put forward is not enough
            with patch.object(parcels, "AWAITED", 2):
                self.assertTrue(self.exchange("home").run("receive"))
        self.assertEqual(self.listed("home")["parcels"], [])

    def test_the_greatest_number_seen_is_kept_when_the_list_is_lost_afterwards(self):
        self.shelf()
        self.build("home")
        parcel = self.parcels_of("home")[0]
        whole = parcel.read_bytes()
        parcel.unlink()
        self.drop_bookkeeping("home")
        home = self.machines["home"]
        with self.at("home"):
            store.remove_root(home["db"], "Shelf")
        self.assertTrue(self.exchange("home").run("receive"))         # the list says 1: it is kept
        (self.shared / "texts" / f"{self.writer('home')}.json").unlink()   # and then the list is lost
        with self.at("home"):
            store.add_root(home["db"], home["shelf"], home["data"])
        self.write("Waves/currents.txt", "Currents of the narrow straits. " * 300, on=("home",))
        self.build("home")
        self.assertNotIn("000001.jsonl.gz", [path.name for path in self.parcels_of("home")])
        parcel.write_bytes(whole)                       # the first, carried late, keeps its meaning
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1]["damaged"], 0)

    def test_what_another_writer_of_this_folder_was_sending_does_not_hold_this_one_back(self):
        self.shelf()
        real, failing = sharing.Folder.put, [True]

        def put(place, name, data):
            if name.endswith(".json") and failing:
                failing.pop()
                raise OSError("the shared place gives no answer")
            return real(place, name, data)

        with patch.object(sharing.Folder, "put", put):
            self.assertEqual(self.build("home")[1]["sent"], 0)
        (self.machines["home"]["data"] / machine.WRITING).unlink()     # the folder restored elsewhere: another writer
        self.assertEqual(self.build("home")[1]["sent"], 4)
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1]["received"], 4)

    def test_a_look_at_the_offers_never_remakes_the_bookkeeping_of_another_place(self):
        self.shelf()
        self.build("home")
        self.build("office", limit=0)
        told = self.exchange("office", sharing.Folder(self.base / "elsewhere"))
        self.assertEqual(told.sizes(), frozenset())
        with contextlib.closing(sqlite3.connect(self.machines["office"]["data"] / parcels.KEEPING)) as db:
            self.assertEqual(db.execute("SELECT value FROM notes WHERE key='place'").fetchone()[0], self.place().identity)

    def test_a_list_of_its_own_in_a_newer_form_is_left_with_its_passing_files(self):
        self.shelf()
        self.build("home")
        writer = self.writer("home")
        listing = self.shared / "texts" / f"{writer}.json"
        listing.write_text(json.dumps({"format": 2, "writer": writer, "last": 11, "objects": []}))
        kept = listing.read_bytes()
        passing = self.shared / "texts" / writer / "000012.jsonl.gz.arriving.77"
        passing.write_bytes(b"being written by the newer version")
        self.assertTrue(self.exchange("home").run("receive", "send"))
        self.assertEqual((listing.read_bytes(), passing.exists()), (kept, True))

    def test_a_mark_raised_once_a_parcel_is_put_keeps_its_list_from_being_written(self):
        self.shelf()
        real = sharing.Folder.put

        def put(place, name, data):
            real(place, name, data)
            if name.endswith(".jsonl.gz"):
                (self.shared / ".shelffind-shared").write_text('{"format": 2}')

        with patch.object(sharing.Folder, "put", put):
            self.assertEqual(self.build("home")[1]["sent"], 0)
        self.assertFalse((self.shared / "texts" / f"{self.writer('home')}.json").exists())

    @unittest.skipIf(os.geteuid() == 0, "the superuser reads any file")
    def test_nothing_is_given_under_a_writer_that_cannot_be_read(self):
        self.shelf()
        self.build("home")
        writer = self.writer("home")
        record = self.machines["home"]["data"] / machine.WRITING
        record.chmod(0)
        try:
            self.write("Waves/currents.txt", "Currents of the narrow straits. " * 300, on=("home",))
            self.assertEqual(self.build("home")[1]["sent"], 0)
        finally:
            record.chmod(0o600)
        self.assertEqual(self.writer("home"), writer)
        self.assertEqual(self.build("home")[1]["sent"], 1)

    def test_nothing_is_given_under_a_writer_that_could_not_be_kept(self):
        self.shelf()
        real = machine.write

        def write(directory, told, name=machine.NAME):
            if name == machine.WRITING:
                raise OSError("No space left on device")
            return real(directory, told, name)

        with patch.object(machine, "write", write):
            self.assertEqual(self.build("home")[1]["sent"], 0)
        self.assertEqual(sorted((self.shared / "texts").glob("*")) if (self.shared / "texts").exists() else [], [])

    def test_a_file_that_changes_while_its_pages_come_takes_none(self):
        self.shelf()
        self.build("home")
        office = self.machines["office"]
        self.measure("office")
        real = parcels.Exchange.fetch

        def fetch(exchange, parcel):
            os.utime(office["shelf"] / "Waves/tides.txt", (WHEN + 5, WHEN + 5))   # rewritten meanwhile
            return real(exchange, parcel)

        self.forbidden.add("office")
        with patch.object(parcels.Exchange, "fetch", fetch):
            self.assertEqual(self.build("office", names_only=True)[1], counts(received=3))
        self.assertEqual(self.pages("office", "Waves/tides.txt"), [])

    def test_a_copied_file_rewritten_in_place_with_its_date_takes_none(self):
        self.shelf()
        self.build("home")
        office = self.machines["office"]
        real = parcels.Exchange.offer

        def offer(exchange, doc, file=None, still=None):
            held = real(exchange, doc, file, still)
            rel = office["db"].execute("SELECT rel FROM files WHERE id=?", (file,)).fetchone()[0]
            if rel == "Waves/tides.txt":
                path = office["shelf"] / rel
                info = path.stat()
                with path.open("r+b") as handle:        # the same size, in the same place on disk,
                    handle.write(b"X")
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))    # and the same date
            return held

        with patch.object(parcels.Exchange, "sizes", return_value=frozenset()), patch.object(parcels.Exchange, "offer", offer):
            self.assertEqual(self.build("office")[1]["received"], 3)
        self.assertTrue(self.pages("office", "Waves/tides.txt")[0][1].startswith("Xide"))   # read here, as it is now

    def test_a_repair_that_would_make_the_list_too_large_leaves_it_as_it_was(self):
        self.shelf()
        with patch.object(parcels, "LIMIT", 400):
            self.build("home")
        writer = self.writer("home")
        listing = self.shared / "texts" / f"{writer}.json"
        compact = json.dumps({"format": 1, "writer": writer, "parcels": json.loads(listing.read_text())["parcels"]},
                             separators=(",", ":")).encode()           # the list as an older form wrote it
        listing.write_bytes(compact)
        with patch.object(parcels, "LISTED", len(compact)):
            self.assertTrue(self.exchange("home").run("receive"))
            self.assertEqual(listing.read_bytes(), compact)
            self.forbidden.add("office")
            self.assertEqual(self.build("office")[1], counts(received=4))

    def test_a_parcel_whose_list_changes_its_number_of_documents_is_not_used(self):
        self.shelf()
        self.build("home")
        self.assertTrue(self.exchange("office").run("receive"))      # its offers are known
        listing = self.shared / "texts" / f"{self.writer('home')}.json"
        told = json.loads(listing.read_text())
        told["parcels"][0]["documents"] = 5
        listing.write_text(json.dumps(told))
        self.assertEqual(self.build("office", limit=0)[1], counts(damaged=1))

    def test_a_list_of_its_own_with_an_entry_that_no_reader_takes_is_written_anew(self):
        self.shelf()
        self.build("home")
        listing = self.shared / "texts" / f"{self.writer('home')}.json"
        told = json.loads(listing.read_text())
        told["parcels"].append("not a parcel")
        listing.write_text(json.dumps(told))
        self.assertTrue(self.exchange("home").run("receive"))
        self.assertNotIn("not a parcel", listing.read_text())
        self.assertEqual(self.build("office", limit=0)[1]["damaged"], 0)

    def test_the_sources_are_looked_at_anew_after_each_round_of_questions(self):
        told = self.exchange("office")
        with patch.object(parcels, "holding", side_effect=[set(), {"library"}]):
            self.assertEqual((told.holders(), told.holders()), (set(), set()))
            told.settled()
            self.assertEqual(told.holders(), {"library"})


class Fourth(Two):
    """What the fourth reviews of the exchange found."""

    def drop_bookkeeping(self, name):
        for path in self.machines[name]["data"].glob(parcels.KEEPING + "*"):
            path.unlink()

    def rewritten(self, rel, text):
        """A file of the office rewritten in place, with other bytes of the same length and its date."""
        path = self.machines["office"]["shelf"] / rel
        info = path.stat()
        path.write_text(text)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))

    def test_a_file_rewritten_with_its_date_while_its_pages_come_takes_none(self):
        self.shelf()
        self.build("home")
        self.measure("office")
        real, done = parcels.Exchange.fetch, []

        def fetch(exchange, parcel):
            if not done:
                done.append(True)
                self.rewritten("Waves/tides.txt", TEXTS["Waves/tides.txt"].replace("northern", "southern"))
            return real(exchange, parcel)

        self.forbidden.add("office")
        with patch.object(parcels.Exchange, "fetch", fetch):
            self.assertEqual(self.build("office", names_only=True)[1], counts(received=3))
        self.assertEqual(self.pages("office", "Waves/tides.txt"), [])

    def test_a_file_made_a_link_while_its_pages_come_takes_none(self):
        self.shelf()
        self.build("home")
        self.measure("office")
        real, done = parcels.Exchange.fetch, []
        shelf = self.machines["office"]["shelf"]

        def fetch(exchange, parcel):
            if not done:
                done.append(True)
                twin = self.base / "elsewhere.txt"      # the same size and date, behind a link
                twin.write_text(TEXTS["Waves/tides.txt"].replace("northern", "southern"))
                info = (shelf / "Waves/tides.txt").stat()
                os.utime(twin, ns=(info.st_atime_ns, info.st_mtime_ns))
                (shelf / "Waves/tides.txt").unlink()
                (shelf / "Waves/tides.txt").symlink_to(twin)
            return real(exchange, parcel)

        self.forbidden.add("office")
        with patch.object(parcels.Exchange, "fetch", fetch):
            self.assertEqual(self.build("office", names_only=True)[1], counts(received=3))

    def test_a_parcel_read_back_short_once_stays_named_and_is_not_deleted(self):
        self.shelf()
        self.build("home")
        parcel = self.parcels_of("home")[0]
        whole = parcel.read_bytes()
        before = self.listed("home")["parcels"]
        with self.at("home"):                           # put, and read back at the next exchange;
            store.remove_root(self.machines["home"]["db"], "Shelf")      # its documents gone from this machine
        real, cut = sharing.Folder.get, []

        def get(place, name, limit=None):
            if name.endswith(".jsonl.gz") and not cut:
                cut.append(True)
                return whole[:-5]                       # one reading comes short
            return real(place, name, limit)

        with patch.object(sharing.Folder, "get", get):
            self.assertTrue(self.exchange("home").run("receive", "send"))
        self.assertEqual((parcel.read_bytes(), self.listed("home")["parcels"]), (whole, before))
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1], counts(received=4))

    def test_a_parcel_missing_for_a_moment_stays_named_and_is_taken_again_when_back(self):
        self.shelf()
        self.build("home")
        self.assertTrue(self.exchange("home").run("receive"))         # read back, and known
        parcel = self.parcels_of("home")[0]
        whole = parcel.read_bytes()
        parcel.unlink()                                 # hidden for a moment by a folder that synchronises
        with self.at("home"):
            store.remove_root(self.machines["home"]["db"], "Shelf")
        self.assertTrue(self.exchange("home").run("receive", "send"))
        self.assertEqual(len(self.listed("home")["parcels"]), 1)
        parcel.write_bytes(whole)                       # and back
        self.assertTrue(self.exchange("home").run("receive", "send"))
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1], counts(received=4))

    def test_an_awaited_parcel_takes_the_description_of_the_list_as_it_is_now(self):
        self.shelf()
        self.build("home")
        listing = self.shared / "texts" / f"{self.writer('home')}.json"
        right = listing.read_bytes()
        parcel = self.parcels_of("home")[0]
        whole = parcel.read_bytes()
        parcel.unlink()
        self.drop_bookkeeping("home")
        with self.at("home"):
            store.remove_root(self.machines["home"]["db"], "Shelf")
        wrong = json.loads(right)
        wrong["parcels"][0]["sha256"] = "0" * 64
        listing.write_text(json.dumps(wrong))
        self.assertTrue(self.exchange("home").run("receive"))         # awaited, as the list wrongly says
        parcel.write_bytes(whole)                       # the parcel arrives, and the list as it should be
        listing.write_bytes(right)
        self.assertTrue(self.exchange("home").run("receive"))
        self.assertEqual(self.listed("home")["parcels"][0]["sha256"], json.loads(right)["parcels"][0]["sha256"])
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1], counts(received=4))

    def test_a_number_named_in_an_entry_left_aside_is_not_given_again(self):
        self.shelf()
        self.build("home")
        writer = self.writer("home")
        listing = self.shared / "texts" / f"{writer}.json"
        told = json.loads(listing.read_text())
        told["parcels"][0]["documents"] = -1            # its number recognisable, the rest wrong
        listing.write_text(json.dumps({"format": 1, "writer": writer, "parcels": told["parcels"]}))
        parcel = self.parcels_of("home")[0]
        whole = parcel.read_bytes()
        parcel.unlink()
        self.drop_bookkeeping("home")
        self.write("Waves/currents.txt", "Currents of the narrow straits. " * 300)
        self.build("home")
        self.assertNotIn("000001.jsonl.gz", [path.name for path in self.parcels_of("home")])
        parcel.write_bytes(whole)
        self.assertEqual(parcel.read_bytes(), whole)

    def test_a_shared_place_that_fails_while_naming_something_not_found_is_no_absence(self):
        tools = self.base / "bin"
        tools.mkdir()
        (tools / "rclone-itself").write_text(RCLONE)
        (tools / "rclone-itself").chmod(0o700)
        (tools / "rclone").write_text('#!/bin/sh\nif [ "$1" = cat ]; then case "$*" in *.shelffind-shared*) '
                                      'echo "ERROR : credential profile not found" >&2; exit 1;; esac; fi\n'
                                      f'exec "{tools / "rclone-itself"}" "$@"\n')
        (tools / "rclone").chmod(0o700)
        (self.shared / ".shelffind-shared").write_text('{"format": 2}')
        self.shelf()
        data = self.machines["home"]["data"]
        (data / "config.toml").write_text('[share]\ntexts = true\nremote = "pretend:shared"\n')
        with self.at("home"), patch.dict(os.environ, {"PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
                                                      "PRETENDED_REMOTE": str(self.base)}):
            self.assertEqual(run_main(data, "index")[0], 0)
            self.assertNotEqual(run_main(data, "sync")[0], 0)
        self.assertEqual((self.shared / ".shelffind-shared").read_text(), '{"format": 2}')
        self.assertFalse((self.shared / "texts").exists())

    def test_a_list_of_files_that_says_nothing_stops_the_exchange(self):
        tools = self.base / "bin"
        tools.mkdir()
        for reply in ("", "{}", "[{}]", "null", '[{"Name": "000001.jsonl.gz"}]'):
            with self.subTest(reply=reply):
                (tools / "rclone").write_text(f"#!/bin/sh\nprintf '%s' '{reply}'\n")
                (tools / "rclone").chmod(0o700)
                with self.assertRaises(ValueError):
                    sharing.Rclone("pretend:shared", program=str(tools / "rclone")).stamps("texts/abc")

    def test_a_newer_mark_raised_during_a_sync_of_journals_keeps_them_from_being_written(self):
        data = self.machines["home"]["data"]
        (data / "config.toml").write_text(f'[share]\nhistory = true\nfolder = "{self.shared}"\n')
        with self.at("home"):
            self.assertEqual(run_main(data, "sync")[0], 0)
            history.record(data, "search", query="tide tables")
        card = next((self.shared / "machines").glob("*.json"))
        kept = card.read_bytes()
        real = sharing.Folder.get

        def get(place, name, limit=None):
            told = real(place, name, limit)
            if name.startswith("machines/"):
                (self.shared / ".shelffind-shared").write_text('{"format": 2}')
            return told

        with self.at("home"), patch.object(sharing.Folder, "get", get):
            self.assertNotEqual(run_main(data, "sync")[0], 0)
        self.assertEqual((card.read_bytes(), sorted((self.shared / "history").rglob("*.jsonl"))), (kept, []))

    def test_a_card_whose_count_of_lines_is_not_a_number_is_written_anew(self):
        data = self.machines["home"]["data"]
        (data / "config.toml").write_text(f'[share]\nhistory = true\nfolder = "{self.shared}"\n')
        with self.at("home"):
            history.record(data, "search", query="tide tables")
            self.assertEqual(run_main(data, "sync")[0], 0)
        card = next((self.shared / "machines").glob("*.json"))
        told = json.loads(card.read_text())
        for month in told["journals"]:
            told["journals"][month]["lines"] = "1"
        card.write_text(json.dumps(told))
        with self.at("home"):
            history.record(data, "search", query="wind roses")
            self.assertEqual(run_main(data, "sync")[0], 0)

    def test_complementary_readings_of_sixteen_pages_are_told_apart(self):
        rows = lambda blanks: [{"id": 100 + index, "page": index + 1, "section": None, "needs_ocr": int(index in blanks),
                                "ztext": store.deflate("" if index in blanks else f"Tidal sounding {index + 1:02d}.")}
                               for index in range(16)]
        seen = {parcels.reading_of(rows(set(blanks))) for blanks in itertools.combinations(range(16), 8)}
        self.assertEqual(len(seen), 12870)
        one = [{"id": 1, "page": 1, "section": None, "needs_ocr": 0, "ztext": b"x"}]
        self.assertNotEqual(parcels.reading_of(one), parcels.reading_of([{**one[0], "section": ""}]))

    def test_the_fingerprint_of_a_page_numbered_far_is_one_in_sql_and_in_python(self):
        db = self.machines["office"]["db"]
        self.exchange("office")
        with db:
            doc = store.new_document(db, 9, "pdf")
            db.execute("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,?,NULL,?,0)", (doc, 1 << 48, store.deflate("Far")))
        rows = db.execute("SELECT id,page,section,ztext,needs_ocr FROM pages WHERE doc=?", (doc,)).fetchall()
        self.assertEqual(db.execute(f"SELECT reading FROM ({parcels.READING}) WHERE doc=?", (doc,)).fetchone()[0],
                         parcels.reading_of(rows))

    def test_a_page_of_plain_text_still_to_recognise_is_refused(self):
        line = self.line("Waves/tides.txt", [[None, None, TEXTS["Waves/tides.txt"], 0], [None, "two", "", 1]])
        self.assertIsNone(parcels.document(json.dumps(line).encode()))
        self.assertIsNotNone(parcels.document(json.dumps({**line, "format": "pdf", "pages": [[1, None, "One", 0], [2, None, "", 1]]}).encode()))

    def test_the_offer_with_fewer_pages_to_recognise_comes_first(self):
        rel, body = "Waves/tides.pdf", TEXTS["Waves/tides.txt"]
        self.write(rel, body, on=("office",))
        self.measure("office")
        late = [[number, None, "", 1] if number in (101, 102) else [number, None, body, 0] for number in range(1, 111)]
        early = [[number, None, "", 1] if number in (1001, 1002, 1003) else [number, None, body, 0] for number in range(1001, 1111)]
        self.gift("a" * 16, [self.line("Waves/tides.txt", early, fmt="pdf")])     # three pages to recognise, at the start
        self.gift("b" * 16, [self.line("Waves/tides.txt", late, fmt="pdf")])      # two, further on
        self.forbidden.add("office")
        self.assertEqual(self.build("office", limit=0)[1]["received"], 1)
        self.assertEqual([page for page, _, blank in self.pages("office", rel) if blank], [101, 102])

    def test_a_second_offer_is_taken_in_the_same_run_when_the_first_is_withdrawn(self):
        self.shelf(on=("office",))
        self.measure("office")
        line = self.line("Waves/tides.txt", [[None, None, TEXTS["Waves/tides.txt"], 0]])
        first = self.gift("a" * 16, [line])
        self.gift("b" * 16, [line])
        self.assertTrue(self.exchange("office").run("receive"))      # both offers known
        (self.shared / first).unlink()                  # the first goes before it is used
        self.forbidden.add("office")
        self.assertEqual(self.build("office", limit=0)[1]["received"], 1)

    def test_lines_refused_under_former_limits_are_taken_under_the_new_ones(self):
        self.shelf()
        self.build("home")
        with patch.object(parcels, "PAGES", 0):         # a reader that takes fewer pages
            self.assertTrue(self.exchange("office").run("receive"))
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1], counts(received=4))

    def test_a_look_at_the_offers_makes_no_bookkeeping(self):
        told = self.exchange("office")
        self.assertEqual(told.sizes(), frozenset())
        self.assertFalse((self.machines["office"]["data"] / parcels.KEEPING).exists())

    def test_a_missing_copy_outside_the_folders_of_a_run_holds_nothing_back(self):
        self.shelf()
        self.build("home")
        office = self.machines["office"]
        other = self.base / "office" / "Elsewhere"
        other.mkdir(parents=True)
        (other / "tides.txt").write_text(TEXTS["Waves/tides.txt"])
        os.utime(other / "tides.txt", (WHEN, WHEN))
        db = office["db"]
        with self.at("office"):
            store.add_root(db, other, office["data"])
            indexer.build(db, office["data"], store.roots(db), names_only=True, progress=lambda _: None)
            folders = {root["id"]: Path(root["path"]) for root in store.roots(db)}
            for row in db.execute("SELECT f.id,f.root,f.rel,f.size,f.mtime FROM files f").fetchall():
                with db:                                # each copy measured where it is
                    indexer.joined(db, row["id"], indexer.whole_checksums(folders[row["root"]] / row["rel"], row))
        (other / "tides.txt").unlink()                  # a copy gone, in a folder the next run leaves aside
        self.forbidden.add("office")
        with self.at("office"):
            told = parcels.Exchange(office["data"], db, self.place())
            shelf = [root for root in store.roots(db) if root["name"] == "Shelf"]
            for _ in range(2):
                indexer.build(db, office["data"], shelf, progress=lambda _: None, exchange=told)
        self.assertEqual(told.counts["received"], 4)
        self.assertEqual(self.found("office", "northern harbours"), ["tides.txt"])


class Fifth(Two):
    """What the fifth reviews of the exchange found."""

    def test_a_parcel_of_its_own_of_a_newer_format_stays_named_beyond_any_waiting(self):
        self.shelf(on=("home",))
        with patch.object(parcels, "FORMAT", 2):        # written by a newer ShelfFind of this data folder
            self.gift(self.writer("home"), [self.line("Waves/tides.txt", [[None, None, TEXTS["Waves/tides.txt"], 0]])])
        before = self.listed("home")["parcels"]
        self.assertTrue(self.exchange("home").run("receive"))
        later = time.time() + parcels.AWAIT + 60       # longer than any waiting
        with patch("shelffind.parcels.time.time", return_value=later), patch.object(parcels, "AWAITED", 1):
            for _ in range(3):
                self.assertTrue(self.exchange("home").run("receive"))
        self.assertEqual(self.listed("home")["parcels"], before)

    def test_a_parcel_kept_from_a_round_serves_only_the_description_it_was_found_whole_for(self):
        self.shelf()
        self.build("home")
        listing = self.shared / "texts" / f"{self.writer('home')}.json"
        told = json.loads(listing.read_text())
        office = self.exchange("office")
        self.assertTrue(office.run("receive"))          # taken: found whole and kept
        told["parcels"][0]["size"] += 1                 # the list now says another size, with the same checksum
        listing.write_text(json.dumps(told))
        self.assertTrue(office.run("receive"))
        self.assertEqual(office.counts["damaged"], 1)

    def test_other_limits_compare_every_reading_again(self):
        self.shelf()
        self.build("home")
        office = self.machines["office"]
        with self.at("office"):                         # the office reads for itself, and gives nothing that home offers
            indexer.build(office["db"], office["data"], store.roots(office["db"]), progress=lambda _: None)
        self.assertTrue(self.exchange("office").run("receive", "send"))
        compared = lambda: sqlite3.connect(office["data"] / parcels.KEEPING).execute("SELECT count(*) FROM compared").fetchone()[0]
        self.assertEqual(compared(), 4)
        with patch.object(parcels, "PAGES", parcels.PAGES + 1):
            self.assertTrue(self.exchange("office").run("receive"))
        self.assertEqual(compared(), 0)

    def test_a_good_offer_behind_many_that_fail_is_taken_in_the_same_run(self):
        self.shelf(on=("office",))
        self.measure("office")
        line = self.line("Waves/tides.txt", [[None, None, TEXTS["Waves/tides.txt"], 0]])
        names = [self.gift(f"{number:x}" * 16, [line]) for number in range(1, 11)]
        self.assertTrue(self.exchange("office").run("receive"))      # all ten offers known
        for name in names[:-1]:
            (self.shared / name).unlink()               # nine of them gone before they are used
        self.forbidden.add("office")
        self.assertEqual(self.build("office", limit=0)[1]["received"], 1)


class Counting(sharing.Folder):
    """A shared folder that counts the parcels fetched from it."""

    def __init__(self, path):
        super().__init__(path)
        self.fetched = 0

    def get(self, name, limit=None):
        self.fetched += name.endswith(".jsonl.gz")
        return super().get(name, limit)


class Turns(Two):
    """How often a data folder fetches, waits and tries again."""

    def scanned(self, path, fmt, work):
        """A scan: one page of text, the second still to recognise."""
        return [extract.Passage(1, None, path.read_text()), extract.Passage(2, None, "", True)]

    def test_a_scan_as_far_along_on_both_machines_is_not_fetched_again(self):
        self.write("Waves/tides.pdf", TEXTS["Waves/tides.txt"])
        with patch("shelffind.extract.passages", side_effect=self.scanned):
            office = self.machines["office"]
            with self.at("office"):                     # the office reads it alone, before any exchange
                self.assertEqual(indexer.build(office["db"], office["data"], store.roots(office["db"]),
                                               progress=lambda _: None), (1, 0))
            self.assertEqual(self.build("home")[0], (1, 0))    # and so does home, as far
        place = Counting(self.shared)
        told = self.exchange("office", place)
        for _ in range(3):
            told.run("receive", "apply", "send")
        self.assertEqual((place.fetched, told.counts["received"]), (1, 0))

    def test_a_names_only_run_goes_on_while_another_process_holds_the_exchange(self):
        self.shelf()
        self.build("home")
        self.build("office", limit=0)                   # the office knows what is offered
        office = self.machines["office"]
        holder = sqlite3.connect(office["data"] / parcels.KEEPING)
        holder.execute("BEGIN IMMEDIATE")               # another process writes in the bookkeeping,
        try:
            with sharing.alone(office["data"]) as free, patch.object(parcels, "WAIT", 0):
                self.assertTrue(free)                   # and holds the exchange
                began = time.monotonic()
                self.assertEqual(self.build("office", names_only=True)[0], (0, 0))
                self.assertLess(time.monotonic() - began, 10)
        finally:
            holder.rollback()
            holder.close()

    def test_a_run_waits_for_an_exchange_under_way_before_it_reads(self):
        self.shelf()
        self.build("home")
        held, done = threading.Event(), threading.Event()

        def holding():
            with sharing.alone(self.machines["office"]["data"]):
                held.set()
                done.wait(1.0)

        thread = threading.Thread(target=holding)
        thread.start()
        held.wait(5)
        self.forbidden.add("office")
        try:
            self.assertEqual(self.build("office"), ((0, 0), counts(received=4)))
        finally:
            done.set()
            thread.join()

    def test_a_shared_place_that_fails_is_tried_again_after_a_pause(self):
        self.shelf()

        class Failing(sharing.Folder):
            def names(self, folder):
                raise ValueError("rclone: the remote gives no answer")

        said = []
        machine_ = self.machines["office"]
        with self.at("office"):
            told = parcels.Exchange(machine_["data"], machine_["db"], Failing(self.shared), said.append)
        start = time.monotonic()
        with patch("shelffind.parcels.time.monotonic", return_value=start):
            self.assertFalse(told.run("receive"))
        self.assertEqual(said, ["The texts could not be exchanged this time: rclone: the remote gives no answer"])
        for later, due in ((parcels.EVERY + 1, False), (parcels.PAUSE + parcels.EVERY + 1, True)):
            with patch("shelffind.parcels.time.monotonic", return_value=start + later):
                self.assertEqual(told.due(), due)

    def test_a_parcel_never_takes_a_number_already_in_the_shared_place(self):
        self.shelf()
        stray = self.shared / "texts" / self.writer("home") / "000001.jsonl.gz"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"put there and never named")
        self.build("home")
        self.assertEqual([path.name for path in self.parcels_of("home")], ["000001.jsonl.gz", "000002.jsonl.gz"])
        self.assertEqual(stray.read_bytes(), b"put there and never named")
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1]["received"], 4)

    def test_a_file_copied_for_its_reading_takes_what_another_read_of_its_bytes(self):
        self.shelf()
        self.build("home")
        self.forbidden.add("office")
        place = Counting(self.shared)
        # The offers are taken, but the office compares no file before its pass: each file is
        # copied for its reading, and is not read, its document taking what home read of those
        # very bytes at the end of the pass; the parcel is fetched once, and kept for that round.
        with patch.object(parcels.Exchange, "sizes", return_value=frozenset()):
            self.assertEqual(self.build("office", place), ((0, 0), counts(received=4)))
        self.assertEqual((self.waiting("office"), place.fetched), (0, 1))
        self.assertEqual(self.found("office", "eastern skies"), ["stars.txt"])
        self.sound("office")

    def test_a_copied_file_that_changes_before_its_pages_come_takes_none(self):
        self.shelf()
        self.build("home")
        office = self.machines["office"]
        self.forbidden.add("office")
        real = parcels.Exchange.offer

        def offer(exchange, doc, file=None, still=None):
            held = real(exchange, doc, file, still)
            rel = office["db"].execute("SELECT rel FROM files WHERE id=?", (file,)).fetchone()[0]
            if rel == "Waves/tides.txt":
                os.utime(office["shelf"] / rel, (WHEN + 5, WHEN + 5))    # another file stands there now
            return held

        with patch.object(parcels.Exchange, "sizes", return_value=frozenset()), patch.object(parcels.Exchange, "offer", offer):
            self.assertEqual(self.build("office")[1], counts(received=3))
        self.assertEqual(self.pages("office", "Waves/tides.txt"), [])
        self.assertEqual(self.build("office")[1], counts(received=1))   # measured anew by the next run, it takes them
        self.sound("office")

    def test_a_shared_place_that_fails_before_a_copied_file_takes_its_pages_is_no_fault_of_the_document(self):
        self.shelf()
        self.build("home")
        real, real_offer, held = sharing.Folder.get, parcels.Exchange.offer, []

        def get(place, name, limit=None):
            if name.endswith(".jsonl.gz") and held:     # once the files wait for their pages
                raise OSError("Input/output error")
            return real(place, name, limit)

        def offer(exchange, doc, file=None, still=None):
            held.append(real_offer(exchange, doc, file, still))
            return held[-1]

        with patch.object(parcels.Exchange, "sizes", return_value=frozenset()), patch.object(parcels.Exchange, "offer", offer), \
                patch.object(sharing.Folder, "get", get), patch.object(parcels, "BODIES", 0):
            self.assertEqual(self.build("office")[0], (4, 0))       # read here, once the shared place failed
        states = {row[0] for row in self.machines["office"]["db"].execute("SELECT state FROM docs")}
        self.assertEqual((states, held[:4]), ({"indexed"}, [True] * 4))
        self.sound("office")

    def test_a_shared_place_marked_by_a_newer_version_during_a_run_gets_nothing_more(self):
        self.shelf()
        marker = self.shared / ".shelffind-shared"
        self.assertEqual(self.build("home", on_document=lambda *_: marker.write_text('{"format": 2}'))[0], (4, 0))
        self.assertEqual(self.parcels_of("home"), [])

    def test_offers_from_a_shared_place_that_a_newer_version_marked_are_not_taken(self):
        self.shelf()
        self.build("home")
        self.assertTrue(self.exchange("office").run("receive"))     # the offers are known,
        (self.shared / ".shelffind-shared").write_text('{"format": 2}')
        with patch.object(parcels.Exchange, "sizes", return_value=frozenset()):
            self.assertEqual(self.build("office"), ((4, 0), counts()))   # and nothing is taken from them

    def test_offers_of_one_reading_in_three_parcels_fetch_one_to_give_it(self):
        self.shelf(on=("office",))
        self.measure("office")
        line = self.line("Waves/tides.txt", [[None, None, TEXTS["Waves/tides.txt"], 0]])
        for writer in ("a" * 16, "b" * 16, "c" * 16):
            self.gift(writer, [line])
        place = Counting(self.shared)
        self.forbidden.add("office")
        self.assertEqual(self.build("office", place, limit=0)[1], counts(received=1))
        self.assertEqual(place.fetched, 3)              # three to take them, the one that gives being kept

    def test_a_reading_that_changes_with_as_many_pages_is_compared_again(self):
        self.shelf()
        self.build("home")
        office = self.machines["office"]
        with self.at("office"):
            indexer.build(office["db"], office["data"], store.roots(office["db"]), progress=lambda _: None)
        told = self.exchange("office")
        self.assertTrue(told.run("receive", "send"))
        self.assertEqual(told.counts["sent"], 0)        # home offers all of it
        with office["db"]:                              # another reading of the same bytes numbers its page
            office["db"].execute("UPDATE pages SET page=1 WHERE doc=(SELECT doc FROM files WHERE rel='Waves/tides.txt')")
        told = self.exchange("office")
        self.assertTrue(told.run("receive", "send"))
        self.assertEqual(told.counts["sent"], 1)

    def test_a_data_folder_takes_back_its_own_texts_once_lost(self):
        self.shelf(on=("home",))
        self.build("home")
        with self.at("home"):
            store.remove_root(self.machines["home"]["db"], "Shelf")      # forgotten with its texts,
            store.add_root(self.machines["home"]["db"], self.machines["home"]["shelf"], self.machines["home"]["data"])
        self.read["home"].clear()
        self.assertEqual(self.build("home"), ((0, 0), counts(received=4)))   # and taken back from its own parcels
        self.assertEqual(self.read["home"], [])


class Settled(Two):
    """A run that takes pages ends with nothing left for the next one, and the same facts give
    the same documents whatever the order."""

    def test_pages_taken_after_the_sources_were_asked_are_followed_by_another_question(self):
        self.shelf()
        self.build("home")
        office = self.machines["office"]
        with self.at("office"):                         # the office lists its files, and takes what was offered
            indexer.build(office["db"], office["data"], store.roots(office["db"]), names_only=True, progress=lambda _: None)
        tides = (office["shelf"] / "Waves/tides.txt").read_bytes()
        winds = (office["shelf"] / "Waves/winds.txt").read_bytes()
        mixed = [f"md5:{hashlib.md5(tides).hexdigest()}", f"sha256:{hashlib.sha256(winds).hexdigest()}"]
        asked = []

        def question(directory, name, op, **fields):     # the MD5 of the tides with the SHA-256 of the winds
            asked.append([Path(file["path"]).name for file in fields["files"]])
            return {"records": [{"n": file["n"], "id": "W1", "keys": mixed, "title": "Mixed Card"}
                                for file in fields["files"] if file["path"].endswith("tides.txt")]}

        tools = self.base / "bin"
        tools.mkdir()
        (tools / "rclone").write_text(STORAGE)
        (tools / "rclone").chmod(0o700)
        usable = lambda directory, progress=None: ({"catalogue": ["catalogue"]}, {"catalogue": False}, set())
        with patch.dict(os.environ, {"PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "SHELFFIND_TEST_TWIN": str(office["shelf"]),
                                     "SHELFFIND_TEST_TWIN_LOG": str(self.base / "twin.log")}), \
                patch("shelffind.link.usable", side_effect=usable), patch("shelffind.sources.question", side_effect=question), \
                patch("shelffind.sources.policy", return_value={"text": "ignore", "identify": True}):
            store.declare({"Shelf": {"path": str(office["shelf"]), "remote": "twin:Shelf"}})
            self.addCleanup(store.declare, {})
            self.forbidden.add("office")
            # The storage gives the MD5 of each file; the catalogue's word about the tides agrees
            # with it; the pages offered then show the true SHA-256, which refutes that word.
            self.assertEqual(self.build("office")[0], (0, 0))
            self.assertEqual(office["db"].execute("SELECT count(*) FROM records").fetchone()[0], 0)
            asked.clear()
            self.assertEqual(self.build("office")[0], (0, 0))
        self.assertEqual(asked, [])                     # the next run asks nothing
        self.assertEqual(self.waiting("office"), 0)
        self.sound("office")

    def library(self, answer):
        """A library that holds texts and is relied upon, answering `answer(path)`."""
        def question(directory, name, op, **fields):
            return {"records": [{"n": file["n"], **answer(file["path"])} for file in fields["files"] if answer(file["path"])]}

        usable = lambda directory, progress=None: ({"library": ["library"]}, {"library": True}, set())
        for target, how in (("shelffind.link.usable", {"side_effect": usable}), ("shelffind.sources.question", {"side_effect": question}),
                            ("shelffind.sources.policy", {"return_value": {"text": "trust", "identify": True}})):
            patching = patch(target, **how)
            patching.start()
            self.addCleanup(patching.stop)

    def test_checksums_that_come_with_an_offer_are_followed_by_another_question(self):
        self.write("Waves/tides.txt")
        self.build("home")
        office = self.machines["office"]
        self.write("Waves/copy.txt", TEXTS["Waves/tides.txt"], on=("office",))
        body = (office["shelf"] / "Waves/tides.txt").read_bytes()
        md5, sha = f"md5:{hashlib.md5(body).hexdigest()}", f"sha256:{hashlib.sha256(body).hexdigest()}"
        self.library(lambda path: {"id": "W1", "keys": [sha], "title": "Tide Tables", "text": True}
                     if path.endswith("copy.txt") else None)
        with self.at("office"):
            indexer.build(office["db"], office["data"], store.roots(office["db"]), names_only=True, progress=lambda _: None)
            with office["db"]:                          # the storage of its folder gives the MD5 of the tides
                file = office["db"].execute("SELECT id FROM files WHERE rel='Waves/tides.txt'").fetchone()[0]
                indexer.joined(office["db"], file, [md5], local=False)
        self.forbidden.add("office")
        # The SHA-256 offered shows the copy that the library holds to be the tides: both are left to it at once.
        self.assertEqual(self.build("office", names_only=True), ((0, 0), counts()))
        states = [row[0] for row in office["db"].execute("SELECT d.state FROM files f JOIN docs d ON d.id=f.doc ORDER BY f.rel")]
        self.assertEqual(states, ["covered", "covered"])
        self.sound("office")

    def test_a_document_that_the_library_answers_for_takes_no_pages_whatever_came_first(self):
        self.write("Waves/tides.txt")
        self.build("home")
        body = (self.machines["office"]["shelf"] / "Waves/tides.txt").read_bytes()
        keys = [f"md5:{hashlib.md5(body).hexdigest()}", f"sha256:{hashlib.sha256(body).hexdigest()}"]
        self.library(lambda path: {"id": "W1", "keys": keys, "title": "Tide Tables", "text": True}
                     if path.endswith("tides.txt") else None)
        self.forbidden.add("office")
        self.assertEqual(self.build("office"), ((0, 0), counts()))
        state = self.machines["office"]["db"].execute("SELECT state FROM docs").fetchone()[0]
        self.assertEqual((state, self.pages("office", "Waves/tides.txt")), ("covered", []))
        self.sound("office")


class Carriage(Two):
    def sized(self, limit, unpacked):
        self.shelf()
        with patch.object(parcels, "LIMIT", limit), patch.object(parcels, "UNPACKED", unpacked):
            self.build("home")
            made = self.parcels_of("home")
            self.assertEqual(len(made), 4)                          # one document in each
            for parcel in made:
                self.assertLessEqual(parcel.stat().st_size, limit)
                self.assertLessEqual(sum(len(line) + 1 for line in parcels.unpack(parcel.read_bytes())), unpacked)
            self.forbidden.add("office")
            self.assertEqual(self.build("office")[1], counts(received=4))

    def test_parcels_stay_within_their_size(self):
        self.sized(400, parcels.UNPACKED)                       # one document fits, two do not

    def test_parcels_stay_within_what_they_unpack_to(self):
        self.sized(parcels.LIMIT, 30_000)

    def test_the_first_line_of_a_parcel_counts_in_what_it_unpacks_to(self):
        raw = json.dumps(self.line("Waves/tides.txt", [[None, None, TEXTS["Waves/tides.txt"], 0]])).encode() + b"\n"
        with patch.object(parcels, "UNPACKED", len(raw)):
            self.assertFalse(parcels.Packer("0" * 16).add(raw))
        with patch.object(parcels, "UNPACKED", len(raw) + 100):
            self.assertTrue(parcels.Packer("0" * 16).add(raw))

    def given(self, **limits):
        """Home reads and gives under the limits `limits`: (what it said, its exchange)."""
        said, machine_ = [], self.machines["home"]
        with self.at("home"), contextlib.ExitStack() as stack:
            for name, value in limits.items():
                stack.enter_context(patch.object(parcels, name, value))
            exchange = parcels.Exchange(machine_["data"], machine_["db"], self.place(), said.append)
            indexer.build(machine_["db"], machine_["data"], store.roots(machine_["db"]), progress=lambda _: None, exchange=exchange)
        return said, exchange

    def test_a_list_that_would_grow_beyond_what_a_reader_takes_stays_as_it_was(self):
        self.shelf()
        writer = self.writer("home")
        two = {name: {"name": name, "size": 300, "sha256": "0" * 64, "documents": 1}
               for name in (f"texts/{writer}/{number:06d}.jsonl.gz" for number in (1, 2))}
        said, exchange = self.given(LIMIT=400, LISTED=len(parcels.Exchange.listing(SimpleNamespace(writer=writer), two, 2)))
        self.assertEqual((exchange.counts["sent"], len(self.listed("home")["parcels"])), (2, 2))
        self.assertTrue(any("is full" in line for line in said))
        self.assertEqual(self.build("office")[1]["received"], 2)

    def test_a_data_folder_that_gave_its_last_number_gives_nothing_more(self):
        self.shelf()
        said, exchange = self.given(LIMIT=400, LAST=2)
        self.assertEqual([path.name for path in self.parcels_of("home")], ["000001.jsonl.gz", "000002.jsonl.gz"])
        self.assertEqual(exchange.counts["sent"], 2)
        self.assertTrue(any("last parcel" in line for line in said))

    def test_a_document_too_large_for_a_parcel_of_its_own_is_not_given_until_the_limits_allow_it(self):
        self.shelf()
        with patch.object(parcels, "LIMIT", 200):
            self.assertEqual(self.build("home")[1]["sent"], 0)
            self.assertEqual(self.parcels_of("home"), [])
            self.assertEqual(self.exchange("home").run("send"), True)
            self.assertEqual(self.parcels_of("home"), [])
        self.assertEqual(self.build("home")[1]["sent"], 4)      # compared again under the limits of now

    def test_a_parcel_beyond_the_size_of_any_parcel_is_left_aside(self):
        self.shelf()
        self.build("home")
        listing = self.shared / "texts" / f"{self.writer('home')}.json"
        told = json.loads(listing.read_text())
        told["parcels"][0]["size"] = parcels.LIMIT + 1
        listing.write_text(json.dumps(told))
        self.assertEqual(self.build("office", limit=0)[1], counts(damaged=1))

    def test_a_document_of_more_pages_than_a_reader_takes_is_not_given(self):
        self.shelf()
        with patch.object(parcels, "PAGES", 0):
            self.assertEqual(self.build("home")[1]["sent"], 0)
        self.assertEqual(self.parcels_of("home"), [])

    def test_the_same_exchange_through_rclone(self):
        tools = self.base / "bin"
        tools.mkdir()
        (tools / "rclone").write_text(RCLONE)
        (tools / "rclone").chmod(0o700)
        place = sharing.Rclone("pretend:Shared", program=str(tools / "rclone"))
        self.shelf()
        with patch.dict(os.environ, {"PRETENDED_REMOTE": str(self.shared)}):
            self.assertEqual(self.build("home", place)[1]["sent"], 4)
            self.forbidden.add("office")
            self.assertEqual(self.build("office", place), ((0, 0), counts(received=4)))
        self.assertEqual(self.found("office", "eastern skies"), ["stars.txt"])

    def test_a_write_that_fails_through_rclone_leaves_the_former_list_whole(self):
        tools = self.base / "bin"
        tools.mkdir()
        (tools / "rclone").write_text(RCLONE)
        (tools / "rclone").chmod(0o700)
        place = sharing.Rclone("pretend:shared", program=str(tools / "rclone"))     # the folder `shared` of the base
        self.write("Waves/tides.txt", on=("home",))
        with patch.dict(os.environ, {"PRETENDED_REMOTE": str(self.base)}):
            self.assertEqual(self.build("home", place)[1]["sent"], 1)
            before = self.listed("home")
            self.write("Waves/winds.txt", on=("home",))
            with patch.dict(os.environ, {"PRETENDED_FULL": "1"}):
                self.assertEqual(self.build("home", place)[1]["sent"], 0)
            self.assertEqual(self.listed("home"), before)       # neither cut nor removed
            self.assertEqual(sorted(path.name for path in (self.shared / "texts").iterdir() if path.is_file()),
                             [f"{self.writer('home')}.json"])
            self.assertEqual(self.build("home", place)[1]["sent"], 1)    # the next exchange gives what was left
            self.shelf(on=("office",))
            self.forbidden.add("office")
            self.assertEqual(self.build("office", place)[1]["received"], 2)

    def test_the_journals_and_the_texts_keep_apart(self):
        self.shelf()
        self.build("home")
        data = self.machines["home"]["data"]
        with self.at("home"):
            history.record(data, "search", "tide tables", None, when=WHEN)
            with sharing.alone(data):
                self.assertEqual(sharing.sync(data, self.place())["sent"], 1)
            label = machine.identity(data)["id"]
        card = json.loads((self.shared / "machines" / f"{label}.json").read_text())
        self.assertEqual((sorted(card), len(self.listed("home")["parcels"])), (["id", "journals", "name", "updated"], 1))
        # A card rewritten as version 0.3 writes it changes nothing of the texts.
        sharing.put_card(self.place(), label, {"id": label, "name": "home", "updated": 0, "journals": {}})
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1]["received"], 4)

    def test_the_commands_share_texts_when_the_settings_ask(self):
        self.shelf()
        for name in ("home", "office"):
            (self.machines[name]["data"] / "config.toml").write_text(f'[share]\ntexts = true\nfolder = "{self.shared}"\n')
            with self.at(name):
                run_main(self.machines[name]["data"], "machine", "--name", name)
        with self.at("home"):
            code, _, err = run_main(self.machines["home"]["data"], "index")
        self.assertEqual((code, err.splitlines()[-1]), (0, "4 documents processed, 0 problems"))
        self.forbidden.add("office")
        with self.at("office"):
            code, out, _ = run_main(self.machines["office"]["data"], "sync")
            self.assertEqual((code, out.strip()), (0, "0 sent, 0 received (no other machine yet); texts of 0 documents given"))
            self.assertEqual(self.found("office", "northern harbours"), [])     # a sync carries parcels and gives no pages
            code, _, err = run_main(self.machines["office"]["data"], "index")
        self.assertEqual((code, err.splitlines()[-1]), (0, "0 documents processed, 0 problems"))
        self.assertEqual(self.found("office", "northern harbours"), ["tides.txt"])
        with self.at("office"):
            code, out, _ = run_main(self.machines["office"]["data"], "sync", "--quiet")
        self.assertEqual((code, out), (0, ""))

    def test_a_sync_whose_transfer_fails_says_so_in_its_exit_code(self):
        self.shelf()
        data = self.machines["home"]["data"]
        (data / "config.toml").write_text(f'[share]\ntexts = true\nfolder = "{self.shared}"\n')
        real = sharing.Folder.names

        def failing(place, folder):
            if folder.startswith("texts"):
                raise OSError("the shared place gives no answer")
            return real(place, folder)

        with self.at("home"):
            self.assertEqual(run_main(data, "index")[0], 0)
            with patch.object(sharing.Folder, "names", failing):
                code, _, err = run_main(data, "sync")
        self.assertEqual(code, 1)
        self.assertIn("could not be exchanged", err)

    def test_a_sync_whose_parcel_cannot_be_fetched_says_so_in_its_exit_code(self):
        tools = self.base / "bin"
        tools.mkdir()
        (tools / "rclone-itself").write_text(RCLONE)
        (tools / "rclone-itself").chmod(0o700)
        (tools / "rclone").write_text(f'#!/bin/sh\ncase "$*" in *cat*.jsonl.gz*) echo "ERROR : read: input/output error" >&2; exit 1;; esac\n'
                                      f'exec "{tools / "rclone-itself"}" "$@"\n')
        (tools / "rclone").chmod(0o700)
        self.shelf()
        self.build("home")
        data = self.machines["office"]["data"]
        (data / "config.toml").write_text('[share]\ntexts = true\nremote = "pretend:shared"\n')
        with self.at("office"), patch.dict(os.environ, {"PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
                                                        "PRETENDED_REMOTE": str(self.base)}):
            code, _, err = run_main(data, "sync")
        self.assertEqual(code, 1)
        self.assertIn("input/output error", err)

    def test_the_lock_of_an_exchange_makes_its_data_folder(self):
        fresh = self.base / "fresh-state"
        with sharing.alone(fresh) as free:
            self.assertTrue(free)
        self.assertTrue(fresh.is_dir())

    def test_without_the_setting_no_text_leaves_the_machine(self):
        self.shelf()
        (self.machines["home"]["data"] / "config.toml").write_text(f'[share]\nhistory = true\nfolder = "{self.shared}"\n')
        with self.at("home"):
            self.assertEqual(run_main(self.machines["home"]["data"], "index")[0], 0)
            self.assertEqual(run_main(self.machines["home"]["data"], "sync")[0], 0)
        self.assertFalse((self.shared / "texts").exists())


if __name__ == "__main__":
    unittest.main()
