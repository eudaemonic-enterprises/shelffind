# Copyright 2026 Eudaemonic Enterprises Inc.
"""Private SQLite catalogue. Search remains available during indexing through WAL.

Text belongs to a document, and a document is recognised by its content, not by its address: the
files of the folders point at documents, so a file that is moved, renamed or copied keeps its
text. Addresses are stored from the home folder (`~/...`) and expanded when used.

A document is known by the checksums of its whole content, MD5 and SHA-256, and by nothing
else. Names, sizes and dates prove nothing and never join a file to a document: a file that is
new at its address, or changed there, gets a document of its own. It joins a known document of
its size and format in one of two ways:

- a measure: the checksum of the file, computed by a full reading or given by the storage of
  the folder, is one the document carries (`files.sum` keeps what was measured of each file);
- the word of a source, which says that the file is a work whose checksum a document carries.

A measure outranks a word. Two different checksums of one kind keep two documents apart; what
was measured of a file refutes what a source says of it; and two documents that were both read
become one by a measure only, never on a word. What a source says is kept in `records` with
the very file it was said of, so that it follows that file and no other. `files.read` marks the
file the pages of its document were read from, and `files.asked` names the sources that were
asked about the file as it is.
"""
import contextlib
import fcntl
import os
import re
import sqlite3
import stat
import time
import zlib
from pathlib import Path

from . import fuzzy
from .config import expand
from .extract import SUPPORTED, file_format

INDEX, EARLIER = "index2.sqlite3", "index.sqlite3"
FORMAT = 3
RETIRED = 101           # the format number of an earlier index once it has been converted
KEEP = 30 * 86400       # seconds a document keeps its text after its last file has gone
SUMS = ("md5:", "sha256:")      # the checksums of a whole file that documents are known by

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
    key TEXT PRIMARY KEY, doc INTEGER NOT NULL REFERENCES docs ON DELETE CASCADE, basis TEXT NOT NULL
);
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
    elif version != FORMAT:
        db.close()
        if version == 2:
            raise ValueError(f"{filename} was made by a trial of this version and cannot be used: move it away; "
                             "the earlier index kept beside it, if any, is then converted again")
        raise ValueError(f"Unsupported index schema {version}")
    if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        db.execute("PRAGMA journal_mode=WAL")       # a converted index takes it at its first opening
    db.execute("PRAGMA synchronous=NORMAL")
    return db


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


def add_root(db, path, directory, name=None):
    path = Path(os.path.expanduser(str(path))).resolve(strict=True)
    if not path.is_dir() or path.is_relative_to(directory.resolve()):
        raise ValueError(f"Choose a document directory: {path}")
    for row in roots(db):
        other = Path(row["path"])
        if other == path:
            return str(path)
        if path.is_relative_to(other) or other.is_relative_to(path):
            raise ValueError(f"Overlapping root already registered: {other}")
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
        db.execute("DELETE FROM files WHERE root=?", (root["id"],))
        db.execute("DELETE FROM roots WHERE id=?", (root["id"],))
        db.execute("DELETE FROM pages WHERE doc IN (SELECT doc FROM leaving) AND NOT EXISTS "
                   "(SELECT 1 FROM files f WHERE f.doc=pages.doc)")
        db.execute("DELETE FROM docs WHERE id IN (SELECT doc FROM leaving) AND NOT EXISTS "
                   "(SELECT 1 FROM files f WHERE f.doc=docs.id)")
        db.execute("DROP TABLE leaving")
        if not db.execute("SELECT 1 FROM roots LIMIT 1").fetchone():
            db.execute("DELETE FROM pages")         # no folder is left: nothing is kept for later
            db.execute("DELETE FROM docs")
    return root["path"]


def available(root):
    path = Path(root["path"])
    return path.is_dir() and mount_point(path) == root["mount"]


def current(db, root):
    """True when `root`, read earlier, is still the registered folder it was."""
    row = db.execute("SELECT path FROM roots WHERE id=?", (root["id"],)).fetchone()
    return row is not None and row["path"] == root["stored"]


def terms_for_name(name):
    divided = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    return name if divided == name else name + " " + divided


