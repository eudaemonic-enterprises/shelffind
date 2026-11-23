# Copyright 2026 Eudaemonic Enterprises Inc.
"""Inventory first. Commit extracted text per document and OCR per page."""
import hashlib
import sqlite3
from pathlib import Path

from . import extract, fuzzy, link, store

BLOCK = 64 * 1024
WHOLE = 4 << 20         # up to this size the whole file is its fingerprint; beyond, samples of it
SAMPLES = 8


def check_stamp(path, row):
    stat = path.stat()
    if path.is_symlink() or (stat.st_size, stat.st_mtime_ns) != (row["size"], row["mtime"]):
        raise ValueError("File changed during indexing; run index again")


def fingerprint(path, fmt):
    """A key drawn from the content of a file: all of it up to 4 MiB; for a larger file its
    size, its first and last 64 KiB and eight samples between. Two files of one format with
    the same key are taken for the same document. The format belongs to the key because a
    document is read by the reader of its format."""
    size = path.stat().st_size
    digest = hashlib.sha256(str(size).encode())
    with path.open("rb") as source:
        if size <= WHOLE:
            while block := source.read(1 << 20):
                digest.update(block)
        else:
            step = (size - 2 * BLOCK) // (SAMPLES + 1)
            for offset in (0, *(BLOCK + number * step for number in range(1, SAMPLES + 1)), size - BLOCK):
                source.seek(offset)
                digest.update(source.read(BLOCK))
    return f"fp:{size}:{digest.hexdigest()[:32]}:{fmt}"


def proven(path, row):
    """The fingerprint of a file that stayed as the inventory saw it while it was read."""
    check_stamp(path, row)
    key = fingerprint(path, row["format"])
    check_stamp(path, row)
    return key


def settle(db, root, progress):
    """Answer by reading what the inventory left open in one folder. A file that resembles a
    known document is fingerprinted, and joins the document that has this fingerprint, if there
    is one; otherwise it stays the new document it was given. A document read before
    fingerprints existed gets its own through the very file it was read from. Returns the
    number of files that could not be read."""
    failures = 0
    asked = db.execute("SELECT id,rel,size,mtime,format FROM files WHERE root=? AND maybe IS NOT NULL",
                       (root["id"],)).fetchall()
    for row in asked:
        try:
            found = proven(Path(root["path"]) / row["rel"], row)
        except ValueError:                  # changed since the inventory, which will see it again
            continue
        except OSError as fault:
            progress(f"Could not compare {row['rel']}: {fault}")
            failures += 1
            continue
        with db:
            owner = db.execute("SELECT doc FROM doc_keys WHERE key=?", (found,)).fetchone()
            now = db.execute("SELECT doc FROM files WHERE id=?", (row["id"],)).fetchone()
            db.execute("UPDATE files SET maybe=NULL,print=? WHERE id=?", (found, row["id"]))
            if owner and now and owner["doc"] != now["doc"]:
                store.merge(db, owner["doc"], now["doc"])
    # The file a document was read from, unchanged since, is the one proof of what its pages
    # hold: no other file may lend the document a fingerprint.
    bare = db.execute(
        "SELECT f.id,f.doc,f.rel,f.size,f.mtime,f.format FROM files f WHERE f.root=? AND f.read=1 AND f.maybe IS NULL "
        "AND EXISTS (SELECT 1 FROM pages p WHERE p.doc=f.doc) AND NOT EXISTS "
        "(SELECT 1 FROM doc_keys k WHERE k.doc=f.doc AND k.key LIKE 'fp:%') ORDER BY f.id", (root["id"],)).fetchall()
    for row in bare:
        if db.execute("SELECT 1 FROM doc_keys WHERE doc=? AND key LIKE 'fp:%'", (row["doc"],)).fetchone():
            continue                        # another of its files gave it a moment ago
        try:
            found = proven(Path(root["path"]) / row["rel"], row)
        except ValueError:
            continue
        except OSError as fault:
            progress(f"Could not fingerprint {row['rel']}: {fault}")
            failures += 1
            continue
        with db:
            owner = db.execute("SELECT doc FROM doc_keys WHERE key=?", (found,)).fetchone()
            if owner is None:
                db.execute("INSERT INTO doc_keys(key,doc,basis) VALUES(?,?,'content')", (found, row["doc"]))
            elif owner["doc"] != row["doc"]:
                store.merge(db, owner["doc"], row["doc"])
    return failures


