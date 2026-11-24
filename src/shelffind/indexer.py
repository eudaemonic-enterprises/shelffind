# Copyright 2026 Eudaemonic Enterprises Inc.
"""Inventory first. Commit extracted text per document and OCR per page.

Between the inventory and the reading, the files are compared with the documents already known:
a file that holds what a known document holds joins it and is not read again. A comparison is
a measure of the whole file, its MD5 and SHA-256 (see `store`): the storage of a folder gives
the first without any reading when the folder has an rclone twin, and a full reading gives
both. Each is made once for a file as it is; `files.sum` keeps the result.

A document is read from a copy of its file made on this machine in one pass, during which the
checksums are computed: the pages kept are those of the very bytes the checksums describe. The
copies are made in a folder of the data folder of ShelfFind, and nowhere else.
"""
import contextlib
import datetime
import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from . import extract, fuzzy, link, store

ASKED = 400             # files in one question to the storage of a folder
CHANGED = "File changed during indexing; run index again"
FILE = "f.id,f.rel,f.size,f.mtime,f.format"


class Unreadable(Exception):
    """A file could not be taken as the inventory saw it: nothing was learnt of what it holds."""


class Unplaced(Exception):
    """The copy of a file could not be written on this machine: nothing is wrong with the file."""


def check_stamp(path, row, same=None):
    """Raise when the file is no longer what the inventory saw. Returns what tells the file
    itself apart from another put in its place, to be given back as `same` at a later check."""
    stat = path.stat()
    itself = (stat.st_dev, stat.st_ino)
    if path.is_symlink() or (stat.st_size, stat.st_mtime_ns) != (row["size"], row["mtime"]) \
            or (same is not None and same != itself):
        raise ValueError(CHANGED)
    return itself


