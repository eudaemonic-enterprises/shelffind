# Copyright 2026 Eudaemonic Enterprises Inc.
"""The text that one machine read, for the other machines.

When the settings share texts (`share.texts`), each data folder puts the pages it read in the
shared place of the journals (see `sharing`), under a name of its own, the writer, which no
other data folder uses, even on the same machine (see `machine.identity`):

    texts/WRITER.json              the list of its parcels, each with its size and SHA-256
    texts/WRITER/NNNNNN.jsonl.gz   a parcel: a line naming the format and the writer, then one
                                   line for each document: the MD5 and the SHA-256 of the bytes
                                   its pages were read from, their size and format, and the pages

A parcel is never changed once written, and its number is never given twice. Only its writer
writes in its folder and its list, so no two writers ever write the same file. The lists are
apart from the cards of the machines that the journals rewrite. A parcel holds at most LIMIT
bytes, unless it holds one document alone, and unpacks to at most UNPACKED bytes; a document
of more than PAGES pages is not shared. Another data folder takes a parcel only when it has the
size and the checksum that the list gives: a parcel cut, changed or missing is taken another
time, and one of a newer format waits for a newer version of ShelfFind. Nothing in a parcel
names a file or a folder; its pages carry the whole text that was read. The shared place is
trusted: anyone who may write there may put texts there.

Taking a parcel only remembers what it offers. The pages are given by a run of the index, once
its inventory is made and its sources asked, to the document that a measure of this machine
knows by the same checksums, when that document has a file, waits for its text, and is not
left to a source that holds its text (`store.taker`, `store.take_pages`): a document without
pages takes the reading whole; one with pages takes, page by page, the pages it still has to
recognise from a reading of the same pages, so that the readings of several machines add up in
whatever order they come. What cannot be given yet stays offered: a complete run compares the
files of the sizes and formats offered (`indexer.waiting`), and a file copied for its reading is
given an offer of its very bytes before anything is extracted from it. A document goes into a
parcel when its reading recognises pages that nothing in the shared place offers for the same
bytes and pages, so that what one data folder received it never sends back.

What a data folder knows of the parcels is kept in `exchange.sqlite3` beside its index, for one
shared place: which parcels it took, what each one offers, and what it compared of its own
documents. It is made anew when the shared place changes, and may be deleted while no run and no
exchange is under way: the lists are then read again, and nothing is read twice. Its own list
is checked at each exchange against its parcels, which are named again when the list was lost,
and given again when they were lost.
"""
import contextlib
import hashlib
import json
import re
import sqlite3
import time
import zlib
from pathlib import Path

from . import extract, link, machine, sharing, store

FORMAT = 1
LIMIT = 16 << 20        # bytes of one parcel at most, unless it holds one document alone
UNPACKED = 256 << 20    # bytes that one parcel may unpack to
LISTED = 16 << 20       # bytes of a list of parcels at most
PAGES = 100_000         # pages of one document at most
BIG = 1 << 53           # no size, page number or count goes beyond this
EVERY = 120             # seconds between two exchanges while documents are being read
WAIT = 120              # seconds that a run waits for an exchange of another process to end
PAUSE = 900             # seconds without exchange after the shared place failed
KEEPING = "exchange.sqlite3"
LAYOUT = "2"            # the form of the bookkeeping: another one is thrown away and made anew
WRITER = re.compile(r"[0-9a-f]{16}\Z")
NAME = re.compile(r"texts/([0-9a-f]{16})/(\d{6})\.jsonl\.gz\Z")
NUMBERED = re.compile(r"(\d{6})\.jsonl\.gz\Z")
MD5 = re.compile(r"md5:[0-9a-f]{32}\Z")
SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
WAITING = ("pending", "error", "needs-ocr", "ocr-error")
PAGED = {"pdf", "djvu", "ps", "ps.gz"}      # formats read page by page
SCHEMA = """
CREATE TABLE IF NOT EXISTS exchange.notes (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exchange.parcels (
    writer TEXT NOT NULL, name TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    documents INTEGER NOT NULL, state TEXT NOT NULL, PRIMARY KEY (writer, name)
);
CREATE TABLE IF NOT EXISTS exchange.entries (
    writer TEXT NOT NULL, name TEXT NOT NULL, line INTEGER NOT NULL, md5 TEXT NOT NULL,
    sha256 TEXT NOT NULL, size INTEGER NOT NULL, format TEXT NOT NULL, layout TEXT NOT NULL,
    pages INTEGER NOT NULL, blanks TEXT NOT NULL, PRIMARY KEY (writer, name, line)
);
CREATE INDEX IF NOT EXISTS exchange.entries_bytes ON entries(md5, format);
CREATE TABLE IF NOT EXISTS exchange.compared (
    md5 TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, format TEXT NOT NULL,
    pages INTEGER NOT NULL, blanks INTEGER NOT NULL, PRIMARY KEY (md5, sha256, size, format)
);
"""


