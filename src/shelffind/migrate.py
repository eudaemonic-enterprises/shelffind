# Copyright 2026 Eudaemonic Enterprises Inc.
"""Convert an index of the earlier format into the present one.

The new index is written beside the old one, in a file that takes its final name only when it
is complete. While it is copied the old index is locked against writing, so that nothing an
earlier version of the program does at that moment is lost; of the old index itself only one
number changes, at the very end, which tells such a version not to use it any more. It is
deleted only on request. An interruption at any moment therefore leaves the old index as it
was, and the conversion is simply done again.
"""
import fcntl
import hashlib
import os
import sqlite3
import time
from pathlib import Path

from . import __version__, fuzzy, history, store

RETIRED = 101           # the format number an old index receives once it has been converted
MARK = "converted"      # the note a new index carries when it was made from an old one
# The better state wins when two copies of one document were read apart by the earlier version.
BETTER = {"indexed": 0, "needs-ocr": 1, "ocr-error": 2, "error": 3, "pending": 4, "name-only": 5}


def refuse_development(directory):
    if __version__.endswith(".dev") and directory.resolve() == store.state_directory().resolve():
        raise ValueError("A development version does not convert the index in use; "
                         "name another folder with --data-dir")


def different(old, one, other):
    """True when the earlier version read two files and found different text in them: on a page
    that both have read, the words differ, or the files have not the same number of pages."""
    pages = [old.execute("SELECT text,needs_ocr FROM pages WHERE file=? ORDER BY id", (file,)).fetchall()
             for file in (one, other)]
    if len(pages[0]) != len(pages[1]):
        return True
    return any(not a["needs_ocr"] and not b["needs_ocr"] and a["text"] != b["text"] for a, b in zip(*pages))


def copy(old, new, progress):
    """Fill the new index from the old one, inside one transaction of the new index."""
    folders = {}
    for row in old.execute("SELECT * FROM roots ORDER BY id"):
        name = store.free_name(new, Path(row["path"]).name or "root")
        new.execute("INSERT INTO roots(id,name,path,mount,scanned,problem) VALUES(?,?,?,?,?,?)",
                    (row["id"], name, store.from_home(row["path"]), store.from_home(row["mount"]),
                     row["scanned"], row["problem"]))
        folders[row["id"]] = (name, Path(row["path"]).name)

    # What the earlier version read of each file: how many pages, how many still to recognise,
    # and how much text.
    read = {row["file"]: (row["pages"], row["blank"], row["letters"]) for row in old.execute(
        "SELECT file,count(*) AS pages,sum(needs_ocr) AS blank,sum(length(text)) AS letters FROM pages GROUP BY file")}

    def worth(row):
        """The smaller, the further the reading of this file went."""
        pages, blank, letters = read.get(row["id"], (0, 0, 0))
        return (BETTER.get(row["state"], 9), blank if pages else 1 << 30, -letters, row["id"])

    # The files: each joins the document that its size, date and format designate, as the
    # inventory would decide (`store.document_for`), unless the earlier version read two such
    # files and found different text in them.
    total = old.execute("SELECT count(*) FROM files").fetchone()[0]
    documents, names_of, document_of, donor = {}, {}, {}, {}
    known = set()
    for count, row in enumerate(old.execute("SELECT * FROM files ORDER BY id"), 1):
        place = folders[row["root"]][0]
        key, basis = store.stamp_key(row["size"], row["mtime"], row["format"], place, row["rel"]), "stamp"
        doc = documents.get(key)
        if doc is not None and row["size"] >= store.SMALL and row["name"] not in names_of[doc]:
            key, basis = key + ":" + store.digest(row["name"]), "stamp and name"
            doc = documents.get(key)
        if doc is not None and row["id"] in read and donor[doc][1] in read \
                and different(old, donor[doc][1], row["id"]):
            key = f"{key}:apart:{store.digest(place + '/' + row['rel'])}"
            doc, basis = documents.get(key), "text read apart"
        if doc is None:
            doc = new.execute("INSERT INTO docs(size,format,state,error) VALUES(?,?,?,?)",
                              (row["size"], row["format"], row["state"], row["error"])).lastrowid
            new.execute("INSERT INTO doc_keys(key,doc,basis) VALUES(?,?,?)", (key, doc, basis))
            documents[key], names_of[doc] = doc, set()
            donor[doc] = (worth(row), row["id"])
        elif worth(row) < donor[doc][0]:
            new.execute("UPDATE docs SET state=?,error=? WHERE id=?", (row["state"], row["error"], doc))
            donor[doc] = (worth(row), row["id"])
        names_of[doc].add(row["name"])
        document_of[row["id"]] = doc
        new.execute("INSERT INTO files(id,root,rel,name,folded,format,size,mtime,doc) VALUES(?,?,?,?,?,?,?,?,?)",
                    (row["id"], row["root"], row["rel"], row["name"], row["folded"], row["format"],
                     row["size"], row["mtime"], doc))
        folder = str(Path(folders[row["root"]][1]) / Path(row["rel"]).parent)
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
    new.execute("INSERT INTO meta(key,value) VALUES(?,?)", (MARK, str(int(time.time()))))