def whole_checksums(path, row, copy=None):
    """The MD5 and the SHA-256 of a file read in full, once, through one opening of it. The
    file opened is the one the inventory saw, and it stayed so while it was read: its address,
    its size, its date and the file itself are checked before and after, on the address and on
    what was opened, and so is the time of the last change of the file, which a writer that
    puts the date back cannot put back. With `copy`, the bytes read are written there: the
    checksums are then those of that copy, whatever happens to the file afterwards. Unplaced
    is raised when the copy cannot be written."""
    same = check_stamp(path, row)
    found = hashlib.md5(), hashlib.sha256()
    try:
        kept = copy.open("wb") if copy is not None else contextlib.nullcontext()
    except OSError as fault:
        raise Unplaced(str(fault)) from None
    with path.open("rb") as source, kept as target:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != same or (opened.st_size, opened.st_mtime_ns) != (row["size"], row["mtime"]):
            raise ValueError(CHANGED)
        taken = 0
        while block := source.read(1 << 20):
            taken += len(block)
            for one in found:
                one.update(block)
            if target is not None:
                try:
                    target.write(block)
                except OSError as fault:
                    raise Unplaced(str(fault)) from None
        closed = os.fstat(source.fileno())
        if taken != row["size"] or (closed.st_size, closed.st_mtime_ns, closed.st_ctime_ns) != \
                (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
            raise ValueError(CHANGED)
    check_stamp(path, row, same)
    return [f"md5:{found[0].hexdigest()}", f"sha256:{found[1].hexdigest()}"]


def private_copy(path, row, hold, known=None):
    """A copy of a file on this machine, with its checksums and with what tells the file itself
    apart from another: (copy, checksums, itself). `known` is the MD5 that was measured of the
    file before, by its storage as a rule. A reading that contradicts it is made a second
    time, since a file that comes through a network may arrive damaged: the second reading
    stands when it agrees with what was known or with the first, and the file is not read when
    the two readings differ from each other and from it."""
    copy = hold / path.name
    same = check_stamp(path, row)
    measured = whole_checksums(path, row, copy)
    if known and known != measured[0]:
        again = whole_checksums(path, row, copy)
        if again != measured and again[0] != known:
            raise OSError("Two readings of the file gave two different contents")
        measured = again
    check_stamp(path, row, same)
    return copy, measured, same


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
    """Keep the checksums `measured` of the whole of a file and let the file join the document
    known by them: see `store.measure`. The caller holds the transaction."""
    return store.measure(db, file, measured, local)


def waiting(db, root, only=None, reading=False, thorough=False, absent=frozenset()):
    """The files of a folder that a measure would place or prove, and that have none of the
    kind wanted.

    For the storage of the folder, which tells an MD5 without any reading: the files of the
    size and format of another document, which may be that document; and, with `thorough`,
    those of which a source gave a checksum, which is then proved or refuted.

    For a reading in full (`reading`), which costs one and gives both checksums: the files
    whose pages were read by an earlier version and never tied to bytes, which only a reading
    does; and the files of which a source gave checksums that nothing measured of them can
    prove or refute, having none of their kinds. In a folder without storage, that is every
    file of which a source gave a checksum, and the files of the size and format of a document
    known by a checksum are read as well. In a folder with a storage, a file waits for the
    storage to answer, unless the storage answered and did not hold the file (`absent`), or
    gave a checksum of another kind than the source."""
    lacking = "instr(coalesce(f.sum,''),'sha256:')=0" if reading else "instr(coalesce(f.sum,''),'md5:')=0"
    rows = db.execute(f"SELECT {FILE},f.doc,f.sum,f.read AND d.origin IS NULL AS untied FROM files f JOIN docs d ON d.id=f.doc "
                      f"WHERE f.root=? AND {lacking} ORDER BY f.id", (root["id"],)).fetchall()
    rows = [row for row in rows if only is None or row["rel"] in only]
    if not rows:
        return []
    told = {}                                       # file -> the kinds of checksum each statement about it gives
    for record in db.execute("SELECT r.file,r.keys FROM records r JOIN files f ON f.id=r.file WHERE f.root=? AND r.keys!=''",
                             (root["id"],)):
        told.setdefault(record["file"], []).append(set(store.kinds(record["keys"].split())))
    if not reading:
        several = {(doc["size"], doc["format"]) for doc in
                   db.execute("SELECT size,format FROM docs GROUP BY size,format HAVING count(*)>1")}
        return [row for row in rows if not row["untied"]
                and ((row["size"], row["format"]) in several or (thorough and row["id"] in told))]

    def unproved(row):
        """A source gave, for this file, checksums of which no kind was measured of it."""
        measured = set(store.kinds((row["sum"] or "").split()))
        return any(not kinds & measured for kinds in told.get(row["id"], ()))

    if root.get("remote"):
        return [row for row in rows if row["untied"] or (unproved(row) and (row["sum"] or row["id"] in absent))]
    alike = {}                                      # size and format -> the document known by a checksum, or -1 for several
    for doc in db.execute("SELECT d.id,d.size,d.format FROM docs d WHERE EXISTS (SELECT 1 FROM doc_keys k WHERE k.doc=d.id)"):
        alike[(doc["size"], doc["format"])] = -1 if (doc["size"], doc["format"]) in alike else doc["id"]
    other = lambda row: alike.get((row["size"], row["format"]), row["doc"]) != row["doc"]
    return [row for row in rows if row["untied"] or unproved(row) or (not row["sum"] and other(row))]


def ask_storage(db, root, rows, progress, absent=None):
    """Keep what the storage of a folder says of the files `rows`, and let each join the
    document known by its checksum. No file is read. Returns the number of files of which
    something was learnt, and whether the storage stopped answering; `absent` collects the
    files of which a storage that answered said nothing, since it does not hold them as they
    are."""
    learnt = 0
    for start in range(0, len(rows), ASKED):
        batch = rows[start:start + ASKED]
        if len(rows) > ASKED:
            progress(f"{root['name']}: the storage is asked about {min(start + ASKED, len(rows)):,} of {len(rows):,} files")
        told = stored_checksums(root["remote"], batch)
        if told is None:
            progress(f"The storage of {root['name']} gives no answer ({root['remote']}): "
                     "its files will be compared when it does")
            return learnt, True
        for row in batch:
            given = told.get(row["rel"])
            if not given:
                if absent is not None:
                    absent.add(row["id"])
                continue
            try:                                # the file itself is still what the inventory saw
                check_stamp(Path(root["path"]) / row["rel"], row)
            except ValueError:
                progress(f"Changed since the inventory: {row['rel']}; run index again")
                with db:
                    store.afresh(db, row["id"])
                continue
            except OSError:
                continue
            with db:
                learnt += joined(db, row["id"], [given], local=False)
    return learnt, False


def read_in_full(db, root, rows, progress, failed):
    """Measure the files `rows` by a full reading, and let each join the document known by its
    checksums. Returns (files of which something was learnt, files that could not be read)."""
    learnt = failures = 0
    for row in rows:
        if row["id"] in failed:
            continue
        try:
            measured = whole_checksums(Path(root["path"]) / row["rel"], row)
        except ValueError:
            progress(f"Changed while it was compared: {row['rel']}; run index again")
            with db:                            # another file stands in its place: nothing known of it holds
                store.afresh(db, row["id"])
        except OSError as fault:
            progress(f"Could not compare {row['rel']}: {fault}")
        else:
            with db:
                learnt += joined(db, row["id"], measured)
            continue
        failed.add(row["id"])
        failures += 1
    return learnt, failures


def settle(db, roots, chosen, progress, reads=True, failed=None, asked=None, thorough=False, absent=None):
    """Compare the files of the folders with the documents already known, as far as it has not
    been done: see `waiting` for the files concerned. Where a folder has an rclone twin, its
    storage is asked, which costs no reading. Then, when `reads` allows, the files that only a
    reading can measure are read in full, once each. What a reading teaches may make a file of
    another folder worth reading: the folders are gone through again for as long as something
    is learnt. `chosen` limits the work, per folder, to some addresses. Returns the number of
    files that could not be read; `failed` collects them, `asked` the files of which the
    storage was asked and `absent` those it did not hold, so that one run says a failure once
    and asks a question once."""
    failures = 0
    failed = set() if failed is None else failed
    asked = set() if asked is None else asked
    absent = set() if absent is None else absent
    silent = set()
    while True:
        learnt = 0
        for root in roots:
            if not root.get("remote") or root["id"] in silent:
                continue
            rows = [row for row in waiting(db, root, chosen[root["id"]], thorough=thorough) if row["id"] not in asked]
            asked.update(row["id"] for row in rows)
            found, stopped = ask_storage(db, root, rows, progress, absent) if rows else (0, False)
            learnt += found
            if stopped:
                silent.add(root["id"])
        for root in roots if reads else ():
            rows = waiting(db, root, chosen[root["id"]], reading=True, absent=absent)
            found, missed = read_in_full(db, root, rows, progress, failed)
            learnt += found
            failures += missed
        if not learnt:
            return failures


def update_text(db, root, row, ocr, languages, wanted=None, hold=None):
    """Read one document through one of its files, and return True when pages were written.

    The file is first copied to this machine, in the folder `hold`, and its checksums are
    those of the copy: the file then belongs to the document known by them, which may be
    another than the one it was taken for, and may need no reading, in which case nothing is
    extracted and False is returned: `wanted(document, state)` tells whether the run reads a
    document in that state, which by default it does when the document waits for its text, or
    for its recognition with `ocr`. The pages are read from the copy, so they are those of the
    bytes that the checksums describe. A file that cannot be copied as the inventory saw it
    raises Unreadable, a copy that cannot be written raises Unplaced, and any other failure is
    that of what the file holds."""
    path = Path(root["path"]) / row["rel"]
    file = db.execute("SELECT id,sum FROM files WHERE id=? AND root=? AND rel=?", (row["id"], root["id"], row["rel"])).fetchone()
    if file is None:
        return False
    if row["format"] in extract.PLAIN and row["size"] > extract.MAX_TEXT:
        raise ValueError("Plain text exceeds 64 MiB")       # said before a copy of that size is made
    try:
        folder = tempfile.TemporaryDirectory(prefix="copy-", dir=hold)
    except OSError as fault:
        raise Unplaced(str(fault)) from None
    with folder as held:
        try:
            copy, measured, same = private_copy(path, row, Path(held), store.sums(file["sum"]).get("md5"))
        except (OSError, ValueError) as fault:
            raise Unreadable(str(fault)) from None

        def still():
            """The file is still the one that was copied: what is kept is kept for it."""
            try:
                check_stamp(path, row, same)
            except (OSError, ValueError) as fault:
                raise Unreadable(str(fault)) from None
        with db:
            joined(db, file["id"], measured)
        told = db.execute("SELECT d.id,d.state FROM files f JOIN docs d ON d.id=f.doc WHERE f.id=?", (file["id"],)).fetchone()
        doc = told["id"]
        if wanted is None:
            wanted = lambda doc, state: state == ("needs-ocr" if ocr else "pending")
        if not wanted(doc, told["state"]):
            return False
        with extract.document(copy, row["format"]) as (source, fmt, work):
            if ocr:
                blanks = db.execute("SELECT id,page FROM pages WHERE doc=? AND needs_ocr=1 "
                                    "ORDER BY page", (doc,)).fetchall()
                for page in blanks:
                    text = extract.recognize(source, fmt, work, page["page"], languages)
                    with db:
                        db.execute("UPDATE pages SET ztext=?,needs_ocr=0 WHERE id=?", (store.deflate(text), page["id"]))
                        fuzzy.remember_more(db, (text,))
                with db:
                    db.execute("UPDATE docs SET state='indexed',error=NULL WHERE id=?", (doc,))
                still()
                return True
            pages = list(extract.passages(source, fmt, work))
            if not pages:
                raise ValueError("No readable pages or EPUB chapters found")
            state = "needs-ocr" if any(p.needs_ocr for p in pages) else "indexed"
            still()
            with db:
                db.execute("DELETE FROM pages WHERE doc=?", (doc,))
                fuzzy.remember_text(db, (p.text for p in pages))
                db.executemany("INSERT INTO pages(doc,page,section,ztext,needs_ocr) VALUES(?,?,?,?,?)",
                               [(doc, p.page, p.section, store.deflate(p.text), p.needs_ocr) for p in pages])
                db.execute("UPDATE docs SET state=?,error=NULL,origin=? WHERE id=?", (state, " ".join(measured), doc))
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
    folder tells their checksums, and what the sources say is not proved. Returns the number
    of readings tried, which `limit` caps, a file found to need no reading not being one, and
    the number of problems."""
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
        failed, asked, absent, offline = set(), set(), set(), set()

        def acquaint(reads, thorough, again=False):
            """Compare the files with the known documents, then ask the sources. What the
            sources say may show more files to be known documents, and is proved by a measure
            of the file; and a file just measured is a new question to the sources that did
            not know it by its address: so the two alternate for as long as the sources
            recognise more, which ends, since a file is measured once and a statement that a
            measure refuted is not taken a second time."""
            live = [root for root in ready if root["id"] not in offline]
            missed = settle(db, live, chosen, progress, reads, failed, asked, thorough, absent)
            first = True
            while link.link(db, directory, live, progress, everything=again and first):
                first = False
                missed += settle(db, live, chosen, progress, reads, failed, asked, thorough, absent)
            link.cover(db, directory)
            return missed

        failures += acquaint(not names_only, not names_only, relink)
        if names_only:
            return done, failures
        # The copies are made in a folder of ShelfFind's own, which no folder of documents can
        # be registered in. What a run that was cut short left there is removed: no other run
        # is at work, since this one holds the lock.
        hold = Path(directory) / "copies"
        shutil.rmtree(hold, ignore_errors=True)
        states = ["needs-ocr"] if ocr else ["pending"]
        if retry:
            states.append("ocr-error" if ocr else "error")
        failing = "ocr-error" if ocr else "error"
        marks = ",".join("?" * len(states))
        waiting_of = lambda file: db.execute(
            f"SELECT d.id FROM files f JOIN docs d ON d.id=f.doc WHERE f.id=? AND d.state IN ({marks})", (file, *states)).fetchone()
        charged, tried, halted = set(), set(), False
        wanted = lambda doc, state: state in states and doc not in charged
        try:
            while not halted and (limit is None or done < limit):
                # A pass takes a snapshot of its queue. One document may be read through any
                # of its files: the others are tried when one fails, or turns out to hold
                # something else. What a pass reads may leave other documents waiting, which
                # the next pass takes up; no file is taken twice in one run.
                candidates = {}
                for root in ready:
                    if root["id"] in offline:
                        continue
                    query = ("SELECT f.id,f.doc,f.rel,f.size,f.mtime,f.format FROM files f JOIN docs d ON d.id=f.doc "
                             f"WHERE f.root=? AND d.state IN ({marks})")
                    params = [root["id"], *states]
                    if paths:
                        rels = sorted(chosen[root["id"]])
                        if not rels:
                            continue
                        query += " AND f.rel IN (" + ",".join("?" * len(rels)) + ")"
                        params += rels
                    for row in db.execute(query + " ORDER BY f.read DESC,f.id", params).fetchall():
                        if row["id"] not in failed and row["id"] not in tried and row["doc"] not in charged:
                            candidates.setdefault(row["doc"], []).append((root, row))
                if not candidates:
                    break
                hold.mkdir(mode=0o700, exist_ok=True)
                total, taken = len(candidates), False
                for seen, (doc, copies) in enumerate(candidates.items()):
                    if halted or (limit is not None and done >= limit):
                        break
                    if on_document:
                        on_document(seen, total, copies[0][1]["rel"])
                    untaken, replaced, before = {}, None, done
                    for root, row in copies:
                        if halted or (limit is not None and done >= limit):
                            break
                        # The file is followed to the document it is in now: a comparison may
                        # have placed it elsewhere, and its document may have been read
                        # through another file.
                        now = waiting_of(row["id"])
                        if root["id"] in offline or now is None or now["id"] in charged:
                            continue
                        if not store.available(root):       # the other folders go on being read
                            progress(f"Disconnected: {root['path']}; saved completed work")
                            offline.add(root["id"])
                            failures += 1
                            continue
                        try:
                            taken = True
                            tried.add(row["id"])
                            done += update_text(db, root, row, ocr, languages, wanted, hold)
                        except sqlite3.Error:
                            raise
                        except Unplaced as exc:     # no room for the copy: nothing is wrong with the documents
                            progress(f"No copy of {row['rel']} could be made in {hold}: {exc}; the reading stops here")
                            failures += 1
                            halted = True
                        except Exception as exc:    # one unreadable file must not stop the others
                            if not store.available(root):
                                progress(f"Disconnected: {root['path']}; saved completed work")
                                offline.add(root["id"])
                                failures += 1
                                continue
                            if isinstance(exc, Unreadable):
                                # Nothing was learnt of what the file holds: another file of
                                # the document may be read in its place.
                                if str(exc) == CHANGED:     # another file stands there: it waits for the next run
                                    with db:
                                        store.afresh(db, row["id"])
                                    replaced = replaced or (row["rel"], exc)
                                else:
                                    untaken[row["id"]] = (row["rel"], exc)
                                    failed.add(row["id"])   # said once: the comparison that follows does not say it again
                                continue
                            # What the file holds cannot be read: the fault is that of the
                            # document known by its checksums, which may be another than the
                            # one waited for.
                            with db:
                                db.execute("UPDATE docs SET state=?,error=? WHERE id=(SELECT doc FROM files WHERE id=?)",
                                           (failing, str(exc)[:1200], row["id"]))
                            charged.update(found["doc"] for found in db.execute("SELECT doc FROM files WHERE id=?", (row["id"],)))
                            progress(f"Could not read {row['rel']}: {exc}")
                            failures += 1
                            done += 1
                    # A document of which no file could be taken waits for a retry when all its
                    # files were tried; one that was not among those chosen may still be read.
                    stuck = {}
                    for file, fault in untaken.items():
                        now = waiting_of(file)
                        if now is not None:
                            stuck.setdefault(now["id"], fault)
                    for now, fault in stuck.items():
                        inside = {found["id"] for found in db.execute("SELECT id FROM files WHERE doc=?", (now,))}
                        if inside <= set(untaken):
                            with db:
                                db.execute("UPDATE docs SET state=?,error=? WHERE id=?", (failing, str(fault[1])[:1200], now))
                            charged.add(now)
                    if (untaken or replaced) and done == before:
                        fault = next(iter(stuck.values()), replaced or next(iter(untaken.values()), None))
                        progress(f"Could not read {fault[0]}: {fault[1]}")
                        failures += 1
                        done += 1
                    if done // 100 > before // 100:
                        progress(f"{done // 100 * 100:,} documents processed")
                if not taken:
                    break
                # What was just read may be what other files hold, and what the sources know by
                # its checksum: the files of its size are compared with it now, and the
                # sources are asked about the files just measured, so that the next run finds
                # nothing left to do.
                failures += acquaint(True, True)
        finally:
            shutil.rmtree(hold, ignore_errors=True)
    return done, failures
