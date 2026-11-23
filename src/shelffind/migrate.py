# Copyright 2026 Eudaemonic Enterprises Inc.
"""Convert an index of the earlier format into the present one.

The new index is written beside the old one, in a file that takes its final name only when it
is complete. The old index is read and never modified, except for one number at the very end
that tells an earlier version of the program not to use it any more; it is deleted only on
request. An interruption at any moment therefore leaves the old index as it was, and the
conversion is simply done again.
"""
import fcntl
import hashlib
import os
import sqlite3
import time
from pathlib import Path

from . import __version__, fuzzy, history, store
from .config import portable

RETIRED = 101           # the format number an old index receives once it has been converted
# The better state wins when two copies of one document were read apart by the earlier version.
BETTER = {"indexed": 0, "needs-ocr": 1, "ocr-error": 2, "error": 3, "pending": 4, "name-only": 5}


def refuse_development(directory):
    if __version__.endswith(".dev") and directory.resolve() == store.state_directory().resolve():
        raise ValueError("A development version does not convert the index in use; "
                         "name another folder with --data-dir")


def earlier(directory):
    old = sqlite3.connect((directory / store.EARLIER).as_uri() + "?mode=ro", uri=True, timeout=60)
    old.row_factory = sqlite3.Row
    return old


def copy(old, new, progress):
    """Fill the new index from the old one, inside one transaction of the new index."""
    folders = {}
    for row in old.execute("SELECT * FROM roots ORDER BY id"):
        new.execute("INSERT INTO roots(id,name,path,mount,scanned,problem) VALUES(?,?,?,?,?,?)",
                    (row["id"], store.free_name(new, Path(row["path"]).name or "root"), portable(row["path"]),
                     portable(row["mount"]), row["scanned"], row["problem"]))
        folders[row["id"]] = Path(row["path"]).name

    # The files: each joins the document that its size, date and format designate, exactly as
    # the inventory would decide (`store.document_for`).
    total = old.execute("SELECT count(*) FROM files").fetchone()[0]
    documents, names_of, document_of, donor = {}, {}, {}, {}
    known = set()
    for count, row in enumerate(old.execute("SELECT * FROM files ORDER BY id"), 1):
        key, basis = store.stamp_key(row["size"], row["mtime"], row["format"], row["rel"]), "stamp"
        doc = documents.get(key)
        if doc is not None and row["size"] >= store.SMALL and row["name"] not in names_of[doc]:
            key += ":" + hashlib.sha256(row["name"].encode("utf-8", "surrogateescape")).hexdigest()[:16]
            doc, basis = documents.get(key), "stamp and name"
        if doc is None:
            doc = new.execute("INSERT INTO docs(size,format,state,error) VALUES(?,?,?,?)",
                              (row["size"], row["format"], row["state"], row["error"])).lastrowid
            new.execute("INSERT INTO doc_keys(key,doc,basis) VALUES(?,?,?)", (key, doc, basis))
            documents[key], names_of[doc] = doc, set()
            donor[doc] = (BETTER.get(row["state"], 9), row["id"])
        elif BETTER.get(row["state"], 9) < donor[doc][0]:
            new.execute("UPDATE docs SET state=?,error=? WHERE id=?", (row["state"], row["error"], doc))
            donor[doc] = (BETTER.get(row["state"], 9), row["id"])
        names_of[doc].add(row["name"])
        document_of[row["id"]] = doc
        new.execute("INSERT INTO files(id,root,rel,name,folded,format,size,mtime,doc) VALUES(?,?,?,?,?,?,?,?,?)",
                    (row["id"], row["root"], row["rel"], row["name"], row["folded"], row["format"],
                     row["size"], row["mtime"], doc))
        folder = str(Path(folders[row["root"]]) / Path(row["rel"]).parent)
        terms = (store.terms_for_name(row["name"]), store.terms_for_name(folder))
        fuzzy.remember_names(new, terms, known)
        new.execute("INSERT INTO names(rowid,name,folder,title,byline) VALUES(?,?,?,'','')", (row["id"], *terms))
        if count % 5000 == 0:
            progress(f"Converting the index: {count:,} of {total:,} files")

    # The text: a document read through several copies keeps the pages of the copy whose
    # reading went furthest.
    texts, current, count = [], None, 0
    for row in old.execute("SELECT file,page,section,text,needs_ocr FROM pages ORDER BY file,id"):
        doc = document_of.get(row["file"])
        if doc is None or donor[doc][1] != row["file"]:
            continue
        if row["file"] != current:
            fuzzy.remember_text(new, texts)
            current, texts = row["file"], []
        new.execute("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,?,?,?,?)",
                    (doc, row["page"], row["section"], store.deflate(row["text"]), row["needs_ocr"]))
        texts.append(row["text"])
        count += 1
        if count % 20000 == 0:
            progress(f"Converting the index: {count:,} pages of text")
    fuzzy.remember_text(new, texts)


