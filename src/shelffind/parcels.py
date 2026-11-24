# Copyright 2026 Eudaemonic Enterprises Inc.
"""The text that one machine read, for the other machines.

When the settings share texts (`share.texts`), each data folder puts the pages it read in the
shared place of the journals (see `sharing`), under a name of its own, the writer, which no
other data folder uses, even on the same machine (see `machine.identity`):

    texts/WRITER.json              the list of its parcels, each with its size and SHA-256, and
                                   the last number it gave
    texts/WRITER/NNNNNN.jsonl.gz   a parcel: a line naming the format and the writer, then one
                                   line for each document: the MD5 and the SHA-256 of the bytes
                                   its pages were read from, their size and format, and the pages

A parcel is never changed once written, and its number is never given twice: the list keeps the
last number given, and the bookkeeping the greatest one it saw, so that only the loss of both
while a parcel is still on its way could let a number come back. Only its
writer writes in its folder and its list, so no two writers ever write the same file. The lists
are apart from the cards of the machines that the journals rewrite. A parcel holds at most LIMIT
bytes and unpacks to at most UNPACKED bytes, its first line included, and a list holds at most
LISTED bytes; a document that would not fit alone in a parcel, or of more than PAGES pages, is
not shared, and a writer whose numbers or list come to their end gives nothing more and says
so. Another data folder takes a parcel only when it has the size and the checksum that the list
gives and holds as many documents as the list says: a parcel cut, changed or missing is taken
another time, and one of a newer format waits for a newer version of ShelfFind. Nothing in a
parcel names a file or a folder; its pages carry the whole text that was read. The shared place
is trusted: anyone who may write there may put texts there.

Taking a parcel only remembers what it offers. The pages are given by a run of the index, once
its inventory is made and its sources asked, to the document that a measure of this machine
knows by the same checksums, when that document has a file, waits for its text, and is not
left to a source that holds its text (`store.taker`, `store.take_pages`): a document without
pages takes the reading whole; one with pages takes, page by page, the pages it still has to
recognise from a reading of the same pages, so that the recognised pages of several machines
add up. What cannot be given yet stays offered: a complete run compares the files of the sizes
and formats offered (`indexer.waiting`), and a file copied for its reading, whose bytes another
data folder read, is not read here (`offer`): its document takes those pages after the sources
were asked about the checksums just measured, provided the file is still the one copied. A
document goes into a parcel when its reading recognises pages that nothing in the shared place
offers for the same bytes and pages, so that what one data folder received it never sends back.
Only a reading sealed by this version is given (`store.SEALED`): one it read from the bytes it
measured, or took whole from a parcel; what an earlier version read stays on its machine.

What a data folder knows of the parcels is kept in `exchange.sqlite3` beside its index, for one
shared place: which parcels it took, what each one offers, and what it compared of its own
documents. It is made anew when the shared place changes, and may be deleted while no run and no
exchange is under way: the lists are then read again, and nothing is read twice. Each exchange
begins with the list of the data folder itself, checked against its parcels (`reconcile`). A
parcel that another writer's list no longer names, or of a writer whose list and parcels are
gone, was withdrawn: what it offered is forgotten.
"""
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import zlib
from pathlib import Path

from . import extract, link, machine, sharing, store

FORMAT = 1
LIMIT = 16 << 20        # bytes of one parcel at most
UNPACKED = 256 << 20    # bytes that one parcel may unpack to, its first line included
LISTED = 16 << 20       # bytes of a list of parcels at most
PAGES = 100_000         # pages of one document at most
LAST = 999_999          # the number of the last parcel of a writer
BIG = 1 << 53           # no size, page number or count goes beyond this
TEXTS = "texts.json"    # in the data folder: when the texts were last exchanged, for `status`
EVERY = 120             # seconds between two exchanges while documents are being read
SHARE = 10              # and at least this many times what the last exchange took, so that reading gets most of the time
WAIT = 120              # seconds that a run waits for an exchange of another process to end
PAUSE = 900             # seconds without exchange after the shared place failed
AWAIT = 7 * 86400       # seconds that a parcel of its own named and not there, or not whole, stays named,
AWAITED = 50            # and exchanges, both, so that a clock put forward does not end it at once
BODIES = 64 << 20       # bytes of parcels found whole that a run keeps for its later rounds
KEEPING = "exchange.sqlite3"
LAYOUT = "4"            # the form of the bookkeeping: another one is thrown away and made anew
WRITER = re.compile(r"[0-9a-f]{16}\Z")
NAME = re.compile(r"texts/([0-9a-f]{16})/(\d{6})\.jsonl\.gz\Z")
NUMBERED = re.compile(r"(\d{6})\.jsonl\.gz\Z")
PASSING = re.compile(r"\.arriving\.\d+\Z")       # a file that a writing cut short left under its passing name
MD5 = re.compile(r"md5:[0-9a-f]{32}\Z")
SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
WAITING = ("pending", "error", "needs-ocr", "ocr-error")
PAGED = {"pdf", "djvu", "ps", "ps.gz"}      # formats read page by page
# What tells a reading from another of the same bytes, from the pages of its document alone: their
# number, and two sums over its pages of a mixing, by shifts, exclusive ors and products on 31
# bits, of the row each is kept in, its page number, whether it is still to recognise, the length
# of its text and the name of its section. A reading changed in any way, taken anew or completed
# page by page, has another, but by a chance of the order of one in 2**60 for two given readings;
# the text of a page counts by its length alone.
PRIME, MASK, MIX = 2147483647, 0x7FFFFFFF, (739982445, 695872825)
_XOR = "(({a})|(({a})>>{s}))-(({a})&(({a})>>{s}))"        # SQLite shifts with the precedence of | and &
READING = (
    "SELECT doc, count(*)||':'||sum(h)||':'||sum(g) AS reading FROM ("
    f"SELECT doc, {_XOR.format(a='h2', s=15)} AS h, {_XOR.format(a='g2', s=15)} AS g FROM ("
    f"SELECT doc, ({_XOR.format(a='h1', s=12)})*{MIX[1]}&{MASK} AS h2, ({_XOR.format(a='g1', s=12)})*{MIX[1]}&{MASK} AS g2 FROM ("
    f"SELECT doc, ({_XOR.format(a='x', s=15)})*{MIX[0]}&{MASK} AS h1, ({_XOR.format(a='y', s=15)})*{MIX[0]}&{MASK} AS g1 FROM ("
    f"SELECT doc, x, (x*48271+11)%{PRIME} AS y FROM ("
    f"SELECT doc, ((id%{PRIME})*2654435761+(coalesce(page,0)%{PRIME})*40503+needs_ocr*977+length(ztext)*69069"
    f"+CASE WHEN section IS NULL THEN 1 ELSE shelf_crc(section)+2 END)%{PRIME} AS x FROM pages))))) GROUP BY doc")
