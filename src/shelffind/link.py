# Copyright 2026 Eudaemonic Enterprises Inc.
"""Which files of the folders the sources know.

After an inventory, every source that answers `identify` is asked about the files it has not
been asked about yet. What it says is kept as a record of the document: the true title and
authors, which then find the file, its checksums, by which two files are known to be the same
document, and whether the source holds its text, in which case ShelfFind does not read it.

A source is asked about each file, also about one that joined a document it knows already: its
answer may show that the file is another work. What a source says is its word, and is taken as
such: it joins files into one document, but never against what ShelfFind itself measured of a
file (see `store`).
"""
import json
import re
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
SUM = re.compile(r"(?:md5:[0-9a-f]{32}|sha256:[0-9a-f]{64})\Z")
ASKED = "instr(' '||asked||' ',?)"      # with ' NAME ' for parameter: whether the source NAME was asked about a file


def retitle(db, doc):
    """Give a document the title and the authors of what the sources say of it now."""
    told = db.execute("SELECT title,byline,year FROM records WHERE doc=? AND coalesce(title,'')!='' "
                      "ORDER BY source LIMIT 1", (doc,)).fetchone()
    db.execute("UPDATE docs SET title=?,byline=?,year=? WHERE id=?",
               (told["title"] if told else None, told["byline"] if told else None, told["year"] if told else None, doc))
    store.describe(db, doc)


def attach(db, file, name, record, known):
    """Keep what the source `name` says of the file `file`: that it is the work `record`.
    Returns False when ShelfFind's own measure of the file says otherwise, and nothing is kept.

    The checksums of the record speak of the bytes of this file. A document that carries them
    already is the same document, and the file joins it. A file whose own checksum, taken from
    its bytes or given by its storage, differs from the record's is not that work, whatever its
    address suggests. And a document that carries another checksum of the same kind, on the
    strength of its other files, does not describe this file, which then gets a document of its
    own; unless the document was read from this very file, in which case it stays the file's
    and the earlier word is withdrawn. Nor is the word about a file taken for a word about a
    document read from another file, unless the two are known to be the same byte for byte."""
    row = db.execute("SELECT doc,sum,read FROM files WHERE id=?", (file,)).fetchone()
    if row is None:
        return False
    keys = [key for key in record.get("keys") or () if isinstance(key, str) and SUM.match(key)]
    if store.against(keys, (row["sum"] or "").split()):
        return False
    doc = row["doc"]
    theirs = store.keys_of(db, doc)
    if store.against(keys, theirs):
        if row["read"]:                         # the document is this file's: what was said of it before goes
            store.disown(db, doc, file)
        else:
            doc = store.set_apart(db, file)
    elif any(key not in theirs for key in keys) and not store.same_whole(db, file, doc) \
            and db.execute("SELECT 1 FROM pages WHERE doc=? LIMIT 1", (doc,)).fetchone():
        # The document was read from another file, with which this one is only known to share
        # the samples of a fingerprint: what is said of this file is not said of that one. The
        # two are one again as soon as the same is said, or measured, of the other.
        doc = store.set_apart(db, file)
    for key in keys:
        doc = store.claim(db, doc, key, "source")           # the same content at two addresses is one document
    title, byline = record.get("title") or "", record.get("byline") or ""
    year = "" if record.get("year") is None else str(record["year"])
    db.execute("INSERT OR REPLACE INTO records(doc,source,ref,title,byline,year,flags,basis,text,local) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)",
               (doc, name, record["id"], title, byline, year, json.dumps(record.get("flags") or []),
                record.get("basis") or "", int(record.get("text") is True), record.get("path")))
    retitle(db, doc)
    if title:
        fuzzy.remember_names(db, (title, byline), known)
    return True


