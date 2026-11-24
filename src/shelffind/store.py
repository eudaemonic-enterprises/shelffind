# Copyright 2026 Eudaemonic Enterprises Inc.
"""Private SQLite catalogue. Search remains available during indexing through WAL.

Text belongs to a document, and a document is recognised by its content, not by its address: the
files of the folders point at documents, so a file that is moved, renamed or copied keeps its
text. Addresses are stored from the home folder (`~/...`) and expanded when used.

A document is known by the checksums of its whole content, MD5 and SHA-256, and by nothing
else. Names, sizes and dates prove nothing and never join a file to a document: a file that is
new at its address, or changed there, gets a document of its own. Where a file belongs follows
from two kinds of facts, and from nothing else, the order in which they came least of all
(`place`):

- what was measured of the file (`files.sum`): its checksums, computed by a reading in full or
  given by the storage of its folder. A measured file belongs to the document known by its
  checksums (`doc_keys`, basis `content`), whatever was said of it;
- what the sources say of the file (`records`, each kept with the very file it was said of).
  A file that was never measured belongs to the document that a measure knows by the checksums
  the sources give for it, and otherwise shares a document with the files of its size and
  format for which they give the same ones (basis `source`).

Pages belong to bytes. They are read from a copy of a file whose checksums are computed in the
same pass, and `docs.origin` keeps those checksums: no other bytes complete them or take them.
Pages that an earlier version read, when checksums were not kept, stay with the file they were
read from (`files.read`) and with it alone, until a reading in full of that file ties them to
its bytes; neither a storage nor a source can do that. Tied so, on the word of the inventory
that the file did not change, they are marked as such (INHERITED, in `docs.origin`). Only pages
that this version read from the very bytes it measured, or took whole from another machine, are
marked as sealed (SEALED), and only those are given to other machines: what an earlier version
wrote in an index, whenever it wrote it, is never given.

What a source says is believed of a file for as long as no measure refutes it: it is dropped
when the checksums it gives contradict those of the file, or are those of a document that the
file is measured not to be, of another size, or known by another checksum of the same kind.
It outlives the file only as far as a measure proved it: when it gives a checksum by which a
measure knows the document, it is kept with that document, as said of those bytes, for a month
after the last file that held them has gone, and so is their text: a file that holds them is
then found again wherever it reappears. Anything else that was said of a file goes with it.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import zlib
from pathlib import Path

from . import extract, fuzzy
from .config import expand
from .extract import file_format

INDEX, EARLIER = "index2.sqlite3", "index.sqlite3"
FORMAT = 3
RETIRED = 101           # the format number of an earlier index once it has been converted
KEEP = 30 * 86400       # seconds a document keeps its text after its last file has gone
SUMS = ("md5:", "sha256:")      # the checksums of a whole file that documents are known by
INHERITED = "earlier"           # in docs.origin: pages read by an earlier version, tied to bytes measured afterwards
SEALED = "sealed"               # in docs.origin: pages that this version read from the bytes measured, or took whole
CONVERTED = "converted_from"    # in the meta of an index converted from version 0.2 (see `migrate`)
SUM = re.compile(r"(?:md5:[0-9a-f]{32}|sha256:[0-9a-f]{64})\Z")

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE roots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, path TEXT NOT NULL UNIQUE,
    mount TEXT NOT NULL, scanned TEXT, problem TEXT
);
CREATE TABLE docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, size INTEGER NOT NULL, format TEXT NOT NULL, state TEXT NOT NULL,
    error TEXT, title TEXT, byline TEXT, year TEXT, gone INTEGER, gone_from INTEGER, origin TEXT
);
CREATE INDEX docs_state ON docs(state, id);
CREATE INDEX docs_size ON docs(size, format);
CREATE INDEX docs_gone ON docs(gone) WHERE gone IS NOT NULL;
CREATE TABLE doc_keys (
    key TEXT NOT NULL, format TEXT NOT NULL, size INTEGER NOT NULL, basis TEXT NOT NULL,
    doc INTEGER NOT NULL REFERENCES docs ON DELETE CASCADE,
    PRIMARY KEY (key, format, basis, size)
) WITHOUT ROWID;
CREATE UNIQUE INDEX doc_keys_measured ON doc_keys(key, format) WHERE basis='content';
CREATE INDEX doc_keys_doc ON doc_keys(doc);
CREATE TABLE files (
    id INTEGER PRIMARY KEY, root INTEGER NOT NULL REFERENCES roots ON DELETE CASCADE,
    rel TEXT NOT NULL, name TEXT NOT NULL, folded TEXT NOT NULL, format TEXT NOT NULL, size INTEGER NOT NULL,
    mtime INTEGER NOT NULL, doc INTEGER NOT NULL REFERENCES docs,
    read INTEGER NOT NULL DEFAULT 0, sum TEXT, asked TEXT NOT NULL DEFAULT '', UNIQUE(root, rel)
);
CREATE INDEX files_doc ON files(doc);
CREATE VIRTUAL TABLE names USING fts5(name, folder, title, byline, tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER files_ad AFTER DELETE ON files BEGIN
    DELETE FROM names WHERE rowid=old.id;
END;
CREATE TABLE pages (
    id INTEGER PRIMARY KEY, doc INTEGER NOT NULL REFERENCES docs ON DELETE CASCADE,
    page INTEGER, section TEXT, ztext BLOB NOT NULL, needs_ocr INTEGER NOT NULL
);
CREATE INDEX pages_doc ON pages(doc, id);
CREATE VIEW page_text AS SELECT id, sf_inflate(ztext) AS text FROM pages;
CREATE VIRTUAL TABLE passages USING fts5(text, content='page_text', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO passages(rowid,text) VALUES(new.id,sf_inflate(new.ztext));
END;
CREATE TRIGGER pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO passages(passages,rowid,text) VALUES('delete',old.id,sf_inflate(old.ztext));
END;
CREATE TRIGGER pages_au AFTER UPDATE OF ztext ON pages BEGIN
    INSERT INTO passages(passages,rowid,text) VALUES('delete',old.id,sf_inflate(old.ztext));
    INSERT INTO passages(rowid,text) VALUES(new.id,sf_inflate(new.ztext));
END;
CREATE TABLE words (
    id INTEGER PRIMARY KEY, term TEXT NOT NULL UNIQUE, tier INTEGER NOT NULL, df INTEGER NOT NULL,
    listed INTEGER NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE spelling USING fts5(grams,size,content='');
CREATE VIRTUAL TABLE common USING fts5(grams,size,content='');
CREATE TABLE records (
    id INTEGER PRIMARY KEY, doc INTEGER NOT NULL REFERENCES docs ON DELETE CASCADE,
    file INTEGER REFERENCES files ON DELETE SET NULL, source TEXT NOT NULL, ref TEXT NOT NULL,
    keys TEXT NOT NULL DEFAULT '', title TEXT, byline TEXT, year TEXT, flags TEXT, basis TEXT,
    text INTEGER NOT NULL DEFAULT 0, local TEXT, UNIQUE(file, source)
);
CREATE INDEX records_doc ON records(doc);
CREATE INDEX records_ref ON records(source, ref);
CREATE TABLE record_keys (
    key TEXT NOT NULL, record INTEGER NOT NULL REFERENCES records ON DELETE CASCADE, PRIMARY KEY (key, record)
) WITHOUT ROWID;
CREATE INDEX record_keys_record ON record_keys(record);
PRAGMA user_version=3;
"""


