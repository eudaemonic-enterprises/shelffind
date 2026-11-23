# Copyright 2026 Eudaemonic Enterprises Inc.
"""Convert an index of the earlier format into the present one.

The new index is written beside the old one, in a file that takes its final name only at the
very end. The order of the steps leaves no moment at which both indexes could be used:

1. no reading of documents by an earlier version is under way (its lock is held), and the old
   index is taken exclusively: this is refused for as long as any other program has it open,
   and no program can open it until the conversion has ended;
2. the new index is written in full under another name;
3. the searches the old index remembers join the journal;
4. the old index is marked as converted, which an earlier version of the program refuses, and
   its lock is released;
5. the new index takes its name.

Cut before step 4, nothing has changed and the conversion is done again. Cut between steps 4
and 5, the old index is frozen and without a successor: it is converted again, which gives the
same result. Of the old index only that mark ever changes; it is deleted on request alone, taken
exclusively in the same way, only while it is still exactly what the new index was made from,
and after its searches have been put in the journal once more.
"""
import contextlib
import fcntl
import gc
import hashlib
import os
import sqlite3
import time
from pathlib import Path

from . import __version__, fuzzy, history, store

RETIRED = store.RETIRED         # the format number an old index receives once it has been converted
ORIGIN = "converted_from"       # in the new index: the print of the old index it was made from


def refuse_development(directory):
    if __version__.endswith(".dev") and directory.resolve() == store.state_directory().resolve():
        raise ValueError("A development version does not convert the index in use; "
                         "name another folder with --data-dir")


@contextlib.contextmanager
def rows(old, question):
    """The rows of a question to the old index. The question is closed with the block, also when
    the reading is cut short: left open, it would keep the old index held after its connection
    is closed."""
    cursor = old.execute(question)
    try:
        yield cursor
    finally:
        cursor.close()


def print_of(old):
    """A print of everything an old index holds: its folders, its files, every page with its
    place and its text, and the searches it remembers. It is the same as long as nothing is
    added to the old index, taken from it or changed in it."""
    told = hashlib.sha256()
    for question in ("SELECT * FROM roots ORDER BY id", "SELECT * FROM files ORDER BY id",
                     "SELECT * FROM pages ORDER BY id", "SELECT * FROM history ORDER BY query,used"):
        told.update(question.encode())
        with rows(old, question) as found:
            for row in found:
                told.update(repr(tuple(row)).encode("utf-8", "surrogatepass"))
    return told.hexdigest()


def exclusively(old):
    """Take an old index for this program alone, until its connection is closed. An index that
    another program has open, were it only a search panel left in a terminal, cannot be taken:
    what that program wrote afterwards would be lost."""
    gc.collect()        # a connection this very process dropped without closing it would hold the index too
    try:
        old.execute("PRAGMA locking_mode=EXCLUSIVE")
        old.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        raise ValueError("The earlier index is open in another program: close the other ShelfFind "
                         "windows, or let them finish, then run this again") from None