def inventory(root, exclude):
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
    if not available(root) or not os.path.samestat(initial, base.stat()):
        raise OSError(f"Root changed while reading: {base}")
    return entries


def new_document(db, size, fmt):
    """A document of its own for a file whose content no known document is proved to share."""
    state = "pending" if fmt in SUPPORTED else "name-only"
    return db.execute("INSERT INTO docs(size,format,state) VALUES(?,?,?)", (size, fmt, state)).lastrowid


def has_pages(db, doc):
    return db.execute("SELECT 1 FROM pages WHERE doc=? LIMIT 1", (doc,)).fetchone() is not None


def kinds(keys):
    """The checksums among `keys`, by kind, without the format that the keys of a document bear."""
    found = {}
    for key in keys:
        key = key.split("@")[0]
        if key.startswith(SUMS):
            found.setdefault(key.split(":", 1)[0], set()).add(key)
    return found


def sums(text):
    """What a text such as `files.sum` holds, as kind -> checksum: one checksum of each kind."""
    return {kind: sorted(found)[0] for kind, found in kinds((text or "").split()).items()}


def against(one, other):
    """True when two sets of keys hold different checksums of one kind: they then describe
    different bytes."""
    one, other = kinds(one), kinds(other)
    return any(kind in other and not one[kind] & other[kind] for kind in one)


def keys_of(db, doc, basis=None):
    """The checksums by which a document is known, without the format they are kept under:
    all of them, or those of one basis, "content" for the measured ones."""
    rows = db.execute("SELECT key,basis FROM doc_keys WHERE doc=? ORDER BY key", (doc,))
    return [row["key"].split("@")[0] for row in rows if basis is None or row["basis"] == basis]


def holder(db, key, fmt):
    """The document known, by a measure, to hold the checksum `key` under the format `fmt`."""
    row = db.execute("SELECT doc FROM doc_keys WHERE key=?", (f"{key}@{fmt}",)).fetchone()
    return row["doc"] if row else None


def refuted(db, size, fmt, keys):
    """Whether what was measured refutes that a file of this size and format has the checksums
    `keys`, and otherwise the document that a measure knows by them: (refuted, document or
    None). A statement is refuted when its checksums are those of two documents, of a document
    of another size, or of a document that carries another checksum of the same kind."""
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


def describe(db, doc):
    """Give the files of a document the title and the authors that the document carries, for
    the search by name."""
    told = db.execute("SELECT title,byline FROM docs WHERE id=?", (doc,)).fetchone()
    if told is not None:
        db.execute("UPDATE names SET title=?,byline=? WHERE rowid IN (SELECT id FROM files WHERE doc=?)",
                   (told["title"] or "", told["byline"] or "", doc))


def retitle(db, doc):
    """Give a document the title and the authors of what the sources say of it now."""
    told = db.execute("SELECT title,byline,year FROM records WHERE doc=? AND coalesce(title,'')!='' "
                      "ORDER BY text DESC,source,id LIMIT 1", (doc,)).fetchone()
    db.execute("UPDATE docs SET title=?,byline=?,year=? WHERE id=?",
               (told["title"] if told else None, told["byline"] if told else None, told["year"] if told else None, doc))
    describe(db, doc)


def blanks(db, doc):
    """How far the reading of a document went: (pages still to recognise, pages), with a large
    first number for a document without pages."""
    pages, waiting = db.execute("SELECT count(*),coalesce(sum(needs_ocr),0) FROM pages WHERE doc=?", (doc,)).fetchone()
    return (waiting if pages else 1 << 30, pages)


def unread(db, doc):
    """A document has lost its pages: it waits for a reading again."""
    db.execute("UPDATE docs SET state=CASE WHEN state='covered' THEN 'covered' WHEN format IN "
               f"({','.join('?' * len(SUPPORTED))}) THEN 'pending' ELSE 'name-only' END,error=NULL,origin=NULL WHERE id=?",
               (*sorted(SUPPORTED), doc))
    db.execute("UPDATE files SET read=0 WHERE doc=?", (doc,))