def positions(text):
    """The positions written as "2,5,7"."""
    return {int(one) for one in text.split(",") if one}


def written(found):
    return ",".join(str(one) for one in sorted(found))


class Packer:
    """One parcel being made, in the gzip format, one line for each document; its size and the
    size it unpacks to are known after each document."""

    def __init__(self, writer):
        self.zip = zlib.compressobj(6, zlib.DEFLATED, 31)
        self.data, self.count, self.unpacked = bytearray(), 0, 0
        self.put(json.dumps({"format": FORMAT, "writer": writer}).encode() + b"\n")

    def put(self, raw):
        self.data += self.zip.compress(raw)
        self.data += self.zip.flush(zlib.Z_SYNC_FLUSH)
        self.unpacked += len(raw)

    def add(self, raw):
        """Add the line of one document. False, with nothing added, when the parcel would grow
        beyond LIMIT or unpack beyond UNPACKED; a document alone in its parcel is always added
        (the caller gives no line that would unpack beyond UNPACKED alone)."""
        kept, length, unpacked = self.zip.copy(), len(self.data), self.unpacked
        self.put(raw)
        if self.count and (len(self.data) + 32 > LIMIT or self.unpacked > UNPACKED):
            self.zip, self.unpacked = kept, unpacked
            del self.data[length:]
            return False
        self.count += 1
        return True

    def close(self):
        self.data += self.zip.flush()
        return bytes(self.data)


