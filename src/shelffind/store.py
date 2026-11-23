# Copyright 2026 Eudaemonic Enterprises Inc.
"""Private SQLite catalogue. Search remains available during indexing through WAL.

Text belongs to a document, and a document is recognised by its content, not by its address: the
files of the folders point at documents, so a file that is moved, renamed or copied keeps its
text. Addresses are stored from the home folder (`~/...`) and expanded when used.
"""
import contextlib
import fcntl
import hashlib
import os
import re
import sqlite3
import stat
import zlib
from pathlib import Path

from . import fuzzy
from .config import expand, portable
from .extract import SUPPORTED, file_format

INDEX, EARLIER = "index2.sqlite3", "index.sqlite3"
FORMAT = 2
SMALL = 64 * 1024       # below this size a file is identified together with its address

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE roots (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, path TEXT NOT NULL UNIQUE,
    mount TEXT NOT NULL, scanned TEXT, problem TEXT
);
CREATE TABLE docs (
    id INTEGER PRIMARY KEY, size INTEGER NOT NULL, format TEXT NOT NULL, state TEXT NOT NULL,
    error TEXT, title TEXT, byline TEXT, year TEXT
);
CREATE INDEX docs_state ON docs(state, id);
CREATE INDEX docs_size ON docs(size, format);
CREATE TABLE doc_keys (
    key TEXT PRIMARY KEY, doc INTEGER NOT NULL REFERENCES docs ON DELETE CASCADE, basis TEXT NOT NULL
);
CREATE INDEX doc_keys_doc ON doc_keys(doc);
CREATE TABLE files (
    id INTEGER PRIMARY KEY, root INTEGER NOT NULL REFERENCES roots ON DELETE CASCADE,
    rel TEXT NOT NULL, name TEXT NOT NULL, folded TEXT NOT NULL, format TEXT NOT NULL, size INTEGER NOT NULL,
    mtime INTEGER NOT NULL, doc INTEGER NOT NULL REFERENCES docs, UNIQUE(root, rel)
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
CREATE INDEX words_ready ON words(id) WHERE listed=0 AND (tier=0 OR df>=2);
CREATE VIRTUAL TABLE spelling USING fts5(grams,content='');
CREATE TABLE records (
    doc INTEGER NOT NULL REFERENCES docs ON DELETE CASCADE, source TEXT NOT NULL, ref TEXT NOT NULL,
    title TEXT, byline TEXT, year TEXT, flags TEXT, basis TEXT, text TEXT, local TEXT,
    PRIMARY KEY(doc, source)
);
PRAGMA user_version=2;
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


def open_index(filename):
    db = sqlite3.connect(filename, timeout=30)
    db.row_factory = sqlite3.Row
    db.create_function("word_grams", 1, fuzzy.grams, deterministic=True)
    db.create_function("sf_inflate", 1, inflate, deterministic=True)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA trusted_schema=ON")      # the view and the triggers call sf_inflate
    return db


def connect(directory, upgrade=True):
    """Open the index of `directory`, creating it when there is none. An index of the earlier
    format is converted first when `upgrade` allows it; otherwise NeedsUpgrade is raised."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = directory / INDEX
    if not filename.exists() and earlier_format(directory) == 1:
        if not upgrade:
            raise NeedsUpgrade("The index has the earlier format: run shelffind upgrade")
        from . import migrate
        migrate.upgrade(directory)
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
        raise ValueError(f"Unsupported index schema {version}")
    if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        db.execute("PRAGMA journal_mode=WAL")       # a converted index takes it at its first opening
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def earlier_format(directory):
    """The format number of an index of the earlier kind in `directory`, 0 when there is none."""
    filename = directory / EARLIER
    if not filename.is_file():
        return 0
    try:
        old = sqlite3.connect(filename.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            return old.execute("PRAGMA user_version").fetchone()[0]
        finally:
            old.close()
    except sqlite3.Error:
        return 0


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


def root_row(row):
    """A registered folder with its address expanded for this machine."""
    return {"id": row["id"], "name": row["name"], "path": expand(row["path"]), "stored": row["path"],
            "mount": expand(row["mount"]), "scanned": row["scanned"], "problem": row["problem"]}


def free_name(db, wanted):
    taken = {row["name"] for row in db.execute("SELECT name FROM roots")}
    name, number = wanted, 2
    while name in taken:
        name, number = f"{wanted}-{number}", number + 1
    return name


def add_root(db, path, directory, name=None):
    path = Path(expand(str(path))).resolve(strict=True)
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
                   (free_name(db, name or path.name or "root"), portable(path), portable(mount_point(path))))
    return str(path)


def roots(db, selector=None):
    rows = [root_row(row) for row in db.execute("SELECT * FROM roots ORDER BY id")]
    if selector is None:
        return rows
    matches = [r for r in rows if selector in (r["path"], r["stored"], r["name"], Path(r["path"]).name)]
    if len(matches) != 1:
        folded = [r for r in rows if selector.lower() in (r["name"].lower(), Path(r["path"]).name.lower())]
        if len(folded) != 1:
            raise ValueError(f"Choose one registered root by its full path: {selector}")
        matches = folded
    return matches


def remove_root(db, selector):
    """Forget one registered folder with its names, and the text of the documents that no other
    folder holds. The documents themselves are not touched."""
    try:
        root = roots(db, selector)[0]
    except ValueError:
        root = roots(db, str(Path(selector).expanduser().absolute()))[0]
    with db:
        db.execute("CREATE TEMP TABLE leaving AS SELECT DISTINCT doc FROM files WHERE root=?", (root["id"],))
        db.execute("DELETE FROM files WHERE root=?", (root["id"],))
        db.execute("DELETE FROM roots WHERE id=?", (root["id"],))
        alone = "doc IN (SELECT doc FROM leaving) AND NOT EXISTS (SELECT 1 FROM files f WHERE f.doc={})"
        db.execute("DELETE FROM pages WHERE " + alone.format("pages.doc"))
        db.execute("DELETE FROM docs WHERE id IN (SELECT doc FROM leaving) AND NOT EXISTS "
                   "(SELECT 1 FROM files f WHERE f.doc=docs.id)")
        db.execute("DROP TABLE leaving")
    return root["path"]


def available(root):
    path = Path(root["path"])
    return path.is_dir() and mount_point(path) == root["mount"]


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


def stamp_key(size, mtime, fmt, rel):
    """What the inventory alone tells of a file's content: its size, its date to the second and
    its format. A small file is identified with its address as well, since many small files are
    written in the same second."""
    key = f"stamp:{size}:{mtime // 1_000_000_000}:{fmt}"
    if size < SMALL:
        key += ":" + hashlib.sha256(rel.encode("utf-8", "surrogateescape")).hexdigest()[:16]
    return key


def new_document(db, key, size, fmt, basis="stamp"):
    state = "pending" if fmt in SUPPORTED else "name-only"
    doc = db.execute("INSERT INTO docs(size,format,state) VALUES(?,?,?)", (size, fmt, state)).lastrowid
    db.execute("INSERT INTO doc_keys(key,doc,basis) VALUES(?,?,?)", (key, doc, basis))
    return doc


def document_for(db, rel, name, size, mtime, fmt):
    """The document of a file that is new at this address, from the inventory alone.

    A known stamp means a copy, or the same file under a new address. Under another name while
    the first file is still there, the two are kept apart until their contents are compared. An
    unknown stamp with the size of a document whose files are all gone is taken for that
    document written anew, as a filing tool does when it copies a file and removes the original.
    """
    key = stamp_key(size, mtime, fmt, rel)
    found = db.execute("SELECT doc FROM doc_keys WHERE key=?", (key,)).fetchone()
    if found:
        names = [row["name"] for row in db.execute("SELECT name FROM files WHERE doc=?", (found["doc"],))]
        if not names or name in names or size < SMALL:
            return found["doc"]
        key += ":" + hashlib.sha256(name.encode("utf-8", "surrogateescape")).hexdigest()[:16]
        again = db.execute("SELECT doc FROM doc_keys WHERE key=?", (key,)).fetchone()
        return again["doc"] if again else new_document(db, key, size, fmt, "stamp and name")
    if size >= SMALL:
        gone = db.execute("SELECT d.id FROM docs d WHERE d.size=? AND d.format=? AND NOT EXISTS "
                          "(SELECT 1 FROM files f WHERE f.doc=d.id) LIMIT 2", (size, fmt)).fetchall()
        if len(gone) == 1:
            db.execute("INSERT INTO doc_keys(key,doc,basis) VALUES(?,?,'size')", (key, gone[0]["id"]))
            return gone[0]["id"]
    return new_document(db, key, size, fmt)


def forget_orphans(db):
    """Drop the small documents that no file points at any more: their identity includes their
    address, so nothing can find them again. A larger document stays, with its text, for the
    day its file is found again under another address."""
    db.execute("DELETE FROM pages WHERE doc IN (SELECT id FROM docs WHERE size<? AND NOT EXISTS "
               "(SELECT 1 FROM files f WHERE f.doc=docs.id))", (SMALL,))
    db.execute("DELETE FROM docs WHERE size<? AND NOT EXISTS (SELECT 1 FROM files f WHERE f.doc=docs.id)",
               (SMALL,))


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
        db.executemany("DELETE FROM files WHERE id=?", [(r["id"],) for r in existing.values()])
        for rel, size, mtime, old in fresh:
            path = Path(rel)
            fmt = file_format(path)
            doc = document_for(db, rel, path.name, size, mtime, fmt)
            if old:
                db.execute("UPDATE files SET size=?,mtime=?,doc=? WHERE id=?", (size, mtime, doc, old["id"]))
                continue
            cur = db.execute("INSERT INTO files(root,rel,name,folded,format,size,mtime,doc) "
                             "VALUES(?,?,?,?,?,?,?,?)",
                             (root["id"], rel, path.name, fuzzy.fold(path.name), fmt, size, mtime, doc))
            folder = str(Path(Path(root["path"]).name) / path.parent)
            described = db.execute("SELECT title,byline FROM docs WHERE id=?", (doc,)).fetchone()
            fuzzy.remember_names(db, (terms_for_name(path.name), terms_for_name(folder)), known)
            db.execute("INSERT INTO names(rowid,name,folder,title,byline) VALUES(?,?,?,?,?)",
                       (cur.lastrowid, terms_for_name(path.name), terms_for_name(folder),
                        described["title"] or "", described["byline"] or ""))
        forget_orphans(db)
        if not available(root):
            raise OSError(f"Root disconnected: {root['path']}")
        db.execute("UPDATE roots SET scanned=CURRENT_TIMESTAMP,problem=NULL WHERE id=?", (root["id"],))
    return len(entries)
