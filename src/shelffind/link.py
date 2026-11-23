# Copyright 2026 Eudaemonic Enterprises Inc.
"""Which files of the folders the sources know.

After an inventory, every source that answers `identify` is asked about the files it has not
been asked about yet. What it says of a file is kept as a record, with that very file: the
true title and authors, which then find the file, the checksums of the work, by which two files
are known to be the same document, and whether the source holds its text, in which case
ShelfFind does not read it.

What a source says is its word, and is taken as such (see `store`): it places a file that was
never measured, and nothing else. A word that a measure refutes is not kept.
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


def say(db, file, name, record, known=None):
    """Keep what the source `name` says now of the file `file`: that it is the work `record`,
    or nothing (None). Returns True when a record is kept.

    What the source said of the file before is replaced in every case, and so is what it had
    said of a file of the same document that has gone since. The checksums of a record speak
    of the bytes of this file: a record is not kept when a measure refutes it, that of the
    file itself, or that of the document known by those checksums, which then has another size
    or another checksum of the same kind. The file is then placed by what is said of it, when
    it was never measured (see `store.place`)."""
    row = db.execute("SELECT doc,size,format,sum FROM files WHERE id=?", (file,)).fetchone()
    if row is None:
        return False
    before = db.execute("SELECT keys FROM records WHERE file=? AND source=?", (file, name)).fetchone()
    changed = db.execute("DELETE FROM records WHERE (file=? OR (doc=? AND file IS NULL)) AND source=?",
                         (file, row["doc"], name)).rowcount
    kept = False
    if record is not None:
        keys, mine = checksums(record), (row["sum"] or "").split()
        if not store.against(keys, mine) and not store.refuted(db, row["size"], row["format"], sorted({*keys, *mine}))[0]:
            title, byline = record.get("title") or "", record.get("byline") or ""
            year = "" if record.get("year") is None else str(record["year"])
            number = db.execute(
                "INSERT INTO records(doc,file,source,ref,keys,title,byline,year,flags,basis,text,local) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["doc"], file, name, record["id"], " ".join(keys), title, byline, year,
                 json.dumps(record.get("flags") or []), record.get("basis") or "", int(record.get("text") is True),
                 record.get("path"))).lastrowid
            db.executemany("INSERT OR IGNORE INTO record_keys(key,record) VALUES(?,?)", [(key, number) for key in keys])
            kept = True
            if title and known is not None:
                fuzzy.remember_names(db, (title, byline), known)
    if kept and before is not None and before["keys"] == " ".join(keys):
        store.retitle(db, row["doc"])               # the same checksums as before: the file stays where it is
    elif changed or kept:
        doc = store.replace(db, file)
        if kept and doc != row["doc"]:              # the same holds in the document the file is in now
            db.execute("DELETE FROM records WHERE doc=? AND file IS NULL AND source=?", (doc, name))
            store.retitle(db, doc)
    return kept


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
    for name in found:
        mark = f" {name} "
        clear = f"UPDATE files SET asked=trim(replace(' '||asked||' ',?,' ')) WHERE {ASKED}>0 AND root=?"
        spoken, answering = 0, True
        for root in selected:
            if not answering:
                break
            # Each folder has its own clock, so that a folder asked alone, again and again,
            # does not postpone the questions about the others.
            clock = f"asked:{name}:{root['id']}"
            last = db.execute("SELECT value FROM meta WHERE key=?", (clock,)).fetchone()
            with db:
                if everything:
                    db.execute(clear, (mark, mark, root["id"]))
                elif last is None or time.time() - float(last["value"]) > AGAIN:
                    # The files it did not know are asked about again, since it may have learnt them.
                    db.execute(clear + " AND NOT EXISTS (SELECT 1 FROM records r WHERE r.file=files.id AND r.source=?)",
                               (mark, mark, root["id"], name))
                    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (clock, str(time.time())))
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
                    told = {}
                    for record in answer["records"]:
                        if 0 <= record["n"] < len(batch):
                            told.setdefault(record["n"], record)
                    for number, row in enumerate(batch):    # what it said of a file before, it says anew or no more
                        spoken += say(db, row["id"], name, told.get(number), known)
                    db.executemany(f"UPDATE files SET asked=trim(asked||' '||?) WHERE id=? AND {ASKED}=0",
                                   [(name, row["id"], mark) for row in batch])
        if spoken:
            progress(f"{name}: {spoken:,} files recognised")
        recognised += spoken
    cover(db, directory)
    return recognised