def unpack(data):
    """The lines of a parcel. ValueError when it is not one whole gzip stream, or unpacks to
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
    lines = out.split(b"\n")
    del out
    if lines and not lines[-1]:
        lines.pop()
    return lines


def readable(text):
    """True for a string that can be kept: one that UTF-8 can write."""
    try:
        text.encode("utf-8")
        return True
    except UnicodeError:
        return False


def document(raw):
    """What the line of a parcel gives, as {"md5", "sha256", "size", "format", "pages"} with
    pages as (page, section, text, still to recognise); None when the line is not one that this
    version writes: fields of the wrong kind, sizes or numbers beyond what can be kept, strings
    that cannot be written, pages that are not those of a reading of that format."""
    try:
        told = json.loads(raw)
        md5, sha, size, fmt, pages = told["md5"], told["sha256"], told["size"], told["format"], told["pages"]
    except (ValueError, KeyError, TypeError, RecursionError):
        return None
    if not (isinstance(md5, str) and MD5.match(md5) and isinstance(sha, str) and SHA.match(sha)
            and type(size) is int and 0 <= size < BIG and isinstance(fmt, str) and fmt in extract.SUPPORTED
            and isinstance(pages, list) and 0 < len(pages) <= PAGES):
        return None
    clean = []
    for page in pages:
        if not (isinstance(page, list) and len(page) == 4):
            return None
        number, section, body, blank = page
        if not ((number is None or (type(number) is int and 0 < number < BIG))
                and (section is None or (isinstance(section, str) and len(section) <= 4096 and readable(section)))
                and isinstance(body, str) and readable(body) and type(blank) in (int, bool) and blank in (0, 1)):
            return None
        clean.append((number, section, body, int(blank)))
    identities = [(number, section) for number, section, _, _ in clean]
    if fmt in PAGED and any(number is None for number, _ in identities):
        return None                             # the pages of these formats are numbered
    if (fmt in PAGED or any(blank for _, _, _, blank in clean)) and len(set(identities)) != len(identities):
        return None                             # pages that are merged one by one are told apart
    return {"md5": md5, "sha256": sha, "size": size, "format": fmt, "pages": clean}


def listing(data, writer):
    """The parcels that a list names, as dicts with name, size, sha256 and documents; ValueError
    when the list is not one of `writer`. What is not a parcel of that writer is left aside."""
    try:
        told = json.loads(data)
    except (ValueError, RecursionError):
        raise ValueError("The list cannot be read") from None
    if not isinstance(told, dict) or told.get("writer") != writer or not isinstance(told.get("parcels"), list):
        raise ValueError("The list is not one of its writer")
    found = []
    for parcel in told["parcels"]:
        if not isinstance(parcel, dict):
            continue
        name, size, digest, documents = parcel.get("name"), parcel.get("size"), parcel.get("sha256"), parcel.get("documents")
        given = NAME.match(name) if isinstance(name, str) else None
        if given and given.group(1) == writer and type(size) is int and 0 < size < BIG \
                and isinstance(digest, str) and DIGEST.match(digest) and type(documents) is int and 0 <= documents < BIG:
            found.append({"name": name, "size": size, "sha256": digest, "documents": documents})
    return found


def holding(directory):
    """The sources whose text is relied upon and that hold texts: a document that one of them
    answers for takes no pages from a parcel."""
    try:
        _, found, _ = link.usable(directory)
    except (OSError, ValueError):
        return set()
    return {name for name, relied in found.items() if relied}


class Exchange:
    """The exchange of texts between this data folder and the others, through the shared place
    `place`, for the index `db` of the data folder `directory`. `progress(text)` receives the
    messages; `counts` keeps what was done."""

    def __init__(self, directory, db, place, progress=lambda text: None, name=None):
        self.directory, self.db, self.place, self.say = Path(directory), db, place, progress
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        me = machine.identity(self.directory, name)
        self.label, self.writer, self.name = me["id"], me["writer"], me["name"]
        self.last = time.monotonic()
        self.counts = dict.fromkeys(("sent", "received", "damaged", "newer", "skipped"), 0)
        self.sources = None
        self.ready = None                       # whether the shared place may be written in: known at the first exchange
        keeping = sqlite3.connect(self.directory / KEEPING)
        try:
            keeping.execute("PRAGMA journal_mode=WAL")      # a parcel taken costs one write, not three
        finally:
            keeping.close()

    @contextlib.contextmanager
    def attached(self):
        """The bookkeeping, reachable from the index as `exchange`: made when it is not there, and
        made anew when it is of another form or describes another shared place."""
        self.db.execute("ATTACH DATABASE ? AS exchange", (str(self.directory / KEEPING),))
        try:
            self.db.execute("PRAGMA exchange.synchronous=NORMAL")
            notes = {}
            if self.db.execute("SELECT 1 FROM exchange.sqlite_master WHERE name='notes'").fetchone():
                notes = dict(self.db.execute("SELECT key,value FROM exchange.notes").fetchall())
            if notes.get("layout") != LAYOUT or notes.get("place") != self.place.identity:
                with self.db:
                    for table in ("notes", "parcels", "entries", "compared"):
                        self.db.execute(f"DROP TABLE IF EXISTS exchange.{table}")
            with self.db:
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        self.db.execute(statement)
                self.db.executemany("INSERT OR REPLACE INTO exchange.notes(key,value) VALUES(?,?)",
                                    [("layout", LAYOUT), ("place", self.place.identity)])
            yield
        finally:
            self.db.execute("DETACH DATABASE exchange")

    def due(self):
        """True when the last exchange is old enough for another during a reading; after a
        failure of the shared place, PAUSE seconds more."""
        return time.monotonic() - self.last >= EVERY

    def run(self, *steps, wait=False):
        """Do the steps of an exchange, among "receive", "apply" and "send", unless another
        process of this data folder is exchanging at this moment, which is waited for WAIT
        seconds with `wait`. Returns True when they were done. A shared place that fails is
        said, and the exchange is taken up after a pause: it is no fault of the documents."""
        with sharing.alone(self.directory, WAIT if wait else 0.0) as free:
            if not free:
                if wait:
                    self.say("Another exchange of this data folder is under way: the texts wait for the next one")
                return False
            return self.steps(*steps)

    def steps(self, *steps):
        """The steps of `run`, for a caller that holds the exchange already."""
        self.last = time.monotonic()
        if self.ready is False:
            return False                        # a newer ShelfFind wrote the shared place: nothing is written there
        try:
            if self.ready is None:
                sharing.check_format(self.place)
                self.ready = True
            with self.attached():
                for step in steps:
                    getattr(self, step)()
        except (OSError, ValueError) as fault:
            if "newer ShelfFind" in str(fault):
                self.ready = False
            self.say(f"The texts could not be exchanged this time: {fault}")
            self.last = time.monotonic() + PAUSE
            return False
        return True

    def sizes(self):
        """The sizes and formats of the documents that the parcels offer and that this data
        folder does not know by a measure: files of these are worth comparing."""
        with self.attached():
            return frozenset((row[0], row[1]) for row in self.db.execute(
                "SELECT DISTINCT e.size,e.format FROM exchange.entries e JOIN exchange.parcels p ON p.writer=e.writer "
                "AND p.name=e.name AND p.state='taken' WHERE NOT EXISTS "
                "(SELECT 1 FROM doc_keys k WHERE k.key=e.md5 AND k.format=e.format AND k.basis='content')").fetchall())

    def holders(self):
        if self.sources is None:
            self.sources = holding(self.directory)
        return self.sources

    # ---- taking ---------------------------------------------------------------------------
    def receive(self):
        """Take the parcels that the lists name and that this data folder has not taken yet:
        what they offer is remembered; the pages are given later, by `apply`. Its own list is
        checked first (`reconcile`). A list or a parcel that cannot be read is counted, and
        the others are taken all the same."""
        self.reconcile()
        for entry in self.place.names("texts"):
            writer = entry.removesuffix(".json")
            if not entry.endswith(".json") or not WRITER.match(writer) or writer == self.writer:
                continue
            try:
                parcels = listing(self.place.get(f"texts/{entry}", LISTED) or b"{}", writer)
            except ValueError:
                self.counts["damaged"] += 1
                continue
            for parcel in parcels:
                self.take(writer, parcel)

    def reconcile(self):
        """Check the list of this data folder against its parcels and its bookkeeping. A parcel
        that the bookkeeping knows and the shared place no longer holds is forgotten, so that
        its documents are given again. A parcel that the shared place holds and the list does
        not name, the list having been lost or cut, is named again. A parcel that the list names
        and the bookkeeping does not know, the bookkeeping having been deleted, is taken again,
        so that nothing is given twice."""
        present = {name for name in self.place.names(f"texts/{self.writer}") if NUMBERED.match(name)}
        try:
            data = self.place.get(f"texts/{self.writer}.json", LISTED)
            listed = {parcel["name"]: parcel for parcel in listing(data, self.writer)} if data is not None else None
        except ValueError:
            listed = None                       # cut or damaged: written anew below
        known = {row["name"]: dict(row) for row in self.db.execute(
            "SELECT name,size,sha256,documents FROM exchange.parcels WHERE writer=? AND state='taken'", (self.writer,)).fetchall()}
        with self.db:
            for name in known:
                if name.rsplit("/", 1)[1] not in present:
                    # Its documents are compared again, and given again in another parcel.
                    self.db.execute("DELETE FROM exchange.compared WHERE (md5,sha256,size,format) IN (SELECT md5,sha256,size,format "
                                    "FROM exchange.entries WHERE writer=? AND name=?)", (self.writer, name))
                    self.db.execute("DELETE FROM exchange.entries WHERE writer=? AND name=?", (self.writer, name))
                    self.db.execute("DELETE FROM exchange.parcels WHERE writer=? AND name=?", (self.writer, name))
        kept = {name: parcel for name, parcel in known.items() if name.rsplit("/", 1)[1] in present}
        for name, parcel in (listed or {}).items():
            if name not in kept and name.rsplit("/", 1)[1] in present:
                self.take(self.writer, parcel)
                if self.db.execute("SELECT 1 FROM exchange.parcels WHERE writer=? AND name=? AND state='taken'",
                                   (self.writer, name)).fetchone():
                    kept[name] = parcel
        if listed is None and kept or listed is not None and set(listed) != set(kept):
            self.publish(kept)

    def publish(self, parcels):
        """Write the list of this data folder, naming the parcels `parcels` (name -> dict)."""
        self.place.put(f"texts/{self.writer}.json", json.dumps(
            {"format": FORMAT, "writer": self.writer, "machine": self.label, "name": self.name, "updated": int(time.time()),
             "parcels": [{key: parcels[name][key] for key in ("name", "size", "sha256", "documents")} for name in sorted(parcels)]},
            indent=1).encode() + b"\n")

    def fetch(self, parcel):
        """The lines of a parcel after its first, or None when it is not there as its list says
        (counted as damaged), or is of a newer format (counted, and remembered)."""
        writer = NAME.match(parcel["name"]).group(1)
        try:
            data = self.place.get(parcel["name"], parcel["size"])
        except ValueError:
            data = None
        if data is None or len(data) != parcel["size"] or hashlib.sha256(data).hexdigest() != parcel["sha256"]:
            self.counts["damaged"] += 1         # cut, changed or missing: taken another time
            return None
        try:
            lines = unpack(data)
            header = json.loads(lines[0])
            made, author = header["format"], header["writer"]
        except (ValueError, KeyError, TypeError, IndexError, RecursionError):
            self.counts["damaged"] += 1
            return None
        if type(made) is not int or made < 1 or author != writer:
            self.counts["damaged"] += 1
            return None
        if made > FORMAT:
            with self.db:
                self.remember(writer, parcel, f"newer:{made}")
            self.counts["newer"] += 1
            return None
        return lines

    def remember(self, writer, parcel, state):
        self.db.execute("INSERT OR REPLACE INTO exchange.parcels(writer,name,sha256,size,documents,state) VALUES(?,?,?,?,?,?)",
                        (writer, parcel["name"], parcel["sha256"], parcel["size"], parcel["documents"], state))

    def take(self, writer, parcel):
        """Take one parcel: what each of its documents offers is remembered, and the parcel
        marked as taken, in one step. A parcel whose name now holds other bytes than those
        taken before is taken anew, and what it offered before is forgotten."""
        row = self.db.execute("SELECT sha256,state FROM exchange.parcels WHERE writer=? AND name=?",
                              (writer, parcel["name"])).fetchone()
        if row is not None and row["sha256"] == parcel["sha256"]:
            if row["state"] == "taken":
                return
            if row["state"].startswith("newer:") and int(row["state"][6:]) > FORMAT:
                self.counts["newer"] += 1
                return
        lines = self.fetch(parcel)
        if lines is None:
            return
        offers = []
        for number, raw in enumerate(lines[1:], 1):
            told = document(raw)
            if told is None:
                self.counts["skipped"] += 1     # a line that this version cannot take
                continue
            identities = [(page, section) for page, section, _, _ in told["pages"]]
            offers.append((writer, parcel["name"], number, told["md5"], told["sha256"], told["size"], told["format"],
                           store.shape(identities), len(identities),
                           written(index for index, page in enumerate(told["pages"]) if page[3])))
        with self.db:
            self.db.execute("DELETE FROM exchange.entries WHERE writer=? AND name=?", (writer, parcel["name"]))
            self.db.executemany("INSERT INTO exchange.entries(writer,name,line,md5,sha256,size,format,layout,pages,blanks) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?)", offers)
            self.remember(writer, parcel, "taken")

    # ---- giving to the documents of this data folder ---------------------------------------
    def candidates(self, doc=None):
        """The offers of the parcels taken that would take a document of this data folder
        further, as (writer, name, line, doc), the most complete first; for one document with
        `doc`. Nothing is fetched to know it."""
        holders = sorted(self.holders())
        one = "" if doc is None else " AND d.id=?"
        rows = self.db.execute(
            "SELECT e.writer,e.name,e.line,e.layout,e.blanks,d.id AS doc FROM exchange.entries e "
            "JOIN exchange.parcels p ON p.writer=e.writer AND p.name=e.name AND p.state='taken' "
            "JOIN doc_keys k ON k.key=e.md5 AND k.format=e.format AND k.basis='content' JOIN docs d ON d.id=k.doc "
            f"WHERE d.size=e.size AND d.state IN ({','.join('?' * len(WAITING))}) "
            "AND EXISTS (SELECT 1 FROM files f WHERE f.doc=d.id) "
            "AND NOT EXISTS (SELECT 1 FROM doc_keys c WHERE c.key LIKE 'sha256:%' AND c.basis='content' AND c.format=e.format "
            "AND ((c.doc=d.id AND c.key!=e.sha256) OR (c.doc!=d.id AND c.key=e.sha256))) "
            "AND NOT EXISTS (SELECT 1 FROM records r WHERE r.doc=d.id AND r.text=1 AND r.keys!='' "
            f"AND r.source IN ({','.join('?' * len(holders))})){one} "
            "ORDER BY length(e.blanks),e.writer,e.name,e.line", (*WAITING, *holders, *([doc] if doc is not None else []))).fetchall()
        readings, found = {}, []
        for row in rows:
            if row["doc"] not in readings:
                readings[row["doc"]] = store.reading(self.db, row["doc"])
            if store.helps(readings[row["doc"]], row["layout"], positions(row["blanks"])):
                found.append((row["writer"], row["name"], row["line"], row["doc"]))
        return found

    def apply(self):
        """Give the documents of this data folder that wait for their text what the parcels
        taken offer for their bytes. Only the parcels that would take a reading further are
        fetched, so an exchange with nothing new fetches nothing."""
        self.give(self.candidates())

    def give(self, found):
        """Fetch the parcels of the offers `found` and give their pages. Returns True when
        something was given."""
        wanted, given = {}, False
        for writer, name, line, doc in found:
            wanted.setdefault((writer, name), []).append(line)
        for (writer, name), lines in wanted.items():
            parcel = self.db.execute("SELECT name,size,sha256,documents FROM exchange.parcels WHERE writer=? AND name=?",
                                     (writer, name)).fetchone()
            got = self.fetch(dict(parcel)) if parcel is not None else None
            for number in lines if got is not None else ():
                told = document(got[number]) if number < len(got) else None
                if told is None:
                    continue
                keys = [told["md5"], told["sha256"]]
                with self.db:
                    doc = store.taker(self.db, keys, told["size"], told["format"], self.holders())
                    if doc is not None and store.take_pages(self.db, doc, keys, told["pages"], self.holders()):
                        self.counts["received"] += 1
                        given = True
        return given

    def offer(self, doc):
        """Give a document the pages that a parcel taken offers for its very bytes, before this
        machine reads it: True when they were given. Called once a file of the document has been
        copied for its reading and measured."""
        with self.attached():
            return self.give(self.candidates(doc))

    # ---- giving to the others ---------------------------------------------------------------
    def send(self):
        """Put in the shared place the readings of this data folder that recognise pages which
        nothing taken offers for the same bytes and pages: documents with a file, read here or
        completed here. A document compared once is compared again only when its pages change."""
        shipped = {}
        for row in self.db.execute("SELECT e.md5,e.sha256,e.size,e.format,e.layout,e.pages,e.blanks FROM exchange.entries e "
                                   "JOIN exchange.parcels p ON p.writer=e.writer AND p.name=e.name AND p.state='taken'").fetchall():
            recognised = set(range(row["pages"])) - positions(row["blanks"])
            shipped.setdefault((row["md5"], row["sha256"], row["size"], row["format"], row["layout"]), set()).update(recognised)
        compared = {(row[0], row[1], row[2], row[3]): (row[4], row[5])
                    for row in self.db.execute("SELECT md5,sha256,size,format,pages,blanks FROM exchange.compared").fetchall()}
        packer, given, seen = None, [], []
        for doc in self.db.execute(
                "SELECT d.id,d.origin,d.size,d.format,count(p.id) AS pages,sum(p.needs_ocr) AS blanks FROM docs d "
                "JOIN pages p ON p.doc=d.id WHERE d.origin IS NOT NULL AND EXISTS (SELECT 1 FROM files f WHERE f.doc=d.id) "
                "GROUP BY d.id ORDER BY d.id").fetchall():
            keys = store.sums(doc["origin"])
            if set(keys) != {"md5", "sha256"} or doc["format"] not in extract.SUPPORTED:
                continue
            bytes_ = (keys["md5"], keys["sha256"], doc["size"], doc["format"])
            if compared.get(bytes_) == (doc["pages"], doc["blanks"]):
                continue
            seen.append((*bytes_, doc["pages"], doc["blanks"]))
            if doc["pages"] > PAGES:
                continue
            rows = self.db.execute("SELECT page,section,ztext,needs_ocr FROM pages WHERE doc=? ORDER BY id", (doc["id"],)).fetchall()
            identities = [(row["page"], row["section"]) for row in rows]
            shape = store.shape(identities)
            recognised = {index for index, row in enumerate(rows) if not row["needs_ocr"]}
            if recognised <= shipped.get((*bytes_, shape), set()):
                continue                        # nothing that the shared place does not offer already
            pages = [[row["page"], row["section"], store.inflate(row["ztext"]), row["needs_ocr"]] for row in rows]
            raw = json.dumps({"md5": keys["md5"], "sha256": keys["sha256"], "size": doc["size"], "format": doc["format"],
                              "pages": pages}, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace") + b"\n"
            if len(raw) > UNPACKED or document(raw) is None:
                continue                        # what no version that reads it could take
            line = (*bytes_, shape, len(rows), written(set(range(len(rows))) - recognised))
            if packer is None:
                packer = Packer(self.writer)
            if not packer.add(raw):
                self.ship(packer, given)
                packer, given = Packer(self.writer), []
                packer.add(raw)
            given.append(line)
            shipped.setdefault((*bytes_, shape), set()).update(recognised)
        if given:
            self.ship(packer, given)
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO exchange.compared(md5,sha256,size,format,pages,blanks) VALUES(?,?,?,?,?,?)",
                                seen)

    def ship(self, packer, given):
        """Put one parcel in the shared place under a number never given before, then name it
        in the list of this data folder, which another reads before taking it; what it offers
        is remembered here last."""
        data = packer.close()
        known = {row["name"]: dict(row) for row in self.db.execute(
            "SELECT name,size,sha256,documents FROM exchange.parcels WHERE writer=? AND state='taken'", (self.writer,)).fetchall()}
        present = [name for name in self.place.names(f"texts/{self.writer}") if NUMBERED.match(name)]
        mark = self.db.execute("SELECT value FROM exchange.notes WHERE key='number'").fetchone()
        number = 1 + max([int(NAME.match(name).group(2)) for name in known] + [int(NUMBERED.match(name).group(1)) for name in present]
                         + [int(mark[0]) if mark else 0])
        with self.db:                           # the number is never given again, whatever is lost afterwards
            self.db.execute("INSERT OR REPLACE INTO exchange.notes(key,value) VALUES('number',?)", (str(number),))
        parcel = {"name": f"texts/{self.writer}/{number:06d}.jsonl.gz", "size": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(), "documents": len(given)}
        self.place.put(parcel["name"], data)
        self.publish({**known, parcel["name"]: parcel})
        with self.db:
            self.remember(self.writer, parcel, "taken")
            self.db.executemany("INSERT INTO exchange.entries(writer,name,line,md5,sha256,size,format,layout,pages,blanks) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?)", [(self.writer, parcel["name"], number, *line)
                                                                for number, line in enumerate(given, 1)])
        self.counts["sent"] += len(given)
        self.say(f"{len(given):,} documents put in the shared place ({len(data) / 1e6:,.1f} MB)")
