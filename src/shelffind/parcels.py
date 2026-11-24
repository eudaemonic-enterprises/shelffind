# Copyright 2026 Eudaemonic Enterprises Inc.
"""The text that one machine read, for the other machines.

When the settings share texts (`share.texts`), a machine puts the pages it read in the shared
place of the journals (see `sharing`), in parcels that are never changed once written:

    texts/LABEL/NNNNNN.jsonl.gz    a line naming the format and the machine, then one line for
                                   each document: the MD5 and the SHA-256 of the bytes its pages
                                   were read from, their size and format, and the pages

The card of the machine, `machines/LABEL.json`, names its parcels with their size and their
SHA-256; only the machine itself writes there, so no two machines ever write the same file.
Another machine takes a parcel only when it has the checksum that the card gives: a parcel cut,
changed or missing is taken another time, and one of a newer format is left for a newer
version of ShelfFind.

The pages of a parcel go to the document of this machine that a measure knows by the same
checksums (`store.taker`), when that document waits for its text or has more pages still to
recognise; a page recognised on one side only completes the other (`store.take_pages`). They
never go to a document that a source answers for, nor to bytes that a measure contradicts.
What cannot be placed yet stays offered: the files of the sizes and formats offered are compared
first by a complete run (`indexer.waiting`), and the pages are placed once a file is known to
hold those bytes. A document goes into a parcel when its reading went further here than
anything that the shared place holds of the same bytes, so what one machine received it never
sends back.

What this machine knows of the parcels is kept in `exchange.sqlite3` beside the index: which
parcels it took, and what each one holds. That file may be deleted at any time: the parcels are
then taken again, and nothing is read twice.
"""
import contextlib
import hashlib
import json
import re
import sqlite3
import time
import zlib
from pathlib import Path

from . import extract, machine, sharing, store

FORMAT = 1
LIMIT = 16 << 20        # bytes of one parcel at most, unless one document alone is larger
UNPACKED = 1 << 30      # bytes that one parcel may unpack to
PAGES = 200_000         # pages of one document at most
EVERY = 120             # seconds between two exchanges while documents are being read
KEEPING = "exchange.sqlite3"
NAME = re.compile(r"texts/([0-9a-f]{16})/(\d{6})\.jsonl\.gz\Z")
MD5 = re.compile(r"md5:[0-9a-f]{32}\Z")
SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
WAITING = ("pending", "error", "needs-ocr", "ocr-error")

SCHEMA = """
CREATE TABLE IF NOT EXISTS parcels (
    machine TEXT NOT NULL, name TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    documents INTEGER NOT NULL, state TEXT NOT NULL, PRIMARY KEY (machine, name)
);
CREATE TABLE IF NOT EXISTS entries (
    machine TEXT NOT NULL, name TEXT NOT NULL, line INTEGER NOT NULL, md5 TEXT NOT NULL,
    sha256 TEXT NOT NULL, size INTEGER NOT NULL, format TEXT NOT NULL, pages INTEGER NOT NULL,
    blanks INTEGER NOT NULL, PRIMARY KEY (machine, name, line)
);
CREATE INDEX IF NOT EXISTS entries_bytes ON entries(md5, format);
"""


class Packer:
    """One parcel being made, in the gzip format, one line for each document; its size is
    known after each document."""

    def __init__(self, label):
        self.zip = zlib.compressobj(6, zlib.DEFLATED, 31)
        self.data, self.count = bytearray(), 0
        self.put(json.dumps({"format": FORMAT, "machine": label}).encode() + b"\n")

    def put(self, raw):
        self.data += self.zip.compress(raw)
        self.data += self.zip.flush(zlib.Z_SYNC_FLUSH)

    def add(self, raw):
        """Add the line of one document. False, with nothing added, when the parcel would grow
        beyond LIMIT; a document alone in its parcel is always added."""
        kept, length = self.zip.copy(), len(self.data)
        self.put(raw)
        if self.count and len(self.data) + 32 > LIMIT:
            self.zip = kept
            del self.data[length:]
            return False
        self.count += 1
        return True

    def close(self):
        self.data += self.zip.flush()
        return bytes(self.data)


