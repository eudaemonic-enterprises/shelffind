# Copyright 2026 Eudaemonic Enterprises Inc.
"""Inventory first. Commit extracted text per document and OCR per page.

Between the inventory and the reading, the files are compared with the documents already known:
a file that holds what a known document holds joins it and is not read again. A comparison is
a measure of the whole file, its MD5 and SHA-256 (see `store`): the storage of a folder gives
the first without any reading when the folder has an rclone twin, and a full reading gives
both. Each is made once for a file as it is; `files.sum` keeps the result.
"""
import datetime
import hashlib
import json
import math
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from . import extract, fuzzy, link, store

ASKED = 400             # files in one question to the storage of a folder
CHANGED = "File changed during indexing; run index again"
FILE = "f.id,f.rel,f.size,f.mtime,f.format"


class AnotherDocument(Exception):
    """The file through which a document was to be read turned out to hold other content."""


def check_stamp(path, row, same=None):
    """Raise when the file is no longer what the inventory saw. Returns what tells the file
    itself apart from another put in its place, to be given back as `same` at a later check."""
    stat = path.stat()
    itself = (stat.st_dev, stat.st_ino)
    if path.is_symlink() or (stat.st_size, stat.st_mtime_ns) != (row["size"], row["mtime"]) \
            or (same is not None and same != itself):
        raise ValueError(CHANGED)
    return itself


def whole_checksums(path, row, same=None):
    """The MD5 and the SHA-256 of a file read in full, which stayed as the inventory saw it,
    and itself, while it was read."""
    same = check_stamp(path, row, same)
    found = hashlib.md5(), hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1 << 20):
            for one in found:
                one.update(block)
    check_stamp(path, row, same)
    return [f"md5:{found[0].hexdigest()}", f"sha256:{found[1].hexdigest()}"]


def storage(arguments, listing, remote, seconds):
    """What rclone prints about the files named in `listing`, or None when it gave no answer."""
    try:
        done = subprocess.run(["rclone", *arguments, "--retries", "1", "--files-from-raw", str(listing), remote],
                              capture_output=True, text=True, timeout=seconds)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def listed(remote, listing, wanted):
    """The files of `wanted` that the storage holds with the size and the instant the inventory
    saw, as address -> (size, instant); None when the storage gives no answer."""
    told = storage(["lsjson", "-R", "--files-only", "--no-mimetype"], listing, remote, 900)
    try:
        entries = json.loads(told) if told is not None else None
    except ValueError:
        entries = None
    if not isinstance(entries, list):
        return None
    agreeing = {}
    for entry in entries:
        row = wanted.get(entry.get("Path")) if isinstance(entry, dict) else None
        if row is None or entry.get("Size") != row["size"]:
            continue
        try:                                    # an instant with its offset, whatever the time zone it is written in
            moment = datetime.datetime.fromisoformat(str(entry.get("ModTime")))
        except ValueError:
            continue
        if moment.tzinfo is not None and math.floor(moment.timestamp()) == row["mtime"] // 1_000_000_000:
            agreeing[entry["Path"]] = (entry["Size"], moment.timestamp())
    return agreeing


def stored_checksums(remote, rows):
    """The MD5 that the storage of a folder gives for some of its files, as address -> key,
    without any of them being read; None when the storage gives no answer at all. A file is
    answered for only when the storage holds it with the size and the instant the inventory
    saw, before the checksum is asked and after it was given: a twin that lags behind the
    folder, runs ahead of it, or changes meanwhile says nothing of the file. The storage is
    believed beyond that: a twin that held other bytes under the same size and the same second
    would not be noticed."""
    wanted = {row["rel"]: row for row in rows if "\n" not in row["rel"] and "\r" not in row["rel"]}
    if not wanted:
        return {}
    with tempfile.TemporaryDirectory(prefix="shelffind-") as work:
        listing = Path(work) / "files"
        listing.write_text("".join(rel + "\n" for rel in wanted), encoding="utf-8")
        before = listed(remote, listing, wanted)
        if before is None:
            return None
        if not before:
            return {}
        listing.write_text("".join(rel + "\n" for rel in before), encoding="utf-8")
        told = storage(["hashsum", "md5"], listing, remote, 1800)
        after = listed(remote, listing, wanted) if told is not None else None
    if told is None or after is None:
        return None
    found = {}
    for line in told.splitlines():
        word, _, rel = line.partition("  ")
        word = word.strip().lower()
        if rel in before and after.get(rel) == before[rel] and len(word) == 32 \
                and all(char in "0123456789abcdef" for char in word):
            found[rel] = f"md5:{word}"
    return found