def hand_over(db, old, new):
    """The pages of the document `old` were read from a file that belongs to the document
    `new`: they go there with it. When `new` has pages of its own, both were read from the same
    bytes: the reading that went furthest is kept, and a page recognised in the other only is
    taken from it. Returns True when the pages of `new` are, afterwards, those that came."""
    told = {row["id"]: row for row in db.execute("SELECT id,state,error FROM docs WHERE id IN (?,?)", (old, new))}
    came = True
    if has_pages(db, new):
        rank = {"indexed": 0, "needs-ocr": 1, "ocr-error": 1}
        worth = lambda doc: (rank.get(told[doc]["state"], 9), blanks(db, doc)[0])
        came = worth(old) < worth(new)
        keep, drop = (old, new) if came else (new, old)
        theirs = {row["page"]: row["ztext"] for row in db.execute(
            "SELECT page,ztext FROM pages WHERE doc=? AND needs_ocr=0 AND page IS NOT NULL", (drop,))}
        if theirs and blanks(db, keep)[1] == blanks(db, drop)[1]:
            for row in db.execute("SELECT id,page FROM pages WHERE doc=? AND needs_ocr=1", (keep,)).fetchall():
                if row["page"] in theirs:
                    db.execute("UPDATE pages SET ztext=?,needs_ocr=0 WHERE id=?", (theirs[row["page"]], row["id"]))
        db.execute("DELETE FROM pages WHERE doc=?", (drop,))
        state, error = told[keep]["state"], told[keep]["error"]
        if state in rank and blanks(db, keep)[0] == 0:
            state, error = "indexed", None
        elif state == "ocr-error" and told[drop]["state"] == "needs-ocr":
            state, error = "needs-ocr", None        # the other reading waited for an ordinary recognition
    else:
        state, error = told[old]["state"], told[old]["error"]
    if came:
        db.execute("UPDATE pages SET doc=? WHERE doc=?", (new, old))
        db.execute("UPDATE files SET read=0 WHERE doc=?", (new,))
        db.execute("UPDATE docs SET origin=NULL WHERE id=?", (new,))
    if state in ("indexed", "needs-ocr", "ocr-error"):
        db.execute("UPDATE docs SET state=?,error=? WHERE id=?", (state, error, new))
    unread(db, old)
    return came


def cards(db, old, new):
    """What the sources said of files that are gone, and that the document `old` kept, is said
    of the bytes that the document `new` is now known to hold: it goes there, the latest word
    of each source being the one kept."""
    db.execute("UPDATE records SET doc=? WHERE doc=? AND file IS NULL", (new, old))
    db.execute("DELETE FROM records WHERE doc=? AND file IS NULL AND id NOT IN "
               "(SELECT max(id) FROM records WHERE doc=? AND file IS NULL GROUP BY source)", (new, new))


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
    if not keys_of(db, doc):
        db.execute("DELETE FROM records WHERE doc=?", (doc,))   # nor of what bytes this was said
    if has_pages(db, doc) or db.execute("SELECT 1 FROM records WHERE doc=? LIMIT 1", (doc,)).fetchone():
        db.execute("UPDATE docs SET gone=?,gone_from=? WHERE id=? AND gone IS NULL", (int(time.time()), root, doc))
        retitle(db, doc)
    else:
        db.execute("DELETE FROM docs WHERE id=?", (doc,))


def move(db, file, target, reader=False):
    """Put a file in another document, with what the sources said of that very file."""
    row = db.execute("SELECT root,doc FROM files WHERE id=?", (file,)).fetchone()
    if row["doc"] == target:
        return
    db.execute("UPDATE files SET doc=?,read=? WHERE id=?", (target, int(reader), file))
    db.execute("UPDATE records SET doc=? WHERE file=?", (target, file))
    db.execute("UPDATE docs SET gone=NULL,gone_from=NULL WHERE id=?", (target,))
    tidy(db, row["doc"], row["root"])


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
    db.execute("DELETE FROM records WHERE doc=? AND file IS NULL", (doc,))
    for row in files[1:]:
        apart = new_document(db, row["size"], row["format"])
        db.execute("UPDATE files SET doc=?,read=0 WHERE id=?", (apart, row["id"]))
        db.execute("UPDATE records SET doc=? WHERE file=?", (apart, row["id"]))
    for row in files:
        place(db, row["id"])


