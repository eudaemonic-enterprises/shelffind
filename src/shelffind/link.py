# Copyright 2026 Eudaemonic Enterprises Inc.
"""Which files of the folders the sources know.

After an inventory, every source that answers `identify` is asked about the files it has not
been asked about yet. What it says of a file is kept as a record, with that very file: the
true title and authors, which then find the file, the checksums of the work, by which two files
are known to be the same document, and whether the source holds its text, in which case
ShelfFind does not read it.

What a source says is its word, and is taken as such (see `store`): it joins a file that was
never read to a document, never two documents that were both read, and never against what was
measured of a file.
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


def checksums(record):
    """The checksums a record gives, when they are well formed and one of each kind at most:
    two checksums of one kind in one record describe no file."""
    given = [key for key in record.get("keys") or () if isinstance(key, str) and SUM.match(key)]
    return given if len({key.split(":", 1)[0] for key in given}) == len(given) else []


def withdraw(db, file, name):
    """Drop what the source `name` said of the file `file`, with the checksums that only its
    word had given to the document."""
    said = db.execute("SELECT doc FROM records WHERE file=? AND source=?", (file, name)).fetchone()
    if said is not None:
        db.execute("DELETE FROM records WHERE file=? AND source=?", (file, name))
        store.unback(db, said["doc"])
        store.retitle(db, said["doc"])


def attach(db, file, name, record, known):
    """Keep what the source `name` says of the file `file`: that it is the work `record`.
    Returns False when the record is not kept.

    The checksums of the record speak of the bytes of this file. A file whose own checksum,
    measured by a reading or given by its storage, differs from the record's is not that work,
    whatever its address suggests, and neither is a file of which another source says
    otherwise: a measure will tell which of the two is right. A document that carries another
    checksum of the same kind, on the strength of its other files, does not describe this file,
    which then gets a document of its own; unless the document was read from this very file, in
    which case it stays the file's, and what was said of the others leaves with them. The file
    then joins the document that the checksums of the record name, if there is one."""
    row = db.execute("SELECT doc,sum,read FROM files WHERE id=?", (file,)).fetchone()
    if row is None:
        return False
    keys = checksums(record)
    if store.against(keys, (row["sum"] or "").split()):
        return False
    for other in db.execute("SELECT keys FROM records WHERE file=? AND source!=?", (file, name)):
        if store.against(keys, other["keys"].split()):
            return False
    withdraw(db, file, name)                    # what the same source said of the file before is replaced
    doc = row["doc"]
    if store.against(keys, store.keys_of(db, doc)):
        if row["read"]:
            store.part(db, doc, file, keys)
        else:
            doc = store.set_apart(db, file)
    for key in keys:
        doc = store.claim(db, doc, key, "source")
    doc = db.execute("SELECT doc FROM files WHERE id=?", (file,)).fetchone()["doc"]
    title, byline = record.get("title") or "", record.get("byline") or ""
    year = "" if record.get("year") is None else str(record["year"])
    db.execute("INSERT INTO records(doc,file,source,ref,keys,title,byline,year,flags,basis,text,local) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (doc, file, name, record["id"], " ".join(keys), title, byline, year, json.dumps(record.get("flags") or []),
                record.get("basis") or "", int(record.get("text") is True), record.get("path")))
    store.retitle(db, doc)
    store.part_strangers(db, doc)
    if title:
        fuzzy.remember_names(db, (title, byline), known)
    return True


def usable(directory, progress=lambda text: None):
    """The sources of the settings; those that tell which files they know, as name -> whether
    the text they hold is relied upon; and the trusted ones that say, at this moment, that they
    hold no text."""
    programs = sources.registered(directory)
    found, without = {}, set()
    for name, argv in programs.items():
        wanted = sources.policy(name)
        try:
            caps = sources.abilities(directory, name, argv)
        except (OSError, ValueError) as fault:
            progress(f"{name}: {fault}")
            continue
        if not caps["text"] and wanted["text"] == "trust":
            without.add(name)
        if "identify" in caps["ops"] and not caps["network"] and wanted["identify"]:
            found[name] = caps["text"] and wanted["text"] == "trust"
    return programs, found, without


def cover(db, directory):
    """Leave to the sources the text they hold, and take back what they no longer answer for.
    The text of a document is left to a source that holds it and is relied upon. It is the
    reader's again once that source is no longer registered or relied upon, or says itself
    that it holds no text. A source that merely fails to answer at this moment keeps its
    documents: a passing fault must not start a reading of thousands."""
    try:
        programs, found, without = usable(directory)
    except (OSError, ValueError):
        return
    relied_upon = [name for name in programs if sources.policy(name)["text"] == "trust" and name not in without]
    holding = [name for name, relied in found.items() if relied]
    with db:
        db.execute(f"UPDATE docs SET state={RESTORED} WHERE state='covered' AND NOT EXISTS (SELECT 1 FROM records r "
                   f"WHERE r.doc=docs.id AND r.text=1 AND r.source IN ({','.join('?' * len(relied_upon))}))",
                   relied_upon)
        db.execute(f"UPDATE docs SET state='covered',error=NULL WHERE state IN ({','.join('?' * len(WAITING))}) "
                   f"AND EXISTS (SELECT 1 FROM records r WHERE r.doc=docs.id AND r.text=1 AND r.source IN "
                   f"({','.join('?' * len(holding))}))", (*WAITING, *holding))


def link(db, directory, selected, progress=lambda text: None, everything=False):
    """Ask the sources about the files of the folders `selected` that they have not been asked
    about, or about all the files of these folders with `everything`. A file that a source no
    longer recognises loses what that source said of it. Returns the number of files
    recognised."""
    try:
        _, found, _ = usable(directory, progress)
    except (OSError, ValueError) as fault:
        progress(f"Source configuration: {fault}")
        return 0
    recognised = 0
    known = set()
    places = [root["id"] for root in selected]
    among = f"root IN ({','.join('?' * len(places))})"
    for name in found:
        mark = f" {name} "
        clear = f"UPDATE files SET asked=trim(replace(' '||asked||' ',?,' ')) WHERE {ASKED}>0 AND {among}"
        last = db.execute("SELECT value FROM meta WHERE key=?", (f"asked:{name}",)).fetchone()
        with db:
            if everything:
                db.execute(clear, (mark, mark, *places))
            elif last is None or time.time() - float(last["value"]) > AGAIN:
                # The files it did not know are asked about again, since it may have learnt them.
                db.execute(clear + " AND NOT EXISTS (SELECT 1 FROM records r WHERE r.file=files.id AND r.source=?)",
                           (mark, mark, *places, name))
                db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (f"asked:{name}", str(time.time())))
        spoken, answering = 0, True
        for root in selected:
            if not answering:
                break
            waiting = db.execute(f"SELECT id,rel,size,mtime,sum FROM files WHERE root=? AND {ASKED}=0 ORDER BY id",
                                 (root["id"], mark)).fetchall()
            for start in range(0, len(waiting), BATCH):
                batch = waiting[start:start + BATCH]
                # With each file goes what was measured of that very file, and nothing of the
                # other files of its document.
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
                            spoken += attach(db, batch[record["n"]]["id"], name, record, known)
                    for number, row in enumerate(batch):
                        if number not in seen:      # what it said of the file before, it says no more
                            withdraw(db, row["id"], name)
                    db.executemany(f"UPDATE files SET asked=trim(asked||' '||?) WHERE id=? AND {ASKED}=0",
                                   [(name, row["id"], mark) for row in batch])
        if spoken:
            progress(f"{name}: {spoken:,} files recognised")
        recognised += spoken
    cover(db, directory)
    return recognised
