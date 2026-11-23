# Copyright 2026 Eudaemonic Enterprises Inc.
"""Private SQLite catalogue. Search remains available during indexing through WAL.

Text belongs to a document, and a document is recognised by its content, not by its address: the
files of the folders point at documents, so a file that is moved, renamed or copied keeps its
text. Addresses are stored from the home folder (`~/...`) and expanded when used.

A file is given the text of a document on the evidence of its content alone. Names, sizes and
dates prove nothing and never join a file to a document: a file that is new at its address, or
changed there, gets a document of its own. It joins a known document of its size and format
when a comparison shows the two to be the same:

- the same checksum of the whole file, computed by a full reading or given by the storage of
  the folder (`files.sum` keeps what is known of each file in this way);
- the same fingerprint (`files.print`), which is the whole file up to 4 MiB and samples of it
  beyond, unless checksums of the whole file show two documents;
- the word of a source, which says that the file is a work whose checksum a document carries.

What was read of a file outranks what a source says of it, and two different checksums of one
kind keep two documents apart whatever else agrees. `files.read` marks the file a document's
pages were read from, and `files.asked` names the sources that were asked about the file as it is.
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
FORMAT = 2
RETIRED = 101           # the format number of an earlier index once it has been converted
KEEP = 30 * 86400       # seconds a document keeps its text after its last file has gone
WHOLE = 4 << 20         # up to this size the whole file is its fingerprint; beyond, samples of it
SUMS = ("md5:", "sha256:")      # the checksums of a whole file that documents are known by

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE roots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, path TEXT NOT NULL UNIQUE,
    mount TEXT NOT NULL, scanned TEXT, problem TEXT
);
CREATE TABLE docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, size INTEGER NOT NULL, format TEXT NOT NULL, state TEXT NOT NULL,
    error TEXT, title TEXT, byline TEXT, year TEXT, gone INTEGER, gone_from INTEGER
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
    read INTEGER NOT NULL DEFAULT 0, print TEXT, sum TEXT, asked TEXT NOT NULL DEFAULT '', UNIQUE(root, rel)
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
    if (not filename.exists() or filename.stat().st_size == 0) and earlier_format(directory) in (1, RETIRED):
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


def set_apart(db, file):
    """Give a file a document of its own, when the document it was in turned out not to describe
    it. What is known of the file itself stays with it; the document it leaves keeps its text,
    its keys and its records. Returns the new document, or None when the file is not there."""
    row = db.execute("SELECT root,size,format FROM files WHERE id=?", (file,)).fetchone()
    if row is None:
        return None
    apart = new_document(db, row["size"], row["format"])
    db.execute("UPDATE files SET doc=?,read=0 WHERE id=?", (apart, file))
    describe(db, apart)                         # the title of the document it left is not its own
    tend_orphans(db, row["root"], drop=False)
    return apart


def disown(db, doc, file):
    """The document `doc` was read from the file `file`, and what the sources said of it turned
    out not to describe that file. The document stays the file's, with its text; the word of the
    sources is withdrawn from it, and the other files that nothing but that word had joined to
    it get documents of their own, about which the sources will be asked again."""
    db.execute("DELETE FROM doc_keys WHERE doc=? AND basis='source'", (doc,))
    db.execute("DELETE FROM records WHERE doc=?", (doc,))
    db.execute("UPDATE docs SET title=NULL,byline=NULL,year=NULL WHERE id=?", (doc,))
    prints = {key.split("#")[0] for key in keys_of(db, doc) if key.startswith("fp:")}
    for row in db.execute("SELECT id,print FROM files WHERE doc=? AND id!=?", (doc, file)).fetchall():
        if row["print"] is None or row["print"] not in prints:
            set_apart(db, row["id"])
        db.execute("UPDATE files SET asked='' WHERE id=?", (row["id"],))
    describe(db, doc)


def same_whole(db, file, doc):
    """True when the file is known to hold, byte for byte, what the document `doc` was read
    from: it is that file, or it has its fingerprint and is small enough for the fingerprint
    to be the whole file, or it shares a checksum that was measured."""
    row = db.execute("SELECT read,size,print,sum FROM files WHERE id=? AND doc=?", (file, doc)).fetchone()
    if row is None:
        return False
    if row["read"]:
        return True
    carried = db.execute("SELECT key,basis FROM doc_keys WHERE doc=?", (doc,)).fetchall()
    if row["size"] <= WHOLE and row["print"] in {key["key"].split("#")[0] for key in carried}:
        return True
    measured = {key["key"].split("@")[0] for key in carried if key["basis"] == "content"}
    return bool(measured & set((row["sum"] or "").split()))


def kinds(keys):
    """The checksums of whole files among `keys`, by kind, without the format some of them bear."""
    found = {}
    for key in keys:
        key = key.split("@")[0]
        if key.startswith(SUMS):
            found.setdefault(key.split(":", 1)[0], set()).add(key)
    return found


def against(one, other):
    """True when two sets of keys hold different checksums of one kind: they then describe
    different bytes."""
    one, other = kinds(one), kinds(other)
    return any(kind in other and not one[kind] & other[kind] for kind in one)


def keys_of(db, doc):
    return [row["key"] for row in db.execute("SELECT key FROM doc_keys WHERE doc=?", (doc,))]


def describe(db, doc):
    """Give the files of a document the title and the authors that the document carries, for
    the search by name."""
    told = db.execute("SELECT title,byline FROM docs WHERE id=?", (doc,)).fetchone()
    if told is not None:
        db.execute("UPDATE names SET title=?,byline=? WHERE rowid IN (SELECT id FROM files WHERE doc=?)",
                   (told["title"] or "", told["byline"] or "", doc))


def blanks(db, doc):
    """How far the reading of a document went: (pages still to recognise, pages), with a large
    first number for a document without pages."""
    pages, waiting = db.execute("SELECT count(*),coalesce(sum(needs_ocr),0) FROM pages WHERE doc=?", (doc,)).fetchone()
    return (waiting if pages else 1 << 30, pages)


def merge(db, one, other):
    """Two documents turned out to be the same: make them one and return it. The document whose
    reading went furthest is kept, a page recognised in the other only is taken from it, and the
    files, the keys and the records of the other go to the one kept. Documents of two formats
    are not merged, since each is read by its own reader, nor documents of two sizes, which
    cannot hold the same bytes: None is returned. The caller holds the transaction."""
    if one == other:
        return one
    rank = {"indexed": 0, "needs-ocr": 1, "ocr-error": 1, "covered": 3, "error": 4, "pending": 5, "name-only": 6}
    told = {row["id"]: row for row in db.execute("SELECT * FROM docs WHERE id IN (?,?)", (one, other))}
    if len(told) < 2:
        return one if one in told else other if other in told else None
    if (told[one]["format"], told[one]["size"]) != (told[other]["format"], told[other]["size"]):
        return None
    # Two different checksums of the whole file prove two documents, whatever else agrees.
    if against(keys_of(db, one), keys_of(db, other)):
        return None
    keep, drop = sorted((one, other), key=lambda doc: (rank.get(told[doc]["state"], 9), blanks(db, doc)[0], doc))
    # A page that only the other document has recognised is not lost with it.
    theirs = {row["page"]: row["ztext"] for row in db.execute(
        "SELECT page,ztext FROM pages WHERE doc=? AND needs_ocr=0 AND page IS NOT NULL", (drop,))}
    if theirs and blanks(db, keep)[1] == blanks(db, drop)[1]:
        for row in db.execute("SELECT id,page FROM pages WHERE doc=? AND needs_ocr=1", (keep,)).fetchall():
            if row["page"] in theirs:
                db.execute("UPDATE pages SET ztext=?,needs_ocr=0 WHERE id=?", (theirs[row["page"]], row["id"]))
        if told[keep]["state"] in ("needs-ocr", "ocr-error") and blanks(db, keep)[0] == 0:
            db.execute("UPDATE docs SET state='indexed',error=NULL WHERE id=?", (keep,))
    if told[keep]["state"] == "ocr-error" and told[drop]["state"] == "needs-ocr":
        # The other copy was waiting for an ordinary recognition: the document still is.
        db.execute("UPDATE docs SET state='needs-ocr',error=NULL WHERE id=? AND state='ocr-error'", (keep,))
    # The pages kept are those read from the files of the document kept: a file of the other
    # is, from now on, a copy by the evidence that joined the two.
    if blanks(db, keep)[1]:
        db.execute("UPDATE files SET read=0 WHERE doc=?", (drop,))
    db.execute("UPDATE files SET doc=? WHERE doc=?", (keep, drop))
    db.execute("UPDATE OR IGNORE doc_keys SET doc=? WHERE doc=?", (keep, drop))
    # Of two records of one source, the one that says the source holds the text is kept.
    db.execute("DELETE FROM records WHERE doc=? AND (text IS NULL OR text!=1) AND EXISTS (SELECT 1 FROM records r "
               "WHERE r.doc=? AND r.source=records.source AND r.text=1)", (keep, drop))
    db.execute("UPDATE OR IGNORE records SET doc=? WHERE doc=?", (keep, drop))
    if not told[keep]["title"] and told[drop]["title"]:
        db.execute("UPDATE docs SET title=?,byline=?,year=? WHERE id=?",
                   (told[drop]["title"], told[drop]["byline"], told[drop]["year"], keep))
    db.execute("UPDATE docs SET gone=NULL,gone_from=NULL WHERE id=? AND EXISTS (SELECT 1 FROM files WHERE doc=?)",
               (keep, keep))
    db.execute("DELETE FROM pages WHERE doc=?", (drop,))
    db.execute("DELETE FROM docs WHERE id=?", (drop,))
    describe(db, keep)
    return keep


def claim(db, doc, key, basis):
    """Give the document `doc` the checksum `key` of its bytes, and return the document that
    carries it afterwards. A document of the same format that carries the key already is the
    same document: the two are merged, unless other checksums keep them apart, in which case
    `doc` is returned without the key. The same bytes under another format are another
    document, read by another reader: the key is then kept under that format."""
    told = db.execute("SELECT format FROM docs WHERE id=?", (doc,)).fetchone()
    if told is None:
        return doc
    for name in (key, f"{key}@{told['format']}"):
        owner = db.execute("SELECT k.doc,d.format FROM doc_keys k JOIN docs d ON d.id=k.doc WHERE k.key=?",
                           (name,)).fetchone()
        if owner is None:
            db.execute("INSERT INTO doc_keys(key,doc,basis) VALUES(?,?,?)", (name, doc, basis))
            return doc
        if owner["doc"] == doc:
            return doc
        if owner["format"] == told["format"]:
            kept = merge(db, owner["doc"], doc)
            return doc if kept is None else kept
    return doc


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
    or that a source knows, is kept for a month after its last file has gone, for the day that
    file is found again elsewhere."""
    now = int(time.time()) if now is None else now
    db.execute("UPDATE docs SET gone=NULL,gone_from=NULL WHERE gone IS NOT NULL AND EXISTS "
               "(SELECT 1 FROM files f WHERE f.doc=docs.id)")
    db.execute("UPDATE docs SET gone=?,gone_from=? WHERE gone IS NULL AND NOT EXISTS "
               "(SELECT 1 FROM files f WHERE f.doc=docs.id)", (now, root))
    if drop:
        # Worth keeping for a while: the text that was read, what a source said of the document,
        # and the fingerprint or the checksums by which its file can be recognised again.
        bare = ("NOT EXISTS (SELECT 1 FROM pages p WHERE p.doc=docs.id) "
                "AND NOT EXISTS (SELECT 1 FROM records r WHERE r.doc=docs.id) "
                "AND NOT EXISTS (SELECT 1 FROM doc_keys k WHERE k.doc=docs.id AND "
                "(k.key LIKE 'fp:%' OR k.key LIKE 'sha256:%' OR k.key LIKE 'md5:%'))")
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
        db.executemany("DELETE FROM files WHERE id=?", [(r["id"],) for r in existing.values()])
        tend_orphans(db, root["id"], drop=False)
        for rel, size, mtime, old in fresh:
            # New at its address, or changed there: a document of its own, whatever its name,
            # its size and its date resemble. What it holds is compared afterwards.
            path = Path(rel)
            fmt = file_format(path)
            doc = new_document(db, size, fmt)
            if old:
                db.execute("UPDATE files SET size=?,mtime=?,format=?,doc=?,read=0,print=NULL,sum=NULL,asked='' "
                           "WHERE id=?", (size, mtime, fmt, doc, old["id"]))
                db.execute("UPDATE names SET title='',byline='' WHERE rowid=?", (old["id"],))
                continue
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
