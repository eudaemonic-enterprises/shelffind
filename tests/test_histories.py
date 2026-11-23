# Copyright 2026 Eudaemonic Enterprises Inc.
"""Random histories of a shelf, each step checked against the truth.

A small world is driven at random: files are written, changed, copied, moved and removed in
two folders, one of which has a storage that tells checksums and may lag behind; two sources
recognise files, by their address, rightly or wrongly, or by their checksum; one work in four
is a scan, whose second page is recognised by a later run, and one in seven cannot be read at
all; the index is brought up to date now
and then, by an inventory, in full, for one file, or for the recognition; a folder is forgotten
and registered again, and a month passes. Since the test knows what every
file holds, it can tell after each step whether a file shows a text that is not its own, and
whether the documents are those that the facts name, whatever the order of the events.

The number of histories is 40, or what SHELFFIND_HISTORIES says; SHELFFIND_HISTORY replays the
one of a given number."""
import hashlib
import os
import random
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import invariants
from shelffind import extract, indexer, link, store

SIZES = (600, 900)
FORMATS = ("txt", "txt", "txt", "md", "bin")
TIMES = (1_700_000_000, 1_700_000_050, 1_700_000_100)


def body(work, size):
    """What the work number `work` holds, at the size `size`: its own word, again and again."""
    text = f"work{work:04d}token " * (size // 14 + 1)
    return text[:size].encode()


class World:
    """A shelf, its storage, its sources and its index, with the truth about each file."""

    def __init__(self, seed, base):
        self.dice = random.Random(seed)
        self.base = base = base.resolve()
        self.rooms = {"Room": base / "Room", "Vault": base / "Vault"}
        for folder in self.rooms.values():
            folder.mkdir()
        self.data = base / "state"
        self.db = store.connect(self.data)
        for name, folder in self.rooms.items():
            store.add_root(self.db, folder, self.data, name=name)
        self.works = {}             # work -> size
        self.holds = {}             # address -> work
        self.twin = {}              # address in the vault -> (work, size, second), as the storage holds it
        self.liars = set()          # addresses of which the storage holds other bytes under the same size and second
        self.silent = False
        self.beliefs = {"library": {}, "catalogue": {}}     # source -> address -> work it takes the file for
        self.known = {"library": set(), "catalogue": set()}     # source -> works it knows by their checksums
        self.questions = 0
        self.aged = False
        self.aim = None
        self.extracted = []
        self.recognised = []
        self.stumbled = []
        self.log = []

    # ---- the truth ------------------------------------------------------------------------
    def new_work(self):
        work = len(self.works) + 1
        self.works[work] = self.dice.choice(SIZES)
        return work

    def bytes_of(self, work):
        return body(work, self.works[work])

    def sums(self, work):
        data = self.bytes_of(work)
        return f"md5:{hashlib.md5(data).hexdigest()}", f"sha256:{hashlib.sha256(data).hexdigest()}"

    def work_of(self, data):
        return int(data[4:8])

    def put(self, path, work, when=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.bytes_of(work))
        when = self.dice.choice(TIMES) if when is None else when
        os.utime(path, ns=(when * 10**9 + self.dice.randrange(10**9), when * 10**9 + self.dice.randrange(10**9)))
        self.holds[path] = work
        self.liars.discard(path)

    # ---- the storage and the sources ------------------------------------------------------
    def stored(self, remote, rows):
        if self.silent:
            return None
        told = {}
        for row in rows:
            path = self.rooms["Vault"] / row["rel"]
            held = self.twin.get(path)
            if held and held[1] == row["size"] and held[2] == row["mtime"] // 10**9:
                told[row["rel"]] = self.sums(held[0])[0]
        return told

    def record(self, name, number, work):
        md5, sha = self.sums(work)
        return {"n": number, "id": f"W{work}", "keys": [sha, md5] if name == "library" else [md5],
                "title": f"Title{work:04d} of {name}", "text": name == "library" and work % 3 == 0}

    def question(self, directory, name, op, **fields):
        self.questions += 1
        records = []
        for file in fields["files"]:
            mine = [work for work in self.known[name] if set(file["keys"]) & set(self.sums(work))]
            if mine:
                records.append(self.record(name, file["n"], mine[0]))
            elif Path(file["path"]) in self.beliefs[name]:
                records.append(self.record(name, file["n"], self.beliefs[name][Path(file["path"])]))
        return {"records": records}

    # ---- the events -----------------------------------------------------------------------
    def some_file(self):
        return self.dice.choice(sorted(self.holds)) if self.holds else None

    def fresh_address(self):
        room = self.rooms[self.dice.choice(sorted(self.rooms))]
        folder = self.dice.choice(("", "a", "b"))
        return room / folder / f"f{self.dice.randrange(10**6):06d}.{self.dice.choice(FORMATS)}"

    def step(self):
        dice = self.dice
        kind = dice.choice(("add", "add", "again", "copy", "move", "rename", "change", "remove", "sync", "sync",
                            "believe", "believe", "learn", "forget", "silence", "lie", "inventory", "inventory",
                            "full", "full", "limited", "relink", "recognise", "targeted", "retry"))
        if not self.aged and dice.random() < 0.01:
            kind = "age"
        elif dice.random() < 0.01:
            kind = dice.choice(("refold", "month"))
        path = self.some_file()
        if kind == "add":
            self.put(self.fresh_address(), self.new_work())
        elif kind == "again" and self.works:                # a work already known, at a new address
            self.put(self.fresh_address(), dice.choice(sorted(self.works)))
        elif kind == "copy" and path:
            target = self.fresh_address().with_suffix(path.suffix)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            self.holds[target] = self.holds[path]
        elif kind == "move" and path:                       # as a filing tool does: a new copy, the original removed
            target = self.fresh_address().with_suffix(path.suffix)
            self.put(target, self.holds[path])
            self.take(path)
        elif kind == "rename" and path:
            target = self.fresh_address().with_suffix(path.suffix)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, target)
            self.holds[target] = self.holds.pop(path)
            self.liars.discard(path)
        elif kind == "change" and path:
            self.put(path, self.new_work())
        elif kind == "remove" and path:
            self.take(path)
        elif kind == "sync":                                # the storage catches up with the vault
            self.twin = {path: (work, path.stat().st_size, path.stat().st_mtime_ns // 10**9)
                         for path, work in self.holds.items() if path.is_relative_to(self.rooms["Vault"])}
        elif kind == "believe" and path:                    # a source takes a file for a work, rightly or not
            name = dice.choice(sorted(self.beliefs))
            self.beliefs[name][path] = self.holds[path] if dice.random() < 0.6 else dice.choice(sorted(self.works))
        elif kind == "learn" and self.works:
            self.known[dice.choice(sorted(self.known))].add(dice.choice(sorted(self.works)))
        elif kind == "forget":
            name = dice.choice(sorted(self.beliefs))
            if self.beliefs[name] and dice.random() < 0.5:
                del self.beliefs[name][dice.choice(sorted(self.beliefs[name]))]
            elif self.known[name]:
                self.known[name].discard(dice.choice(sorted(self.known[name])))
        elif kind == "silence":
            self.silent = not self.silent
        elif kind == "lie" and path and path in self.twin and dice.random() < 0.3:
            # The storage holds other bytes under the size and the second of the file: the one
            # case in which it is believed wrongly (see `misled`).
            others = [work for work, size in self.works.items() if size == self.works[self.holds[path]]]
            self.twin[path] = (dice.choice(others), *self.twin[path][1:])
        elif kind == "age":
            self.age()
        elif kind == "refold":                              # a folder is forgotten, then registered again
            store.remove_root(self.db, "Room")
            self.sound(kind)
            store.add_root(self.db, self.rooms["Room"], self.data, name="Room")
        elif kind == "month":                               # the documents without file have waited long enough
            with self.db:
                store.tend_orphans(self.db, now=int(time.time()) + store.KEEP + 60)
            self.sound(kind)
        elif kind == "targeted":
            if path is None:
                return None
            self.aim = path
            return kind
        elif kind in ("inventory", "full", "limited", "relink", "recognise", "retry"):
            return kind
        self.log.append(kind)
        return None

    def sound(self, kind):
        breaches = invariants.structure(self.db)
        if breaches or invariants.actual(self.db) != invariants.canonical(self.db):
            raise AssertionError(f"after {' '.join(self.log)} {kind}: {breaches or 'the documents are not those the facts name'}")

    def scan_of(self, work):
        """True for a work that is a scan: its second page waits for a recognition."""
        return work % 4 == 0

    def broken(self, work):
        """True for a work that no reader can read: every reading of it fails."""
        return work % 7 == 3

    def misled(self):
        """The files of which the storage holds other bytes under the same size and the same
        second, as after an edit within one second that it has not received yet: the stated
        limit of what a storage can tell, which the checks leave aside."""
        found = set()
        for path, work in self.holds.items():
            held = self.twin.get(path)
            if held and held[0] != work and held[1:] == (path.stat().st_size, path.stat().st_mtime_ns // 10**9):
                found.add(path)
        return found

    def age(self):
        """Make the index what the conversion of an index of the earlier version gives: every
        file a document of its own, with the pages that version had read from it, and nothing
        measured or said of any file."""
        self.aged = True
        db = self.db
        places = {root["id"]: Path(root["path"]) for root in store.roots(db)}
        with db:
            for table in ("records", "doc_keys", "pages"):
                db.execute(f"DELETE FROM {table}")
            db.execute("DELETE FROM meta WHERE key LIKE 'asked:%'")
            for file in db.execute("SELECT * FROM files ORDER BY id").fetchall():
                doc = store.new_document(db, file["size"], file["format"])
                path = places[file["root"]] / file["rel"]
                same = path.is_file() and (path.stat().st_size, path.stat().st_mtime_ns) == (file["size"], file["mtime"])
                read = same and file["format"] in extract.SUPPORTED and self.dice.random() < 0.7 \
                    and not self.broken(self.holds[path])           # which the earlier version could not read either
                if read and self.scan_of(self.holds[path]):
                    db.execute("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,1,NULL,?,0)",
                               (doc, store.deflate(path.read_bytes().decode())))
                    db.execute("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,2,NULL,?,1)", (doc, store.deflate("")))
                    db.execute("UPDATE docs SET state='needs-ocr' WHERE id=?", (doc,))
                elif read:
                    db.execute("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,NULL,NULL,?,0)",
                               (doc, store.deflate(path.read_bytes().decode())))
                    db.execute("UPDATE docs SET state='indexed' WHERE id=?", (doc,))
                db.execute("UPDATE files SET doc=?,sum=NULL,asked='',read=? WHERE id=?", (doc, int(read), file["id"]))
                db.execute("UPDATE names SET title='',byline='' WHERE rowid=?", (file["id"],))
            db.execute("DELETE FROM docs WHERE NOT EXISTS (SELECT 1 FROM files f WHERE f.doc=docs.id)")

    def take(self, path):
        path.unlink()
        del self.holds[path]
        self.liars.discard(path)

    def build(self, kind):
        options = {"inventory": {"names_only": True}, "full": {}, "limited": {"limit": 1},
                   "relink": {"names_only": True, "relink": True}, "recognise": {"ocr": True}, "retry": {"retry": True},
                   "targeted": {"paths": [self.aim]}}[kind]
        self.log.append(kind)
        before = len(self.extracted)
        self.liars |= self.misled()                         # what it says of them now may be kept for long
        done = indexer.build(self.db, self.data, store.roots(self.db), progress=lambda _: None, **options)
        return done, self.extracted[before:]


class Histories(unittest.TestCase):
    def run_one(self, seed):
        with tempfile.TemporaryDirectory() as tmp:
            world = World(seed, Path(tmp))
            original = extract.passages

            def reading(path, fmt, work):
                held = world.work_of(path.read_bytes())
                if world.broken(held):
                    world.stumbled.append((held, fmt))
                    raise RuntimeError("the reader stumbled")
                world.extracted.append((held, fmt))
                if world.scan_of(held):                     # a scan: one page of text, one still to recognise
                    return [extract.Passage(1, None, path.read_bytes().decode()), extract.Passage(2, None, "", True)]
                return original(path, fmt, work)

            def recognising(path, fmt, work, number, languages):
                held = world.work_of(path.read_bytes())
                world.recognised.append((held, fmt))
                return f"work{held:04d}token recognised on page {number}"

            usable = lambda directory, progress=None: (
                {"library": ["library"], "catalogue": ["catalogue"]}, {"library": True, "catalogue": False}, set())
            policy = lambda name: {"text": "trust" if name == "library" else "ignore", "identify": True}
            store.declare({"Vault": {"path": str(world.rooms["Vault"]), "remote": "twin:Vault"}})
            try:
                with patch("shelffind.extract.passages", side_effect=reading), \
                        patch("shelffind.extract.recognize", side_effect=recognising), \
                        patch("shelffind.indexer.stored_checksums", side_effect=world.stored), \
                        patch("shelffind.sources.question", side_effect=world.question), \
                        patch("shelffind.sources.policy", side_effect=policy), \
                        patch("shelffind.link.usable", side_effect=usable):
                    for _ in range(world.dice.randrange(40, 120)):
                        kind = world.step()
                        if kind:
                            self.check(world, kind, seed)
                    self.check(world, "full", seed)
            finally:
                store.declare({})
                world.db.close()

    def texts(self, db):
        """Document -> the works whose words its pages hold."""
        found = {}
        for row in db.execute("SELECT doc,ztext FROM pages"):
            text = store.inflate(row["ztext"])
            found.setdefault(row["doc"], set()).update(
                int(text[at + 4:at + 8]) for at in range(len(text)) if text.startswith("work", at) and text[at + 8:at + 13] == "token")
        return found

    def check(self, world, kind, seed):
        db = world.db
        where = f"history {seed}, after {' '.join(world.log)} {kind}"
        sealed = {(row["origin"], row["format"]) for row in db.execute("SELECT origin,format FROM docs WHERE origin LIKE '%sha256:%'")}
        complete = {(row["origin"], row["format"]) for row in db.execute(
            "SELECT origin,format FROM docs d WHERE origin LIKE '%sha256:%' AND NOT EXISTS "
            "(SELECT 1 FROM pages p WHERE p.doc=d.id AND p.needs_ocr=1)")}
        recognised, stumbled = len(world.recognised), len(world.stumbled)
        (done, failures), extracted = world.build(kind)
        # A reading fails only for a work that cannot be read, once in a run for each such
        # document, and each failure is counted.
        failed = world.stumbled[stumbled:]
        self.assertEqual((failures, len(set(failed))), (len(failed), len(failed)), where)
        self.assertEqual(invariants.structure(db), [], where)
        self.assertEqual(invariants.actual(db), invariants.canonical(db), where)
        # No document is read a second time while the text read from its bytes is kept.
        for work, fmt in extracted:
            self.assertNotIn((" ".join(world.sums(work)), fmt), sealed, f"{where}: work {work} was read again")
        for work, fmt in world.recognised[recognised:]:
            self.assertNotIn((" ".join(world.sums(work)), fmt), complete, f"{where}: work {work} was recognised again")
        # No file shows a text that is not its own, unless a source says so of a file never measured.
        places = {root["id"]: Path(root["path"]) for root in store.roots(db)}
        texts = self.texts(db)
        for file in db.execute("SELECT * FROM files").fetchall():
            path = places[file["root"]] / file["rel"]
            work = world.holds[path]
            if path in world.liars:
                continue
            if file["sum"]:
                self.assertIn(world.sums(work)[0], file["sum"].split(), f"{where}: a wrong measure of {file['rel']}")
            shown = texts.get(file["doc"])
            if shown is None or shown == {work}:
                continue
            words = db.execute("SELECT 1 FROM records WHERE file=? LIMIT 1", (file["id"],)).fetchone()
            self.assertTrue(not file["sum"] and words and len(shown) == 1, f"{where}: {file['rel']} holds work {work} and shows {shown}")
        liars = {path for path in world.liars if path in world.holds}
        if kind not in ("full", "recognise") or world.silent or liars:
            return
        current = {path: (work, path.stat().st_size, path.stat().st_mtime_ns // 10**9)
                   for path, work in world.holds.items() if path.is_relative_to(world.rooms["Vault"])}
        if current != {path: held for path, held in world.twin.items() if path in world.holds}:
            return
        # A full run with a storage that is up to date leaves nothing waiting, and a second run nothing to do.
        states = "'needs-ocr','ocr-error'" if kind == "recognise" else "'pending'"
        waiting = db.execute(f"SELECT f.rel FROM files f JOIN docs d ON d.id=f.doc WHERE d.state IN ({states})").fetchall()
        self.assertEqual([row["rel"] for row in waiting], [], where)
        # A document is left as failed only when it holds a work that cannot be read, and every
        # measured file of such a work is in a document that failed or was left to a source.
        for file in db.execute("SELECT f.*,d.state FROM files f JOIN docs d ON d.id=f.doc").fetchall():
            work = world.holds[places[file["root"]] / file["rel"]]
            if file["state"] == "error":
                works = {world.holds[places[other["root"]] / other["rel"]] for other in
                         db.execute("SELECT root,rel FROM files WHERE doc=?", (file["doc"],))}
                self.assertTrue(any(world.broken(held) for held in works), f"{where}: {file['rel']} is marked as failed")
            elif kind == "full" and world.broken(work) and file["sum"] and file["format"] in extract.SUPPORTED:
                self.assertIn(file["state"], ("covered",), f"{where}: {file['rel']} cannot be read and is {file['state']}")
        snapshot = lambda: [db.execute(question).fetchall() for question in (
            "SELECT id,doc,sum,read,asked FROM files ORDER BY id", "SELECT id,state,origin,title FROM docs ORDER BY id",
            "SELECT key,doc,basis FROM doc_keys ORDER BY key", "SELECT id,doc,file,source,ref,keys FROM records ORDER BY id")]
        before, questions = [[tuple(row) for row in rows] for rows in snapshot()], world.questions
        (done, failures), extracted = world.build(kind)
        world.log.pop()
        self.assertEqual((done, failures, extracted, world.questions - questions, world.stumbled[stumbled + len(failed):]),
                         (0, 0, [], 0, []), f"{where}, a second full run")
        self.assertEqual([[tuple(row) for row in rows] for rows in snapshot()], before, f"{where}, a second full run")
        # Files that hold the same bytes, both measured, are one document.
        seen = {}
        for file in db.execute("SELECT * FROM files WHERE coalesce(sum,'')!=''").fetchall():
            work = world.holds[places[file["root"]] / file["rel"]]
            self.assertEqual(seen.setdefault((work, file["format"]), file["doc"]), file["doc"], f"{where}: two documents for work {work}")

    def test_random_histories_never_show_a_text_that_is_not_the_file_s_own(self):
        only = os.environ.get("SHELFFIND_HISTORY")
        seeds = [int(only)] if only else range(int(os.environ.get("SHELFFIND_HISTORIES", "40")))
        for seed in seeds:
            with self.subTest(history=seed):
                self.run_one(seed)


if __name__ == "__main__":
    unittest.main()
