# Copyright 2026 Eudaemonic Enterprises Inc.
"""Random histories of a shelf, each step checked against the truth.

A small world is driven at random: files are written, changed, copied, moved and removed in
three folders, one of which has a storage that tells checksums and may lag behind, hold other
bytes under the same size and second, or fall silent; two sources recognise files, by their
address, rightly or wrongly, or by their checksum, and give one checksum or two, in one order
or the other, now and then the MD5 of one work with the SHA-256 of another; one work in four
is a scan, whose second page is recognised by a later run, and one in seven cannot be read at
all; some names end oddly, hold a line break, or hold a character that some programs take for
one; the index is brought up to date now and then, by an inventory, in full, for one file, or
for the recognition; a folder is forgotten and registered again, and a month passes. The
storage answers as rclone does, in lines that the program has to read. Since the test knows
what every file holds, it can tell after each step whether a file shows a text that is not its
own, page by page, whether a checksum kept is that of the bytes, whether anything read or
recognised was lost or done twice, whether a run did what it reports, and whether the
documents are those that the facts name, whatever the order of the events. Another machine
shares the shelf through a shared place: now and then it gives the pages of a work that it read,
in a parcel, and every complete run takes them and gives what it read in turn.

The number of histories is 40, or what SHELFFIND_HISTORIES says; SHELFFIND_HISTORY replays the
one of a given number."""
import datetime
import gzip
import hashlib
import json
import os
import random
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import invariants
from shelffind import extract, indexer, machine, parcels, sharing, store

SIZES = (600, 900)
FORMATS = ("txt", "txt", "txt", "txt", "md", "md", "bin", "txt#600")
TIMES = (1_700_000_000, 1_700_000_050, 1_700_000_100)


