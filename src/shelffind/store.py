# Copyright 2026 Eudaemonic Enterprises Inc.
"""Private SQLite catalogue. Search remains available during indexing through WAL."""
import contextlib
import fcntl
import os
from pathlib import Path
import re
import sqlite3
import stat

from . import fuzzy
from .extract import file_format, SUPPORTED


SCHEMA = """
CREATE TABLE roots (
    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, mount TEXT NOT NULL,
    scanned TEXT, problem TEXT
);
CREATE TABLE files (
    id INTEGER PRIMARY KEY, root INTEGER NOT NULL REFERENCES roots ON DELETE CASCADE,
    rel TEXT NOT NULL, name TEXT NOT NULL, folded TEXT NOT NULL, format TEXT NOT NULL, size INTEGER NOT NULL,
    mtime INTEGER NOT NULL, state TEXT NOT NULL, error TEXT, UNIQUE(root, rel)
);
CREATE INDEX files_state ON files(state, id);
CREATE VIRTUAL TABLE names USING fts5(name, folder, tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER files_ad AFTER DELETE ON files BEGIN
    DELETE FROM names WHERE rowid=old.id;
END;
CREATE TABLE pages (
    id INTEGER PRIMARY KEY, file INTEGER NOT NULL REFERENCES files ON DELETE CASCADE,
    page INTEGER, section TEXT, text TEXT NOT NULL, needs_ocr INTEGER NOT NULL
);
CREATE INDEX pages_file ON pages(file);
CREATE VIRTUAL TABLE passages USING fts5(text, content='pages', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO passages(rowid,text) VALUES(new.id,new.text);
END;
CREATE TRIGGER pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO passages(passages,rowid,text) VALUES('delete',old.id,old.text);
END;
CREATE TRIGGER pages_au AFTER UPDATE OF text ON pages BEGIN
    INSERT INTO passages(passages,rowid,text) VALUES('delete',old.id,old.text);
    INSERT INTO passages(rowid,text) VALUES(new.id,new.text);
END;
CREATE TABLE words (term TEXT NOT NULL UNIQUE);
CREATE VIRTUAL TABLE spelling USING fts5(grams,content='');
CREATE TRIGGER words_ai AFTER INSERT ON words BEGIN
    INSERT INTO spelling(rowid,grams) VALUES(new.rowid,word_grams(new.term));
END;
CREATE TABLE history (query TEXT PRIMARY KEY, used INTEGER NOT NULL);
PRAGMA user_version=1;
"""


def state_directory():
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"
    return Path(base) / "shelffind"


def connect(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = directory / "index.sqlite3"
    os.close(os.open(filename, os.O_RDWR | os.O_CREAT, 0o600))
    db = sqlite3.connect(filename, timeout=30)
    db.row_factory = sqlite3.Row
    db.create_function("word_grams", 1, fuzzy.grams, deterministic=True)
    db.execute("PRAGMA foreign_keys=ON")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        with (directory / "schema.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
    elif version != 1:
        raise ValueError(f"Unsupported index schema {version}")
    return db


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


def add_root(db, path, directory):
    path = path.expanduser().resolve(strict=True)
    if not path.is_dir() or path.is_relative_to(directory.resolve()):
        raise ValueError(f"Choose a document directory: {path}")
    for row in db.execute("SELECT path FROM roots"):
        other = Path(row["path"])
        if other == path:
            return str(path)
        if path.is_relative_to(other) or other.is_relative_to(path):
            raise ValueError(f"Overlapping root already registered: {other}")
    with db:
        db.execute("INSERT INTO roots(path,mount) VALUES(?,?)", (str(path), mount_point(path)))
    return str(path)


def roots(db, selector=None):
    rows = db.execute("SELECT * FROM roots ORDER BY id").fetchall()
    if selector is None:
        return rows
    matches = [r for r in rows if selector in (r["path"], Path(r["path"]).name)]
    if len(matches) != 1:
        raise ValueError(f"Choose one registered root by its full path: {selector}")
    return matches


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


def scan(db, root, directory):
    """Only a complete enumeration may retire an indexed path."""
    entries = inventory(root, directory.resolve())
    existing = {r["rel"]: r for r in db.execute("SELECT * FROM files WHERE root=?", (root["id"],))}
    known = set()
    with db:
        for rel, (size, mtime) in entries.items():
            old = existing.pop(rel, None)
            if old and (size, mtime) == (old["size"], old["mtime"]):
                continue
            path = Path(rel)
            fmt = file_format(path)
            state = "pending" if fmt in SUPPORTED else "name-only"
            if old:
                db.execute("DELETE FROM pages WHERE file=?", (old["id"],))
                db.execute("UPDATE files SET size=?,mtime=?,state=?,error=NULL WHERE id=?",
                           (size, mtime, state, old["id"]))
            else:
                cur = db.execute("INSERT INTO files(root,rel,name,folded,format,size,mtime,state) "
                                 "VALUES(?,?,?,?,?,?,?,?)",
                                 (root["id"], rel, path.name, fuzzy.fold(path.name), fmt, size, mtime, state))
                folder = str(Path(Path(root["path"]).name) / path.parent)
                fuzzy.remember_words(db, (terms_for_name(path.name), terms_for_name(folder)), known)
                db.execute("INSERT INTO names(rowid,name,folder) VALUES(?,?,?)",
                           (cur.lastrowid, terms_for_name(path.name), terms_for_name(folder)))
        db.executemany("DELETE FROM files WHERE id=?", [(r["id"],) for r in existing.values()])
        if not available(root):
            raise OSError(f"Root disconnected: {root['path']}")
        db.execute("UPDATE roots SET scanned=CURRENT_TIMESTAMP,problem=NULL WHERE id=?", (root["id"],))
    return len(entries)