def joined(db, file, measured, local=True):
    """Keep the checksums `measured` of the whole of a file, computed by a full reading
    (`local`) or given by its storage, and let the file join the document that carries one of
    them. Returns True when something was learnt. The caller holds the transaction.

    A reading outranks the storage, which never replaces what a reading found. What a source
    said of this very file, and that the measure contradicts, was said of other bytes: it is
    dropped, and the sources are asked about the file again. A document speaks for the file it
    was read from, or, when it was never read, for its only file: such a file keeps its
    document, and every statement to the contrary leaves it. Any other file whose measure
    contradicts its document leaves the document. And a file that sits, on the word of a
    source, in a document read from another file which was not measured yet keeps its measure
    to itself, unless another document is known by it."""
    row = db.execute("SELECT doc,sum,read FROM files WHERE id=?", (file,)).fetchone()
    if row is None:
        return False
    known, new = store.sums(row["sum"]), store.sums(" ".join(measured))
    kept = {**known, **new} if local else {**new, **known}
    if kept == known:
        return False
    mine = [kept[kind] for kind in sorted(kept)]
    db.execute("UPDATE files SET sum=? WHERE id=?", (" ".join(mine), file))
    doc = row["doc"]
    refuted = [record["id"] for record in db.execute("SELECT id,keys FROM records WHERE file=?", (file,))
               if store.against(record["keys"].split(), mine)]
    if refuted:
        db.executemany("DELETE FROM records WHERE id=?", [(number,) for number in refuted])
        db.execute("UPDATE files SET asked='' WHERE id=?", (file,))
        store.unback(db, doc)
        store.retitle(db, doc)
        store.release(db, doc)
        doc = db.execute("SELECT doc FROM files WHERE id=?", (file,)).fetchone()["doc"]
    read = store.has_pages(db, doc)
    alone = db.execute("SELECT count(*) FROM files WHERE doc=?", (doc,)).fetchone()[0] == 1
    measured_there = {found["key"].split("@")[0] for found in
                      db.execute("SELECT key FROM doc_keys WHERE doc=? AND basis='content'", (doc,))}
    if store.against(mine, store.keys_of(db, doc)):
        if row["read"] or (alone and not read):
            store.part(db, doc, file, mine)
        else:
            doc = store.set_apart(db, file)
    elif read and not row["read"] and not measured_there & set(mine):
        fmt = db.execute("SELECT format FROM docs WHERE id=?", (doc,)).fetchone()["format"]
        elsewhere = db.execute(f"SELECT 1 FROM doc_keys WHERE key IN ({','.join('?' * len(mine))}) AND doc!=? LIMIT 1",
                               (*(f"{key}@{fmt}" for key in mine), doc)).fetchone()
        if not elsewhere:
            return True
        doc = store.set_apart(db, file)
    for key in mine:
        doc = store.claim(db, doc, key, "content")
    store.part_strangers(db, doc)
    return True


def uncompared(db, root, only=None, storage_only=False):
    """The files of a folder that may be a known document of their size and format: a document
    that carries checksums of which the file has none of the same kind, so that the two were
    never compared. With `storage_only`, those among them that an MD5 would settle."""
    has = lambda kind: f"EXISTS (SELECT 1 FROM doc_keys k WHERE k.doc=d.id AND k.key LIKE '{kind}:%')"
    lacks = lambda kind: f"instr(coalesce(f.sum,''),'{kind}:')=0"
    known = has("md5") if storage_only else "EXISTS (SELECT 1 FROM doc_keys k WHERE k.doc=d.id)"
    rows = db.execute(
        f"SELECT {FILE} FROM files f WHERE f.root=? AND {lacks('md5') if storage_only else '1'} AND EXISTS ("
        f"SELECT 1 FROM docs d WHERE d.size=f.size AND d.format=f.format AND d.id!=f.doc AND {known} "
        f"AND ({lacks('md5')} OR NOT {has('md5')}) AND ({lacks('sha256')} OR NOT {has('sha256')})) "
        "ORDER BY f.id", (root["id"],)).fetchall()
    return [row for row in rows if only is None or row["rel"] in only]