def purge(db, doc):
    """Drop what the sources said of the files of a document and that its measured checksums
    refute; a file that such a word alone had brought is placed again."""
    carried = keys_of(db, doc, "content")
    astray = []
    for record in db.execute("SELECT r.id,r.file,r.keys,f.sum FROM records r LEFT JOIN files f ON f.id=r.file "
                             "WHERE r.doc=?", (doc,)).fetchall():
        if against(record["keys"].split(), carried):
            db.execute("DELETE FROM records WHERE id=?", (record["id"],))
            if record["file"] is not None:
                db.execute("UPDATE files SET asked='' WHERE id=?", (record["file"],))
                if not record["sum"]:
                    astray.append(record["file"])
    for file in astray:
        place(db, file)
    retitle(db, doc)


def by_measure(db, row, mine):
    """The document of a file of which the checksums `mine` were measured: the one known by
    them, which the file joins whatever was said of it; otherwise a document of its own, known
    by them from now on. The pages that were read from this very file, and that no reading in
    full has tied to other bytes, go with it."""
    file, here, fmt = row["id"], row["doc"], row["format"]
    target = None
    for key in mine:
        doc = holder(db, key, fmt)
        if doc is not None and not against(mine, keys_of(db, doc, "content")) \
                and db.execute("SELECT size FROM docs WHERE id=?", (doc,)).fetchone()["size"] == row["size"]:
            target = doc
            break
    if target is None:
        if own(db, here, file):
            target = here
            db.execute("DELETE FROM doc_keys WHERE doc=?", (here,))     # what was said of it is a word no more
        else:
            target = new_document(db, row["size"], fmt)
    reader = bool(row["read"])
    if target != here:
        origin = db.execute("SELECT origin FROM docs WHERE id=?", (here,)).fetchone()["origin"]
        reader = reader and has_pages(db, here) and "sha256:" not in (origin or "") and hand_over(db, here, target)
        move(db, file, target, reader)
    for key in mine:
        name = f"{key}@{fmt}"
        db.execute("INSERT OR IGNORE INTO doc_keys(key,doc,basis) VALUES(?,?,'content')", (name, target))
        # The files that were this work on the word of a source alone are placed again: with
        # this document when the word can be true of them, and without their word otherwise.
        for group in db.execute("SELECT DISTINCT k.doc,d.size FROM doc_keys k JOIN docs d ON d.id=k.doc "
                                "WHERE k.key>? AND k.key<?", (name + "#", name + "$")).fetchall():
            if db.execute("SELECT 1 FROM files WHERE doc=? LIMIT 1", (group["doc"],)).fetchone():
                regroup(db, group["doc"])
            else:                                   # the work whose file had gone is found again
                if group["size"] == row["size"]:
                    cards(db, group["doc"], target)
                db.execute("DELETE FROM docs WHERE id=?", (group["doc"],))
        # A source that said this checksum of a file measured to be another document, or of
        # the bytes of another document, is refuted by the two measures.
        for record in db.execute(
                "SELECT r.id,r.file,r.doc FROM record_keys k JOIN records r ON r.id=k.record JOIN docs d ON d.id=r.doc "
                "LEFT JOIN files f ON f.id=r.file WHERE k.key=? AND d.format=? AND r.doc!=? "
                "AND (coalesce(f.sum,'')!='' OR (r.file IS NULL AND EXISTS "
                "(SELECT 1 FROM doc_keys c WHERE c.doc=r.doc AND c.basis='content')))", (key, fmt, target)).fetchall():
            db.execute("DELETE FROM records WHERE id=?", (record["id"],))
            if record["file"] is not None:
                db.execute("UPDATE files SET asked='' WHERE id=?", (record["file"],))
            retitle(db, record["doc"])
    if reader and has_pages(db, target):
        origin = db.execute("SELECT origin FROM docs WHERE id=?", (target,)).fetchone()["origin"]
        if origin is None or ("sha256:" not in origin and len(mine) > 1):
            db.execute("UPDATE docs SET origin=? WHERE id=?", (" ".join(mine), target))
    purge(db, target)
    return target


