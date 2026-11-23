# Copyright 2026 Eudaemonic Enterprises Inc.
"""Inventory first. Commit extracted text per document and OCR per page."""
import hashlib
import subprocess
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from . import extract, fuzzy, store

READ_ERRORS = (OSError, ValueError, KeyError, RuntimeError, EOFError, subprocess.TimeoutExpired,
               BadZipFile, ParseError)
BLOCK = 64 * 1024


def check_stamp(path, row):
    stat = path.stat()
    if path.is_symlink() or (stat.st_size, stat.st_mtime_ns) != (row["size"], row["mtime"]):
        raise ValueError("File changed during indexing; run index again")


def fingerprint(path):
    """A key drawn from the content: the size, and the first and last 64 KiB (the whole of a
    file that is not larger). Two files with the same key are taken for the same document."""
    size = path.stat().st_size
    digest = hashlib.sha256(str(size).encode())
    with path.open("rb") as source:
        if size <= 2 * BLOCK:
            digest.update(source.read())
        else:
            digest.update(source.read(BLOCK))
            source.seek(size - BLOCK)
            digest.update(source.read(BLOCK))
    return f"fp:{size}:{digest.hexdigest()[:32]}"


def merge(db, keep, drop):
    """Two documents turned out to be one: the files and the keys of `drop` go to `keep`."""
    with db:
        db.execute("UPDATE files SET doc=? WHERE doc=?", (keep, drop))
        db.execute("UPDATE OR IGNORE doc_keys SET doc=? WHERE doc=?", (keep, drop))
        db.execute("DELETE FROM pages WHERE doc=?", (drop,))
        db.execute("DELETE FROM docs WHERE id=?", (drop,))


def settle(db, root, progress):
    """Check the documents that the inventory took back on the strength of their size alone,
    after a file was written anew elsewhere. Where the content is known and differs, the file
    becomes a document of its own again."""
    doubtful = db.execute(
        "SELECT k.key,k.doc,f.id AS file,f.rel,f.size,f.mtime,f.format FROM doc_keys k "
        "JOIN files f ON f.doc=k.doc WHERE k.basis='size' AND f.root=?", (root["id"],)).fetchall()
    for row in doubtful:
        path = Path(root["path"]) / row["rel"]
        try:
            check_stamp(path, row)
            found = fingerprint(path)
        except OSError:
            continue
        known = [r["key"] for r in db.execute(
            "SELECT key FROM doc_keys WHERE doc=? AND key LIKE 'fp:%'", (row["doc"],))]
        with db:
            if known and found not in known:
                progress(f"Another document of the same size: {row['rel']}")
                db.execute("DELETE FROM doc_keys WHERE key=?", (row["key"],))
                other = db.execute("SELECT doc FROM doc_keys WHERE key=?", (found,)).fetchone()
                doc = other["doc"] if other else store.new_document(db, found, row["size"], row["format"], "content")
                db.execute("UPDATE files SET doc=? WHERE id=?", (doc, row["file"]))
            else:
                db.execute("INSERT OR IGNORE INTO doc_keys(key,doc,basis) VALUES(?,?,'content')", (found, row["doc"]))
                db.execute("UPDATE doc_keys SET basis='stamp' WHERE key=?", (row["key"],))


def update_text(db, root, row, ocr, languages):
    """Read one document through one of its files. Returns False when its content turned out
    to be that of a document already read, in which case nothing was extracted."""
    path = Path(root["path"]) / row["rel"]
    doc = row["doc"]
    check_stamp(path, row)
    if not ocr:
        key = fingerprint(path)
        other = db.execute("SELECT doc FROM doc_keys WHERE key=?", (key,)).fetchone()
        if other and other["doc"] != doc:
            merge(db, other["doc"], doc)
            return False
        with db:
            db.execute("INSERT OR IGNORE INTO doc_keys(key,doc,basis) VALUES(?,?,'content')", (key, doc))
    with extract.document(path, row["format"]) as (source, fmt, work):
        if ocr:
            blanks = db.execute("SELECT id,page FROM pages WHERE doc=? AND needs_ocr=1 "
                                "ORDER BY page", (doc,)).fetchall()
            texts = []
            for page in blanks:
                text = extract.recognize(source, fmt, work, page["page"], languages)
                check_stamp(path, row)
                with db:
                    db.execute("UPDATE pages SET ztext=?,needs_ocr=0 WHERE id=?", (store.deflate(text), page["id"]))
                texts.append(text)
            with db:
                fuzzy.remember_text(db, texts)
                db.execute("UPDATE docs SET state='indexed',error=NULL WHERE id=?", (doc,))
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
    return True


def build(db, directory, selected, *, names_only=False, ocr=False, languages="eng",
          retry=False, limit=None, progress=print, paths=(), on_document=None):
    """Refresh the inventory, then read the waiting documents. `progress(text)` receives the
    messages; `on_document(done, total, name)` is called before each document is read."""
    done = failures = 0
    ready = []
    with store.index_lock(directory):
        for root in selected:
            try:
                count = store.scan(db, root, directory)
                ready.append(root)
                progress(f"{root['path']}: {count:,} file names")
            except (OSError, UnicodeError) as exc:
                with db:
                    db.execute("UPDATE roots SET problem=? WHERE id=?", (str(exc), root["id"]))
                progress(str(exc))
                failures += 1
        if names_only:
            return done, failures
        # A pass takes a snapshot of its queue so failures cannot retry in a tight loop.
        states = ["needs-ocr"] if ocr else ["pending"]
        if retry:
            states.append("ocr-error" if ocr else "error")
        queues = []
        for root in ready:
            settle(db, root, progress)
            query = ("SELECT f.doc,f.rel,f.size,f.mtime,d.format FROM files f JOIN docs d ON d.id=f.doc "
                     "WHERE f.root=? AND d.state IN (" + ",".join("?" * len(states)) + ")")
            params = [root["id"], *states]
            if paths:
                rels = [str(p.relative_to(root["path"])) for p in paths if p.is_relative_to(root["path"])]
                if not rels:
                    continue
                query += " AND f.rel IN (" + ",".join("?" * len(rels)) + ")"
                params += rels
            queues.append((root, db.execute(query + " ORDER BY f.id", params).fetchall()))
        waiting = {row["doc"] for _, queue in queues for row in queue}
        total = len(waiting) if limit is None else min(len(waiting), limit)
        handled = set()                 # a document with several copies is read once
        for root, queue in queues:
            for row in queue:
                if row["doc"] in handled:
                    continue
                if limit is not None and done >= limit:
                    return done, failures
                if not store.available(root):
                    progress(f"Disconnected: {root['path']}; saved completed work")
                    return done, failures + 1
                if on_document:
                    on_document(done, total, row["rel"])
                handled.add(row["doc"])
                try:
                    update_text(db, root, row, ocr, languages)
                except READ_ERRORS as exc:
                    if not store.available(root):
                        progress(f"Disconnected: {root['path']}; saved completed work")
                        return done, failures + 1
                    with db:
                        db.execute("UPDATE docs SET state=?,error=? WHERE id=?",
                                   ("ocr-error" if ocr else "error", str(exc)[:1200], row["doc"]))
                    progress(f"Could not read {row['rel']}: {exc}")
                    failures += 1
                done += 1
                if done % 100 == 0:
                    progress(f"{done:,} documents processed")
    return done, failures