def unmeasured(db, root, only=None, kind="md5"):
    """The files a document was read from, of which no checksum of `kind` is known yet: the
    document is recognised by it when the file is moved, and its copies when they come."""
    rows = db.execute(f"SELECT {FILE} FROM files f WHERE f.root=? AND f.read=1 AND instr(coalesce(f.sum,''),?)=0 "
                      "ORDER BY f.id", (root["id"], kind + ":")).fetchall()
    return [row for row in rows if only is None or row["rel"] in only]


def ask_storage(db, root, rows, progress):
    """Keep what the storage of a folder says of the files `rows`, and let each join the
    document that carries its checksum. No file is read. Returns the number of files of which
    something was learnt, or None when the storage gave no answer."""
    learnt = 0
    for start in range(0, len(rows), ASKED):
        batch = rows[start:start + ASKED]
        if len(rows) > ASKED:
            progress(f"{root['name']}: the storage is asked about {min(start + ASKED, len(rows)):,} of {len(rows):,} files")
        told = stored_checksums(root["remote"], batch)
        if told is None:
            progress(f"The storage of {root['name']} gives no answer ({root['remote']}): "
                     "its files will be compared when it does")
            return None
        for row in batch:
            given = told.get(row["rel"])
            if not given:
                continue
            try:                                # the file itself is still what the inventory saw
                check_stamp(Path(root["path"]) / row["rel"], row)
            except (OSError, ValueError):
                continue
            with db:
                learnt += joined(db, row["id"], [given], local=False)
    return learnt


def read_in_full(db, root, rows, progress, failed):
    """Measure the files `rows` by a full reading, and let each join the document that carries
    its checksums. Returns (files of which something was learnt, files that could not be read)."""
    learnt = failures = 0
    for row in rows:
        if row["id"] in failed:
            continue
        try:
            measured = whole_checksums(Path(root["path"]) / row["rel"], row)
        except ValueError:
            progress(f"Changed while it was compared: {row['rel']}; run index again")
        except OSError as fault:
            progress(f"Could not compare {row['rel']}: {fault}")
        else:
            with db:
                learnt += joined(db, row["id"], measured)
            continue
        failed.add(row["id"])
        failures += 1
    return learnt, failures


def reconsider(db, root, only=None):
    """A file that sits in a document read from another file keeps its measure to itself (see
    `joined`). When another document comes to be known by that very measure, the file is that
    document: it goes there. No reading is needed. Returns the number of files that moved."""
    moved = 0
    rows = db.execute(
        "SELECT f.id,f.rel,f.doc,f.sum,f.format FROM files f WHERE f.root=? AND f.sum IS NOT NULL AND f.read=0 "
        "AND EXISTS (SELECT 1 FROM pages p WHERE p.doc=f.doc) ORDER BY f.id", (root["id"],)).fetchall()
    for row in rows:
        if only is not None and row["rel"] not in only:
            continue
        mine = row["sum"].split()
        names = [f"{key}@{row['format']}" for key in mine]
        marks = ",".join("?" * len(names))
        if db.execute(f"SELECT 1 FROM doc_keys WHERE key IN ({marks}) AND doc=? LIMIT 1", (*names, row["doc"])).fetchone():
            continue                            # its document is known by the same measure
        if db.execute(f"SELECT 1 FROM doc_keys WHERE key IN ({marks}) AND doc!=? LIMIT 1", (*names, row["doc"])).fetchone():
            with db:
                doc = store.set_apart(db, row["id"])
                for key in mine:
                    doc = store.claim(db, doc, key, "content")
                store.part_strangers(db, doc)
            moved += 1
    return moved


def settle(db, roots, chosen, progress, reads=True, failed=None):
    """Compare the files of the folders with the documents already known, as far as it has not
    been done. Where a folder has an rclone twin, its storage is asked first, which costs no
    reading. Then, when `reads` allows, the files that the storage could not settle, and those
    of the folders without twin, are read in full, as are, in a folder without twin, the files
    that documents were read from before checksums were kept. What one folder teaches may
    settle a file of another: the folders are gone through again while something is learnt.
    `chosen` limits the work, per folder, to some addresses. Returns the number of files that
    could not be read; `failed` collects them, so that one run says a failure once."""
    failures = 0
    failed = set() if failed is None else failed
    silent, asked = set(), set()
    for _ in range(3):
        learnt = 0
        for root in roots:
            only = chosen[root["id"]]
            learnt += reconsider(db, root, only)
            if root.get("remote") and root["id"] not in silent:
                wanted = {row["id"]: row for row in uncompared(db, root, only, storage_only=True) + unmeasured(db, root, only)}
                rows = [row for number, row in wanted.items() if number not in asked]
                asked.update(row["id"] for row in rows)
                told = ask_storage(db, root, rows, progress) if rows else 0
                if told is None:
                    silent.add(root["id"])
                learnt += told or 0
            if reads:
                rows = uncompared(db, root, only)
                if not root.get("remote"):
                    rows = list({row["id"]: row for row in rows + unmeasured(db, root, only, "sha256")}.values())
                found, missed = read_in_full(db, root, rows, progress, failed)
                learnt += found
                failures += missed
        if not learnt:
            break
    return failures