def by_word(db, row):
    """The document of a file that was never measured: the one that a measure knows by the
    checksums the sources give for the file; otherwise one that it shares with the files of
    its size and format for which the sources give the same checksums; otherwise its own. A
    word that a measure refutes is dropped, and the source will be asked about the file again."""
    file, fmt, size = row["id"], row["format"], row["size"]
    while True:
        keys = said(db, file)
        wrong, known = refuted(db, size, fmt, keys)
        if not wrong:
            break
        dropped = [record["id"] for record in db.execute("SELECT id,keys FROM records WHERE file=?", (file,)).fetchall()
                   if refuted(db, size, fmt, record["keys"].split())[0]]
        if not dropped:
            keys, known = [], None                  # the sources contradict a measure only together
            break
        db.executemany("DELETE FROM records WHERE id=?", [(number,) for number in dropped])
        db.execute("UPDATE files SET asked='' WHERE id=?", (file,))
    names = [f"{key}@{fmt}#{size}" for key in keys]
    if known is not None:
        target, names = known, []
    else:
        groups = sorted(found["doc"] for found in db.execute(
            f"SELECT DISTINCT doc FROM doc_keys WHERE key IN ({','.join('?' * len(names))})", names)) if names else []
        here = db.execute("SELECT doc FROM files WHERE id=?", (file,)).fetchone()["doc"]
        if groups:
            target = groups[0]
            for other in groups[1:]:                # said to be one work: one document
                db.execute("UPDATE files SET doc=? WHERE doc=?", (target, other))
                db.execute("UPDATE records SET doc=? WHERE doc=?", (target, other))
                db.execute("UPDATE doc_keys SET doc=? WHERE doc=?", (target, other))
                db.execute("DELETE FROM docs WHERE id=?", (other,))
        elif own(db, here, file) and not has_pages(db, here):
            target = here
            db.execute("DELETE FROM doc_keys WHERE doc=?", (here,))
        else:
            target = new_document(db, size, fmt)
    move(db, file, target)
    for name in names:
        db.execute("INSERT OR IGNORE INTO doc_keys(key,doc,basis) VALUES(?,?,'source')", (name, target))
    retitle(db, target)
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
    know it by its checksum."""
    row = db.execute("SELECT sum FROM files WHERE id=?", (file,)).fetchone()
    if row is None:
        return False
    known, new = sums(row["sum"]), sums(" ".join(measured))
    kept = {**known, **new} if local else {**new, **known}
    if kept == known:
        return False
    mine = [kept[kind] for kind in sorted(kept)]
    db.execute("UPDATE files SET sum=? WHERE id=?", (" ".join(mine), file))
    db.executemany("DELETE FROM records WHERE id=?", [(record["id"],) for record in db.execute(
        "SELECT id,keys FROM records WHERE file=?", (file,)).fetchall() if against(record["keys"].split(), mine)])
    place(db, file)
    # Nor is a word kept that the measure refutes together with what was measured elsewhere.
    told = db.execute("SELECT doc,size,format FROM files WHERE id=?", (file,)).fetchone()
    refuted_now = [record["id"] for record in db.execute("SELECT id,keys FROM records WHERE file=?", (file,)).fetchall()
                   if refuted(db, told["size"], told["format"], sorted({*record["keys"].split(), *mine}))[0]]
    if refuted_now:
        db.executemany("DELETE FROM records WHERE id=?", [(number,) for number in refuted_now])
        retitle(db, told["doc"])
    still = " ".join(record["source"] for record in db.execute("SELECT source FROM records WHERE file=? ORDER BY source", (file,)))
    db.execute("UPDATE files SET asked=? WHERE id=?", (still, file))
    return True


def leave(db, file):
    """A file is gone from its address, or holds other bytes there: what was known of it was
    known of bytes that are no longer there. What a source said of it stays with its document
    when the document is known by the checksums of the file, measured, or by those the source
    gave: it is then said of those bytes, wherever a file that holds them is found again.
    Anything else that was said of the file is dropped, and so are the pages read from it when
    no measure ties them to its bytes.
    Returns the document the file was in, to be put in order once the file is out of it."""
    row = db.execute("SELECT doc,sum,read FROM files WHERE id=?", (file,)).fetchone()
    doc = row["doc"]
    carried = set(keys_of(db, doc))
    measured = bool(set((row["sum"] or "").split()) & carried)
    for record in db.execute("SELECT id,keys FROM records WHERE file=?", (file,)).fetchall():
        if measured or set(record["keys"].split()) & carried:
            db.execute("UPDATE records SET file=NULL WHERE id=?", (record["id"],))
        else:
            db.execute("DELETE FROM records WHERE id=?", (record["id"],))
    cards(db, doc, doc)
    if row["read"] and db.execute("SELECT origin FROM docs WHERE id=?", (doc,)).fetchone()["origin"] is None:
        db.execute("DELETE FROM pages WHERE doc=?", (doc,))
        unread(db, doc)
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


def tend_orphans(db, root=None, now=None, drop=True):
    """Note the documents that have just lost their last file and, when `drop` allows, forget
    those that have nothing worth keeping or have waited long enough. A document that was read,
    or that a source knows, is kept for a month after its last file has gone, for the day a
    file that holds its bytes is found again elsewhere."""
    now = int(time.time()) if now is None else now
    db.execute("UPDATE docs SET gone=NULL,gone_from=NULL WHERE gone IS NOT NULL AND EXISTS "
               "(SELECT 1 FROM files f WHERE f.doc=docs.id)")
    db.execute("UPDATE docs SET gone=?,gone_from=? WHERE gone IS NULL AND NOT EXISTS "
               "(SELECT 1 FROM files f WHERE f.doc=docs.id)", (now, root))
    if drop:
        # Worth keeping for a while: the text that was read, and what a source said of the bytes.
        bare = ("NOT EXISTS (SELECT 1 FROM pages p WHERE p.doc=docs.id) "
                "AND NOT EXISTS (SELECT 1 FROM records r WHERE r.doc=docs.id)")
        db.execute(f"DELETE FROM docs WHERE gone IS NOT NULL AND (({bare}) OR gone<?)", (now - KEEP,))


def scan(db, root, directory):
    """Only a complete enumeration may retire an indexed path."""
    entries = inventory(root, directory.resolve())
    existing = {r["rel"]: r for r in db.execute("SELECT * FROM files WHERE root=?", (root["id"],))}
    known = set()
    with db:
        fresh = []
        for rel, (size, mtime) in entries.items():
            old = existing.pop(rel, None)
            if not old or (size, mtime) != (old["size"], old["mtime"]):
                fresh.append((rel, size, mtime, old))
        # The addresses that vanished go first: a file found again elsewhere then meets a
        # document without a file, and takes it back with its text.
        for old in existing.values():
            doc = leave(db, old["id"])
            db.execute("DELETE FROM files WHERE id=?", (old["id"],))
            tidy(db, doc, root["id"])
        for rel, size, mtime, old in fresh:
            # New at its address, or changed there: a document of its own, whatever its name,
            # its size and its date resemble. What it holds is compared afterwards.
            path = Path(rel)
            fmt = file_format(path)
            if old:
                # What was measured of the file, and what was said of it, was of other bytes.
                left = leave(db, old["id"])
                doc = new_document(db, size, fmt)
                db.execute("UPDATE files SET size=?,mtime=?,format=?,doc=?,read=0,sum=NULL,asked='' WHERE id=?",
                           (size, mtime, fmt, doc, old["id"]))
                db.execute("UPDATE names SET title='',byline='' WHERE rowid=?", (old["id"],))
                tidy(db, left, root["id"])
                continue
            doc = new_document(db, size, fmt)
            cur = db.execute("INSERT INTO files(root,rel,name,folded,format,size,mtime,doc) VALUES(?,?,?,?,?,?,?,?)",
                             (root["id"], rel, path.name, fuzzy.fold(path.name), fmt, size, mtime, doc))
            folder = str(Path(Path(root["path"]).name) / path.parent)
            fuzzy.remember_names(db, (terms_for_name(path.name), terms_for_name(folder)), known)
            db.execute("INSERT INTO names(rowid,name,folder,title,byline) VALUES(?,?,?,'','')",
                       (cur.lastrowid, terms_for_name(path.name), terms_for_name(folder)))
        tend_orphans(db, root["id"])
        if not available(root):
            raise OSError(f"Root disconnected: {root['path']}")
        db.execute("UPDATE roots SET scanned=CURRENT_TIMESTAMP,problem=NULL WHERE id=?", (root["id"],))
    return len(entries)
