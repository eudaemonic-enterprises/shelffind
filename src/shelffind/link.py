# Copyright 2026 Eudaemonic Enterprises Inc.
"""Which files of the folders the sources know.

After an inventory, every source that answers `identify` is asked about the files it has not
been asked about yet. What it says is kept as a record of the document: the true title and
authors, which then find the file, its checksums, by which two files are known to be the same
document, and whether the source holds its text, in which case ShelfFind does not read it.
"""
import json
import time
from pathlib import Path

from . import fuzzy, sources, store

BATCH = 500             # files in one question
AGAIN = 20 * 3600       # seconds after which the files a source did not know are asked about again
WAITING = ("pending", "error", "needs-ocr", "ocr-error")
# What a document whose text was left to a source becomes when that source is gone: what its
# own pages say of it.
RESTORED = ("CASE WHEN EXISTS (SELECT 1 FROM pages p WHERE p.doc=docs.id AND p.needs_ocr=1) THEN 'needs-ocr' "
            "WHEN EXISTS (SELECT 1 FROM pages p WHERE p.doc=docs.id) THEN 'indexed' ELSE 'pending' END")


def attach(db, file, name, record, trusted, known):
    """Keep what the source `name` says of the document of the file `file`. A checksum that
    another document of the same size and format carries already shows that the two are one.

    A checksum speaks of the bytes of this file. It goes to the document when the document was
    read from this very file, or was never read. When the document was read from another file,
    of which this one is only presumed to be a copy, the file gets a document of its own, which
    carries the checksum, and is marked for comparison: equal fingerprints will join the two
    again, and a checksum that contradicts the one the document carries keeps them apart."""
    row = db.execute("SELECT f.doc,f.name,f.read,d.size,d.format,d.state FROM files f JOIN docs d ON d.id=f.doc "
                     "WHERE f.id=?", (file,)).fetchone()
    if row is None:
        return
    doc = row["doc"]
    keys = list(record.get("keys") or ())
    carried = {r["key"] for r in db.execute("SELECT key FROM doc_keys WHERE doc=?", (doc,))}
    contradicted = any(key not in carried and any(other.startswith(key.split(":", 1)[0] + ":") for other in carried)
                       for key in keys)
    read_elsewhere = not row["read"] and (any(key.startswith("fp:") for key in carried) or db.execute(
        "SELECT 1 FROM pages WHERE doc=? LIMIT 1", (doc,)).fetchone() is not None)
    if contradicted or (read_elsewhere and any(key not in carried for key in keys)):
        apart = db.execute("INSERT INTO docs(size,format,state) VALUES(?,?,?)",
                           (row["size"], row["format"], "name-only" if row["state"] == "name-only" else "pending")).lastrowid
        db.execute("INSERT INTO doc_keys(key,doc,basis) VALUES(?,?,'apart until compared')",
                   (f"apart:{apart}:{store.digest(row['name'])}", apart))
        db.execute("UPDATE files SET doc=?,maybe=?,read=0 WHERE id=?", (apart, None if contradicted else 1, file))
        store.tend_orphans(db, drop=False)
        doc = apart
    for key in keys:
        owner = db.execute("SELECT doc FROM doc_keys WHERE key=?", (key,)).fetchone()
        if owner is None:
            db.execute("INSERT INTO doc_keys(key,doc,basis) VALUES(?,?,'source')", (key, doc))
        elif owner["doc"] != doc:
            doc = store.merge(db, owner["doc"], doc) or doc         # the same content under two dates
    title, byline = record.get("title") or "", record.get("byline") or ""
    year = "" if record.get("year") is None else str(record["year"])
    db.execute("INSERT OR REPLACE INTO records(doc,source,ref,title,byline,year,flags,basis,text,local) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)",
               (doc, name, record["id"], title, byline, year, json.dumps(record.get("flags") or []),
                record.get("basis") or "", int(record.get("text") is True), record.get("path")))
    if title:
        db.execute("UPDATE docs SET title=?,byline=?,year=? WHERE id=?", (title, byline, year, doc))
        store.describe(db, doc)
        fuzzy.remember_names(db, (title, byline), known)
    if trusted and record.get("text") is True:
        placeholders = ",".join("?" * len(WAITING))
        db.execute(f"UPDATE docs SET state='covered',error=NULL WHERE id=? AND state IN ({placeholders})",
                   (doc, *WAITING))