def forget(db, file):
    """A file was replaced while it was read: what was measured of it and said of it was of
    the file that is gone. It starts again as a document of its own."""
    db.execute("UPDATE records SET file=NULL WHERE file=?", (file,))
    store.set_apart(db, file)
    db.execute("UPDATE files SET sum=NULL,asked='',read=0 WHERE id=?", (file,))


def update_text(db, root, row, ocr, languages):
    """Read one document through one of its files. Returns False when the file turned out to be
    a document that needs no reading, in which case nothing was extracted."""
    path = Path(root["path"]) / row["rel"]
    doc = row["doc"]
    file = db.execute("SELECT id,read,sum FROM files WHERE root=? AND rel=? AND doc=?", (root["id"], row["rel"], doc)).fetchone()
    if file is None:
        raise AnotherDocument(row["rel"])
    same = check_stamp(path, row)
    if not root.get("remote") and "sha256:" not in (file["sum"] or ""):
        # No storage tells what this file holds: it is measured by a full reading, once, and
        # may then turn out to be a document already known.
        measured = whole_checksums(path, row, same)
        with db:
            joined(db, file["id"], measured)
        now = db.execute("SELECT f.read,f.sum,f.doc,d.state FROM files f JOIN docs d ON d.id=f.doc WHERE f.id=?",
                         (file["id"],)).fetchone()
        if now["doc"] != doc:
            if ocr or store.has_pages(db, now["doc"]) or now["state"] not in ("pending", "error"):
                return False
            doc = now["doc"]
        file = {"id": file["id"], "read": now["read"], "sum": now["sum"]}
    if ocr and not file["read"]:
        # Pages are recognised only through a file that holds what the document was read from:
        # the file it was read from, or one that a measure shows to be the same.
        content = {found["key"].split("@")[0] for found in
                   db.execute("SELECT key FROM doc_keys WHERE doc=? AND basis='content'", (doc,))}
        if not content & set((file["sum"] or "").split()):
            raise ValueError("The file its pages were read from is not at hand, and no measure shows this one to be the same")
    with extract.document(path, row["format"]) as (source, fmt, work):
        if ocr:
            blanks = db.execute("SELECT id,page FROM pages WHERE doc=? AND needs_ocr=1 "
                                "ORDER BY page", (doc,)).fetchall()
            for page in blanks:
                text = extract.recognize(source, fmt, work, page["page"], languages)
                check_stamp(path, row, same)
                with db:
                    db.execute("UPDATE pages SET ztext=?,needs_ocr=0 WHERE id=?", (store.deflate(text), page["id"]))
                    fuzzy.remember_more(db, (text,))
            with db:
                db.execute("UPDATE docs SET state='indexed',error=NULL WHERE id=?", (doc,))
            return True
        pages = list(extract.passages(source, fmt, work))
        if not pages:
            raise ValueError("No readable pages or EPUB chapters found")
        state = "needs-ocr" if any(p.needs_ocr for p in pages) else "indexed"
        check_stamp(path, row, same)
        with db:
            db.execute("DELETE FROM pages WHERE doc=?", (doc,))
            fuzzy.remember_text(db, (p.text for p in pages))
            db.executemany("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,?,?,?,?)",
                           [(doc, p.page, p.section, store.deflate(p.text), p.needs_ocr) for p in pages])
            db.execute("UPDATE docs SET state=?,error=NULL WHERE id=?", (state, doc))
            db.execute("UPDATE files SET read=0 WHERE doc=?", (doc,))
            db.execute("UPDATE files SET read=1 WHERE id=?", (file["id"],))
    return True