def converted(directory):
    """True when the new index of `directory` was made from the old one."""
    try:
        new = sqlite3.connect((directory / store.INDEX).as_uri() + "?mode=ro", uri=True, timeout=30)
        try:
            return new.execute("SELECT 1 FROM meta WHERE key=?", (MARK,)).fetchone() is not None
        finally:
            new.close()
    except sqlite3.Error:
        return False


def finish(directory):
    """What follows the appearance of the new index: the searches remembered by the old one
    join the journal, then the old index is marked as converted, so that an earlier version of
    the program refuses it. Both may be repeated without harm. Returns False when either could
    not be done, in which case the old index is left unmarked for another time."""
    try:
        old = sqlite3.connect((directory / store.EARLIER).as_uri() + "?mode=rw", uri=True, timeout=10)
    except sqlite3.Error:
        return False
    try:
        if old.execute("PRAGMA user_version").fetchone()[0] == 1:
            asked = [(used // 1_000_000_000, query)
                     for query, used in old.execute("SELECT query,used FROM history ORDER BY used")]
            if not history.absorb(directory, asked):
                return False
            old.execute(f"PRAGMA user_version={RETIRED}")
            old.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        old.close()


def upgrade(directory, progress=lambda text: None, wait=20.0):
    """Convert the earlier index of `directory`. Returns the path of the new index, or None when
    there is nothing to convert."""
    directory = Path(directory).absolute()
    target = directory / store.INDEX
    if not directory.is_dir():
        return None
    with (directory / "migrate.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        present = target.exists() and target.stat().st_size > 0
        earlier = store.earlier_format(directory)
        if present:
            # Done already, perhaps by another process, perhaps cut short before its last step.
            # A new index that was not made from the old one says nothing of it: the old one
            # is then left as it is.
            if earlier == 1 and converted(directory):
                finish(directory)
            return target
        if earlier != 1:
            return None
        refuse_development(directory)
        for stale in directory.glob(store.INDEX + ".partial.*"):
            stale.unlink()
        # A reading of documents by an earlier version may be under way: it is given some time
        # to end. The old index is then locked against writing for the time of the copy, so
        # what such a version commits before the lock is copied, and it can commit nothing
        # after: no change of the old index is lost.
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
            try:
                old = sqlite3.connect((directory / store.EARLIER).as_uri() + "?mode=rw", uri=True,
                                      timeout=max(wait, 0.1))
            except sqlite3.Error as fault:
                raise ValueError(f"The earlier index cannot be opened for its conversion: {fault}") from None
            old.row_factory = sqlite3.Row
            new = None
            try:
                try:
                    old.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError:
                    raise ValueError("The earlier index is in use by another program: "
                                     "run the conversion again when it has finished") from None
                os.close(os.open(partial, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
                new = store.open_index(partial)
                # The partial file is thrown away after any failure, so it needs no journal of
                # its own; it is written to the disk in full before it takes its name.
                new.execute("PRAGMA journal_mode=OFF")
                new.execute("PRAGMA synchronous=OFF")
                new.executescript("BEGIN;\n" + store.SCHEMA + "\nCOMMIT;")
                new.execute("BEGIN")
                copy(old, new, progress)
                new.execute("COMMIT")
                new.close()
                new = None
                with open(partial, "rb") as written:
                    os.fsync(written.fileno())
                os.replace(partial, target)
                folder = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(folder)
                finally:
                    os.close(folder)
            except BaseException:
                if new is not None:
                    new.close()
                partial.unlink(missing_ok=True)
                old.rollback()
                old.close()
                raise
            # The last step, still under the lock of the old index: its searches join the
            # journal and it is marked. Cut here, it is completed at a later start (`finish`).
            try:
                asked = [(row["used"] // 1_000_000_000, row["query"])
                         for row in old.execute("SELECT query,used FROM history ORDER BY used")]
                if history.absorb(directory, asked):
                    old.execute(f"PRAGMA user_version={RETIRED}")
                    old.commit()
            except sqlite3.Error:
                pass
            finally:
                old.rollback()
                old.close()
        progress("The index is converted; the earlier one is kept until `shelffind upgrade --discard-old`")
        return target


def discard_old(directory):
    """Delete the earlier index once the new one stands for it. Returns the bytes freed."""
    directory = Path(directory).absolute()
    if not directory.is_dir():
        raise ValueError("The new index does not exist yet: nothing is deleted")
    with (directory / "migrate.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target = directory / store.INDEX
        if not target.exists() or target.stat().st_size == 0:
            raise ValueError("The new index does not exist yet: nothing is deleted")
        earlier = store.earlier_format(directory)
        if earlier == 1:
            # Not marked yet: either the conversion was cut before its last step, which is
            # done now, or the new index was never made from this one.
            if not converted(directory):
                raise ValueError("The earlier index was not converted into the new one: it is kept. "
                                 "Move it away yourself if it is no longer wanted")
            if not finish(directory):
                raise ValueError("The searches of the earlier index could not be saved: it is kept")
        freed = 0
        for name in (store.EARLIER, store.EARLIER + "-wal", store.EARLIER + "-shm"):
            path = directory / name
            if path.exists():
                freed += path.stat().st_size
                path.unlink()
        return freed