def update_text(db, root, row, ocr, languages):
    """Read one document through one of its files. Returns False when its content turned out
    to be that of a document already read, in which case nothing was extracted."""
    path = Path(root["path"]) / row["rel"]
    doc = row["doc"]
    key = None
    if ocr:
        check_stamp(path, row)
    else:
        key = proven(path, row)
        with db:
            db.execute("UPDATE files SET print=? WHERE root=? AND rel=?", (key, root["id"], row["rel"]))
        owner = db.execute("SELECT doc FROM doc_keys WHERE key=?", (key,)).fetchone()
        if owner and owner["doc"] != doc:
            with db:
                kept = store.merge(db, owner["doc"], doc)
            if kept is not None:
                return False
    with extract.document(path, row["format"]) as (source, fmt, work):
        if ocr:
            blanks = db.execute("SELECT id,page FROM pages WHERE doc=? AND needs_ocr=1 "
                                "ORDER BY page", (doc,)).fetchall()
            for page in blanks:
                text = extract.recognize(source, fmt, work, page["page"], languages)
                check_stamp(path, row)
                with db:
                    db.execute("UPDATE pages SET ztext=?,needs_ocr=0 WHERE id=?", (store.deflate(text), page["id"]))
                    fuzzy.remember_more(db, (text,))
            with db:
                db.execute("UPDATE docs SET state='indexed',error=NULL WHERE id=?", (doc,))
                db.execute("UPDATE files SET read=1 WHERE root=? AND rel=?", (root["id"], row["rel"]))
            return True
        pages = list(extract.passages(source, fmt, work))
        if not pages:
            raise ValueError("No readable pages or EPUB chapters found")
        state = "needs-ocr" if any(p.needs_ocr for p in pages) else "indexed"
        check_stamp(path, row)
        with db:
            db.execute("DELETE FROM pages WHERE doc=?", (doc,))
            fuzzy.remember_text(db, (p.text for p in pages))
            db.executemany("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,?,?,?,?)",
                           [(doc, p.page, p.section, store.deflate(p.text), p.needs_ocr) for p in pages])
            db.execute("UPDATE docs SET state=?,error=NULL WHERE id=?", (state, doc))
            # The fingerprint is kept once the reading has succeeded, and not before.
            db.execute("INSERT OR IGNORE INTO doc_keys(key,doc,basis) VALUES(?,?,'content')", (key, doc))
            db.execute("UPDATE files SET read=1 WHERE root=? AND rel=?", (root["id"], row["rel"]))
    return True


def build(db, directory, selected, *, names_only=False, ocr=False, languages="eng",
          retry=False, limit=None, progress=print, paths=(), on_document=None, relink=False):
    """Refresh the inventory, settle what it left open, ask the sources which files they know,
    then read the waiting documents. `progress(text)` receives the messages;
    `on_document(done, total, name)` is called before each document is read; `relink` asks the
    sources about every file again."""
    done = failures = 0
    ready = []
    with store.index_lock(directory):
        for root in selected:
            if not store.current(db, root):     # removed, or replaced, since the command began
                progress(f"No longer a registered folder: {root['path']}")
                failures += 1
                continue
            try:
                count = store.scan(db, root, directory)
                ready.append(root)
                progress(f"{root['path']}: {count:,} file names")
            except (OSError, UnicodeError) as exc:
                with db:
                    db.execute("UPDATE roots SET problem=? WHERE id=?", (str(exc), root["id"]))
                progress(str(exc))
                failures += 1
        for root in ready:
            failures += settle(db, root, progress)
        link.link(db, directory, ready, progress, everything=relink)
        if names_only:
            return done, failures
        # A pass takes a snapshot of its queue so failures cannot retry in a tight loop.
        states = ["needs-ocr"] if ocr else ["pending"]
        if retry:
            states.append("ocr-error" if ocr else "error")
        # One document may be read through any of its files: the others are tried when one fails.
        candidates = {}
        for root in ready:
            query = ("SELECT f.doc,f.rel,f.size,f.mtime,f.format FROM files f JOIN docs d ON d.id=f.doc "
                     "WHERE f.root=? AND f.maybe IS NULL AND d.state IN (" + ",".join("?" * len(states)) + ")")
            params = [root["id"], *states]
            if paths:
                rels = [str(p.relative_to(root["path"])) for p in paths if p.is_relative_to(root["path"])]
                if not rels:
                    continue
                query += " AND f.rel IN (" + ",".join("?" * len(rels)) + ")"
                params += rels
            for row in db.execute(query + " ORDER BY f.id", params).fetchall():
                candidates.setdefault(row["doc"], []).append((root, row))
        total = len(candidates) if limit is None else min(len(candidates), limit)
        for doc, copies in candidates.items():
            if limit is not None and done >= limit:
                return done, failures
            if on_document:
                on_document(done, total, copies[0][1]["rel"])
            fault = None
            for root, row in copies:
                if not store.available(root):
                    progress(f"Disconnected: {root['path']}; saved completed work")
                    return done, failures + 1
                still = db.execute("SELECT doc FROM files WHERE root=? AND rel=?", (root["id"], row["rel"])).fetchone()
                if still is None or still["doc"] != doc:
                    continue                    # this file went to another document meanwhile
                try:
                    update_text(db, root, row, ocr, languages)
                    fault = None
                    break
                except sqlite3.Error:
                    raise
                except Exception as exc:        # one unreadable file must not stop the others
                    if not store.available(root):
                        progress(f"Disconnected: {root['path']}; saved completed work")
                        return done, failures + 1
                    fault = (row["rel"], exc)
            if fault:
                with db:
                    db.execute("UPDATE docs SET state=?,error=? WHERE id=?",
                               ("ocr-error" if ocr else "error", str(fault[1])[:1200], doc))
                progress(f"Could not read {fault[0]}: {fault[1]}")
                failures += 1
            done += 1
            if done % 100 == 0:
                progress(f"{done:,} documents processed")
    return done, failures