EXPECTED = []           # what an index of this version declares, worked out at the first question


class NeedsUpgrade(ValueError):
    """An index of the earlier format waits to be converted."""


def state_directory():
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"
    return Path(base) / "shelffind"


def deflate(text):
    return zlib.compress(text.encode("utf-8"), 6)


def inflate(blob):
    """The text of a stored page. A damaged page reads as empty instead of breaking a search."""
    try:
        return zlib.decompress(blob).decode("utf-8", "replace")
    except (zlib.error, TypeError):
        return ""


def from_home(path):
    """An address as the index keeps it: from the home folder when it lies there. Nothing else
    is changed, so a name that holds a dollar sign or a tilde further on stays what it is."""
    path, home = str(path), os.environ.get("HOME") or str(Path.home())
    home = home.rstrip("/") or "/"
    if home != "/" and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home):]
    return path


def to_home(stored):
    """The address on this machine of what `from_home` stored."""
    if stored == "~" or stored.startswith("~/"):
        return (os.environ.get("HOME") or str(Path.home())).rstrip("/") + stored[1:]
    return stored


def open_index(filename):
    db = sqlite3.connect(filename, timeout=30)
    db.row_factory = sqlite3.Row
    db.create_function("word_grams", 1, fuzzy.grams, deterministic=True)
    db.create_function("word_size", 1, fuzzy.size, deterministic=True)
    db.create_function("sf_inflate", 1, inflate, deterministic=True)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA trusted_schema=ON")      # the view and the triggers call sf_inflate
    return db