def build(db, directory, selected, *, names_only=False, ocr=False, languages="eng",
          retry=False, limit=None, progress=print, paths=(), on_document=None, relink=False):
    """Refresh the inventory, compare the files with the documents already known, ask the
    sources which files they know, then read the waiting documents. `selected` is the list of
    folders, or a function that gives it once no other build can change them.
    `progress(text)` receives the messages; `on_document(seen, total, name)` is called before
    each waiting document is taken up; `relink` asks the sources about every file again. With
    `names_only` no file is opened: the files are compared only as far as the storage of a
    folder tells their checksums. Returns the number of documents on which a reading was tried,
    which `limit` caps, a file found to need no reading not being one, and the number of
    problems."""
    done = failures = 0
    ready = []
    with store.index_lock(directory):
        if callable(selected):
            selected = selected()
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
        chosen = {root["id"]: {str(p.relative_to(root["path"])) for p in paths if p.is_relative_to(root["path"])}
                  if paths else None for root in ready}
        failed = set()
        failures += settle(db, ready, chosen, progress, not names_only, failed)
        if link.link(db, directory, ready, progress, everything=relink):
            # What the sources said may show more files to be known documents.
            failures += settle(db, ready, chosen, progress, not names_only, failed)
        link.cover(db, directory)
        if names_only:
            return done, failures
        # A pass takes a snapshot of its queue so failures cannot retry in a tight loop.
        states = ["needs-ocr"] if ocr else ["pending"]
        if retry:
            states.append("ocr-error" if ocr else "error")
        # One document may be read through any of its files, the one it was read from first:
        # the others are tried when one fails.
        candidates = {}
        for root in ready:
            query = ("SELECT f.id,f.doc,f.rel,f.size,f.mtime,f.format FROM files f JOIN docs d ON d.id=f.doc "
                     "WHERE f.root=? AND d.state IN (" + ",".join("?" * len(states)) + ")")
            params = [root["id"], *states]
            if paths:
                rels = sorted(chosen[root["id"]])
                if not rels:
                    continue
                query += " AND f.rel IN (" + ",".join("?" * len(rels)) + ")"
                params += rels
            for row in db.execute(query + " ORDER BY f.read DESC,f.id", params).fetchall():
                if row["id"] not in failed:     # said already: it could not be read for its comparison
                    candidates.setdefault(row["doc"], []).append((root, row))
        total = len(candidates)
        offline = set()
        for seen, (doc, copies) in enumerate(candidates.items()):
            if limit is not None and done >= limit:
                break
            if on_document:
                on_document(seen, total, copies[0][1]["rel"])
            fault, tried = None, 0
            for root, row in copies:
                if root["id"] in offline:
                    continue
                if not store.available(root):       # the other folders go on being read
                    progress(f"Disconnected: {root['path']}; saved completed work")
                    offline.add(root["id"])
                    failures += 1
                    continue
                try:
                    tried += update_text(db, root, row, ocr, languages)
                except sqlite3.Error:
                    raise
                except AnotherDocument:
                    continue                    # not this document after all: the next file is tried
                except Exception as exc:        # one unreadable file must not stop the others
                    if not store.available(root):
                        progress(f"Disconnected: {root['path']}; saved completed work")
                        offline.add(root["id"])
                        failures += 1
                        continue
                    if str(exc) == CHANGED:     # another file stands in its place: nothing known of it holds
                        with db:
                            forget(db, row["id"])
                    fault = fault or (row["rel"], exc)
                    continue
                # Done with this document when it was read, or needs no reading any more. A file
                # that turned out to be another document was read as that one: the next file of
                # this document is then tried.
                state = db.execute("SELECT state FROM docs WHERE id=?", (doc,)).fetchone()
                if state is None or state["state"] not in states:
                    fault = None
                    break
            if fault:
                with db:
                    db.execute("UPDATE docs SET state=?,error=? WHERE id=?",
                               ("ocr-error" if ocr else "error", str(fault[1])[:1200], doc))
                progress(f"Could not read {fault[0]}: {fault[1]}")
                failures += 1
                tried += 1
            if tried:
                done += tried
                if done // 100 > (done - tried) // 100:
                    progress(f"{done // 100 * 100:,} documents processed")
        if done:
            # What was just read may be what other files hold, and its checksum is asked where
            # a storage tells it: neither needs a reading.
            settle(db, [root for root in ready if root["id"] not in offline], chosen, progress, False, failed)
            link.cover(db, directory)
    return done, failures