def unpack(data):
    """The lines of a parcel. ValueError when it is not a whole gzip stream, or unpacks to
    more than UNPACKED bytes."""
    opened, out = zlib.decompressobj(31), bytearray()
    try:
        for start in range(0, len(data), 1 << 20):
            out += opened.decompress(data[start:start + (1 << 20)], UNPACKED + 1 - len(out))
            if len(out) > UNPACKED or opened.unconsumed_tail:
                raise ValueError("The parcel unpacks to more than it may")
        out += opened.flush()
    except zlib.error as fault:
        raise ValueError(f"The parcel is damaged: {fault}") from None
    if not opened.eof or opened.unused_data:
        raise ValueError("The parcel is cut or followed by other bytes")
    lines = bytes(out).split(b"\n")
    if lines and not lines[-1]:
        lines.pop()
    return lines


def document(raw):
    """What the line of a parcel gives, as {"md5", "sha256", "size", "format", "pages"} with
    pages as (page, section, text, still to recognise); None when the line is not one that
    this version writes."""
    try:
        told = json.loads(raw)
        md5, sha, size, fmt, pages = told["md5"], told["sha256"], told["size"], told["format"], told["pages"]
    except (ValueError, KeyError, TypeError):
        return None
    if not (isinstance(md5, str) and MD5.match(md5) and isinstance(sha, str) and SHA.match(sha)
            and type(size) is int and size >= 0 and isinstance(fmt, str) and fmt in extract.SUPPORTED
            and isinstance(pages, list) and 0 < len(pages) <= PAGES):
        return None
    clean = []
    for page in pages:
        if not (isinstance(page, list) and len(page) == 4):
            return None
        number, section, body, blank = page
        if not ((number is None or (type(number) is int and number > 0)) and (section is None or isinstance(section, str))
                and isinstance(body, str) and type(blank) in (int, bool) and blank in (0, 1)):
            return None
        clean.append((number, section, body, int(blank)))
    return {"md5": md5, "sha256": sha, "size": size, "format": fmt, "pages": clean}


def listed(card, label):
    """The parcels that a card names, as dicts with name, size and sha256; what is not a
    parcel of that machine is left aside."""
    found = []
    for parcel in card.get("parcels") or () if isinstance(card, dict) else ():
        if not isinstance(parcel, dict):
            continue
        name, size, digest = parcel.get("name"), parcel.get("size"), parcel.get("sha256")
        given = NAME.match(name) if isinstance(name, str) else None
        if given and given.group(1) == label and type(size) is int and size > 0 \
                and isinstance(digest, str) and DIGEST.match(digest):
            found.append({"name": name, "size": size, "sha256": digest,
                          "documents": parcel.get("documents") if type(parcel.get("documents")) is int else 0})
    return found


