# Copyright 2026 Eudaemonic Enterprises Inc.
"""Two machines share the text they read through one shared place: what one read the other
finds without reading it, a parcel damaged on the way is taken another time, and nothing is
given to bytes that a measure contradicts."""
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import invariants
from shelffind import extract, indexer, machine, parcels, sharing, store
from shelffind.search import search

from support import run_main
from test_sharing import RCLONE

TEXTS = {
    "Waves/tides.txt": "Tide tables of the northern harbours. " * 400,
    "Waves/winds.txt": "Wind roses of the southern capes. " * 500,
    "Charts/shoals.txt": "Charts of the western shoals. " * 600,
    "Charts/stars.txt": "Star almanac of the eastern skies. " * 700,
}
WHEN = 1_700_000_000


class Two(unittest.TestCase):
    """The machines home and office, each with its home folder, its shelf, its data folder and
    its identity, and one shared folder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.shared = self.base / "shared"
        self.shared.mkdir()
        self.machines, self.read, self.forbidden = {}, {"home": [], "office": []}, set()
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
        for name in ("home", "office"):
            shelf = self.base / name / "Shelf"
            shelf.mkdir(parents=True)
            (self.base / f"{name}-id").write_text(f"{name * 8}\n")
            data = self.base / f"{name}-state"
            with self.at(name):
                db = store.connect(data)
                store.add_root(db, shelf, data)
            self.addCleanup(db.close)
            self.machines[name] = {"shelf": shelf, "data": data, "db": db}

    @contextlib.contextmanager
    def at(self, name):
        """Run what follows as on the machine `name`."""
        before, self.here = self.here, name
        try:
            with patch.dict(os.environ, {"HOME": str(self.base / name)}), \
                    patch.object(machine, "SOURCES", (str(self.base / f"{name}-id"),)):
                yield self.machines.get(name)
        finally:
            self.here = before

    def write(self, rel, text=None, on=("home", "office")):
        for name in on:
            path = self.machines[name]["shelf"] / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(TEXTS[rel] if text is None else text)
            os.utime(path, (WHEN, WHEN))

    def shelf(self):
        for rel in TEXTS:
            self.write(rel)

    def place(self):
        return sharing.Folder(self.shared)

    def exchange(self, name, place=None):
        machine_ = self.machines[name]
        with self.at(name):
            return parcels.Exchange(machine_["data"], machine_["db"], place or self.place())

    def build(self, name, place=None, **opts):
        """A complete run on the machine `name`, sharing texts: ((done, problems), counts)."""
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
        """Measure every file of the machine `name` by a reading in full, as a complete run does
        when it has a reason to."""
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

    def parcels_of(self, name):
        with self.at(name):
            label = machine.identity(self.machines[name]["data"])["id"]
        return sorted((self.shared / "texts" / label).glob("*.jsonl.gz"))

    def waiting(self, name):
        return self.machines[name]["db"].execute("SELECT count(*) FROM docs WHERE state='pending'").fetchone()[0]


class Texts(Two):
    def test_what_one_machine_read_the_other_finds_without_reading_it(self):
        self.shelf()
        self.assertEqual(self.build("home"), ((4, 0), {"sent": 4, "received": 0, "damaged": 0, "newer": 0}))
        self.forbidden.add("office")
        self.assertEqual(self.build("office"), ((0, 0), {"sent": 0, "received": 4, "damaged": 0, "newer": 0}))
        self.assertEqual((self.found("office", "northern harbours"), self.found("office", "eastern skies")),
                         (["tides.txt"], ["stars.txt"]))
        self.assertEqual((self.waiting("office"), self.read["office"]), (0, []))
        self.assertEqual(self.parcels_of("office"), [])     # what it received, it does not send back
        self.assertEqual(self.build("office")[1]["received"], 0)
        self.assertEqual(self.build("home")[1], {"sent": 0, "received": 0, "damaged": 0, "newer": 0})
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
                    patch.object(parcels.Exchange, "run", lambda self, *steps: True):
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
        self.assertEqual(self.build("office"), ((1, 0), {"sent": 1, "received": 0, "damaged": 0, "newer": 0}))
        self.assertEqual((self.found("office", "northern"), self.found("office", "southern")), ([], ["tides.txt"]))
        self.sound("office")

    def test_a_document_that_a_source_answers_for_takes_pages_once_the_source_stops(self):
        self.shelf()
        self.build("home")
        self.measure("office")
        db = self.machines["office"]["db"]
        with db:
            db.execute("UPDATE docs SET state='covered'")
        self.assertEqual(self.exchange("office").counts["received"], 0)
        told = self.exchange("office")
        told.run("receive")
        self.assertEqual(told.counts["received"], 0)
        with db:
            db.execute("UPDATE docs SET state='pending'")
        told.run("apply")
        self.assertEqual(told.counts["received"], 4)
        self.assertEqual(self.found("office", "southern capes"), ["winds.txt"])
        self.sound("office")

    def test_recognised_pages_travel_and_complete_the_other_reading(self):
        self.write("Waves/tides.txt")
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
            self.assertEqual(self.build("home", ocr=True), ((1, 0), {"sent": 1, "received": 0, "damaged": 0, "newer": 0}))
        with patch("shelffind.extract.recognize", side_effect=AssertionError("the office recognised a page")):
            self.assertEqual(self.build("office", ocr=True), ((0, 0), {"sent": 0, "received": 1, "damaged": 0, "newer": 0}))
        self.assertEqual((state("office"), self.found("office", "harbour charts")), ("indexed", ["tides.txt"]))
        self.sound("office")


class Damage(Two):
    """What a transfer can do to a parcel, and what a machine can lose."""

    def damaged(self, spoil):
        """Home reads and gives; the parcel is spoilt; the office takes nothing; once the parcel
        is whole again, the office takes it and reads nothing."""
        self.shelf()
        self.build("home")
        parcel = self.parcels_of("home")[0]
        whole = parcel.read_bytes()
        spoil(parcel)
        self.assertEqual(self.build("office", limit=0), ((0, 0), {"sent": 0, "received": 0, "damaged": 1, "newer": 0}))
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

    def test_a_parcel_taken_in_part_is_taken_again_and_nothing_is_given_twice(self):
        self.shelf()
        self.build("home")
        self.measure("office")
        real, calls = store.take_pages, []

        def stopping(db, doc, keys, pages):
            calls.append(doc)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(db, doc, keys, pages)

        with patch("shelffind.store.take_pages", side_effect=stopping), self.assertRaises(KeyboardInterrupt):
            self.exchange("office").run("receive")
        self.assertEqual(self.waiting("office"), 3)
        told = self.exchange("office")
        told.run("receive")
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
        told.run("receive")
        self.assertEqual((told.counts["received"], told.counts["newer"], told.counts["damaged"]), (0, 1, 0))
        told.run("receive")                             # remembered: not fetched again
        self.assertEqual(told.counts["newer"], 2)
        with patch.object(parcels, "FORMAT", 2):
            told = self.exchange("office")
            told.run("receive")
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

    def test_a_machine_that_lost_its_bookkeeping_gives_nothing_twice(self):
        self.shelf()
        self.build("home")
        (self.machines["home"]["data"] / parcels.KEEPING).unlink()
        self.assertEqual(self.build("home"), ((0, 0), {"sent": 0, "received": 0, "damaged": 0, "newer": 0}))
        self.assertEqual(len(self.parcels_of("home")), 1)
        (self.machines["office"]["data"] / parcels.KEEPING).unlink(missing_ok=True)
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1]["received"], 4)
        (self.machines["office"]["data"] / parcels.KEEPING).unlink()
        self.assertEqual(self.build("office"), ((0, 0), {"sent": 0, "received": 0, "damaged": 0, "newer": 0}))

    def test_what_a_card_names_wrongly_is_left_aside(self):
        self.shelf()
        self.build("home")
        with self.at("home"):
            label = machine.identity(self.machines["home"]["data"])["id"]
        card = json.loads((self.shared / "machines" / f"{label}.json").read_text())
        card["parcels"] += [{"name": "../../outside.jsonl.gz", "size": 10, "sha256": "0" * 64},
                            {"name": f"texts/{'f' * 16}/000001.jsonl.gz", "size": 10, "sha256": "0" * 64}, "not a parcel"]
        (self.shared / "machines" / f"{label}.json").write_text(json.dumps(card))
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1], {"sent": 0, "received": 4, "damaged": 0, "newer": 0})


class Carriage(Two):
    def test_parcels_stay_within_their_size_and_are_all_taken(self):
        self.shelf()
        with patch.object(parcels, "LIMIT", 1):
            self.build("home")
        self.assertEqual(len(self.parcels_of("home")), 4)  # a document alone in its parcel, whatever its size
        self.forbidden.add("office")
        self.assertEqual(self.build("office")[1]["received"], 4)

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
            self.assertEqual(self.build("office", place), ((0, 0), {"sent": 0, "received": 4, "damaged": 0, "newer": 0}))
        self.assertEqual(self.found("office", "eastern skies"), ["stars.txt"])

    def test_the_journals_keep_the_parcels_on_the_card(self):
        self.shelf()
        self.build("home")
        with self.at("home"):
            from shelffind import history
            history.record(self.machines["home"]["data"], "search", "tide tables", None, when=WHEN)
            with sharing.alone(self.machines["home"]["data"]):
                self.assertEqual(sharing.sync(self.machines["home"]["data"], self.place())["sent"], 1)
            label = machine.identity(self.machines["home"]["data"])["id"]
        card = json.loads((self.shared / "machines" / f"{label}.json").read_text())
        self.assertEqual((len(card["parcels"]), len(card["journals"])), (1, 1))
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
            self.assertEqual((code, out.strip()), (0, "0 sent, 0 received (home); texts of 0 documents given, 0 taken"))
            code, _, err = run_main(self.machines["office"]["data"], "index")
        self.assertEqual((code, err.splitlines()[-1]), (0, "0 documents processed, 0 problems"))
        self.assertEqual(self.found("office", "northern harbours"), ["tides.txt"])
        with self.at("office"):
            code, out, _ = run_main(self.machines["office"]["data"], "sync", "--quiet")
        self.assertEqual((code, out), (0, ""))

    def test_without_the_setting_no_text_leaves_the_machine(self):
        self.shelf()
        (self.machines["home"]["data"] / "config.toml").write_text(f'[share]\nhistory = true\nfolder = "{self.shared}"\n')
        with self.at("home"):
            self.assertEqual(run_main(self.machines["home"]["data"], "index")[0], 0)
            self.assertEqual(run_main(self.machines["home"]["data"], "sync")[0], 0)
        self.assertFalse((self.shared / "texts").exists())


if __name__ == "__main__":
    unittest.main()