def link(db, directory, selected, progress=lambda text: None, everything=False):
    """Ask the sources about the files of the folders `selected` that they have not been asked
    about. Returns the number of files they recognised."""
    try:
        programs = sources.registered(directory)
    except (OSError, ValueError) as fault:
        progress(f"Source configuration: {fault}")
        return 0
    # A document whose text was left to a source is the reader's again once that source is no
    # longer registered or no longer relied upon. A source that merely fails to answer at this
    # moment keeps its documents: a passing fault must not start a reading of thousands.
    relied_upon = [name for name in programs if sources.policy(name)["text"] == "trust"]
    with db:
        db.execute(f"UPDATE docs SET state={RESTORED} WHERE state='covered' AND NOT EXISTS (SELECT 1 FROM records r "
                   f"WHERE r.doc=docs.id AND r.text=1 AND r.source IN ({','.join('?' * len(relied_upon))}))",
                   relied_upon)
    usable = {}
    for name, argv in programs.items():
        try:
            caps = sources.abilities(directory, name, argv)
        except (OSError, ValueError) as fault:
            progress(f"{name}: {fault}")
            continue
        wanted = sources.policy(name)
        if "identify" in caps["ops"] and not caps["network"] and wanted["identify"]:
            usable[name] = caps["text"] and wanted["text"] == "trust"
    recognised = 0
    known = set()
    for name, relied in usable.items():
        last = db.execute("SELECT value FROM meta WHERE key=?", (f"asked:{name}",)).fetchone()
        if everything or last is None or time.time() - float(last["value"]) > AGAIN:
            with db:
                db.execute("DELETE FROM records WHERE source=? AND ref=''", (name,))
                db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (f"asked:{name}", str(time.time())))
        for root in selected:
            # A file whose document is still in question is left for the day it is settled.
            waiting = db.execute(
                "SELECT f.id,f.doc,f.rel,f.size,f.mtime FROM files f WHERE f.root=? AND f.maybe IS NULL AND NOT EXISTS "
                "(SELECT 1 FROM records r WHERE r.doc=f.doc AND r.source=?) ORDER BY f.id", (root["id"], name)).fetchall()
            for start in range(0, len(waiting), BATCH):
                batch = waiting[start:start + BATCH]
                files = []
                for number, row in enumerate(batch):
                    sums = [r["key"] for r in db.execute(
                        "SELECT key FROM doc_keys WHERE doc=? AND (key LIKE 'sha256:%' OR key LIKE 'md5:%')",
                        (row["doc"],))]
                    files.append({"n": number, "path": str(Path(root["path"]) / row["rel"]), "size": row["size"],
                                  "mtime": row["mtime"] // 1_000_000_000, "keys": sums})
                try:
                    answer = sources.question(directory, name, "identify", files=files)
                except (OSError, ValueError) as fault:
                    progress(f"{name}: {fault}")
                    break
                with db:
                    answered = set()
                    for record in answer["records"]:
                        if 0 <= record["n"] < len(batch) and record["n"] not in answered:
                            answered.add(record["n"])
                            attach(db, batch[record["n"]]["id"], name, record, relied, known)
                    # The files the source does not know are remembered as asked.
                    db.executemany("INSERT OR IGNORE INTO records(doc,source,ref,text) "
                                   "SELECT doc,?,'',0 FROM files WHERE id=?",
                                   [(name, row["id"]) for number, row in enumerate(batch) if number not in answered])
                recognised += len(answered)
        if recognised:
            progress(f"{name}: {recognised:,} files recognised")
    return recognised