def connect(directory, upgrade=True):
    """Open the index of `directory`, creating it when there is none. An index of the earlier
    format is converted first when `upgrade` allows it; otherwise NeedsUpgrade is raised."""
    directory = Path(directory).absolute()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = directory / INDEX
    # An earlier index without a new one beside it waits to be converted, also when it bears
    # the mark of a conversion: that conversion was cut before the new index took its name.
    if not filename.exists() or filename.stat().st_size == 0:
        earlier = earlier_format(directory)
        if earlier in (1, RETIRED):
            if not upgrade:
                raise NeedsUpgrade("The index has the earlier format: run shelffind upgrade")
            from . import migrate
            migrate.upgrade(directory)
        elif earlier or holds_something(directory):
            # Not an index this version knows: a new, empty index beside it would hide it.
            raise ValueError(f"{directory / EARLIER} holds data and is not an index that this version can convert "
                             f"(format {earlier}): move it away, and nothing of it is touched")
    os.close(os.open(filename, os.O_RDWR | os.O_CREAT, 0o600))
    db = open_index(filename)
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        with (directory / "schema.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
    elif not suits(db):
        db.close()
        raise unusable(filename, version)
    if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        db.execute("PRAGMA journal_mode=WAL")       # a converted index takes it at its first opening
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def layout(db):
    """What an index declares: its tables, its indexes, its triggers and its views, each with
    its definition, without what SQLite itself keeps beside them."""
    listed = [(row[0], row[1], " ".join((row[2] or "").split())) for row in db.execute("SELECT type,name,sql FROM sqlite_master")]
    words = {name for kind, name, sql in listed if sql.upper().startswith("CREATE VIRTUAL TABLE")}
    beside = lambda name: name.rpartition("_")[0] in words and name.rpartition("_")[2] in ("config", "content", "data", "docsize", "idx")
    return sorted(one for one in listed if not one[1].startswith("sqlite_") and not beside(one[1]))


def suits(db):
    """True when an index has the format of this version and is laid out exactly as this
    version lays it out. An index made by a trial of this version, were it under the same
    format number, is not one that it can use, nor one beside which an earlier index may be
    taken for a leftover."""
    if not EXPECTED:
        model = open_index(":memory:")
        try:
            model.executescript(SCHEMA)
            EXPECTED.extend(layout(model))
        finally:
            model.close()
    try:
        return db.execute("PRAGMA user_version").fetchone()[0] == FORMAT and layout(db) == EXPECTED
    except sqlite3.Error:
        return False


def unusable(filename, version):
    """The refusal of an index that this version did not make."""
    how = "laid out otherwise" if version == FORMAT else f"format {version}"
    return ValueError(f"{filename} was not made by this version ({how}) and cannot be used: move it away; "
                      "the earlier index kept beside it, if any, is then converted again")


def earlier_format(directory):
    """The format number of an index of the earlier kind in `directory`, 0 when there is none.
    An index that is there and cannot be read is an error, never an absence: a new index made
    beside it would hide what it holds."""
    filename = Path(directory).absolute() / EARLIER
    if not filename.is_file():
        return 0
    try:
        old = sqlite3.connect(filename.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            return old.execute("PRAGMA user_version").fetchone()[0]
        finally:
            old.close()
    except sqlite3.Error as fault:
        raise ValueError(f"The earlier index cannot be read ({fault}): {filename}") from None


def holds_something(directory):
    """True when a file under the name of the earlier index holds tables of any kind."""
    filename = Path(directory).absolute() / EARLIER
    if not filename.is_file() or filename.stat().st_size == 0:
        return False
    try:
        old = sqlite3.connect(filename.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            return old.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] > 0
        finally:
            old.close()
    except sqlite3.Error as fault:
        raise ValueError(f"The earlier index cannot be read ({fault}): {filename}") from None


@contextlib.contextmanager
def index_lock(directory):
    with (directory / "index.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("An index build is already running") from None
        yield


def mount_point(path):
    for parent in (path, *path.parents):
        if parent.is_mount():
            return str(parent)
    return "/"


# The folders named in the settings, as name -> {"path": the address as written, "remote": its
# rclone twin or None}. The command line fills this once the settings are read.
declared = {}


def declare(tables):
    """Take the [roots.NAME] tables of the settings."""
    declared.clear()
    for name, table in tables.items():
        declared[name] = {"path": table["path"], "remote": table.get("remote")}


def root_row(row):
    """A registered folder with its address expanded for this machine."""
    return {"id": row["id"], "name": row["name"], "path": to_home(row["path"]), "stored": row["path"],
            "mount": to_home(row["mount"]), "scanned": row["scanned"], "problem": row["problem"],
            "remote": declared.get(row["name"], {}).get("remote")}


def adopt_declared(db, directory, progress=lambda text: None):
    """Register the folders that the settings name and the index does not know yet. A folder
    registered under another name takes the name of the settings; a folder whose address
    changed in the settings follows it, and its files keep their documents. A folder that
    cannot be reached now is left for another time."""
    for name, wanted in declared.items():
        try:
            path = Path(expand(wanted["path"])).resolve()
        except ValueError as fault:
            progress(f"[roots.{name}] {fault}")
            continue
        known = {root["name"]: root for root in roots(db)}
        if name in known and Path(known[name]["path"]) == path:
            continue
        if not path.is_dir():
            progress(f"The folder {name} of the settings cannot be reached now: {path}")
            continue
        try:
            if name in known:
                admit(db, path, directory, known[name]["id"])       # as for a folder registered anew
                with db:
                    db.execute("UPDATE roots SET path=?,mount=? WHERE id=?",
                               (from_home(path), from_home(mount_point(path)), known[name]["id"]))
                continue
            same = next((root for root in known.values() if Path(root["path"]) == path), None)
            if same:
                with db:
                    db.execute("UPDATE roots SET name=? WHERE id=?", (name, same["id"]))
            else:
                add_root(db, path, directory, name=name)
        except (ValueError, sqlite3.IntegrityError) as fault:
            progress(f"[roots.{name}] {fault}")


def free_name(db, wanted):
    taken = {row["name"] for row in db.execute("SELECT name FROM roots")}
    name, number = wanted, 2
    while name in taken:
        name, number = f"{wanted}-{number}", number + 1
    return name


def admit(db, path, directory, but=None):
    """Raise unless `path` may be a folder of documents: a folder outside the data folder of
    ShelfFind, where the program keeps files of its own and removes some, and neither inside
    another registered folder nor around one. `but` is the registered folder that is being
    moved there, when one is."""
    if not path.is_dir() or path.is_relative_to(Path(directory).resolve()):
        raise ValueError(f"Choose a document directory: {path}")
    for row in roots(db):
        other = Path(row["path"])
        if row["id"] != but and other != path and (path.is_relative_to(other) or other.is_relative_to(path)):
            raise ValueError(f"Overlapping root already registered: {other}")


def add_root(db, path, directory, name=None):
    path = Path(os.path.expanduser(str(path))).resolve(strict=True)
    admit(db, path, directory)
    if any(Path(row["path"]) == path for row in roots(db)):
        return str(path)
    with db:
        db.execute("INSERT INTO roots(name,path,mount) VALUES(?,?,?)",
                   (free_name(db, name or path.name or "root"), from_home(path), from_home(mount_point(path))))
    return str(path)


def roots(db, selector=None):
    """The registered folders, or the one that `selector` designates: by its name first, which
    is unique, then by its address, then by the last part of its address when no other folder
    ends alike."""
    rows = [root_row(row) for row in db.execute("SELECT * FROM roots ORDER BY id")]
    if selector is None:
        return rows
    for match in (lambda r: r["name"] == selector, lambda r: selector in (r["path"], r["stored"]),
                  lambda r: Path(r["path"]).name == selector, lambda r: r["name"].lower() == selector.lower(),
                  lambda r: Path(r["path"]).name.lower() == selector.lower()):
        found = [r for r in rows if match(r)]
        if len(found) == 1:
            return found
        if found:
            break
    raise ValueError(f"Choose one registered root by its name or its full path: {selector}")


def remove_root(db, selector):
    """Forget one registered folder with its names, and the text of the documents that no other
    folder holds, those it lost earlier included. The documents themselves are not touched."""
    try:
        root = roots(db, selector)[0]
    except ValueError:
        root = roots(db, str(Path(selector).expanduser().absolute()))[0]
    with db:
        # With the folder go the documents it lost earlier, and those without file whose folder
        # is not known: what was forgotten must not come back under another name.
        db.execute("CREATE TEMP TABLE leaving AS SELECT DISTINCT doc FROM files WHERE root=? "
                   "UNION SELECT id FROM docs WHERE gone IS NOT NULL AND (gone_from=? OR gone_from IS NULL)",
                   (root["id"], root["id"]))
        for file in db.execute("SELECT id FROM files WHERE root=?", (root["id"],)).fetchall():
            leave(db, file["id"])
        db.execute("DELETE FROM files WHERE root=?", (root["id"],))
        db.execute("DELETE FROM roots WHERE id=?", (root["id"],))
        drop(db, *(gone["doc"] for gone in db.execute(
            "SELECT l.doc FROM leaving l JOIN docs d ON d.id=l.doc WHERE NOT EXISTS (SELECT 1 FROM files f WHERE f.doc=l.doc)").fetchall()))
        # The documents that another folder still holds are put in order without the files that left.
        for kept in db.execute("SELECT l.doc FROM leaving l JOIN docs d ON d.id=l.doc").fetchall():
            tidy(db, kept["doc"])
        db.execute("DROP TABLE leaving")
        if not db.execute("SELECT 1 FROM roots LIMIT 1").fetchone():
            db.execute("DELETE FROM pages")         # no folder is left: nothing is kept for later
            db.execute("DELETE FROM docs")
    return root["path"]


def available(root):
    """The folder is there, on the file system it was registered on. A network folder whose
    program has died answers every question with an error ("Transport endpoint is not
    connected"): it is unavailable, and that is never a fault of the program."""
    path = Path(root["path"])
    try:
        return path.is_dir() and mount_point(path) == root["mount"]
    except OSError:
        return False


def current(db, root):
    """True when `root`, read earlier, is still the registered folder it was."""
    row = db.execute("SELECT path FROM roots WHERE id=?", (root["id"],)).fetchone()
    return row is not None and row["path"] == root["stored"]


def terms_for_name(name):
    divided = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    return name if divided == name else name + " " + divided


def inventory(root, exclude, seen=None):
    """The regular files of a folder, as relative address -> (size, date); with `seen`, what tells
    each file itself apart from another put at its address is kept there too: (device, inode)."""
    base = Path(root["path"])
    if not available(root):
        raise OSError(f"Root unavailable: {base}")
    initial = base.stat()
    entries = {}
    def raise_error(error):
        raise error
    for parent, dirs, names in os.walk(base, onerror=raise_error, followlinks=False):
        folder = Path(parent)
        dirs[:] = [d for d in dirs if folder / d != exclude and not (folder / d).is_symlink()]
        for name in names:
            path = folder / name
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                relative = str(path.relative_to(base))
                # SQLite TEXT expects Unicode. Files with undecodable names get a visible error.
                relative.encode("utf-8")
                entries[relative] = (info.st_size, info.st_mtime_ns)
                if seen is not None:
                    seen[relative] = (info.st_dev, info.st_ino)
    if not available(root) or not os.path.samestat(initial, base.stat()):
        raise OSError(f"Root changed while reading: {base}")
    return entries


def new_document(db, size, fmt):
    """A document of its own for a file whose content no known document is proved to share."""
    state = "pending" if fmt in extract.formats() else "name-only"
    return db.execute("INSERT INTO docs(size,format,state) VALUES(?,?,?)", (size, fmt, state)).lastrowid


def has_pages(db, doc):
    return db.execute("SELECT 1 FROM pages WHERE doc=? LIMIT 1", (doc,)).fetchone() is not None


def kinds(keys):
    """The checksums among `keys`, by kind."""
    found = {}
    for key in keys:
        if key.startswith(SUMS):
            found.setdefault(key.split(":", 1)[0], set()).add(key)
    return found


def checksums(given):
    """The checksums among what a source gives, when they are well formed and one of each kind
    at most: two checksums of one kind describe no file, and are taken for none."""
    found = sorted({key for key in given or () if isinstance(key, str) and SUM.match(key)})
    return found if len({key.split(":", 1)[0] for key in found}) == len(found) else []


def sums(text):
    """What a text such as `files.sum` holds, as kind -> checksum: one checksum of each kind."""
    return {kind: sorted(found)[0] for kind, found in kinds((text or "").split()).items()}


def against(one, other):
    """True when two sets of keys hold different checksums of one kind: they then describe
    different bytes."""
    one, other = kinds(one), kinds(other)
    return any(kind in other and not one[kind] & other[kind] for kind in one)


def keys_of(db, doc, basis=None):
    """The checksums by which a document is known: all of them, or those of one basis,
    "content" for the measured ones."""
    rows = db.execute("SELECT key,basis FROM doc_keys WHERE doc=? ORDER BY key", (doc,))
    return [row["key"] for row in rows if basis is None or row["basis"] == basis]


def holder(db, key, fmt):
    """The document known, by a measure, to hold the checksum `key` under the format `fmt`."""
    row = db.execute("SELECT doc FROM doc_keys WHERE key=? AND format=? AND basis='content'", (key, fmt)).fetchone()
    return row["doc"] if row else None


def refuted(db, size, fmt, keys):
    """Whether what was measured refutes that a file of this size and format has the checksums
    `keys`, and otherwise the document that a measure knows by them: (refuted, document or
    None). A statement is refuted when it holds two checksums of one kind, or when its
    checksums are those of two documents, of a document of another size, or of a document that
    carries another checksum of the same kind."""
    if any(len(found) > 1 for found in kinds(keys).values()):
        return True, None                           # two checksums of one kind are the checksums of no file
    known = {doc for doc in (holder(db, key, fmt) for key in keys) if doc is not None}
    if not known:
        return False, None
    if len(known) > 1:
        return True, None
    doc = known.pop()
    if db.execute("SELECT size FROM docs WHERE id=?", (doc,)).fetchone()["size"] != size \
            or against(keys, keys_of(db, doc, "content")):
        return True, None
    return False, doc


def said(db, file):
    """The checksums that the sources give for a file, one of each kind, when what they say
    agrees: sources that contradict one another say nothing of what the file holds."""
    given = [row["keys"].split() for row in db.execute("SELECT keys FROM records WHERE file=? ORDER BY id", (file,))]
    if any(against(one, other) for number, one in enumerate(given) for other in given[number + 1:]):
        return []
    found = {}
    for keys in given:
        found.update(sums(" ".join(keys)))
    return [found[kind] for kind in sorted(found)]


def describe(db, doc, file=None):
    """Give the files of a document, or one of them, the title and the authors that the
    document carries, for the search by name."""
    told = db.execute("SELECT title,byline FROM docs WHERE id=?", (doc,)).fetchone()
    if told is None:
        return
    which, value = ("rowid=?", file) if file is not None else ("rowid IN (SELECT id FROM files WHERE doc=?)", doc)
    db.execute(f"UPDATE names SET title=?,byline=? WHERE {which}", (told["title"] or "", told["byline"] or "", value))


def retitle(db, doc, file=None):
    """Give a document the title and the authors of what the sources say of it now. `file` is
    a file that has just joined it: the only one whose searchable name is brought up to date
    when the title of the document has not changed."""
    old = db.execute("SELECT title,byline,year FROM docs WHERE id=?", (doc,)).fetchone()
    if old is None:
        return
    told = db.execute("SELECT title,byline,year FROM records WHERE doc=? AND coalesce(title,'')!='' "
                      "ORDER BY text DESC,source,id LIMIT 1", (doc,)).fetchone()
    new = tuple(told) if told else (None, None, None)
    if tuple(old) != new:
        db.execute("UPDATE docs SET title=?,byline=?,year=? WHERE id=?", (*new, doc))
        describe(db, doc)
    elif file is not None:
        describe(db, doc, file)


def blanks(db, doc):
    """How far the reading of a document went: (pages still to recognise, pages), with a large
    first number for a document without pages."""
    pages, waiting = db.execute("SELECT count(*),coalesce(sum(needs_ocr),0) FROM pages WHERE doc=?", (doc,)).fetchone()
    return (waiting if pages else 1 << 30, pages)


def unread(db, doc):
    """A document has lost its pages: it waits for a reading again."""
    db.execute("UPDATE docs SET state=CASE WHEN state='covered' THEN 'covered' WHEN format IN "
               f"({','.join('?' * len(extract.formats()))}) THEN 'pending' ELSE 'name-only' END,error=NULL,origin=NULL WHERE id=?",
               (*sorted(extract.formats()), doc))
    db.execute("UPDATE files SET read=0 WHERE doc=?", (doc,))


def hand_over(db, old, new):
    """The pages of the document `old`, read by an earlier version from a file that belongs to
    the document `new`, go there with it. When `new` has pages of its own, both readings are of
    the same bytes, and nothing recognised is lost where it can be kept: readings of the same
    pages are merged page by page, the pages that `new` recognised staying as they are; of
    readings of other pages, the one that recognises more pages is kept. A document that keeps
    pages of the earlier reading is marked so (INHERITED, in `docs.origin`, see `by_measure`):
    the bytes they belong to were measured afterwards, not read, so they are not given to other
    machines. The state of `new` is then what its pages say, whatever a source had been left to
    answer for before. Returns True when the pages of `new` are, afterwards, those that came,
    or were completed by them."""
    told = {row["id"]: row for row in db.execute("SELECT id,state,error FROM docs WHERE id IN (?,?)", (old, new))}
    rows = lambda doc: db.execute("SELECT id,page,section,needs_ocr,ztext FROM pages WHERE doc=? ORDER BY id", (doc,)).fetchall()
    ours, theirs = rows(new), rows(old)
    keep, came, asks = old, True, False
    if ours:
        recognised = lambda pages: sum(1 for page in pages if not page["needs_ocr"])
        if [(row["page"], row["section"]) for row in ours] == [(row["page"], row["section"]) for row in theirs]:
            filled = [(mine["ztext"], row["id"]) for row, mine in zip(ours, theirs) if row["needs_ocr"] and not mine["needs_ocr"]]
            db.executemany("UPDATE pages SET ztext=?,needs_ocr=0 WHERE id=?", filled)
            keep, came = new, bool(filled)
        else:
            came = recognised(theirs) > recognised(ours)
            keep = old if came else new
        drop = new if keep == old else old
        # The other reading still waited for an ordinary recognition, whatever a source had
        # been left to answer for meanwhile.
        asks = blanks(db, drop)[0] > 0 and told[drop]["state"] != "ocr-error"
        db.execute("DELETE FROM pages WHERE doc=?", (drop,))
    if keep == old:
        db.execute("UPDATE pages SET doc=? WHERE doc=?", (new, old))
        db.execute("UPDATE files SET read=0 WHERE doc=?", (new,))
        db.execute("UPDATE docs SET origin=NULL WHERE id=?", (new,))
    elif came:                                      # completed: the file that comes is the one read
        db.execute("UPDATE files SET read=0 WHERE doc=?", (new,))
        db.execute(f"UPDATE docs SET origin=origin||' {INHERITED}' WHERE id=? AND origin IS NOT NULL AND instr(origin,?)=0",
                   (new, INHERITED))
    if blanks(db, new)[0] == 0:
        state, error = "indexed", None
    elif told[keep]["state"] == "ocr-error" and not asks:
        state, error = "ocr-error", told[keep]["error"]     # its recognition failed, and nothing asks for another
    else:
        state, error = "needs-ocr", None
    db.execute("UPDATE docs SET state=?,error=? WHERE id=?", (state, error, new))
    unread(db, old)
    return came


def cards(db, doc):
    """Of what the sources said of files of a document that are gone, the latest word of each
    source is the one kept."""
    db.execute("DELETE FROM records WHERE doc=? AND file IS NULL AND id NOT IN "
               "(SELECT max(id) FROM records WHERE doc=? AND file IS NULL GROUP BY source)", (doc, doc))


def drop(db, *docs):
    """Forget documents, all together. A file that was never measured, and of which the sources
    give a checksum that one of them was known by, is placed again once they are all gone:
    what is said of it may now name one document where it named two, or none where it named
    these."""
    waiting = set()
    for doc in docs:
        waiting.update(row["file"] for row in db.execute(
            "SELECT DISTINCT r.file FROM doc_keys k JOIN record_keys n ON n.key=k.key JOIN records r ON r.id=n.record "
            "JOIN files f ON f.id=r.file WHERE k.doc=? AND k.basis='content' AND f.format=k.format AND coalesce(f.sum,'')=''",
            (doc,)))
    db.executemany("DELETE FROM docs WHERE id=?", [(doc,) for doc in docs])
    for file in sorted(waiting):
        replace(db, file)


def tidy(db, doc, root=None):
    """Put in order a document that a file has just left. A document held together by the
    words of sources alone is made anew from what is said of the files it keeps. A document
    without any file keeps, for the day a file that holds its bytes is found again, the pages
    that a measure ties to those bytes and what the sources said of the bytes it is known by;
    it is forgotten at once when it has neither."""
    if db.execute("SELECT 1 FROM files WHERE doc=? LIMIT 1", (doc,)).fetchone():
        if has_pages(db, doc) or keys_of(db, doc, "content"):
            retitle(db, doc)
        else:
            regroup(db, doc)
        return
    told = db.execute("SELECT origin FROM docs WHERE id=?", (doc,)).fetchone()
    if told is None:
        return
    if told["origin"] is None:
        db.execute("DELETE FROM pages WHERE doc=?", (doc,))     # nothing can tell whose pages they were
    if has_pages(db, doc) or db.execute("SELECT 1 FROM records WHERE doc=? LIMIT 1", (doc,)).fetchone():
        db.execute("UPDATE docs SET gone=?,gone_from=? WHERE id=? AND gone IS NULL", (int(time.time()), root, doc))
        retitle(db, doc)
    else:
        drop(db, doc)


def move(db, file, target, reader=False):
    """Put a file in another document, with what the sources said of that very file."""
    row = db.execute("SELECT root,doc FROM files WHERE id=?", (file,)).fetchone()
    if row["doc"] == target:
        return
    db.execute("UPDATE files SET doc=?,read=? WHERE id=?", (target, int(reader), file))
    db.execute("UPDATE records SET doc=? WHERE file=?", (target, file))
    db.execute("UPDATE docs SET gone=NULL,gone_from=NULL WHERE id=?", (target,))
    tidy(db, row["doc"], row["root"])
    retitle(db, target, file)


def own(db, doc, file):
    """True when a document has this one file and is known by no measure: it can become
    whatever that file turns out to be."""
    others = db.execute("SELECT 1 FROM files WHERE doc=? AND id!=? LIMIT 1", (doc, file)).fetchone()
    return others is None and not keys_of(db, doc, "content")


def regroup(db, doc):
    """Make anew the documents of files that only the words of sources hold together, after one
    of them left or after something else was said or measured: each file starts again as a
    document of its own and is placed by what is said of it now."""
    if has_pages(db, doc) or keys_of(db, doc, "content"):
        return
    files = db.execute("SELECT id,size,format FROM files WHERE doc=? ORDER BY id", (doc,)).fetchall()
    db.execute("DELETE FROM doc_keys WHERE doc=?", (doc,))
    for row in files[1:]:
        apart = new_document(db, row["size"], row["format"])
        db.execute("UPDATE files SET doc=?,read=0 WHERE id=?", (apart, row["id"]))
        db.execute("UPDATE records SET doc=? WHERE file=?", (apart, row["id"]))
    for row in files:
        place(db, row["id"])


def purge(db, doc, only=None):
    """Drop what a measure refutes among what the sources said of the files of a document and
    of its bytes or, with `only`, of that one file. A statement is refuted by the checksums
    measured of its file; one about a file that is gone, by those of the document it stayed
    with; and either by those of another document that it names as well. When the whole
    document is looked at, its files that were never measured are placed again: what the
    sources say of one of them may now contradict a measure only when taken together. The
    sources will be asked again about a file whose record is dropped."""
    told = db.execute("SELECT size,format FROM docs WHERE id=?", (doc,)).fetchone()
    if told is None:
        return
    carried = keys_of(db, doc, "content")
    one = "" if only is None else " AND r.file=?"
    for record in db.execute("SELECT r.id,r.file,r.keys,f.sum FROM records r LEFT JOIN files f ON f.id=r.file "
                             f"WHERE r.doc=?{one}", (doc,) if only is None else (doc, only)).fetchall():
        mine = carried if record["file"] is None else (record["sum"] or "").split()
        if refuted(db, told["size"], told["format"], sorted({*record["keys"].split(), *mine}))[0]:
            db.execute("DELETE FROM records WHERE id=?", (record["id"],))
            if record["file"] is not None:
                db.execute("UPDATE files SET asked='' WHERE id=?", (record["file"],))
    if only is None:
        for file in db.execute("SELECT id FROM files WHERE doc=? AND coalesce(sum,'')=''", (doc,)).fetchall():
            place(db, file["id"])
    if db.execute("SELECT 1 FROM files WHERE doc=? LIMIT 1", (doc,)).fetchone():
        retitle(db, doc)
    else:
        tidy(db, doc)                               # without file, and perhaps with nothing left worth keeping


def by_measure(db, row, mine):
    """The document of a file of which the checksums `mine` were measured: the one known by
    them, which the file joins whatever was said of it; otherwise a document of its own, known
    by them from now on. The pages that an earlier version read from this very file, and that
    nothing has tied to bytes yet, go with it and are tied to the bytes measured."""
    file, here, fmt, size = row["id"], row["doc"], row["format"], row["size"]
    target = None
    for key in mine:
        doc = holder(db, key, fmt)
        if doc is not None and not against(mine, keys_of(db, doc, "content")) \
                and db.execute("SELECT size FROM docs WHERE id=?", (doc,)).fetchone()["size"] == size:
            target = doc
            break
    origin = db.execute("SELECT origin FROM docs WHERE id=?", (here,)).fetchone()["origin"]
    untied = bool(row["read"]) and origin is None and has_pages(db, here)
    if target is None:
        if own(db, here, file) and (untied or not has_pages(db, here)):
            target = here
            db.execute("DELETE FROM doc_keys WHERE doc=?", (here,))     # what was said of it is a word no more
        else:
            target = new_document(db, size, fmt)
    if target != here:
        untied = untied and hand_over(db, here, target)
        move(db, file, target, untied)
    grew = learn(db, target, mine)
    if untied and len(mine) > 1 and has_pages(db, target):
        # The pages read from this file by an earlier version are taken for those of the bytes
        # measured, on the word of the inventory; they are marked as such, for they were not
        # read from these very bytes, and no other machine receives them.
        db.execute("UPDATE docs SET origin=? WHERE id=? AND origin IS NULL", (" ".join(mine) + f" {INHERITED}", target))
    purge(db, target, None if grew else file)
    return target


def learn(db, doc, keys):
    """A measure tells that a document holds the bytes of the checksums `keys`: they become
    its keys. Returns True when one of them is new to it.

    Every statement that names one of these checksums is looked at again, once the document
    is known by all of them: a statement may give one of them with another that is not its
    own. A file never measured is placed anew by what is said of it, which may bring it here;
    a statement about a file measured to be another document, or about other bytes, is
    refuted."""
    told = db.execute("SELECT size,format FROM docs WHERE id=?", (doc,)).fetchone()
    grew = False
    for key in keys:
        grew |= db.execute("INSERT OR IGNORE INTO doc_keys(key,format,size,basis,doc) VALUES(?,?,?,'content',?)",
                           (key, told["format"], told["size"], doc)).rowcount > 0
    for key in keys:
        for record in db.execute(
                "SELECT DISTINCT r.doc,r.file,coalesce(f.sum,'') AS sum FROM record_keys k JOIN records r ON r.id=k.record "
                "JOIN docs d ON d.id=r.doc LEFT JOIN files f ON f.id=r.file WHERE k.key=? AND d.format=? AND r.doc!=?",
                (key, told["format"], doc)).fetchall():
            if record["file"] is not None and not record["sum"]:
                replace(db, record["file"])
            else:
                purge(db, record["doc"])
    return grew


def shape(identities):
    """What tells two readings of the same bytes apart by their pages: a digest of the page
    numbers and sections, in their order."""
    return hashlib.sha256(json.dumps([list(one) for one in identities]).encode()).hexdigest()[:16]


def reading(db, doc):
    """The reading of a document as far as another reading is compared with it: (layout,
    positions of the pages still to recognise), or None when it has no pages."""
    rows = db.execute("SELECT page,section,needs_ocr FROM pages WHERE doc=? ORDER BY id", (doc,)).fetchall()
    if not rows:
        return None
    return shape((row["page"], row["section"]) for row in rows), {number for number, row in enumerate(rows) if row["needs_ocr"]}


def helps(ours, theirs, blanks):
    """Whether a reading of the layout `theirs`, with the pages at the positions `blanks`
    still to recognise, takes the reading `ours` (see `reading`) further: ours has no pages,
    or has the same pages and one of its blanks is recognised there."""
    return ours is None or (ours[0] == theirs and bool(ours[1] - set(blanks)))


def answered(db, doc, holding):
    """True when a source among `holding`, whose text is relied upon, gives the checksums of
    the document and holds its text: the text is left to that source."""
    if not holding:
        return False
    return db.execute(f"SELECT 1 FROM records WHERE doc=? AND text=1 AND keys!='' AND source IN ({','.join('?' * len(holding))}) "
                      "LIMIT 1", (doc, *sorted(holding))).fetchone() is not None


def taker(db, keys, size, fmt, holding=()):
    """The document that would take pages read elsewhere from the bytes of the checksums
    `keys` (an MD5 and a SHA-256), of this size and format: the one that a measure knows by
    them, with a file, waiting for its text, and whose text no source among `holding` answers
    for. None when there is none, or when a measure contradicts these checksums."""
    if fmt not in extract.formats():
        return None
    wrong, doc = refuted(db, size, fmt, keys)
    if wrong or doc is None:
        return None
    told = db.execute("SELECT state FROM docs WHERE id=?", (doc,)).fetchone()
    if told["state"] not in ("pending", "error", "needs-ocr", "ocr-error") or answered(db, doc, holding) \
            or not db.execute("SELECT 1 FROM files WHERE doc=? LIMIT 1", (doc,)).fetchone():
        return None
    return doc


def take_pages(db, doc, keys, pages, holding=()):
    """Give a document the pages that another machine read from the bytes of the checksums
    `keys` (an MD5 and a SHA-256), as (page, section, text, still to recognise); `taker` chose
    the document. Returns True when something was given.

    The checksums are learnt first: they are facts about the bytes the document is known by,
    and may refute what a source said of one of its files, which then leaves it. The document
    is then looked at again: it must still have a file, wait for its text, and not be left to
    a source. A document without pages takes the reading whole. A document with pages takes
    only pages that it still has to recognise, from a reading of the same pages, page by page:
    nothing recognised here is ever replaced, so the readings of several machines add up,
    in whatever order they come."""
    if db.execute("SELECT 1 FROM docs WHERE id=?", (doc,)).fetchone() is None:
        return False                            # gone meanwhile: what was learnt moved its files elsewhere
    if learn(db, doc, keys):
        purge(db, doc)
    told = db.execute("SELECT state FROM docs WHERE id=?", (doc,)).fetchone()
    if told is None or told["state"] not in ("pending", "error", "needs-ocr", "ocr-error") or answered(db, doc, holding) \
            or not db.execute("SELECT 1 FROM files WHERE doc=? LIMIT 1", (doc,)).fetchone():
        return False
    ours = db.execute("SELECT id,page,section,needs_ocr FROM pages WHERE doc=? ORDER BY id", (doc,)).fetchall()
    if not ours:
        db.executemany("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,?,?,?,?)",
                       [(doc, page, section, deflate(body), int(bool(blank))) for page, section, body, blank in pages])
        fuzzy.remember_text(db, (body for _, _, body, blank in pages if not blank))
        state = "needs-ocr" if any(page[3] for page in pages) else "indexed"
    else:
        if [(row["page"], row["section"]) for row in ours] != [(page[0], page[1]) for page in pages]:
            return False                        # another layout of the same bytes: ours stays as it is
        filled = [(page[2], row["id"]) for row, page in zip(ours, pages) if row["needs_ocr"] and not page[3]]
        if not filled:
            return False
        db.executemany("UPDATE pages SET ztext=?,needs_ocr=0 WHERE id=?", [(deflate(body), number) for body, number in filled])
        fuzzy.remember_more(db, (body for body, _ in filled))
        left = len([row for row in ours if row["needs_ocr"]]) - len(filled)
        state = "indexed" if not left else ("ocr-error" if told["state"] == "ocr-error" else "needs-ocr")
    db.execute("UPDATE docs SET state=?,error=CASE WHEN ?='ocr-error' THEN error END,"
               "origin=CASE WHEN origin IS NULL THEN ? ELSE origin END WHERE id=?",
               (state, state, " ".join(sorted(keys)) + f" {SEALED}", doc))
    return True


def disproved(db, row):
    """Drop what the sources say of a file never measured and that a measure refutes: the
    sources will be asked about the file again."""
    dropped = [record["id"] for record in db.execute("SELECT id,keys FROM records WHERE file=?", (row["id"],)).fetchall()
               if refuted(db, row["size"], row["format"], record["keys"].split())[0]]
    if dropped:
        db.executemany("DELETE FROM records WHERE id=?", [(number,) for number in dropped])
        db.execute("UPDATE files SET asked='' WHERE id=?", (row["id"],))


def by_word(db, row):
    """The document of a file that was never measured: the one that a measure knows by the
    checksums the sources give for the file; otherwise one that it shares with the files of
    its size and format for which the sources give the same checksums; otherwise its own. A
    word that a measure refutes is dropped, and the source will be asked about the file again."""
    file, fmt, size = row["id"], row["format"], row["size"]
    disproved(db, row)
    keys = said(db, file)
    wrong, known = refuted(db, size, fmt, keys)
    if wrong:
        keys, known = [], None                      # the sources contradict a measure only together
    if known is not None:
        target, keys = known, []
    else:
        here = db.execute("SELECT doc FROM files WHERE id=?", (file,)).fetchone()["doc"]
        mine = own(db, here, file) and not has_pages(db, here)
        if mine:                                    # what it holds, it held by what was said before
            db.execute("DELETE FROM doc_keys WHERE doc=?", (here,))
        groups = [found["doc"] for found in db.execute(
            "SELECT DISTINCT doc FROM doc_keys WHERE basis='source' AND format=? AND size=? "
            f"AND key IN ({','.join('?' * len(keys))}) ORDER BY doc", (fmt, size, *keys)).fetchall()] if keys else []
        if groups:
            target = groups[0]
            for other in groups[1:]:                # said to be one work: one document
                db.execute("UPDATE files SET doc=? WHERE doc=?", (target, other))
                db.execute("UPDATE records SET doc=? WHERE doc=?", (target, other))
                db.execute("UPDATE doc_keys SET doc=? WHERE doc=?", (target, other))
                db.execute("DELETE FROM docs WHERE id=?", (other,))
                describe(db, target)
        elif mine:
            target = here
        else:
            target = new_document(db, size, fmt)
    move(db, file, target)
    for key in keys:
        db.execute("INSERT OR IGNORE INTO doc_keys(key,format,size,basis,doc) VALUES(?,?,?,'source',?)", (key, fmt, size, target))
    retitle(db, target, file)
    return target


def place(db, file):
    """Put a file in the document that the facts name, and return that document.

    A file that was measured belongs to the document known by its checksums. A file that was
    not, and that the pages of its document were read from by an earlier version, keeps that
    document to itself until it is measured. Any other file is placed by what the sources say
    of it. The result depends on what is measured and said now, and not on the order in which
    it came to be known."""
    row = db.execute("SELECT id,doc,format,size,sum,read FROM files WHERE id=?", (file,)).fetchone()
    if row is None:
        return None
    mine = sums(row["sum"])
    if mine:
        return by_measure(db, row, [mine[kind] for kind in sorted(mine)])
    if row["read"] and has_pages(db, row["doc"]):
        disproved(db, row)
        retitle(db, row["doc"])
        return row["doc"]
    return by_word(db, row)


def replace(db, file):
    """Place a file again after what the sources say of it has changed."""
    row = db.execute("SELECT doc,sum,read FROM files WHERE id=?", (file,)).fetchone()
    if row is None:
        return None
    doc = row["doc"]
    if row["sum"] or has_pages(db, doc) or keys_of(db, doc, "content") \
            or db.execute("SELECT count(*) FROM files WHERE doc=?", (doc,)).fetchone()[0] == 1:
        return place(db, file)
    regroup(db, doc)                                # held with others by words alone: all are placed again
    return db.execute("SELECT doc FROM files WHERE id=?", (file,)).fetchone()["doc"]


def measure(db, file, measured, local=True):
    """Keep the checksums `measured` of the whole of a file, computed by a full reading
    (`local`) or given by its storage, and put the file in the document known by them. Returns
    True when something was learnt. The caller holds the transaction.

    A reading outranks the storage, which never replaces what a reading found. What a source
    said of this very file, and that the measure contradicts, was said of other bytes: it is
    dropped. The sources that did not know the file are asked about it again, since they may
    know it by its checksum.

    What a storage says of a file whose pages were read by an earlier version, and never tied
    to bytes, is not taken: a storage that held other bytes under the size and the date of the
    file would give those pages to every file that truly holds those bytes. Such a file is
    measured by a reading in full, which ties its pages to what it holds."""
    row = db.execute("SELECT f.sum,f.read,d.origin FROM files f JOIN docs d ON d.id=f.doc WHERE f.id=?", (file,)).fetchone()
    if row is None or (not local and row["read"] and row["origin"] is None):
        return False                                # see above: only a reading ties such pages to bytes
    known, new = sums(row["sum"]), sums(" ".join(measured))
    kept = {**known, **new} if local else {**new, **known}
    if kept == known:
        return False
    mine = [kept[kind] for kind in sorted(kept)]
    db.execute("UPDATE files SET sum=? WHERE id=?", (" ".join(mine), file))
    place(db, file)
    still = " ".join(record["source"] for record in db.execute("SELECT source FROM records WHERE file=? ORDER BY source", (file,)))
    db.execute("UPDATE files SET asked=? WHERE id=?", (still, file))
    return True


def leave(db, file):
    """A file is gone from its address, or holds other bytes there: what was known of it was
    known of bytes that are no longer there. What a source said of it stays with its document
    only as far as a measure proved it: when it gives a checksum by which a measure knows the
    document, it is said of those bytes, wherever a file that holds them is found again.
    Anything else that was said of the file is forgotten with it: a statement that nothing
    proved, or that gives no checksum, would otherwise speak for bytes it may not describe,
    with no file left to refute it. Returns the document the file was in, to be put in order
    once the file is out of it (`tidy`), which also drops the pages read from the file when no
    measure ties them to its bytes."""
    doc = db.execute("SELECT doc FROM files WHERE id=?", (file,)).fetchone()["doc"]
    carried = set(keys_of(db, doc, "content"))
    for record in db.execute("SELECT id,keys FROM records WHERE file=?", (file,)).fetchall():
        if carried & set(record["keys"].split()):
            db.execute("UPDATE records SET file=NULL WHERE id=?", (record["id"],))
        else:
            db.execute("DELETE FROM records WHERE id=?", (record["id"],))
    cards(db, doc)
    return doc


def afresh(db, file):
    """Another file stands at the address of this one: nothing that was known of the file
    holds for it. It starts again as a document of its own."""
    row = db.execute("SELECT root,size,format FROM files WHERE id=?", (file,)).fetchone()
    if row is None:
        return None
    old = leave(db, file)
    apart = new_document(db, row["size"], row["format"])
    db.execute("UPDATE files SET doc=?,read=0,sum=NULL,asked='' WHERE id=?", (apart, file))
    db.execute("UPDATE names SET title='',byline='' WHERE rowid=?", (file,))
    tidy(db, old, row["root"])
    return apart


def slow_mounts():
    """The places of this machine whose files come through a network or a program, not from a
    disk: a document there opens after a wait."""
    found = []
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as table:
            for line in table:
                left, _, right = line.partition(" - ")
                kind = (right.split() or [""])[0]
                if kind == "fuse" or kind.startswith(("fuse.", "nfs", "cifs", "smb", "davfs", "sshfs", "9p", "ceph")):
                    place = left.split()[4]
                    found.append(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), place).rstrip("/") + "/")
    except (OSError, IndexError):
        pass
    return found


def at_hand(path, slow=None):
    """True when `path` is on a disk of this machine."""
    slow = slow_mounts() if slow is None else slow
    return not any(str(path).startswith(place) for place in slow)


def tend_orphans(db, root=None, now=None, forget=True):
    """Note the documents that have just lost their last file and, when `forget` allows, forget
    those that have nothing worth keeping or have waited long enough. A document that was read,
    or that a source knows, is kept for a month after its last file has gone, for the day a
    file that holds its bytes is found again elsewhere."""
    now = int(time.time()) if now is None else now
    db.execute("UPDATE docs SET gone=NULL,gone_from=NULL WHERE gone IS NOT NULL AND EXISTS "
               "(SELECT 1 FROM files f WHERE f.doc=docs.id)")
    db.execute("UPDATE docs SET gone=?,gone_from=? WHERE gone IS NULL AND NOT EXISTS "
               "(SELECT 1 FROM files f WHERE f.doc=docs.id)", (now, root))
    if forget:
        # Worth keeping for a while: the text that was read, and what a source said of the bytes.
        bare = ("NOT EXISTS (SELECT 1 FROM pages p WHERE p.doc=docs.id) "
                "AND NOT EXISTS (SELECT 1 FROM records r WHERE r.doc=docs.id)")
        drop(db, *(gone["id"] for gone in db.execute(
            f"SELECT id FROM docs WHERE gone IS NOT NULL AND (({bare}) OR gone<?)", (now - KEEP,)).fetchall()))


def scan(db, root, directory, seen=None):
    """Only a complete enumeration may retire an indexed path. With `seen`, what tells each file
    of the folder apart from another put at its address is kept there, by (folder, address)."""
    found = {} if seen is not None else None
    entries = inventory(root, directory.resolve(), found)
    if seen is not None:
        seen.update({(root["id"], rel): itself for rel, itself in found.items()})
    existing = {r["rel"]: r for r in db.execute("SELECT * FROM files WHERE root=?", (root["id"],))}
    known = set()
    with db:
        # A format that is read since the files were listed (a new version, or a converter that
        # the settings now name): its documents wait for their text.
        marks = ",".join("?" * len(extract.formats()))
        db.execute(f"UPDATE docs SET state='pending' WHERE state='name-only' AND format IN ({marks})", sorted(extract.formats()))
        fresh = []
        for rel, (size, mtime) in entries.items():
            old = existing.pop(rel, None)
            if not old or (size, mtime) != (old["size"], old["mtime"]):
                fresh.append((rel, size, mtime, old))
        # A file is new at its address, changed there, or gone from it. Each document that
        # lost a file is put in order once, when all the addresses have been gone through.
        left = set()
        for old in existing.values():
            left.add(leave(db, old["id"]))
            db.execute("DELETE FROM files WHERE id=?", (old["id"],))
        for rel, size, mtime, old in fresh:
            # New at its address, or changed there: a document of its own, whatever its name,
            # its size and its date resemble. What it holds is compared afterwards.
            path = Path(rel)
            fmt = file_format(path)
            if old:
                # What was measured of the file, and what was said of it, was of other bytes.
                left.add(leave(db, old["id"]))
                doc = new_document(db, size, fmt)
                db.execute("UPDATE files SET size=?,mtime=?,format=?,doc=?,read=0,sum=NULL,asked='' WHERE id=?",
                           (size, mtime, fmt, doc, old["id"]))
                db.execute("UPDATE names SET title='',byline='' WHERE rowid=?", (old["id"],))
                continue
            doc = new_document(db, size, fmt)
            cur = db.execute("INSERT INTO files(root,rel,name,folded,format,size,mtime,doc) VALUES(?,?,?,?,?,?,?,?)",
                             (root["id"], rel, path.name, fuzzy.fold(path.name), fmt, size, mtime, doc))
            folder = str(Path(Path(root["path"]).name) / path.parent)
            fuzzy.remember_names(db, (terms_for_name(path.name), terms_for_name(folder)), known)
            db.execute("INSERT INTO names(rowid,name,folder,title,byline) VALUES(?,?,?,'','')",
                       (cur.lastrowid, terms_for_name(path.name), terms_for_name(folder)))
        for doc in sorted(left):
            tidy(db, doc, root["id"])
        tend_orphans(db, root["id"])
        if not available(root):
            raise OSError(f"Root disconnected: {root['path']}")
        db.execute("UPDATE roots SET scanned=CURRENT_TIMESTAMP,problem=NULL WHERE id=?", (root["id"],))
    return len(entries)