class Exchange:
    """The exchange of texts between this machine and the others, through the shared place
    `place`, for the index `db` of the data folder `directory`. `progress(text)` receives the
    messages. The counts of what was done are kept in `counts`."""

    def __init__(self, directory, db, place, progress=lambda text: None, name=None):
        self.directory, self.db, self.place, self.say = Path(directory), db, place, progress
        self.me = machine.identity(self.directory, name)
        self.label = self.me["id"]
        self.last = time.monotonic()
        self.counts = dict.fromkeys(("sent", "received", "damaged", "newer"), 0)
        keeping = sqlite3.connect(self.directory / KEEPING)
        try:
            keeping.executescript(SCHEMA)
        finally:
            keeping.close()

    @contextlib.contextmanager
    def attached(self):
        """The bookkeeping of the exchange, reachable from the index as `exchange`."""
        self.db.execute("ATTACH DATABASE ? AS exchange", (str(self.directory / KEEPING),))
        try:
            yield
        finally:
            self.db.execute("DETACH DATABASE exchange")

    def due(self):
        """True when the last exchange is old enough for another during a reading."""
        return time.monotonic() - self.last >= EVERY

    def run(self, *steps):
        """Do the steps of an exchange, among "receive", "apply" and "send", unless another
        process of this data folder is exchanging at this moment. Returns True when they were
        done. A shared place that fails is said, and the exchange is taken up next time: it is
        no fault of the documents."""
        with sharing.alone(self.directory) as free:
            return free and self.steps(*steps)

    def steps(self, *steps):
        """The steps of `run`, for a caller that holds the exchange already."""
        self.last = time.monotonic()
        with self.attached():
            try:
                for step in steps:
                    getattr(self, step)()
            except (OSError, ValueError) as fault:
                self.say(f"The texts could not be exchanged this time: {fault}")
                return False
        return True

    def sizes(self):
        """The sizes and formats of the documents that the other machines offer and that this
        one does not know by a measure: files of these are worth comparing."""
        with self.attached():
            return frozenset((row[0], row[1]) for row in self.db.execute(
                "SELECT DISTINCT e.size,e.format FROM exchange.entries e WHERE e.machine!=? AND NOT EXISTS "
                "(SELECT 1 FROM doc_keys k WHERE k.key=e.md5 AND k.format=e.format AND k.basis='content')",
                (self.label,)).fetchall())

    # ---- taking -----------------------------------------------------------------------------
    def receive(self):
        """Take the parcels of the other machines that this one has not taken yet, and give
        their pages to the documents that wait for them. The parcels that this machine gave
        are taken as well when it no longer remembers them, its bookkeeping having been
        deleted: it then knows again what the shared place holds, and gives nothing twice."""
        for entry in self.place.names("machines"):
            label = entry.removesuffix(".json")
            if not machine.LABEL.match(label):
                continue
            try:
                card = json.loads(self.place.get(f"machines/{entry}") or b"{}")
            except ValueError:
                self.counts["damaged"] += 1
                continue
            for parcel in listed(card, label):
                row = self.db.execute("SELECT sha256,state FROM exchange.parcels WHERE machine=? AND name=?",
                                      (label, parcel["name"])).fetchone()
                if row is not None and row["sha256"] == parcel["sha256"]:
                    if row["state"] == "taken":
                        continue
                    if row["state"].startswith("newer:") and int(row["state"][6:]) > FORMAT:
                        self.counts["newer"] += 1
                        continue
                self.take(label, parcel)

    def fetch(self, parcel):
        """The lines of a parcel after the header, or None when it is not there as its card
        says (counted as damaged), or is of a newer format (counted, and remembered)."""
        data = self.place.get(parcel["name"])
        if data is None or len(data) != parcel["size"] or hashlib.sha256(data).hexdigest() != parcel["sha256"]:
            self.counts["damaged"] += 1         # cut, changed or missing: taken another time
            return None
        try:
            lines = unpack(data)
            header = json.loads(lines[0])
            made, author = header["format"], header["machine"]
        except (ValueError, KeyError, TypeError, IndexError):
            self.counts["damaged"] += 1
            return None
        if type(made) is not int or author != NAME.match(parcel["name"]).group(1):
            self.counts["damaged"] += 1
            return None
        if made > FORMAT:
            with self.db:
                self.remember(author, parcel, f"newer:{made}")
            self.counts["newer"] += 1
            return None
        return lines

    def remember(self, label, parcel, state):
        self.db.execute("INSERT OR REPLACE INTO exchange.parcels(machine,name,sha256,size,documents,state) VALUES(?,?,?,?,?,?)",
                        (label, parcel["name"], parcel["sha256"], parcel["size"], parcel.get("documents", 0), state))

    def take(self, label, parcel):
        """Take one parcel of another machine: what it holds is remembered, and its pages are
        given to the documents that gain by them. A parcel is marked as taken last, so that
        one taken in part is taken again, with nothing given twice."""
        lines = self.fetch(parcel)
        if lines is None:
            return
        for number, raw in enumerate(lines[1:], 1):
            told = document(raw)
            if told is None:
                continue                        # a line that this version cannot read is left aside
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO exchange.entries(machine,name,line,md5,sha256,size,format,pages,blanks) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (label, parcel["name"], number, told["md5"], told["sha256"], told["size"], told["format"],
                     len(told["pages"]), sum(page[3] for page in told["pages"])))
                self.give(told)
        with self.db:
            self.remember(label, parcel, "taken")

    def give(self, told):
        """Give the pages of one document of a parcel to the document of this machine that
        gains by them. Returns True when they were given."""
        keys = [told["md5"], told["sha256"]]
        doc = store.taker(self.db, keys, told["size"], told["format"])
        if doc is None or not store.gains(self.db, doc, told["pages"]):
            return False
        store.take_pages(self.db, doc, keys, told["pages"])
        self.counts["received"] += 1
        return True

    def apply(self):
        """Give the documents of this machine that wait for their text the pages that a parcel
        taken earlier offers for the same bytes: a file measured since may have made them
        known, or a source may have stopped answering for them."""
        rows = self.db.execute(
            "SELECT e.machine,e.name,e.line FROM exchange.entries e "
            "JOIN doc_keys k ON k.key=e.md5 AND k.format=e.format AND k.basis='content' JOIN docs d ON d.id=k.doc "
            "JOIN exchange.parcels p ON p.machine=e.machine AND p.name=e.name AND p.state='taken' "
            f"WHERE e.machine!=? AND d.size=e.size AND d.state IN ({','.join('?' * len(WAITING))}) "
            "AND (d.state IN ('pending','error') OR e.blanks<=(SELECT sum(needs_ocr) FROM pages WHERE doc=d.id)) "
            "ORDER BY e.blanks,e.machine,e.name,e.line", (self.label, *WAITING)).fetchall()
        wanted = {}
        for row in rows:
            wanted.setdefault((row["machine"], row["name"]), []).append(row["line"])
        for (label, name), numbers in wanted.items():
            known = self.db.execute("SELECT name,size,sha256 FROM exchange.parcels WHERE machine=? AND name=?",
                                    (label, name)).fetchone()
            lines = self.fetch(dict(known))
            if lines is None:
                continue
            for number in numbers:
                told = document(lines[number]) if number < len(lines) else None
                if told is not None:
                    with self.db:
                        self.give(told)

    # ---- giving -------------------------------------------------------------------------------
    def send(self):
        """Put in the shared place the pages of the documents whose reading went further here
        than anything that the shared place holds of the same bytes, in parcels of at most
        LIMIT bytes."""
        shipped = {(row[0], row[1], row[2]): row[3] for row in self.db.execute(
            "SELECT md5,sha256,format,min(blanks) FROM exchange.entries GROUP BY md5,sha256,format").fetchall()}
        packer, given = None, []
        for doc in self.db.execute("SELECT id,origin,size,format FROM docs d WHERE origin IS NOT NULL "
                                   "AND EXISTS (SELECT 1 FROM pages p WHERE p.doc=d.id) ORDER BY id").fetchall():
            keys = store.sums(doc["origin"])
            if set(keys) != {"md5", "sha256"} or doc["format"] not in extract.SUPPORTED:
                continue
            ours = store.blanks(self.db, doc["id"])[0]
            if shipped.get((keys["md5"], keys["sha256"], doc["format"]), ours + 1) <= ours:
                continue
            pages = [[row["page"], row["section"], store.inflate(row["ztext"]), row["needs_ocr"]] for row in self.db.execute(
                "SELECT page,section,ztext,needs_ocr FROM pages WHERE doc=? ORDER BY id", (doc["id"],)).fetchall()]
            raw = json.dumps({"md5": keys["md5"], "sha256": keys["sha256"], "size": doc["size"], "format": doc["format"],
                              "pages": pages}, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            line = (keys["md5"], keys["sha256"], doc["size"], doc["format"], len(pages), ours)
            if packer is None:
                packer = Packer(self.label)
            if not packer.add(raw):
                self.ship(packer, given)
                packer, given = Packer(self.label), []
                packer.add(raw)
            given.append(line)
            shipped[(keys["md5"], keys["sha256"], doc["format"])] = ours
        if given:
            self.ship(packer, given)

    def ship(self, packer, given):
        """Put one parcel in the shared place, then name it on the card of this machine, which
        another machine reads before taking it; what it holds is remembered here last."""
        data = packer.close()
        try:
            card = json.loads(self.place.get(f"machines/{self.label}.json") or b"{}")
            card = card if isinstance(card, dict) else {}
        except ValueError:
            card = {}                           # rebuilt below from what this machine remembers
        parcels = {parcel["name"]: parcel for parcel in listed(card, self.label)}
        for row in self.db.execute("SELECT name,size,sha256,documents FROM exchange.parcels WHERE machine=? AND state='taken'",
                                   (self.label,)).fetchall():
            parcels.setdefault(row["name"], dict(row))
        number = 1 + max((int(NAME.match(name).group(2)) for name in parcels), default=0)
        parcel = {"name": f"texts/{self.label}/{number:06d}.jsonl.gz", "size": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(), "documents": len(given)}
        self.place.put(parcel["name"], data)
        parcels[parcel["name"]] = parcel
        card.update(id=self.label, name=self.me["name"], updated=int(time.time()),
                    parcels=[parcels[name] for name in sorted(parcels)])
        sharing.put_card(self.place, self.label, card)
        with self.db:
            self.remember(self.label, parcel, "taken")
            self.db.executemany(
                "INSERT OR REPLACE INTO exchange.entries(machine,name,line,md5,sha256,size,format,pages,blanks) "
                "VALUES(?,?,?,?,?,?,?,?,?)", [(self.label, parcel["name"], number, *line) for number, line in enumerate(given, 1)])
        self.counts["sent"] += len(given)
        self.say(f"{len(given):,} documents put in the shared place ({len(data) / 1e6:,.1f} MB)")
