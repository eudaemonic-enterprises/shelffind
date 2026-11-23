# Copyright 2026 Eudaemonic Enterprises Inc.
"""Inventory first. Commit extracted text per file and OCR per page."""
import subprocess
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from . import extract, fuzzy, store

READ_ERRORS = (OSError, ValueError, KeyError, RuntimeError, EOFError, subprocess.TimeoutExpired,
               BadZipFile, ParseError)


def check_stamp(path, row):
    stat = path.stat()
    if path.is_symlink() or (stat.st_size, stat.st_mtime_ns) != (row["size"], row["mtime"]):
        raise ValueError("File changed during indexing; run index again")


def update_text(db, root, row, ocr, languages, known):
    path = Path(root["path"]) / row["rel"]
    check_stamp(path, row)
    with extract.document(path, row["format"]) as (source, fmt, work):
        if ocr:
            blanks = db.execute("SELECT id,page FROM pages WHERE file=? AND needs_ocr=1 "
                                "ORDER BY page", (row["id"],)).fetchall()
            for page in blanks:
                text = extract.recognize(source, fmt, work, page["page"], languages)
                check_stamp(path, row)
                with db:
                    fuzzy.remember_words(db, [text], known)
                    db.execute("UPDATE pages SET text=?,needs_ocr=0 WHERE id=?", (text, page["id"]))
            state = "indexed"
        else:
            pages = list(extract.passages(source, fmt, work))
            if not pages:
                raise ValueError("No readable pages or EPUB chapters found")
            state = "needs-ocr" if any(p.needs_ocr for p in pages) else "indexed"
            check_stamp(path, row)
            with db:
                db.execute("DELETE FROM pages WHERE file=?", (row["id"],))
                fuzzy.remember_words(db, (p.text for p in pages), known)
                db.executemany("INSERT INTO pages(file,page,section,text,needs_ocr) VALUES(?,?,?,?,?)",
                               [(row["id"], p.page, p.section, p.text, p.needs_ocr) for p in pages])
                db.execute("UPDATE files SET state=?,error=NULL WHERE id=?", (state, row["id"]))
            return
    with db:
        db.execute("UPDATE files SET state=?,error=NULL WHERE id=?", (state, row["id"]))


def build(db, directory, selected, *, names_only=False, ocr=False, languages="eng",
          retry=False, limit=None, progress=print, paths=(), on_document=None):
    """Refresh the inventory, then read the waiting documents. `progress(text)` receives the
    messages; `on_document(done, total, name)` is called before each document is read."""
    done = failures = 0
    known = set()
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
            query = "SELECT * FROM files WHERE root=? AND state IN (" + ",".join("?" * len(states)) + ")"
            params = [root["id"], *states]
            if paths:
                rels = [str(p.relative_to(root["path"])) for p in paths if p.is_relative_to(root["path"])]
                if not rels:
                    continue
                query += " AND rel IN (" + ",".join("?" * len(rels)) + ")"
                params += rels
            queues.append((root, db.execute(query + " ORDER BY id", params).fetchall()))
        total = sum(len(queue) for _, queue in queues)
        total = total if limit is None else min(total, limit)
        for root, queue in queues:
            for row in queue:
                if limit is not None and done >= limit:
                    return done, failures
                if not store.available(root):
                    progress(f"Disconnected: {root['path']}; saved completed work")
                    return done, failures + 1
                if on_document:
                    on_document(done, total, row["rel"])
                try:
                    update_text(db, root, row, ocr, languages, known)
                except READ_ERRORS as exc:
                    if not store.available(root):
                        progress(f"Disconnected: {root['path']}; saved completed work")
                        return done, failures + 1
                    with db:
                        db.execute("UPDATE files SET state=?,error=? WHERE id=?",
                                   ("ocr-error" if ocr else "error", str(exc)[:1200], row["id"]))
                    progress(f"Could not read {row['rel']}: {exc}")
                    failures += 1
                done += 1
                if done % 100 == 0:
                    progress(f"{done:,} documents processed")
    return done, failures
