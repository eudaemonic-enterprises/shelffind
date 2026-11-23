# Copyright 2026 Eudaemonic Enterprises Inc.
"""Convert an index of the earlier format into the present one.

The new index is written beside the old one, in a file that takes its final name only at the
very end. The order of the steps leaves no moment at which both indexes could be used:

1. no reading of documents by an earlier version is under way (its lock is held), and the old
   index is locked against writing;
2. the new index is written in full under another name;
3. the searches the old index remembers join the journal;
4. the old index is marked as converted, which an earlier version of the program refuses, and
   its lock is released;
5. the new index takes its name.

Cut before step 4, nothing has changed and the conversion is done again. Cut between steps 4
and 5, the old index is frozen and without a successor: it is converted again, which gives the
same result. Of the old index only that mark ever changes; it is deleted on request alone, and
only while it is still exactly what the new index was made from.
"""
import fcntl
import hashlib
import os
import sqlite3
import time
from pathlib import Path

from . import __version__, fuzzy, history, store

RETIRED = store.RETIRED         # the format number an old index receives once it has been converted
ORIGIN = "converted_from"       # in the new index: the print of the old index it was made from
# The better state wins when two copies of one document were read apart by the earlier version.
BETTER = {"indexed": 0, "needs-ocr": 1, "ocr-error": 1, "error": 3, "pending": 4, "name-only": 5}


def refuse_development(directory):
    if __version__.endswith(".dev") and directory.resolve() == store.state_directory().resolve():
        raise ValueError("A development version does not convert the index in use; "
                         "name another folder with --data-dir")


def print_of(old):
    """A print of everything an old index holds: its folders, its files, the text of its pages
    and the searches it remembers. It is the same as long as nothing is added to the old index,
    taken from it or changed in it."""
    told = hashlib.sha256()
    for question in ("SELECT id,path FROM roots ORDER BY id",
                     "SELECT id,root,rel,size,mtime,state,error FROM files ORDER BY id",
                     "SELECT id,file,page,needs_ocr,text FROM pages ORDER BY id",
                     "SELECT query,used FROM history ORDER BY query"):
        for row in old.execute(question):
            told.update(repr(tuple(row)).encode("utf-8", "surrogatepass"))
    return told.hexdigest()


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

    # The files: each joins the document that its name, size, date and format designate, as the
    # inventory would decide, unless the earlier version read it and one of the files of that
    # document, and found different text in them.
    total = old.execute("SELECT count(*) FROM files").fetchone()[0]
    documents, members, document_of, donor = {}, {}, {}, {}
    known = set()
    for count, row in enumerate(old.execute("SELECT * FROM files ORDER BY id"), 1):
        place = folders[row["root"]][0]
        base = store.stamp_key(row["size"], row["mtime"], row["format"], place, row["rel"])
        doc = documents.get((base, row["name"]))
        if doc is not None and row["id"] in read and any(
                different(old, other, row["id"]) for other in members[doc] if other in read):
            doc = None
        if doc is None:
            doc = new.execute("INSERT INTO docs(size,format,state,error) VALUES(?,?,?,?)",
                              (row["size"], row["format"], row["state"], row["error"])).lastrowid
            shared = new.execute("SELECT 1 FROM doc_keys WHERE key=?", (base,)).fetchone()
            key, basis = (f"{base}:{store.digest(row['name'])}:{doc}", "stamp shared") if shared else (base, "stamp")
            new.execute("INSERT INTO doc_keys(key,doc,basis) VALUES(?,?,?)", (key, doc, basis))
            documents.setdefault((base, row["name"]), doc)
            members[doc] = []
            donor[doc] = (worth(row), row["id"])
        elif worth(row) < donor[doc][0]:
            new.execute("UPDATE docs SET state=?,error=? WHERE id=?", (row["state"], row["error"], doc))
            donor[doc] = (worth(row), row["id"])
        members[doc].append(row["id"])
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
    # reading went furthest, and takes from the other copies the pages only they recognised.
    several = {doc for doc, files in members.items() if sum(file in read for file in files) > 1}
    spare, texts, current, count = {}, [], None, 0
    for row in old.execute("SELECT file,page,section,text,needs_ocr FROM pages ORDER BY file,id"):
        doc = document_of.get(row["file"])
        if doc is None:
            continue
        if donor[doc][1] != row["file"]:
            if doc in several and not row["needs_ocr"] and row["page"] is not None:
                spare.setdefault((doc, row["page"]), row["text"])
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
    for (doc, page), text in spare.items():
        blank = new.execute("SELECT id FROM pages WHERE doc=? AND page=? AND needs_ocr=1", (doc, page)).fetchone()
        if blank:
            new.execute("UPDATE pages SET ztext=?,needs_ocr=0 WHERE id=?", (store.deflate(text), blank["id"]))
            fuzzy.remember_more(new, (text,))
    new.execute("UPDATE docs SET state='indexed',error=NULL WHERE state IN ('needs-ocr','ocr-error') AND EXISTS "
                "(SELECT 1 FROM pages p WHERE p.doc=docs.id) AND NOT EXISTS "
                "(SELECT 1 FROM pages p WHERE p.doc=docs.id AND p.needs_ocr=1)")
    # The file a document's pages were read from, for the fingerprint it will be given.
    new.executemany("UPDATE files SET read=1 WHERE id=?", [(file,) for _, file in donor.values() if file in read])
    new.execute("INSERT INTO meta(key,value) VALUES(?,?)", (ORIGIN, print_of(old)))