SCHEMA = """
CREATE TABLE IF NOT EXISTS exchange.notes (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exchange.parcels (
    writer TEXT NOT NULL, name TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    documents INTEGER NOT NULL, state TEXT NOT NULL, stamp TEXT, PRIMARY KEY (writer, name)
);
CREATE TABLE IF NOT EXISTS exchange.entries (
    writer TEXT NOT NULL, name TEXT NOT NULL, line INTEGER NOT NULL, md5 TEXT NOT NULL,
    sha256 TEXT NOT NULL, size INTEGER NOT NULL, format TEXT NOT NULL, layout TEXT NOT NULL,
    pages INTEGER NOT NULL, blanks TEXT NOT NULL, PRIMARY KEY (writer, name, line)
);
CREATE INDEX IF NOT EXISTS exchange.entries_bytes ON entries(md5, format);
CREATE TABLE IF NOT EXISTS exchange.compared (
    md5 TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, format TEXT NOT NULL,
    reading TEXT NOT NULL, verdict TEXT NOT NULL, PRIMARY KEY (md5, sha256, size, format)
);
"""
TABLES = ("notes", "parcels", "entries", "compared")


class Newer(ValueError):
    """What a newer version of ShelfFind wrote."""


def positions(text):
    """The positions written as "2,5,7"."""
    return {int(one) for one in text.split(",") if one}


def written(found):
    return ",".join(str(one) for one in sorted(found))


def crc(text):
    """The CRC-32 of a name, for READING."""
    return zlib.crc32(text.encode("utf-8", "surrogatepass")) if text is not None else 0


def mixed(x):
    """The mixing of READING, of one number below 2**31."""
    x = (x ^ x >> 15) * MIX[0] & MASK
    x = (x ^ x >> 12) * MIX[1] & MASK
    return x ^ x >> 15