def retire(directory):
    """Mark the old index as converted, so that an earlier version of the program refuses it.
    Returns False when it is busy; the mark is then put at a later start."""
    try:
        old = sqlite3.connect(directory / store.EARLIER, timeout=10)
        try:
            old.execute(f"PRAGMA user_version={RETIRED}")
            old.commit()
        finally:
            old.close()
        return True
    except sqlite3.Error:
        return False


def finish(directory):
    """What follows the appearance of the new index: the searches remembered by the old one
    join the journal, and the old index is marked. Both may be repeated without harm."""
    try:
        old = earlier(directory)
        try:
            asked = [(row["used"] // 1_000_000_000, row["query"])
                     for row in old.execute("SELECT query,used FROM history ORDER BY used")]
        finally:
            old.close()
        history.absorb(directory, asked)
    except sqlite3.Error:
        pass
    return retire(directory)


def upgrade(directory, progress=lambda text: None, wait=20.0):
    """Convert the earlier index of `directory`. Returns the path of the new index, or None when
    there is nothing to convert."""
    directory = Path(directory)
    target = directory / store.INDEX
    with (directory / "migrate.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists():                     # done already, perhaps by another process
            if store.earlier_format(directory) == 1:
                finish(directory)
            return target
        if store.earlier_format(directory) != 1:
            return None
        refuse_development(directory)
        for stale in directory.glob(store.INDEX + ".partial.*"):
            stale.unlink()
        # A reading of documents by an earlier version may be under way: it is given some time
        # to end, then the conversion proceeds from what the old index holds at that moment.
        with (directory / "index.lock").open("a") as building:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(building, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        progress("A reading of documents is under way: its latest documents will be read again")
                        break
                    time.sleep(0.2)
            partial = directory / f"{store.INDEX}.partial.{os.getpid()}"
            os.close(os.open(partial, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
            old, new = earlier(directory), store.open_index(partial)
            try:
                # The partial file is thrown away after any failure, so it needs no journal of
                # its own; it is written to the disk in full before it takes its name.
                new.execute("PRAGMA journal_mode=OFF")
                new.execute("PRAGMA synchronous=OFF")
                old.execute("BEGIN")            # one view of the old index for the whole copy
                new.executescript("BEGIN;\n" + store.SCHEMA + "\nCOMMIT;")
                new.execute("BEGIN")
                copy(old, new, progress)
                new.execute("COMMIT")
                new.close()
                old.close()
                with open(partial, "rb") as written:
                    os.fsync(written.fileno())
                os.replace(partial, target)
                folder = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(folder)
                finally:
                    os.close(folder)
            except BaseException:
                new.close()
                old.close()
                partial.unlink(missing_ok=True)
                raise
        finish(directory)
        progress("The index is converted; the earlier one is kept until `shelffind upgrade --discard-old`")
        return target


def discard_old(directory):
    """Delete the earlier index once the new one exists. Returns the bytes freed."""
    directory = Path(directory)
    if not (directory / store.INDEX).exists():
        raise ValueError("The new index does not exist yet: nothing is deleted")
    freed = 0
    for name in (store.EARLIER, store.EARLIER + "-wal", store.EARLIER + "-shm"):
        path = directory / name
        if path.exists():
            freed += path.stat().st_size
            path.unlink()
    return freed