def link(db, directory, selected, progress=lambda text: None, everything=False):
    """Ask the sources about the files of the folders `selected` that they have not been asked
    about, or about all of them with `everything`. Returns the number of files they recognised."""
    try:
        programs = sources.registered(directory)
    except (OSError, ValueError) as fault:
        progress(f"Source configuration: {fault}")
        return 0
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
    for name in usable:
        mark = f" {name} "
        last = db.execute("SELECT value FROM meta WHERE key=?", (f"asked:{name}",)).fetchone()
        if everything or last is None or time.time() - float(last["value"]) > AGAIN:
            # The files it did not know are asked about again, since it may have learnt them;
            # with `everything`, all the files are.
            with db:
                db.execute(f"UPDATE files SET asked=trim(replace(' '||asked||' ',?,' ')) WHERE {ASKED}>0"
                           + ("" if everything else " AND NOT EXISTS (SELECT 1 FROM records r WHERE r.doc=files.doc "
                                                    "AND r.source=?)"), (mark, mark) if everything else (mark, mark, name))
                db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (f"asked:{name}", str(time.time())))
        spoken, answering = [], True
        for root in selected:
            if not answering:
                break
            waiting = db.execute(f"SELECT id,rel,size,mtime,sum FROM files WHERE root=? AND {ASKED}=0 ORDER BY id",
                                 (root["id"], mark)).fetchall()
            for start in range(0, len(waiting), BATCH):
                batch = waiting[start:start + BATCH]
                # With each file goes what ShelfFind measured of that very file, and nothing of
                # the other files of its document.
                files = [{"n": number, "path": str(Path(root["path"]) / row["rel"]), "size": row["size"],
                          "mtime": row["mtime"] // 1_000_000_000, "keys": (row["sum"] or "").split()}
                         for number, row in enumerate(batch)]
                try:
                    answer = sources.question(directory, name, "identify", files=files)
                except (OSError, ValueError) as fault:
                    progress(f"{name}: {fault}")
                    answering = False
                    break
                with db:
                    seen = set()
                    for record in answer["records"]:
                        if 0 <= record["n"] < len(batch) and record["n"] not in seen:
                            seen.add(record["n"])
                            if attach(db, batch[record["n"]]["id"], name, record, known):
                                spoken.append(batch[record["n"]]["id"])
                    db.executemany(f"UPDATE files SET asked=trim(asked||' '||?) WHERE id=? AND {ASKED}=0",
                                   [(name, row["id"], mark) for row in batch])
        if everything and answering:
            # Asked about every file again, the source no longer knows some works it knew: what
            # it said of a document is withdrawn when it was asked about every file of the
            # document and recognised none.
            with db:
                held = {row["doc"] for file in spoken for row in db.execute("SELECT doc FROM files WHERE id=?", (file,))}
                stale = [row["doc"] for row in db.execute(
                    f"SELECT r.doc FROM records r WHERE r.source=? AND EXISTS (SELECT 1 FROM files f WHERE f.doc=r.doc) "
                    f"AND NOT EXISTS (SELECT 1 FROM files f WHERE f.doc=r.doc AND {ASKED}=0)", (name, mark))]
                for doc in stale:
                    if doc not in held:
                        db.execute("DELETE FROM records WHERE doc=? AND source=?", (doc, name))
                        retitle(db, doc)
        if spoken:
            progress(f"{name}: {len(spoken):,} files recognised")
        recognised += len(spoken)
    # The text of a document is left to a source that holds it and is relied upon; and it is the
    # reader's again once that source is no longer registered or no longer relied upon. A source
    # that merely fails to answer at this moment keeps its documents: a passing fault must not
    # start a reading of thousands.
    relied_upon = [name for name in programs if sources.policy(name)["text"] == "trust"]
    holding = [name for name, relied in usable.items() if relied]
    with db:
        db.execute(f"UPDATE docs SET state={RESTORED} WHERE state='covered' AND NOT EXISTS (SELECT 1 FROM records r "
                   f"WHERE r.doc=docs.id AND r.text=1 AND r.source IN ({','.join('?' * len(relied_upon))}))",
                   relied_upon)
        db.execute(f"UPDATE docs SET state='covered',error=NULL WHERE state IN ({','.join('?' * len(WAITING))}) "
                   f"AND EXISTS (SELECT 1 FROM records r WHERE r.doc=docs.id AND r.text=1 AND r.source IN "
                   f"({','.join('?' * len(holding))}))", (*WAITING, *holding))
    return recognised