def searches(old):
    """The searches an old index remembers, as (time in seconds, query), the earliest first."""
    return [(row["used"] // 1_000_000_000, row["query"])
            for row in old.execute("SELECT query,used FROM history ORDER BY used").fetchall()]


def copy(old, new, progress):
    """Fill the new index from the old one, inside one transaction of the new index. Every file
    of the old index becomes a document of its own with the pages the earlier version read from
    it, exactly as that version held them. Files that hold the same content become one
    document later, when their fingerprints or their checksums show it."""
    folders = {}
    for row in old.execute("SELECT * FROM roots ORDER BY id").fetchall():
        name = store.free_name(new, Path(row["path"]).name or "root")
        new.execute("INSERT INTO roots(id,name,path,mount,scanned,problem) VALUES(?,?,?,?,?,?)",
                    (row["id"], name, store.from_home(row["path"]), store.from_home(row["mount"]),
                     row["scanned"], row["problem"]))
        folders[row["id"]] = Path(row["path"]).name
    total = old.execute("SELECT count(*) FROM files").fetchone()[0]
    known = set()
    with rows(old, "SELECT * FROM files ORDER BY id") as files:
        for count, row in enumerate(files, 1):
            new.execute("INSERT INTO docs(id,size,format,state,error) VALUES(?,?,?,?,?)",
                        (row["id"], row["size"], row["format"], row["state"], row["error"]))
            new.execute("INSERT INTO files(id,root,rel,name,folded,format,size,mtime,doc) VALUES(?,?,?,?,?,?,?,?,?)",
                        (row["id"], row["root"], row["rel"], row["name"], row["folded"], row["format"],
                         row["size"], row["mtime"], row["id"]))
            folder = str(Path(folders[row["root"]]) / Path(row["rel"]).parent)
            terms = (store.terms_for_name(row["name"]), store.terms_for_name(folder))
            fuzzy.remember_names(new, terms, known)
            new.execute("INSERT INTO names(rowid,name,folder,title,byline) VALUES(?,?,?,'','')", (row["id"], *terms))
            if count % 5000 == 0:
                progress(f"Converting the index: {count:,} of {total:,} files")
    texts, current, count = [], None, 0
    with rows(old, "SELECT file,page,section,text,needs_ocr FROM pages ORDER BY file,id") as pages:
        for row in pages:
            if row["file"] != current:
                fuzzy.remember_text(new, texts)
                current, texts = row["file"], []
            new.execute("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,?,?,?,?)",
                        (row["file"], row["page"], row["section"], store.deflate(row["text"]), row["needs_ocr"]))
            texts.append(row["text"])
            count += 1
            if count % 20000 == 0:
                progress(f"Converting the index: {count:,} pages of text")
    fuzzy.remember_text(new, texts)
    # The file a document's pages were read from, for the fingerprint it will be given.
    new.execute("UPDATE files SET read=1 WHERE EXISTS (SELECT 1 FROM pages p WHERE p.doc=files.doc)")
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
                # A program that has the old index open keeps it for as long as it runs: waiting
                # for it at length would only delay the message that asks to close it.
                old = sqlite3.connect((directory / store.EARLIER).as_uri() + "?mode=rw", uri=True,
                                      timeout=max(min(wait, 3.0), 0.1))
            except sqlite3.Error as fault:
                raise ValueError(f"The earlier index cannot be opened for its conversion: {fault}") from None
            old.row_factory = sqlite3.Row
            new = None
            try:
                exclusively(old)
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
                if not history.absorb(directory, searches(old)):
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


def discard_old(directory, wait=5.0):
    """Delete the earlier index, provided it is still exactly what the new index was made from
    and no other program has it open. Returns the bytes freed."""
    directory = Path(directory).absolute()
    if not directory.is_dir():
        raise ValueError("The new index does not exist yet: nothing is deleted")
    with (directory / "migrate.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target = directory / store.INDEX
        if not target.exists() or target.stat().st_size == 0:
            raise ValueError("The new index does not exist yet: nothing is deleted")
        if not (directory / store.EARLIER).is_file():
            return 0
        made_from = origin(directory)
        # The old index is this program's alone from its comparison to its deletion: nothing can
        # be added to it between the two, and no program that has it open is left writing into
        # a file that no longer has a name.
        try:
            old = sqlite3.connect((directory / store.EARLIER).as_uri() + "?mode=rw", uri=True, timeout=wait)
        except sqlite3.Error as fault:
            raise ValueError(f"The earlier index cannot be read ({fault}): it is kept") from None
        old.row_factory = sqlite3.Row
        try:
            exclusively(old)
            try:
                retired = old.execute("PRAGMA user_version").fetchone()[0] == RETIRED
                same = retired and made_from is not None and print_of(old) == made_from
                asked = searches(old) if same else []
            except sqlite3.Error as fault:
                raise ValueError(f"The earlier index cannot be read ({fault}): it is kept") from None
            if not same:
                raise ValueError("The earlier index holds something the new one was not made from: it is kept. "
                                 "Move the file away yourself once you are sure it is no longer wanted")
            # The journal may have been lost since the conversion, with a folder restored in
            # part: the searches go into it once more, which changes nothing when they are there.
            if not history.absorb(directory, asked):
                raise ValueError("The searches of the earlier index could not be saved: it is kept")
            freed = 0
            for name in (store.EARLIER, store.EARLIER + "-wal", store.EARLIER + "-shm"):
                path = directory / name
                if path.exists():
                    freed += path.stat().st_size
                    path.unlink()
            return freed
        finally:
            old.rollback()
            old.close()
