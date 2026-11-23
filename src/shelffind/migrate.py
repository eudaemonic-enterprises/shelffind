# Copyright 2026 Eudaemonic Enterprises Inc.
"""Convert an index of the earlier format into the present one.

The new index is written beside the old one, in a file that takes its final name only when it
is complete. The old index is read and never modified, except for one number at the very end
that tells an earlier version of the program not to use it any more; it is deleted only on
request. An interruption at any moment therefore leaves the old index as it was.
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


def refuse_development(directory):
    if __version__.endswith(".dev") and directory.resolve() == store.state_directory().resolve():
        raise ValueError("A development version does not convert the index in use; "
                         "name another folder with --data-dir")


def copy(old, new, directory, progress):
    """Fill the new index from the old one, inside one transaction of the new index."""
    names_taken, roots = set(), {}
    for row in old.execute("SELECT * FROM roots ORDER BY id"):
        name, number = Path(row["path"]).name or "root", 2
        while name in names_taken:
            name, number = f"{Path(row['path']).name}-{number}", number + 1
        names_taken.add(name)
        new.execute("INSERT INTO roots(id,name,path,mount,scanned,problem) VALUES(?,?,?,?,?,?)",
                    (row["id"], name, portable(row["path"]), portable(row["mount"]), row["scanned"], row["problem"]))
        roots[row["id"]] = Path(row["path"]).name

    total = old.execute("SELECT count(*) FROM files").fetchone()[0]
    docs, doc_names, file_doc = {}, {}, {}
    better = {"indexed": 0, "needs-ocr": 1, "ocr-error": 2, "error": 3, "pending": 4, "name-only": 5}
    known = set()
    for count, row in enumerate(old.execute("SELECT * FROM files ORDER BY id"), 1):
        key = store.stamp_key(row["size"], row["mtime"], row["format"], row["rel"])
        doc = docs.get(key)
        if doc is not None and row["size"] >= store.SMALL and row["name"] not in doc_names[doc]:
            key += ":" + hashlib.sha256(row["name"].encode("utf-8", "surrogateescape")).hexdigest()[:16]
            doc = docs.get(key)
        if doc is None:
            doc = new.execute("INSERT INTO docs(size,format,state,error) VALUES(?,?,?,?)",
                              (row["size"], row["format"], row["state"], row["error"])).lastrowid
            new.execute("INSERT INTO doc_keys(key,doc,basis) VALUES(?,?,'stamp')", (key, doc))
            docs[key], doc_names[doc] = doc, set()
        else:
            state = new.execute("SELECT state FROM docs WHERE id=?", (doc,)).fetchone()[0]
            if better.get(row["state"], 9) < better.get(state, 9):
                new.execute("UPDATE docs SET state=?,error=? WHERE id=?", (row["state"], row["error"], doc))
        doc_names[doc].add(row["name"])
        file_doc[row["id"]] = doc
        new.execute("INSERT INTO files(id,root,rel,name,folded,format,size,mtime,doc) VALUES(?,?,?,?,?,?,?,?,?)",
                    (row["id"], row["root"], row["rel"], row["name"], row["folded"], row["format"],
                     row["size"], row["mtime"], doc))
        folder = str(Path(roots[row["root"]]) / Path(row["rel"]).parent)
        terms = (store.terms_for_name(row["name"]), store.terms_for_name(folder))
        fuzzy.remember_names(new, terms, known)
        new.execute("INSERT INTO names(rowid,name,folder,title,byline) VALUES(?,?,?,'','')", (row["id"], *terms))
        if count % 5000 == 0:
            progress(f"Converting the index: {count:,} of {total:,} files")

    filled, texts, current = set(), [], None
    pages = old.execute("SELECT file,page,section,text,needs_ocr FROM pages ORDER BY file,id")
    for count, row in enumerate(pages, 1):
        doc = file_doc.get(row["file"])
        if doc is None:
            continue
        if row["file"] != current:
            if texts:
                fuzzy.remember_text(new, texts)
            if current is not None:
                filled.add(file_doc[current])
            current, texts = row["file"], []
        if doc in filled:               # another copy of the same document gave its text already
            continue
        new.execute("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,?,?,?,?)",
                    (doc, row["page"], row["section"], store.deflate(row["text"]), row["needs_ocr"]))
        texts.append(row["text"])
        if count % 20000 == 0:
            progress(f"Converting the index: {count:,} pages of text")
    if texts:
        fuzzy.remember_text(new, texts)

    for row in old.execute("SELECT query,used FROM history ORDER BY used"):
        history.record(directory, "search", row["query"], when=row["used"] // 1_000_000_000)


def upgrade(directory, progress=lambda text: None, wait=20.0):
    """Convert the earlier index of `directory`. Returns the path of the new index, or None when
    there is nothing to convert."""
    directory = Path(directory)
    target = directory / store.INDEX
    with (directory / "migrate.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists():
            return target                       # another process did it in the meantime
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
                    time.sleep(0.5)
            partial = directory / f"{store.INDEX}.partial.{os.getpid()}"
            os.close(os.open(partial, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
            old = sqlite3.connect((directory / store.EARLIER).as_uri() + "?mode=ro", uri=True, timeout=60)
            old.row_factory = sqlite3.Row
            new = store.open_index(partial)
            try:
                old.execute("BEGIN")            # one view of the old index for the whole copy
                new.executescript("BEGIN;\n" + store.SCHEMA + "\nCOMMIT;")
                new.execute("BEGIN")
                copy(old, new, directory, progress)
                new.execute("COMMIT")
                new.execute("INSERT INTO passages(passages) VALUES('optimize')")
                new.commit()
                new.close()
                old.close()
                with open(partial, "rb") as written:
                    os.fsync(written.fileno())
                os.replace(partial, target)
            except BaseException:
                new.close()
                old.close()
                partial.unlink(missing_ok=True)
                raise
        retire = sqlite3.connect(directory / store.EARLIER, timeout=60)
        try:
            retire.execute(f"PRAGMA user_version={RETIRED}")
            retire.commit()
        finally:
            retire.close()
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