def body(work, size):
    """What the work number `work` holds, at the size `size`: its own word, again and again."""
    text = f"work{work:04d}token " * (size // 14 + 1)
    return text[:size].encode()


OTHER = "0123456789abcdef"     # the label of the other machine


class World:
    """A shelf, its storage, its sources and its index, with the truth about each file."""

    def __init__(self, seed, base):
        self.dice = random.Random(seed)
        self.base = base = base.resolve()
        self.rooms = {"Room": base / "Room", "Shed": base / "Shed", "Vault": base / "Vault"}
        for folder in self.rooms.values():
            folder.mkdir()
        self.data = base / "state"
        self.db = store.connect(self.data)
        for name, folder in self.rooms.items():
            store.add_root(self.db, folder, self.data, name=name)
        self.works = {}             # work -> size
        self.holds = {}             # address -> work
        self.twin = {}              # address in the vault -> (work, size, second), as the storage holds it
        self.liars = set()          # addresses of which the storage held other bytes under the same size and second
        self.silent = False
        self.beliefs = {"library": {}, "catalogue": {}}     # source -> address -> work it takes the file for
        self.known = {"library": set(), "catalogue": set()}     # source -> works it knows by their checksums
        self.styles = {"library": 0, "catalogue": 0}        # which checksums a source gives, and in what order
        self.questions = 0
        self.aged = False
        self.aim = None
        self.extracted = []
        self.recognised = []
        self.stumbled = []
        self.log = []
        self.shared = base / "shared"               # the shared place, and the parcels of another machine
        self.gifts = []

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

    def scan_of(self, work):
        """True for a work that is a scan: its second page waits for a recognition."""
        return work % 4 == 0

    def broken(self, work):
        """True for a work that no reader can read: every reading of it fails."""
        return work % 7 == 3

    def pages_of(self, work):
        """What the pages of a work may hold, as page -> the texts allowed there."""
        text = self.bytes_of(work).decode()
        if self.scan_of(work):
            return {1: {text}, 2: {"", f"work{work:04d}token recognised on page 2"}}
        return {None: {text}}

    def put(self, path, work, when=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.bytes_of(work))
        when = self.dice.choice(TIMES) if when is None else when
        os.utime(path, ns=(when * 10**9 + self.dice.randrange(10**9), when * 10**9 + self.dice.randrange(10**9)))
        self.holds[path] = work
        self.liars.discard(path)

    # ---- the storage and the sources ------------------------------------------------------
    def storage(self, arguments, listing, remote, seconds):
        """What rclone would print about the files listed, from what the storage holds: their
        sizes and dates as JSON, or their MD5, one line for each file."""
        if self.silent:
            return None
        asked = [rel for rel in Path(listing).read_bytes().decode().split("\n") if rel]
        held = {rel: self.twin[self.rooms["Vault"] / rel] for rel in asked if self.rooms["Vault"] / rel in self.twin}
        if arguments[0] == "lsjson":
            stamp = lambda second: datetime.datetime.fromtimestamp(second, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return json.dumps([{"Path": rel, "Size": size, "ModTime": stamp(second)} for rel, (work, size, second) in held.items()],
                              ensure_ascii=False)
        return "".join(f"{self.sums(work)[0][4:]}  {rel}\n" for rel, (work, size, second) in held.items())

    def record(self, name, number, work):
        md5, sha = self.sums(work)
        style = work + self.styles[name] + len(name)
        keys = ([md5, sha], [sha, md5], [md5], [sha])[style % 4]
        if style % 9 == 4:                                  # the MD5 of the work with the SHA-256 of another
            keys = [md5, self.sums(sorted(self.works)[style % len(self.works)])[1]]
        return {"n": number, "id": f"W{work}", "keys": keys,
                "title": f"Title{work:04d} of {name}", "text": name == "library" and work % 3 == 0}

    def question(self, directory, name, op, **fields):
        self.questions += 1
        records = []
        for file in fields["files"]:
            mine = [work for work in sorted(self.known[name]) if set(file["keys"]) & set(self.sums(work))]
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
        # A line break in a name, which no storage is asked about, or a character that is none
        # and that some programs take for one.
        odd = self.dice.choice(("\nx", "\u2028x", "\x85x", "\x0bx", "\x0cx")) if self.dice.random() < 0.06 else ""
        return room / folder / f"f{self.dice.randrange(10**6):06d}{odd}.{self.dice.choice(FORMATS)}"

    def step(self):
        dice = self.dice
        kind = dice.choice(("add", "add", "again", "copy", "move", "rename", "change", "remove", "sync", "sync",
                            "believe", "believe", "learn", "forget", "silence", "lie", "inventory", "inventory",
                            "full", "full", "limited", "relink", "recognise", "targeted", "retry", "gift"))
        if not self.aged and dice.random() < 0.01:
            kind = "age"
        elif dice.random() < 0.015:
            kind = dice.choice(("refold", "month", "restyle"))
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
        elif kind == "gift":
            self.gift()
        elif kind == "restyle":                             # a source comes to give other checksums, or another order
            self.styles[dice.choice(sorted(self.styles))] += 1
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
                    seen = self.dice.random() < 0.5         # its second page may have been recognised already
                    db.execute("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,1,NULL,?,0)",
                               (doc, store.deflate(path.read_bytes().decode())))
                    db.execute("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,2,NULL,?,?)",
                               (doc, store.deflate(f"work{self.holds[path]:04d}token recognised on page 2" if seen else ""), int(not seen)))
                    db.execute("UPDATE docs SET state=? WHERE id=?", ("indexed" if seen else "needs-ocr", doc))
                elif read:
                    db.execute("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,NULL,NULL,?,0)",
                               (doc, store.deflate(path.read_bytes().decode())))
                    db.execute("UPDATE docs SET state='indexed' WHERE id=?", (doc,))
                db.execute("UPDATE files SET doc=?,sum=NULL,asked='',read=? WHERE id=?", (doc, int(read), file["id"]))
                db.execute("UPDATE names SET title='',byline='' WHERE rowid=?", (file["id"],))
            db.execute("DELETE FROM docs WHERE NOT EXISTS (SELECT 1 FROM files f WHERE f.doc=docs.id)")

    def gift(self):
        """Another machine gives, in a parcel of its own, the true pages of a work it read, under
        a readable format: a work that a file of this shelf holds, or one that none holds yet. A
        work that no reader can read is given by nobody."""
        works = [work for work in sorted(self.works) if not self.broken(work)]
        if not works:
            return
        work = self.dice.choice(works)
        fmt = self.dice.choice(("txt", "md"))
        body = self.bytes_of(work).decode()
        if self.scan_of(work):
            seen = self.dice.random() < 0.5
            pages = [[1, None, body, 0], [2, None, f"work{work:04d}token recognised on page 2" if seen else "", int(not seen)]]
        else:
            pages = [[None, None, body, 0]]
        md5, sha = self.sums(work)
        line = {"md5": md5, "sha256": sha, "size": self.works[work], "format": fmt, "pages": pages}
        header = {"format": parcels.FORMAT, "writer": OTHER}
        data = gzip.compress((json.dumps(header) + "\n" + json.dumps(line) + "\n").encode())
        self.gifts.append({"name": f"texts/{OTHER}/{len(self.gifts) + 1:06d}.jsonl.gz", "size": len(data),
                           "sha256": hashlib.sha256(data).hexdigest(), "documents": 1})
        place = sharing.Folder(self.shared)
        place.put(self.gifts[-1]["name"], data)
        place.put(f"texts/{OTHER}.json", json.dumps({"format": 1, "writer": OTHER, "parcels": self.gifts}).encode())

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
        exchange = parcels.Exchange(self.data, self.db, sharing.Folder(self.shared))
        done = indexer.build(self.db, self.data, store.roots(self.db), progress=lambda _: None, exchange=exchange, **options)
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
                world.recognised.append((held, fmt, number))
                return f"work{held:04d}token recognised on page {number}"

            usable = lambda directory, progress=None: (
                {"library": ["library"], "catalogue": ["catalogue"]}, {"library": True, "catalogue": False}, set())
            policy = lambda name: {"text": "trust" if name == "library" else "ignore", "identify": True}
            store.declare({"Vault": {"path": str(world.rooms["Vault"]), "remote": "twin:Vault"}})
            try:
                with patch("shelffind.extract.passages", side_effect=reading), \
                        patch("shelffind.extract.recognize", side_effect=recognising), \
                        patch("shelffind.indexer.storage", side_effect=world.storage), \
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

    def texts(self, world, where):
        """Document -> (the work its pages hold, page -> text), after checking that the pages
        of each document are those of one work, each with a text that this page may have."""
        db, rows = world.db, {}
        for row in db.execute("SELECT doc,page,ztext,needs_ocr FROM pages ORDER BY doc,id"):
            rows.setdefault(row["doc"], []).append((row["page"], store.inflate(row["ztext"]), row["needs_ocr"]))
        found = {}
        sealed = dict(db.execute("SELECT id,origin FROM docs"))
        for doc, pages in rows.items():
            first = pages[0][1]
            self.assertTrue(first.startswith("work") and first[8:13] == "token", f"{where}: document {doc} begins with no work")
            work = int(first[4:8])
            # The pages are tied to the bytes of the work they hold, and to no other, with or without a file.
            self.assertIn(sealed[doc], (None, " ".join(world.sums(work)), " ".join(world.sums(work)) + f" {store.INHERITED}"),
                          f"{where}: document {doc} holds work {work} under other checksums")
            allowed = world.pages_of(work)
            self.assertEqual(sorted(str(page) for page, _, _ in pages), sorted(str(page) for page in allowed),
                             f"{where}: document {doc} does not have the pages of work {work}")
            for page, text, blank in pages:
                self.assertIn(text, allowed[page], f"{where}: page {page} of document {doc} is not a page of work {work}")
                self.assertEqual(bool(blank), text == "", f"{where}: page {page} of document {doc} is wrongly marked")
            found[doc] = (work, {page: text for page, text, _ in pages})
        return found

    def check(self, world, kind, seed):
        db = world.db
        where = f"history {seed}, after {' '.join(world.log)} {kind}"
        places = {root["id"]: Path(root["path"]) for root in store.roots(db)}
        address = lambda file: places[file["root"]] / file["rel"]
        sealed = {(row["origin"], row["format"]) for row in db.execute("SELECT origin,format FROM docs WHERE origin IS NOT NULL")}
        complete = {(row["origin"], row["format"], row["page"]) for row in db.execute(
            "SELECT d.origin,d.format,p.page FROM pages p JOIN docs d ON d.id=p.doc WHERE d.origin IS NOT NULL AND p.needs_ocr=0")}
        # What each file shows of its own before the run: it is not to lose any of it.
        shown = self.texts(world, where + " (before the run)")
        wrong = world.liars | world.misled()
        own = {}
        for file in db.execute("SELECT * FROM files").fetchall():
            if file["doc"] in shown and (file["sum"] or file["read"]) and file["root"] in places and address(file) not in wrong:
                # (the file is known again by its address and its date: its number may be given to another)
                own[(file["root"], file["rel"], file["size"], file["mtime"])] = {
                    page: text for page, text in shown[file["doc"]][1].items() if text}
        recognised, stumbled = len(world.recognised), len(world.stumbled)
        (done, failures), extracted = world.build(kind)
        # A reading fails only for a work that cannot be read, once in a run for each such
        # document, and each failure is counted.
        failed = world.stumbled[stumbled:]
        self.assertEqual((failures, len(set(failed))), (len(failed), len(failed)), where)
        self.assertEqual(invariants.structure(db), [], where)
        self.assertEqual(invariants.actual(db), invariants.canonical(db), where)
        self.assertEqual(sorted(path.name for path in world.data.glob("copies*")), [], f"{where}: copies are left")
        # The run did what it reports: one reading for each document read, recognised or found unreadable.
        works = {(work, fmt) for work, fmt, _ in world.recognised[recognised:]}
        self.assertEqual(done, len(works) if kind == "recognise" else len(extracted) + len(failed), f"{where}: the run reports {done}")
        # Nothing is read twice, in one run or while the text read from its bytes is kept; and
        # no page is recognised twice.
        self.assertEqual(len(extracted), len(set(extracted)), f"{where}: read twice in one run")
        for work, fmt in extracted:
            self.assertNotIn((" ".join(world.sums(work)), fmt), sealed, f"{where}: work {work} was read again")
        lately = world.recognised[recognised:]
        self.assertEqual(len(lately), len(set(lately)), f"{where}: recognised twice in one run")
        for work, fmt, page in lately:
            self.assertNotIn((" ".join(world.sums(work)), fmt, page), complete, f"{where}: page {page} of work {work} was recognised again")
        # No file shows a text that is not its own, unless a source says, of a file never
        # measured, a checksum that the document shown is known by.
        places = {root["id"]: Path(root["path"]) for root in store.roots(db)}
        shown = self.texts(world, where)
        for file in db.execute("SELECT * FROM files").fetchall():
            path = address(file)
            work = world.holds[path]
            if path in world.liars:
                continue
            if file["sum"]:                                 # each checksum kept is that of the bytes
                self.assertLessEqual(set(file["sum"].split()), set(world.sums(work)), f"{where}: a wrong measure of {file['rel']!r}")
            if file["doc"] in shown and shown[file["doc"]][0] != work:
                known = {row["key"] for row in db.execute("SELECT key FROM doc_keys WHERE doc=? AND basis='content'", (file["doc"],))}
                words = any(known & set(record["keys"].split()) for record in db.execute("SELECT keys FROM records WHERE file=?", (file["id"],)))
                self.assertTrue(not file["sum"] and words, f"{where}: {file['rel']!r} holds work {work} and shows {shown[file['doc']][0]}")
            # What it showed of its own, it still shows.
            for page, text in own.get((file["root"], file["rel"], file["size"], file["mtime"]), {}).items():
                self.assertEqual(shown.get(file["doc"], (None, {}))[1].get(page), text, f"{where}: {file['rel']!r} lost its page {page}")
        # Files that hold the same bytes, both measured, are one document.
        seen = {}
        for file in db.execute("SELECT * FROM files WHERE coalesce(sum,'')!=''").fetchall():
            if address(file) not in world.liars:
                work = world.holds[address(file)]
                self.assertEqual(seen.setdefault((work, file["format"]), file["doc"]), file["doc"], f"{where}: two documents for work {work}")
        # What this data folder gave carries, under the checksums of a work, pages of that work
        # alone, each with a text that this page may have.
        try:
            writer = json.loads((world.data / machine.WRITING).read_text())["writer"]
        except OSError:
            writer = None
        for parcel in sorted((world.shared / "texts" / writer).glob("*.jsonl.gz")) if writer else ():
            for raw in parcels.unpack(parcel.read_bytes())[1:]:
                told = parcels.document(raw)
                texts = [body for _, _, body, blank in told["pages"] if not blank]
                self.assertTrue(texts and all(body.startswith("work") for body in texts), f"{where}: {parcel.name} gives no work")
                work = int(texts[0][4:8])
                self.assertEqual((told["md5"], told["sha256"]), tuple(world.sums(work)), f"{where}: {parcel.name} gives work {work} "
                                                                                    "under other checksums")
                allowed = world.pages_of(work)
                for page, _, body, blank in told["pages"]:
                    self.assertTrue(blank or body in allowed.get(page, ()), f"{where}: {parcel.name} gives a page {page} "
                                                                             f"that work {work} does not have")
        if kind not in ("full", "recognise"):
            return
        # A complete run leaves nothing waiting, whatever the storage does.
        states = "'needs-ocr','ocr-error'" if kind == "recognise" else "'pending'"
        waiting = db.execute(f"SELECT f.rel FROM files f JOIN docs d ON d.id=f.doc WHERE d.state IN ({states})").fetchall()
        self.assertEqual([row["rel"] for row in waiting], [], where)
        # In the folders without storage, every statement that gives a checksum is proved by
        # then: its file was measured by a checksum of a kind it gives.
        vault = next(root["id"] for root in store.roots(db) if root["name"] == "Vault")
        proofs = db.execute("SELECT r.keys,f.rel,f.sum,f.root FROM records r JOIN files f ON f.id=r.file WHERE r.keys!=''").fetchall()
        for record in proofs:
            if record["root"] != vault:
                self.assertTrue(set(invariants.kinds(record["keys"].split())) & set(invariants.kinds((record["sum"] or "").split())),
                                f"{where}: what is said of {record['rel']!r} was not proved")
        liars = {path for path in world.liars if path in world.holds}
        if world.silent or liars:
            return
        current = {path: (work, path.stat().st_size, path.stat().st_mtime_ns // 10**9)
                   for path, work in world.holds.items() if path.is_relative_to(world.rooms["Vault"])}
        if current != {path: held for path, held in world.twin.items() if path in world.holds}:
            return
        # With a storage that is up to date, the same holds of its folder, and a second run finds nothing to do.
        # A document is left as failed only when it holds a work that cannot be read, and every
        # measured file of such a work is in a document that failed or was left to a source.
        for file in db.execute("SELECT f.*,d.state FROM files f JOIN docs d ON d.id=f.doc").fetchall():
            work = world.holds[address(file)]
            if file["state"] == "error":
                works = {world.holds[address(other)] for other in db.execute("SELECT root,rel FROM files WHERE doc=?", (file["doc"],))}
                self.assertTrue(any(world.broken(held) for held in works), f"{where}: {file['rel']!r} is marked as failed")
            elif kind == "full" and world.broken(work) and file["sum"] and file["format"] in extract.SUPPORTED:
                self.assertIn(file["state"], ("covered",), f"{where}: {file['rel']!r} cannot be read and is {file['state']}")
        # Every statement that gives a checksum is proved: its file was measured by a checksum
        # of a kind it gives, and a source answers for a text only by such a statement.
        for record in proofs:
            self.assertTrue(set(invariants.kinds(record["keys"].split())) & set(invariants.kinds((record["sum"] or "").split())),
                            f"{where}: what is said of {record['rel']!r} was not proved")
        # Two statements kept with one document never give two checksums of one kind: a reading settled it.
        for doc in db.execute("SELECT DISTINCT doc FROM records WHERE keys!=''").fetchall():
            given = [key for record in db.execute("SELECT keys FROM records WHERE doc=?", (doc["doc"],)) for key in record["keys"].split()]
            self.assertTrue(all(len(found) == 1 for found in invariants.kinds(given).values()),
                            f"{where}: document {doc['doc']} keeps statements that contradict one another")
        for doc in db.execute("SELECT id FROM docs d WHERE state='covered' AND EXISTS (SELECT 1 FROM files f WHERE f.doc=d.id)").fetchall():
            self.assertTrue(db.execute("SELECT 1 FROM records WHERE doc=? AND text=1 AND keys!='' AND source='library'", (doc["id"],)).fetchone(),
                            f"{where}: document {doc['id']} is left to a source that gave no checksum for it")
        snapshot = lambda: [db.execute(question).fetchall() for question in (
            "SELECT id,doc,sum,read,asked FROM files ORDER BY id", "SELECT id,state,origin,title FROM docs ORDER BY id",
            "SELECT key,format,size,basis,doc FROM doc_keys ORDER BY key,format,basis,size",
            "SELECT id,doc,file,source,ref,keys FROM records ORDER BY id")]
        before, questions = [[tuple(row) for row in rows] for rows in snapshot()], world.questions
        seen = (len(world.stumbled), len(world.recognised))
        (done, failures), extracted = world.build(kind)
        world.log.pop()
        self.assertEqual((done, failures, extracted, world.questions - questions, len(world.stumbled), len(world.recognised)),
                         (0, 0, [], 0, *seen), f"{where}, a second run")
        self.assertEqual([[tuple(row) for row in rows] for rows in snapshot()], before, f"{where}, a second run")

    def test_random_histories_never_show_a_text_that_is_not_the_file_s_own(self):
        only = os.environ.get("SHELFFIND_HISTORY")
        seeds = [int(only)] if only else range(int(os.environ.get("SHELFFIND_HISTORIES", "40")))
        for seed in seeds:
            with self.subTest(history=seed):
                self.run_one(seed)


if __name__ == "__main__":
    unittest.main()