def origin(directory):
    """The print of the old index that the new index of `directory` was made from, or None."""
    try:
        new = sqlite3.connect((directory / store.INDEX).as_uri() + "?mode=ro", uri=True, timeout=30)
        try:
            row = new.execute("SELECT value FROM meta WHERE key=?", (ORIGIN,)).fetchone()
            return row[0] if row else None
        finally:
            new.close()
    except sqlite3.Error:
        return None


def upgrade(directory, progress=lambda text: None, wait=20.0):
    """Convert the earlier index of `directory`. Returns the path of the new index, or None when
    there is nothing to convert. A new index that is there already is never touched, and says
    nothing of an old index beside it."""
    directory = Path(directory).absolute()
    target = directory / store.INDEX
    if not directory.is_dir():
        return None
    with (directory / "migrate.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists() and target.stat().st_size > 0:
            return target
        earlier = store.earlier_format(directory)
        if earlier not in (1, RETIRED):
            if earlier:
                raise ValueError(f"Unsupported index schema {earlier}")
            return None
        refuse_development(directory)
        for stale in directory.glob(store.INDEX + ".partial.*"):
            stale.unlink()
        # Step 1. A reading of documents by an earlier version writes for as long as it runs:
        # it is given some time to end, and the conversion does not begin while it lasts.
        with (directory / "index.lock").open("a") as building:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(building, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError("A reading of documents is under way: let it end, or stop it, "
                                         "then run the conversion again") from None
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
                # Step 2. The partial file is thrown away after any failure, so it needs no
                # journal of its own; it is written to the disk in full.
                os.close(os.open(partial, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
                new = store.open_index(partial)
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
                # Step 3.
                asked = [(row["used"] // 1_000_000_000, row["query"])
                         for row in old.execute("SELECT query,used FROM history ORDER BY used")]
                if not history.absorb(directory, asked):
                    raise ValueError("The searches of the earlier index could not be saved: nothing was converted")
                # Step 4.
                old.execute(f"PRAGMA user_version={RETIRED}")
                old.commit()
                # Step 5.
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
                raise
            finally:
                old.rollback()
                old.close()
        progress("The index is converted; the earlier one is kept until `shelffind upgrade --discard-old`")
        return target


def discard_old(directory):
    """Delete the earlier index, provided it is still exactly what the new index was made from.
    Returns the bytes freed."""
    directory = Path(directory).absolute()
    if not directory.is_dir():
        raise ValueError("The new index does not exist yet: nothing is deleted")
    with (directory / "migrate.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target = directory / store.INDEX
        if not target.exists() or target.stat().st_size == 0:
            raise ValueError("The new index does not exist yet: nothing is deleted")
        if store.earlier_format(directory):
            made_from = origin(directory)
            try:
                old = sqlite3.connect((directory / store.EARLIER).as_uri() + "?mode=ro", uri=True, timeout=30)
                try:
                    retired = old.execute("PRAGMA user_version").fetchone()[0] == RETIRED
                    same = made_from is not None and print_of(old) == made_from
                finally:
                    old.close()
            except sqlite3.Error as fault:
                raise ValueError(f"The earlier index cannot be read ({fault}): it is kept") from None
            if not (retired and same):
                raise ValueError("The earlier index holds something the new one was not made from: it is kept. "
                                 "Move the file away yourself once you are sure it is no longer wanted")
        freed = 0
        for name in (store.EARLIER, store.EARLIER + "-wal", store.EARLIER + "-shm"):
            path = directory / name
            if path.exists():
                freed += path.stat().st_size
                path.unlink()
        return freed