def reading_of(rows):
    """READING, for the rows of the pages of a document."""
    xs = [((row["id"] % PRIME) * 2654435761 + ((row["page"] or 0) % PRIME) * 40503 + row["needs_ocr"] * 977
           + len(row["ztext"]) * 69069 + (1 if row["section"] is None else crc(row["section"]) + 2)) % PRIME for row in rows]
    return f"{len(xs)}:{sum(mixed(x) for x in xs)}:{sum(mixed((x * 48271 + 11) % PRIME) for x in xs)}"


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
        beyond LIMIT or unpack beyond UNPACKED, its first line included: a document that does not
        fit in a parcel of its own is not added to any."""
        kept, length, unpacked = self.zip.copy(), len(self.data), self.unpacked
        self.put(raw)
        if len(self.data) + 32 > LIMIT or self.unpacked > UNPACKED:
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
            and type(size) is int and 0 <= size < BIG and isinstance(fmt, str) and fmt in extract.formats()
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
    if fmt not in PAGED and any(blank for _, _, _, blank in clean):
        return None                             # only a page of a scan waits for its recognition
    if (fmt in PAGED or any(blank for _, _, _, blank in clean)) and len(set(identities)) != len(identities):
        return None                             # pages that are merged one by one are told apart
    return {"md5": md5, "sha256": sha, "size": size, "format": fmt, "pages": clean}


def listing(data, writer):
    """What the list of `writer` gives: (the parcels it names, as dicts with name, size, sha256
    and documents; the number of entries left aside, which are not parcels of that writer as
    this version writes them; the last number the writer gave). ValueError when the list cannot
    be read or is not one of `writer`, Newer when a newer ShelfFind wrote it."""
    try:
        told = json.loads(data)
    except (ValueError, RecursionError):
        raise ValueError("The list cannot be read") from None
    if not isinstance(told, dict) or told.get("writer") != writer:
        raise ValueError("The list is not one of its writer")
    made = told.get("format")
    if type(made) is not int or made < 1:
        raise ValueError("The list names no format")
    if made > FORMAT:                           # whatever else it holds is that version's
        raise Newer(f"The list of {writer} was written by a newer ShelfFind")
    if not isinstance(told.get("parcels"), list):
        raise ValueError("The list names no parcels")
    last = told.get("last")
    last = last if type(last) is int and 0 <= last <= LAST else 0
    found, aside, names = [], 0, set()
    for parcel in told["parcels"]:
        name, size, digest, documents = (parcel.get(key) for key in ("name", "size", "sha256", "documents")) \
            if isinstance(parcel, dict) else (None,) * 4
        given = NAME.match(name) if isinstance(name, str) else None
        if given and given.group(1) == writer and name not in names and type(size) is int and 0 < size <= LIMIT \
                and isinstance(digest, str) and DIGEST.match(digest) and type(documents) is int and 0 < documents < BIG:
            found.append({"name": name, "size": size, "sha256": digest, "documents": documents})
            names.add(name)
        else:
            aside += 1
        if given and given.group(1) == writer:  # a number named is a number given, whatever else is wrong
            last = max(last, int(given.group(2)))
    return found, aside, last


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
        self.last, self.took = time.monotonic(), 0.0
        self.counts = dict.fromkeys(("sent", "received", "damaged", "newer", "skipped"), 0)
        self.learnt = 0                         # documents that pages offered made known by more checksums
        self.sources = None
        self.ready = None                       # whether the shared place may be used: known at each exchange
        self.failing = False                    # whether the last exchange failed
        self.writing = True                     # whether this data folder may give texts
        self.named, self.number = {}, 0         # what its list is to name, and the last number it gave
        self.fresh = set()                      # documents measured since the sources were last asked
        self.held = {}                          # file copied in this run -> (its document, whether it is still the one copied)
        self.told, self.entries, self.folders = set(), None, None
        self.began = time.time_ns()             # a file changed since then takes no pages in this run (`unchanged`)
        self.refused = set()                    # documents that took no pages, a file of theirs having changed
        self.seen = {}                          # (folder, address) -> what told the file apart when this run listed it
        self.weighed = set()                    # readings found too large, or refused, in this run
        self.bodies, self.kept = {}, 0          # parcels fetched and found whole, for the later rounds of this run
        db.create_function("shelf_crc", 1, crc, deterministic=True)

    def tell(self, text):
        """Say something once in the life of this exchange."""
        if text not in self.told:
            self.told.add(text)
            self.say(text)

    @contextlib.contextmanager
    def attached(self, mend=True):
        """The bookkeeping, reachable from the index as `exchange`. With `mend`, which only the
        holder of the exchange asks for, it is made when it is not there, and made anew when it
        is of another form or describes another shared place; yields True. Without, nothing is
        written in it, and False is yielded when it is not the bookkeeping of this shared place."""
        if not mend and not (self.directory / KEEPING).exists():
            yield False                         # not made yet: a look makes nothing
            return
        self.db.execute("ATTACH DATABASE ? AS exchange", (str(self.directory / KEEPING),))
        try:
            self.db.execute("PRAGMA exchange.synchronous=NORMAL")
            tables = {row[0] for row in self.db.execute("SELECT name FROM exchange.sqlite_master WHERE type='table'")}
            notes = dict(self.db.execute("SELECT key,value FROM exchange.notes").fetchall()) if "notes" in tables else {}
            mine = notes.get("layout") == LAYOUT and notes.get("place") == self.place.identity and set(TABLES) <= tables
            if not mend:
                yield mine
                return
            if notes.get("layout") != LAYOUT or notes.get("place") != self.place.identity:
                with self.db:
                    for table in TABLES:
                        self.db.execute(f"DROP TABLE IF EXISTS exchange.{table}")
                tables, notes = set(), {}
            if not set(TABLES) <= tables:
                self.db.execute("PRAGMA exchange.journal_mode=WAL")     # a parcel taken costs one write, not three
                with self.db:
                    for statement in SCHEMA.split(";"):
                        if statement.strip():
                            self.db.execute(statement)
                    self.db.executemany("INSERT OR REPLACE INTO exchange.notes(key,value) VALUES(?,?)",
                                        [("layout", LAYOUT), ("place", self.place.identity)])
            limits = f"{LIMIT} {UNPACKED} {PAGES}"
            if notes.get("limits") != limits:   # under other limits the parcels of the others are taken again, lines
                with self.db:                   # refused before included, and every reading is compared again
                    self.db.execute("DELETE FROM exchange.compared")
                    self.db.execute("DELETE FROM exchange.entries WHERE writer!=?", (self.writer or "",))
                    self.db.execute("DELETE FROM exchange.parcels WHERE writer!=?", (self.writer or "",))
                    self.db.execute("INSERT OR REPLACE INTO exchange.notes(key,value) VALUES('limits',?)", (limits,))
            yield True
        finally:
            self.db.execute("DETACH DATABASE exchange")

    def due(self):
        """True when the last exchange is old enough for another during a reading: EVERY seconds
        after its end, or SHARE times as long as it took when that is longer, since an exchange
        with a distant place may take minutes and would leave no time to read; after a failure
        of the shared place, PAUSE seconds more."""
        return time.monotonic() - self.last >= max(EVERY, SHARE * self.took)

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
        """The steps of `run`, for a caller that holds the exchange already. The mark of the
        shared place is looked at each time, and again before each thing written there, so
        that a newer ShelfFind that marked it since is left alone; an exchange that receives
        or sends begins with the list of this data folder (`reconcile`)."""
        began = self.last = time.monotonic()
        self.entries = None
        try:
            self.ready = False
            sharing.check_format(self.place)
            self.ready = True
            with self.attached():
                if {"receive", "send"} & set(steps):
                    self.reconcile()
                for step in steps:
                    getattr(self, step)()
        except (OSError, ValueError, sqlite3.OperationalError) as fault:
            self.failing = True
            self.tell(f"The texts could not be exchanged this time: {fault}")
            self.last = time.monotonic() + PAUSE
            return False
        self.failing = False
        self.took = time.monotonic() - began
        self.last = time.monotonic()
        try:
            machine.write(self.directory, {"at": int(time.time())}, TEXTS)    # what `status` says of the last exchange
        except OSError:
            pass
        return True

    def put(self, name, data):
        """Write a file in the shared place, unless a newer ShelfFind marked it meanwhile."""
        sharing.check_format(self.place)
        self.place.put(name, data)

    def delete(self, name):
        sharing.check_format(self.place)
        self.place.delete(name)

    def sizes(self):
        """The sizes and formats of the documents that the parcels offer and that this data
        folder does not know by a measure: files of these are worth comparing. None of them
        when the bookkeeping cannot be read at this moment, or is not yet that of this place."""
        try:
            with self.attached(mend=False) as mine:
                if not mine:
                    return frozenset()
                return frozenset((row[0], row[1]) for row in self.db.execute(
                    "SELECT DISTINCT e.size,e.format FROM exchange.entries e JOIN exchange.parcels p ON p.writer=e.writer "
                    "AND p.name=e.name AND p.state='taken' WHERE NOT EXISTS "
                    "(SELECT 1 FROM doc_keys k WHERE k.key=e.md5 AND k.format=e.format AND k.basis='content')").fetchall())
        except (OSError, sqlite3.OperationalError) as fault:
            self.tell(f"The texts offered could not be looked at this time: {fault}")
            return frozenset()

    def holders(self):
        if self.sources is None:
            self.sources = holding(self.directory)
        return self.sources

    # ---- the list of this data folder ------------------------------------------------------
    def reconcile(self):
        """Check the list of this data folder against its parcels and its bookkeeping, and keep
        in `named` what it is to name. A parcel that the bookkeeping knows as taken is named as
        the bookkeeping knows it, whatever the list says, while the shared place holds it with
        the size and the date it had when it was last found whole; when they change, it is read
        again, twice if need be. A parcel that the list names and the bookkeeping does not know,
        the bookkeeping having been deleted, is taken again, so that nothing is given twice. A
        parcel put whose list could not be written is named once it is found whole. A parcel of
        this data folder that is not there, or holds other bytes than those it was put with, or
        is of a newer format, stays named as the list or the bookkeeping last described it, since
        it may yet arrive or be put right, or be read by a newer version, while its documents
        are given again under another number; it is named no more once it was so for AWAIT
        seconds and AWAITED exchanges, and nothing in the shared place is ever deleted but what
        a writing of this data folder cut short left under a passing name. No number is given
        twice: the greatest one seen, in the list, in the shared place or in the bookkeeping, is
        kept in the list and in the bookkeeping. A list that a newer ShelfFind wrote is left to
        it, and nothing is given; so is a list that cannot be fetched, which stops the exchange."""
        self.named = {}
        if self.writer is None:
            self.writing = False
            self.tell("The name under which this data folder gives its texts cannot be read or kept at this moment: "
                      "they wait for the next exchange")
            return
        entries = self.entries = self.place.names("texts")
        self.writing, listed, last, spoilt = True, None, 0, False
        try:
            data = self.place.get(f"texts/{self.writer}.json", LISTED)
        except sharing.Oversized:
            self.writing = False
            self.tell("The list of this data folder is larger than this version reads: it is left as it is")
            return
        if data is not None:
            try:
                found, aside, last = listing(data, self.writer)
                listed, spoilt = {parcel["name"]: parcel for parcel in found}, aside > 0
            except Newer:
                self.writing = False
                self.tell("A newer ShelfFind wrote the list of this data folder: its texts are left to that version")
                return
            except ValueError:
                listed, spoilt = None, True     # cut or damaged: written anew below
        here = self.place.stamps(f"texts/{self.writer}")
        present = {f"texts/{self.writer}/{name}": stamp for name, stamp in here.items() if NUMBERED.match(name)}
        for name in [f"texts/{self.writer}/{name}" for name in here if PASSING.search(name)] + \
                [f"texts/{name}" for name in entries if name.startswith(f"{self.writer}.json.") and PASSING.search(name)]:
            self.delete(name)
        rows = {row["name"]: dict(row) for row in self.db.execute(
            "SELECT name,size,sha256,documents,state,stamp FROM exchange.parcels WHERE writer=?", (self.writer,)).fetchall()}
        key = f"number:{self.writer}"
        mark = self.db.execute("SELECT value FROM exchange.notes WHERE key=?", (key,)).fetchone()
        self.number = max([last, int(mark[0]) if mark else 0] +
                          [int(NAME.match(name).group(2)) for name in [*rows, *present, *(listed or {})]])
        with self.db:                           # the greatest number seen is never given, whatever is lost afterwards
            if not mark or int(mark[0]) != self.number:
                self.db.execute("INSERT OR REPLACE INTO exchange.notes(key,value) VALUES(?,?)", (key, str(self.number)))
            for writer, name in self.db.execute("SELECT writer,name FROM exchange.parcels WHERE state='sending' AND writer!=?",
                                                (self.writer,)).fetchall():
                self.forget(writer, name)       # what another writer of this folder was sending is not this one's to finish
        named, now = {}, int(time.time())
        taken = "UPDATE exchange.parcels SET state='taken',stamp=? WHERE writer=? AND name=?"
        for name in sorted({*rows, *(listed or {})}):
            row, told = rows.get(name), (listed or {}).get(name)
            state = row["state"] if row else ""
            stamp = present.get(name)
            if state == "taken" and stamp is not None and row["stamp"] == stamp:
                named[name] = row               # as it was found whole
                continue
            if state.startswith("newer:") and stamp is not None and not (told and told["sha256"] != row["sha256"]):
                named[name] = row               # left to a newer version
                continue
            if state.startswith("awaited:") and stamp is not None and row["stamp"] == stamp \
                    and not self.again(state, now):
                if self.awaited(name, told or row, state, stamp, now):
                    named[name] = told or row   # still the bytes found wrong before: not read again this time
                continue
            if state in ("taken", "sending") and stamp is not None and self.whole(row):
                with self.db:                   # read back, or touched with the same bytes, or put and never named
                    self.db.execute(taken, (stamp, self.writer, name))
                named[name] = row
                if state == "sending":
                    self.counts["sent"] += row["documents"]
                continue
            if (state == "taken" or state.startswith("newer:")) and stamp is not None and told \
                    and told["sha256"] != row["sha256"] and self.whole(told):
                with self.db:                   # put anew, and named so in the list: what the list says of it stands
                    self.forget(self.writer, name)
                if self.take(self.writer, told):
                    with self.db:
                        self.db.execute(taken, (stamp, self.writer, name))
                named[name] = told              # taken, or left to a newer version, or cut: as the list says
                continue
            if state == "sending":
                with self.db:                   # never put whole: its documents are given again
                    self.forget(self.writer, name)
                continue
            described = told if not row or state.startswith("awaited:") else row
            if described is None:
                described = row                 # the list lost: the bookkeeping remembers it
            if stamp is not None and (row is None or state.startswith("awaited:") and self.whole(described)):
                if self.take(self.writer, described):
                    with self.db:               # taken again, the bookkeeping having been deleted, or awaited and come
                        self.db.execute(taken, (stamp, self.writer, name))
                    named[name] = described
                    continue
                if self.later(name):
                    named[name] = described     # left to a newer version, for as long as it is there
                    continue
            if self.awaited(name, described, state, stamp, now):
                named[name] = described         # not there, or not as it was put: it may yet be
        self.named = named
        fields = lambda parcels: {name: tuple(parcel[key] for key in ("size", "sha256", "documents")) for name, parcel in parcels.items()}
        if listed is None and (named or spoilt) or listed is not None and (spoilt or fields(listed) != fields(named)
                                                                           or last < self.number):
            self.publish()

    @staticmethod
    def again(state, now):
        """Whether a parcel that waits, and is there, is read again at this exchange although
        nothing of it seems to have changed: at the exchanges numbered by the powers of two, so
        that a reading that came short is not the last word, and always before it would be named
        no more."""
        parts = state.split(":")
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            return True
        since, seen = int(parts[1]), int(parts[2])
        return seen & (seen - 1) == 0 or now - since >= AWAIT and seen + 1 >= AWAITED

    def awaited(self, name, parcel, state, stamp, now):
        """Keep a parcel of this data folder named while it is not there, or not as it was put:
        what it offered is forgotten, so that its documents are given again, and the time and
        the exchanges since it was first so are counted. Returns False, and forgets it, once
        they reach AWAIT seconds and AWAITED exchanges."""
        since, seen = now, 0
        if state.startswith("awaited:"):
            parts = state.split(":")
            since, seen = (int(parts[1]), int(parts[2])) if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit() else (now, 0)
        with self.db:
            if now - since >= AWAIT and seen >= AWAITED:
                self.forget(self.writer, name)
                return False
            if not state.startswith("awaited:"):
                self.forget(self.writer, name)  # its documents are given again elsewhere
            self.remember(self.writer, parcel, f"awaited:{since}:{seen + 1}", stamp)
        return True

    def later(self, name):
        """True when a parcel of this data folder was found to be of a newer format."""
        row = self.db.execute("SELECT state FROM exchange.parcels WHERE writer=? AND name=?", (self.writer, name)).fetchone()
        return row is not None and row["state"].startswith("newer:")

    def whole(self, parcel):
        """True when the shared place holds a parcel of this data folder as it was put. A reading
        that disagrees is followed by a second, so that a reading cut short, or a copy that a
        folder which synchronises was still bringing, is not taken for a changed parcel."""
        for _ in range(2):
            try:
                data = self.place.get(parcel["name"], parcel["size"])
            except sharing.Oversized:
                data = None
            if data is not None and len(data) == parcel["size"] and hashlib.sha256(data).hexdigest() == parcel["sha256"]:
                return True
        return False

    def listing(self, named, last):
        """The bytes of the list of this data folder that names `named` (name -> dict)."""
        return json.dumps({"format": FORMAT, "writer": self.writer, "last": last,
                           "parcels": [{key: named[name][key] for key in ("name", "size", "sha256", "documents")}
                                       for name in sorted(named)]}, indent=1).encode() + b"\n"

    def publish(self):
        """Write the list of this data folder, naming `named`, unless it would grow beyond what
        a reader takes: the list in the shared place then stays as it is, and nothing more is
        given."""
        data = self.listing(self.named, self.number)
        if len(data) > LISTED:
            self.writing = False
            self.tell("The list of this data folder is full: its texts are no longer given")
            return
        self.put(f"texts/{self.writer}.json", data)

    def forget(self, writer, name):
        """Forget a parcel and what it offered: what was compared with it is compared again.
        The caller holds the transaction."""
        self.db.execute("DELETE FROM exchange.compared WHERE (md5,sha256,size,format) IN (SELECT md5,sha256,size,format "
                        "FROM exchange.entries WHERE writer=? AND name=?)", (writer, name))
        self.db.execute("DELETE FROM exchange.entries WHERE writer=? AND name=?", (writer, name))
        self.db.execute("DELETE FROM exchange.parcels WHERE writer=? AND name=?", (writer, name))

    # ---- taking ---------------------------------------------------------------------------
    def receive(self):
        """Take the parcels that the lists of the other writers name and that this data folder
        has not taken yet: what they offer is remembered; the pages are given later, by
        `apply`. What a list no longer names was withdrawn by its writer, as was everything of
        a writer whose list and parcels are gone: it is forgotten. A list or a parcel that
        cannot be read once fetched is counted, and the others are taken all the same; a shared
        place that fails while it is fetched stops the exchange."""
        entries = self.entries if self.entries is not None else self.place.names("texts")
        writers = {entry.removesuffix(".json") for entry in entries if entry.endswith(".json")}
        writers = {writer for writer in writers if WRITER.match(writer) and writer != self.writer}
        for writer in sorted(writers):
            try:
                data = self.place.get(f"texts/{writer}.json", LISTED)
            except sharing.Oversized:
                self.counts["damaged"] += 1
                continue
            try:
                found, aside, _ = listing(data or b"", writer)
            except Newer:
                self.counts["newer"] += 1
                continue
            except ValueError:
                self.counts["damaged"] += 1
                continue
            self.counts["damaged"] += aside
            names = {parcel["name"] for parcel in found}
            with self.db:
                for (name,) in self.db.execute("SELECT name FROM exchange.parcels WHERE writer=?", (writer,)).fetchall():
                    if name not in names:
                        self.forget(writer, name)
            for parcel in found:
                self.take(writer, parcel)
        for (writer,) in self.db.execute("SELECT DISTINCT writer FROM exchange.parcels WHERE writer!=?", (self.writer,)).fetchall():
            if writer not in writers and not any(NUMBERED.match(name) for name in self.place.names(f"texts/{writer}")):
                with self.db:
                    for (name,) in self.db.execute("SELECT name FROM exchange.parcels WHERE writer=?", (writer,)).fetchall():
                        self.forget(writer, name)

    def fetch(self, parcel):
        """The lines of a parcel after its first, or None when it is not there as its list says
        (counted as damaged), or is of a newer format (counted, and remembered). A shared place
        that fails raises OSError or ValueError, as it does at any other step."""
        writer = NAME.match(parcel["name"]).group(1)
        data = self.bodies.get((parcel["name"], parcel["sha256"], parcel["size"]))
        if data is None:
            try:
                data = self.place.get(parcel["name"], parcel["size"])
            except sharing.Oversized:
                data = None
            if data is None or len(data) != parcel["size"] or hashlib.sha256(data).hexdigest() != parcel["sha256"]:
                self.counts["damaged"] += 1     # cut, changed or missing: taken another time
                return None
            while self.bodies and self.kept + len(data) > BODIES:
                self.kept -= len(self.bodies.pop(next(iter(self.bodies))))
            if len(data) <= BODIES:
                self.bodies[(parcel["name"], parcel["sha256"], parcel["size"])] = data
                self.kept += len(data)
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

    def remember(self, writer, parcel, state, stamp=None):
        self.db.execute("INSERT OR REPLACE INTO exchange.parcels(writer,name,sha256,size,documents,state,stamp) "
                        "VALUES(?,?,?,?,?,?,?)", (writer, parcel["name"], parcel["sha256"], parcel["size"], parcel["documents"],
                                                  state, stamp))

    def take(self, writer, parcel):
        """Take one parcel: what each of its documents offers is remembered, and the parcel
        marked as taken, in one step. A parcel that its list now describes otherwise than when
        it was taken is taken anew, and what it offered before is forgotten. Returns True when
        the parcel is taken."""
        row = self.db.execute("SELECT sha256,size,documents,state FROM exchange.parcels WHERE writer=? AND name=?",
                              (writer, parcel["name"])).fetchone()
        if row is not None and (row["sha256"], row["size"], row["documents"]) == (parcel["sha256"], parcel["size"],
                                                                                  parcel["documents"]):
            if row["state"] == "taken":
                return True
            if row["state"].startswith("newer:") and int(row["state"][6:]) > FORMAT:
                self.counts["newer"] += 1
                return False
        elif row is not None and row["state"] == "taken":
            with self.db:
                self.forget(writer, parcel["name"])
        lines = self.fetch(parcel)
        if lines is None:
            return False
        if len(lines) - 1 != parcel["documents"]:
            self.counts["damaged"] += 1         # not the parcel that its list describes
            return False
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
            self.forget(writer, parcel["name"])
            self.db.executemany("INSERT INTO exchange.entries(writer,name,line,md5,sha256,size,format,layout,pages,blanks) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?)", offers)
            self.remember(writer, parcel, "taken")
        return True

    # ---- giving to the documents of this data folder ---------------------------------------
    def candidates(self, doc=None, skip=()):
        """The offers of the parcels taken that would take a document of this data folder
        further, as (writer, name, line, doc), the most complete first; for one document with
        `doc`; the documents `skip` left aside. Nothing is fetched to know it."""
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
            "ORDER BY length(e.blanks)-length(replace(e.blanks,',',''))+(e.blanks!=''),e.writer,e.name,e.line",
            (*WAITING, *holders, *([doc] if doc is not None else []))).fetchall()
        readings, found = {}, []
        for row in rows:
            if row["doc"] in skip:
                continue
            if row["doc"] not in readings:
                readings[row["doc"]] = store.reading(self.db, row["doc"])
            if store.helps(readings[row["doc"]], row["layout"], positions(row["blanks"])):
                found.append((row["writer"], row["name"], row["line"], row["doc"]))
        return found

    def settled(self):
        """The sources were asked about the files measured since the last time: their documents
        may now take what is offered for them, and the sources are looked at anew, since one
        may have come or gone."""
        self.fresh.clear()
        self.sources = None

    def measured(self, file):
        """A file was copied for its reading, and measured: its document takes nothing before the
        sources are asked about it (`settled`)."""
        try:
            row = self.db.execute("SELECT doc FROM files WHERE id=?", (file,)).fetchone()
        except sqlite3.Error:
            return
        if row is not None:
            self.fresh.add(row["doc"])

    def apply(self):
        """Give the documents of this data folder that wait for their text what the parcels
        taken offer for their bytes, except those measured since the sources were last asked,
        which wait for their answer. Each document takes its most complete offer; what another
        offer adds is given at the next round (see `indexer.build`). Only the parcels that would
        take a reading further are fetched, each once, so an exchange with nothing new fetches
        nothing."""
        self.folders = None
        while self.give(self.candidates(skip=self.fresh | self.refused)):
            pass                                # a parcel that failed was forgotten: the next offers are taken

    def give(self, found):
        """Fetch the parcels of the offers `found` and give their pages, the first offer of each
        document only. A parcel is fetched only while one of its offers still takes a document
        further, and one found changed since it was taken is forgotten, to be taken again if its
        list still names it. The checksums that come with an offer are learnt first, and the
        document is chosen again after them; it takes nothing when a file of it changed since
        this run looked at it (`unchanged`)."""
        best, failed = {}, False
        for writer, name, line, doc in found:
            best.setdefault(doc, (writer, name, line))
        wanted = {}
        for doc, (writer, name, line) in best.items():
            wanted.setdefault((writer, name), []).append((line, doc))
        for (writer, name), lines in wanted.items():
            lines = [(line, doc) for line, doc in lines if self.helps(writer, name, line, doc)]
            parcel = self.db.execute("SELECT name,size,sha256,documents FROM exchange.parcels WHERE writer=? AND name=? "
                                     "AND state='taken'", (writer, name)).fetchone()
            if not lines or parcel is None:
                continue
            got = self.fetch(dict(parcel))
            if got is None:
                with self.db:
                    self.forget(writer, name)
                failed = True
                continue
            for number, _ in lines:
                told = document(got[number]) if number < len(got) else None
                if told is None:
                    continue
                keys = [told["md5"], told["sha256"]]
                with self.db:
                    target = store.taker(self.db, keys, told["size"], told["format"], self.holders())
                    if target is not None and store.learn(self.db, target, keys):
                        store.purge(self.db, target)
                        self.learnt += 1        # what was learnt may have moved the files, or left the text to a source
                        target = store.taker(self.db, keys, told["size"], told["format"], self.holders())
                    if target is not None and not self.unchanged(target):
                        self.refused.add(target)        # it is read here, as it is now
                    elif target is not None and store.take_pages(self.db, target, keys, told["pages"], self.holders()):
                        self.counts["received"] += 1
        return failed

    def helps(self, writer, name, line, doc):
        """True when an offer would still take a document further."""
        row = self.db.execute("SELECT layout,blanks FROM exchange.entries WHERE writer=? AND name=? AND line=?",
                              (writer, name, line)).fetchone()
        state = self.db.execute("SELECT state FROM docs WHERE id=?", (doc,)).fetchone()
        return row is not None and state is not None and state["state"] in WAITING \
            and store.helps(store.reading(self.db, doc), row["layout"], positions(row["blanks"]))

    def unchanged(self, doc):
        """False when a file of the document may hold other bytes than when this run looked at it:
        a file copied for its reading must still be the one copied, and every file of a folder at
        hand must still be a file, not a link, with the size and the date that the inventory
        saw, nothing of it may have changed since this run began, which the time of its last
        change says, and it must still be the very file that this run listed, whatever was done
        to its folder. A file that fails is left to be measured anew (`store.afresh`), so that what
        stands at its address later is not taken for it by its size and date. A file that this run
        did not list and that is no longer there shows no text, and the next inventory forgets it;
        a folder out of reach is taken at its word."""
        if self.folders is None:
            self.folders = {root["id"]: Path(root["path"]) if store.available(root) else None for root in store.roots(self.db)}
        def refused(file):
            store.afresh(self.db, file["id"])   # whatever stands there now is measured anew, whatever its size and date
            return False

        for file in self.db.execute("SELECT id,root,rel,size,mtime FROM files WHERE doc=?", (doc,)).fetchall():
            held = self.held.get(file["id"])
            if held is not None and not held[1]():
                return refused(file)
            folder = self.folders.get(file["root"])
            if folder is None:
                continue
            itself = self.seen.get((file["root"], file["rel"]))
            try:
                info = os.lstat(folder / file["rel"])
            except FileNotFoundError:
                if itself is not None:
                    return refused(file)        # there when this run listed it, and gone since
                continue
            except OSError:
                return False
            if stat.S_ISLNK(info.st_mode) or (info.st_size, info.st_mtime_ns) != (file["size"], file["mtime"]) \
                    or info.st_ctime_ns > self.began or itself is not None and (info.st_dev, info.st_ino) != itself:
                return refused(file)
        return True

    def offer(self, doc, file=None, still=None):
        """Whether a document whose file `file` was just copied for its reading, and measured,
        is to wait for what another data folder read of these very bytes: True when a parcel
        taken offers a reading that takes it further. The document is then not read here, and
        takes those pages at the first exchange that follows the next questions to the sources
        (`settled`), provided `still()` then says that the file is still the one copied.
        Nothing is fetched or written here. False, and the document is read here, when the
        shared place failed at the last exchange or may not be used."""
        self.fresh.add(doc)
        if self.ready is not True or self.failing or doc in self.refused:
            return False
        try:
            with self.attached(mend=False) as mine:
                if not mine or not self.candidates(doc):
                    return False
        except (OSError, sqlite3.OperationalError):
            return False
        if file is not None:
            self.held[file] = (doc, still or (lambda: True))
        return True

    # ---- giving to the others ---------------------------------------------------------------
    def send(self):
        """Put in the shared place the readings of this data folder that recognise pages which
        nothing taken offers for the same bytes and pages: documents with a file, read here or
        completed here, apart from pages that an earlier version read. A reading compared once
        is compared again when it changes (READING), or when what it was compared with is
        withdrawn; each comparison is made on one reading of the pages of the document, read at
        once."""
        if not self.writing:
            return
        # A reading found too large, or refused, is weighed again once in each run: what tells it
        # apart from another of the same bytes counts the text of a page by its compressed length.
        compared = {(row[0], row[1], row[2], row[3]): row[4]
                    for row in self.db.execute("SELECT md5,sha256,size,format,reading,verdict FROM exchange.compared").fetchall()
                    if row[5] not in ("large", "refused") or (row[0], row[1], row[2], row[3]) in self.weighed}
        changed = []
        for doc in self.db.execute(
                f"SELECT d.id,d.origin,d.size,d.format,r.reading FROM docs d JOIN ({READING}) r ON r.doc=d.id "
                "WHERE instr(d.origin,?)>0 AND instr(d.origin,?)=0 AND EXISTS (SELECT 1 FROM files f WHERE f.doc=d.id) "
                "ORDER BY d.id", (store.SEALED, store.INHERITED)).fetchall():
            keys = store.sums(doc["origin"])
            if set(keys) != {"md5", "sha256"} or doc["format"] not in extract.formats():
                continue
            bytes_ = (keys["md5"], keys["sha256"], doc["size"], doc["format"])
            if compared.get(bytes_) != doc["reading"]:
                changed.append((doc["id"], doc["origin"], bytes_, doc["reading"]))
        if not changed:
            return
        shipped, wanted = {}, {bytes_ for _, _, bytes_, _ in changed}
        sums = sorted({bytes_[0] for bytes_ in wanted})
        for start in range(0, len(sums), 500):
            chunk = sums[start:start + 500]
            for row in self.db.execute(
                    "SELECT e.md5,e.sha256,e.size,e.format,e.layout,e.pages,e.blanks FROM exchange.entries e "
                    "JOIN exchange.parcels p ON p.writer=e.writer AND p.name=e.name "
                    "AND (p.state='taken' OR (p.state='sending' AND p.writer=?)) "
                    f"WHERE e.md5 IN ({','.join('?' * len(chunk))})", (self.writer, *chunk)).fetchall():
                if (row["md5"], row["sha256"], row["size"], row["format"]) in wanted:
                    shipped.setdefault((row["md5"], row["sha256"], row["size"], row["format"], row["layout"]), set()).update(
                        set(range(row["pages"])) - positions(row["blanks"]))
        packer, given, seen = None, [], []
        for doc, origin, bytes_, reading in changed:
            rows = self.db.execute("SELECT p.id,p.page,p.section,p.ztext,p.needs_ocr,d.origin FROM docs d JOIN pages p ON p.doc=d.id "
                                   "WHERE d.id=? AND EXISTS (SELECT 1 FROM files f WHERE f.doc=d.id) ORDER BY p.id", (doc,)).fetchall()
            if not rows or rows[0]["origin"] != origin or reading_of(rows) != reading:
                continue                        # changed meanwhile: compared at the next exchange
            if len(rows) > PAGES:
                seen.append((*bytes_, reading, "large"))
                self.weighed.add(bytes_)
                continue
            shape = store.shape((row["page"], row["section"]) for row in rows)
            recognised = {index for index, row in enumerate(rows) if not row["needs_ocr"]}
            if recognised <= shipped.get((*bytes_, shape), set()):
                seen.append((*bytes_, reading, "offered"))
                continue                        # nothing that the shared place does not offer already
            pages = [[row["page"], row["section"], store.inflate(row["ztext"]), row["needs_ocr"]] for row in rows]
            raw = json.dumps({"md5": bytes_[0], "sha256": bytes_[1], "size": bytes_[2], "format": bytes_[3], "pages": pages},
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace") + b"\n"
            if document(raw) is None:
                seen.append((*bytes_, reading, "refused"))
                self.weighed.add(bytes_)
                continue                        # what no version that reads it could take
            line = (*bytes_, shape, len(rows), written(set(range(len(rows))) - recognised), reading)
            packer = packer or Packer(self.writer)
            if not packer.add(raw):
                if packer.count:
                    self.ship(packer, given)
                    if not self.writing:
                        break
                    packer, given = Packer(self.writer), []
                if not packer.add(raw):
                    seen.append((*bytes_, reading, "large"))
                    self.weighed.add(bytes_)
                    continue                    # too large for a parcel of its own
            given.append(line)
            shipped.setdefault((*bytes_, shape), set()).update(recognised)
        if given and self.writing:
            self.ship(packer, given)
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO exchange.compared(md5,sha256,size,format,reading,verdict) "
                                "VALUES(?,?,?,?,?,?)", seen)

    def ship(self, packer, given):
        """Put one parcel in the shared place under the next number, then name it in the list of
        this data folder, which another reads before taking it. The parcel is written in the
        bookkeeping first, as being sent, so that a writing cut short is finished or forgotten
        by the next exchange (`reconcile`). When the numbers or the list would come to their
        end, nothing is put, and this data folder gives nothing more."""
        data = packer.close()
        number = self.number + 1
        if number > LAST:
            self.writing = False
            self.tell("This data folder has given its last parcel: its texts are no longer given")
            return
        parcel = {"name": f"texts/{self.writer}/{number:06d}.jsonl.gz", "size": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(), "documents": len(given)}
        named = {**self.named, parcel["name"]: parcel}
        listed = self.listing(named, number)
        if len(listed) > LISTED:
            self.writing = False
            self.tell("The list of this data folder is full: its texts are no longer given")
            return
        with self.db:                           # the number is never given again, whatever is lost afterwards
            self.db.execute("INSERT OR REPLACE INTO exchange.notes(key,value) VALUES(?,?)", (f"number:{self.writer}", str(number)))
            self.remember(self.writer, parcel, "sending")
            self.db.executemany("INSERT INTO exchange.entries(writer,name,line,md5,sha256,size,format,layout,pages,blanks) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?)", [(self.writer, parcel["name"], line, *told[:7])
                                                                for line, told in enumerate(given, 1)])
        self.number = number
        self.put(parcel["name"], data)
        self.put(f"texts/{self.writer}.json", listed)
        self.named = named
        with self.db:
            self.db.execute("UPDATE exchange.parcels SET state='taken' WHERE writer=? AND name=?", (self.writer, parcel["name"]))
            self.db.executemany("INSERT OR REPLACE INTO exchange.compared(md5,sha256,size,format,reading,verdict) "
                                "VALUES(?,?,?,?,?,'offered')", [(*told[:4], told[7]) for told in given])
        self.counts["sent"] += len(given)
        self.say(f"{len(given):,} documents put in the shared place ({len(data) / 1e6:,.1f} MB)")
