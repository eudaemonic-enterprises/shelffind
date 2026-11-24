# Copyright 2026 Eudaemonic Enterprises Inc.
"""Inventory first. Commit extracted text per document and OCR per page.

Between the inventory and the reading, the files are compared with the documents already known:
a file that holds what a known document holds joins it and is not read again. A comparison is
a measure of the whole file, its MD5 and SHA-256 (see `store`): the storage of a folder gives
the first without any reading when the folder has an rclone twin, and a full reading gives
both. Each is made once for a file as it is; `files.sum` keeps the result.

A document is read from a copy of its file made on this machine in one pass, during which the
checksums are computed: the pages kept are those of the very bytes the checksums describe. The
copies, and what their reading writes, are kept in a folder that the run makes for itself in
the data folder of ShelfFind, and nowhere else; the index names that folder for as long as it
exists, and nothing is ever removed that the index does not name.
"""
import contextlib
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from . import extract, fuzzy, link, store

ASKED = 400             # files in one question to the storage of a folder
COPIES = "copies"       # in the index: the folders of the data folder in which runs made their copies
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


def put(target, block):
    """Write the whole of a block into the copy of a file; Unplaced when it cannot be."""
    try:
        left = memoryview(block)
        while left:
            left = left[target.write(left):]
    except OSError as fault:
        raise Unplaced(str(fault)) from None


def whole_checksums(path, row, copy=None):
    """The MD5 and the SHA-256 of a file read in full, once, through one opening of it. The
    file opened is the one the inventory saw, and it stayed so while it was read: its address,
    its size, its date and the file itself are checked before and after, on the address and on
    what was opened, and so is the time of the last change of the file, which a writer that
    puts the date back cannot put back. With `copy`, the bytes read are written there: the
    checksums are then those of that copy, whatever happens to the file afterwards. The copy
    is written without a buffer and closed here, so that a fault of the place where it is made
    shows as what it is: Unplaced is raised when the copy cannot be made, written or closed,
    and nothing is then said against the file."""
    same = check_stamp(path, row)
    found = hashlib.md5(), hashlib.sha256()
    target = None
    if copy is not None:
        try:
            target = open(copy, "wb", buffering=0)
        except OSError as fault:
            raise Unplaced(str(fault)) from None
    try:
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != same or (opened.st_size, opened.st_mtime_ns) != (row["size"], row["mtime"]):
                raise ValueError(CHANGED)
            taken = 0
            while block := source.read(1 << 20):
                taken += len(block)
                for one in found:
                    one.update(block)
                if target is not None:
                    put(target, block)
            closed = os.fstat(source.fileno())
            if taken != row["size"] or (closed.st_size, closed.st_mtime_ns, closed.st_ctime_ns) != \
                    (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
                raise ValueError(CHANGED)
        if target is not None:
            written, target = target, None
            try:
                written.close()
            except OSError as fault:
                raise Unplaced(str(fault)) from None
    finally:
        if target is not None:
            with contextlib.suppress(OSError):
                target.close()
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
    """What rclone prints about the files named in `listing`, or None when it gave no answer.
    The answer is taken as the bytes it was written in: no line ending is rewritten."""
    try:
        done = subprocess.run(["rclone", *arguments, "--retries", "1", "--files-from-raw", str(listing), remote],
                              capture_output=True, timeout=seconds)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else None


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


def askable(rel):
    """Whether an address can be put to a storage: the list of the files asked about, and the
    answer, have one address on each line."""
    return "\n" not in rel and "\r" not in rel


def stored_checksums(remote, rows):
    """The MD5 that the storage of a folder gives for some of its files, as address -> key,
    without any of them being read; None when the storage gives no answer at all. A file is
    answered for only when the storage holds it with the size and the instant the inventory
    saw, before the checksum is asked and after it was given: a twin that lags behind the
    folder, runs ahead of it, or changes meanwhile says nothing of the file. The answer is
    read as it is written, one line for each file and the line feed alone between two lines,
    whatever other character an address holds; an address that the answer gives two different
    checksums for is not answered for. The storage is believed beyond that: a twin that held
    other bytes under the same size and the same second would not be noticed."""
    wanted = {row["rel"]: row for row in rows if askable(row["rel"])}
    if not wanted:
        return {}
    with tempfile.TemporaryDirectory(prefix="shelffind-") as work:
        listing = Path(work) / "files"
        listing.write_text("".join(rel + "\n" for rel in wanted), encoding="utf-8", newline="")
        before = listed(remote, listing, wanted)
        if before is None:
            return None
        if not before:
            return {}
        listing.write_text("".join(rel + "\n" for rel in before), encoding="utf-8", newline="")
        told = storage(["hashsum", "md5"], listing, remote, 1800)
        after = listed(remote, listing, wanted) if told is not None else None
    if told is None or after is None:
        return None
    found, twice = {}, set()
    for line in told.split("\n"):
        word, _, rel = line.partition("  ")
        word = word.strip().lower()
        if rel in before and after.get(rel) == before[rel] and len(word) == 32 \
                and all(char in "0123456789abcdef" for char in word):
            if found.setdefault(rel, f"md5:{word}") != f"md5:{word}":
                twice.add(rel)
    return {rel: key for rel, key in found.items() if rel not in twice}


def joined(db, file, measured, local=True):
    """Keep the checksums `measured` of the whole of a file and let the file join the document
    known by them: see `store.measure`. The caller holds the transaction."""
    return store.measure(db, file, measured, local)


def waiting(db, root, only=None, reading=False, thorough=False, absent=frozenset(), offered=frozenset()):
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
    gave a checksum of another kind than the source. In either kind of folder, the files of a
    document of which the statements kept contradict one another, giving two checksums of one
    kind, are read as well: one of the statements is false, and the reading tells which.

    The files never measured whose size and format are those of a text that another machine
    offers (`offered`, see `parcels`) are compared as well, by the storage or by a reading:
    they may hold those bytes, which then need no reading of their own."""
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
                   db.execute("SELECT size,format FROM docs GROUP BY size,format HAVING count(*)>1")} | set(offered)
        return [row for row in rows if not row["untied"]
                and ((row["size"], row["format"]) in several or (thorough and row["id"] in told))]

    def unproved(row):
        """A source gave, for this file, checksums of which no kind was measured of it."""
        measured = set(store.kinds((row["sum"] or "").split()))
        return any(not kinds & measured for kinds in told.get(row["id"], ()))

    given = {}                                      # document -> kind -> the checksums that the statements kept with it give
    for record in db.execute("SELECT doc,keys FROM records WHERE keys!='' AND doc IN (SELECT doc FROM files WHERE root=?)",
                             (root["id"],)):
        for kind, found in store.kinds(record["keys"].split()).items():
            given.setdefault(record["doc"], {}).setdefault(kind, set()).update(found)
    disputed = {doc for doc, found in given.items() if any(len(keys) > 1 for keys in found.values())}
    if root.get("remote"):
        return [row for row in rows if row["untied"]
                or ((unproved(row) or row["doc"] in disputed) and (row["sum"] or row["id"] in absent))
                or (not row["sum"] and row["id"] in absent and (row["size"], row["format"]) in offered)]
    alike = {}                                      # size and format -> the document known by a checksum, or -1 for several
    for doc in db.execute("SELECT d.id,d.size,d.format FROM docs d WHERE EXISTS (SELECT 1 FROM doc_keys k WHERE k.doc=d.id)"):
        alike[(doc["size"], doc["format"])] = -1 if (doc["size"], doc["format"]) in alike else doc["id"]
    other = lambda row: alike.get((row["size"], row["format"]), row["doc"]) != row["doc"]
    return [row for row in rows if row["untied"] or unproved(row) or row["doc"] in disputed
            or (not row["sum"] and (other(row) or (row["size"], row["format"]) in offered))]


def ask_storage(db, root, rows, progress, absent=None, failed=None):
    """Keep what the storage of a folder says of the files `rows`, and let each join the
    document known by its checksum. No file is read. Returns the number of files of which
    something was learnt, whether the storage stopped answering, and the number of files that
    were no longer what the inventory saw when the answer came, which `failed` collects;
    `absent` collects the files of which a storage that answered said nothing, since it does
    not hold them as they are, and those whose address cannot be put to it."""
    learnt = failures = 0
    absent = set() if absent is None else absent
    absent.update(row["id"] for row in rows if not askable(row["rel"]))
    for start in range(0, len(rows), ASKED):
        batch = rows[start:start + ASKED]
        if len(rows) > ASKED:
            progress(f"{root['name']}: the storage is asked about {min(start + ASKED, len(rows)):,} of {len(rows):,} files")
        told = stored_checksums(root["remote"], batch)
        if told is None:
            progress(f"The storage of {root['name']} gives no answer ({root['remote']}): "
                     "its files will be compared when it does")
            return learnt, True, failures
        for row in batch:
            given = told.get(row["rel"])
            if not given:
                absent.add(row["id"])
                continue
            try:                                # the file itself is still what the inventory saw
                check_stamp(Path(root["path"]) / row["rel"], row)
            except ValueError:
                progress(f"Changed since the inventory: {row['rel']}; run index again")
                with db:
                    store.afresh(db, row["id"])
            except OSError as fault:
                progress(f"Could not compare {row['rel']}: {fault}")
            else:
                with db:
                    learnt += joined(db, row["id"], [given], local=False)
                continue
            failures += 1                       # nothing was learnt of the file, and the run says so
            if failed is not None:
                failed.add(row["id"])
    return learnt, False, failures


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


def settle(db, roots, chosen, progress, reads=True, failed=None, asked=None, thorough=False, absent=None,
           offered=frozenset()):
    """Compare the files of the folders with the documents already known, as far as it has not
    been done: see `waiting` for the files concerned. Where a folder has an rclone twin, its
    storage is asked, which costs no reading. Then, when `reads` allows, the files that only a
    reading can measure are read in full, once each. What a reading teaches may make a file of
    another folder worth reading: the folders are gone through again for as long as something
    is learnt. `chosen` limits the work, per folder, to some addresses. Returns the number of
    files that could not be compared, being unreadable or no longer what the inventory saw;
    `failed` collects them, `asked` the files of which the storage was asked and `absent` those
    it did not hold, so that one run says a failure once and asks a question once."""
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
            rows = [row for row in waiting(db, root, chosen[root["id"]], thorough=thorough, offered=offered)
                    if row["id"] not in asked]
            asked.update(row["id"] for row in rows)
            found, stopped, missed = ask_storage(db, root, rows, progress, absent, failed) if rows else (0, False, 0)
            learnt += found
            failures += missed
            if stopped:
                silent.add(root["id"])
        for root in roots if reads else ():
            rows = waiting(db, root, chosen[root["id"]], reading=True, absent=absent, offered=offered)
            found, missed = read_in_full(db, root, rows, progress, failed)
            learnt += found
            failures += missed
        if not learnt:
            return failures


def copies_folder(db, directory):
    """A folder of its own for the copies of the documents that a run reads, made in the data
    folder under a name drawn at random; the index keeps that name from before the first copy
    until the folder is removed. Unplaced is raised when it cannot be made."""
    try:
        hold = Path(tempfile.mkdtemp(prefix="copies-", dir=directory))
    except OSError as fault:
        raise Unplaced(str(fault)) from None
    with db:
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (f"{COPIES}:{hold.name}", hold.name))
    return hold


def forget_copies(db, directory, progress):
    """Remove the folders in which runs made their copies: those that the index names, and
    nothing else. A run that was cut short leaves its folder behind, and no other run is at
    work, since the one that asks holds the lock. Nothing is removed on the strength of a name
    or of a place: such a folder was made by ShelfFind itself, empty, inside its own data
    folder, where no folder of documents can be registered, and the index kept its name before
    any copy was made in it. Returns the number of folders that could not be removed, which
    the index goes on naming."""
    problems = 0
    for row in db.execute("SELECT key,value FROM meta WHERE key LIKE ?", (COPIES + ":%",)).fetchall():
        folder = Path(directory) / row["value"]
        if re.fullmatch(r"copies-[A-Za-z0-9_]+", row["value"]) and row["key"] == f"{COPIES}:{row['value']}" \
                and folder.is_dir() and not folder.is_symlink():
            if any(Path(root["path"]).is_relative_to(folder.resolve()) for root in store.roots(db)):
                progress(f"A folder of documents lies in {folder}, where copies were made: nothing is removed there")
                problems += 1
                continue
            shutil.rmtree(folder, ignore_errors=True)
            if folder.exists():
                progress(f"The copies left in {folder} could not be removed: remove that folder")
                problems += 1
                continue
        with db:
            db.execute("DELETE FROM meta WHERE key=?", (row["key"],))
    return problems


def update_text(db, root, row, ocr, languages, wanted=None, hold=None, offered=None):
    """Read one document through one of its files, and return True when pages were written.

    The file is first copied to this machine, in the folder `hold`, and its checksums are
    those of the copy: the file then belongs to the document known by them, which may be
    another than the one it was taken for, and may need no reading, in which case nothing is
    extracted and False is returned: `wanted(document, state)` tells whether the run reads a
    document in that state, which by default it does when the document waits for its text, or
    for its recognition with `ocr`. The pages are read from the copy, so they are those of the
    bytes that the checksums describe. A file that cannot be copied as the inventory saw it
    raises Unreadable, a copy that cannot be written raises Unplaced, and any other failure is
    that of what the file holds. Once the pages are kept, nothing that happens to the copy or
    to its folder is a failure of the document. `offered(document, file, unchanged)` tells,
    once the copy has measured the bytes and before anything is extracted, whether the document
    is to wait for what another data folder read of these very bytes (see `parcels.Exchange.offer`),
    `unchanged()` saying later whether the file is still the one copied: it is then not read."""
    path = Path(root["path"]) / row["rel"]
    file = db.execute("SELECT id,sum FROM files WHERE id=? AND root=? AND rel=?", (row["id"], root["id"], row["rel"])).fetchone()
    if file is None:
        return False
    if row["format"] in extract.PLAIN and row["size"] > extract.MAX_TEXT:
        raise ValueError("Plain text exceeds 64 MiB")       # said before a copy of that size is made
    try:                                            # what cannot be removed afterwards goes with the folder of the run
        folder = tempfile.TemporaryDirectory(prefix="copy-", dir=hold, ignore_cleanup_errors=True)
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
        if offered is not None:
            try:
                touched = path.stat().st_ctime_ns       # what a rewriting in place that kept the date still changes
            except OSError:
                touched = None

            def unchanged():
                try:
                    check_stamp(path, row, same)
                    return path.stat().st_ctime_ns == touched
                except (OSError, ValueError):
                    return False
            if offered(doc, file["id"], unchanged):
                return False                    # what another read of these bytes is on its way: nothing to read
        with extract.document(copy, row["format"], held) as (source, fmt, work):
            if ocr:
                # The pages are recognised from the copy, whose bytes are those the document is
                # known by: each is kept as soon as it is recognised, and the last one together
                # with the state of the document, so that a run cut there leaves nothing to mend.
                blanks = db.execute("SELECT id,page FROM pages WHERE doc=? AND needs_ocr=1 "
                                    "ORDER BY page", (doc,)).fetchall()
                for page in blanks:
                    text = extract.recognize(source, fmt, work, page["page"], languages)
                    with db:
                        db.execute("UPDATE pages SET ztext=?,needs_ocr=0 WHERE id=?", (store.deflate(text), page["id"]))
                        fuzzy.remember_more(db, (text,))
                        if page is blanks[-1]:
                            db.execute("UPDATE docs SET state='indexed',error=NULL WHERE id=?", (doc,))
                if not blanks:
                    with db:
                        db.execute("UPDATE docs SET state='indexed',error=NULL WHERE id=?", (doc,))
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
                db.execute("UPDATE docs SET state=?,error=NULL,origin=? WHERE id=?",
                           (state, " ".join(measured) + f" {store.SEALED}", doc))
                db.execute("UPDATE files SET read=0 WHERE doc=?", (doc,))
                db.execute("UPDATE files SET read=1 WHERE id=?", (file["id"],))
    return True


def build(db, directory, selected, *, names_only=False, ocr=False, languages="eng",
          retry=False, limit=None, progress=print, paths=(), on_document=None, relink=False, exchange=None):
    """Refresh the inventory, compare the files with the documents already known, ask the
    sources which files they know, then read the waiting documents. `selected` is the list of
    folders, or a function that gives it once no other build can change them.
    `progress(text)` receives the messages; `on_document(seen, total, name)` is called before
    each waiting document is taken up; `relink` asks the sources about every file again. With
    `names_only` no file is opened: the files are compared only as far as the storage of a
    folder tells their checksums, and what the sources say is not proved. Returns the number
    of readings tried, which `limit` caps, a file found to need no reading not being one, and
    the number of problems.

    With `exchange` (see `parcels`), a run shares texts with the other data folders. It takes
    the parcels that they gave before comparing the files, so that the files of the sizes
    offered are compared, and gives their pages once its inventory is made and its sources
    asked, so that a document read elsewhere is not read here and a document left to a source
    stays so; an inventory alone does this much. A complete run then reads the documents in an
    order of this data folder's own, so that two data folders reading at once begin with
    different documents; a copied file of which another read the very bytes is not read, its
    document taking those pages at the end of the pass, once the sources were asked about the
    checksums just measured; the run exchanges texts after a document read when the last
    exchange is more than two minutes old, and gives what it read at the end. Pages given make
    documents known by more checksums: the files are then compared and the sources asked
    again, for as long as that teaches something. The exchange concerns the whole index,
    whatever the folders or the files chosen."""
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
        offered = frozenset()
        if exchange is not None:
            exchange.run("receive", wait=True)  # what was read elsewhere is not read here
            offered = exchange.sizes()

        def acquaint(reads, thorough, again=False):
            """Compare the files with the known documents, then ask the sources. What the
            sources say may show more files to be known documents, and is proved by a measure
            of the file; and a file just measured is a new question to the sources that did
            not know it by its address: so the two alternate for as long as the sources
            recognise more, which ends, since a file is measured once and a statement that a
            measure refuted is not taken a second time."""
            live = [root for root in ready if root["id"] not in offline]
            missed = settle(db, live, chosen, progress, reads, failed, asked, thorough, absent, offered)
            first = True
            while link.link(db, directory, live, progress, everything=again and first):
                first = False
                missed += settle(db, live, chosen, progress, reads, failed, asked, thorough, absent, offered)
            link.cover(db, directory)
            return missed

        def exchanged():
            """Give the documents what was offered for them, the sources having just been asked.
            Pages taken from another machine, or the checksums that come with them, make a
            document known by more checksums, which may refute what a source said of a file or
            show what other files hold: the files are then compared and the sources asked again,
            and what is offered given again, for as long as that teaches something, so that the
            run ends with nothing left for the next one. Returns the problems of the comparisons."""
            missed = 0
            for _ in range(1000):                # each round gives pages or learns checksums, of which there are so many
                taken, learnt = exchange.counts["received"], exchange.learnt
                exchange.settled()
                exchange.run("apply")
                if (exchange.counts["received"], exchange.learnt) == (taken, learnt):
                    break
                missed += acquaint(not names_only, not names_only)
            return missed

        failures += acquaint(not names_only, not names_only, relink)
        if exchange is not None:                # the files compared may hold what was offered
            failures += exchanged()
        if names_only:
            return done, failures
        # The copies are made in a folder that the run makes for itself in the data folder,
        # when the first is needed. What a run that was cut short left behind is removed first.
        failures += forget_copies(db, directory, progress)
        hold = None
        states = ["needs-ocr"] if ocr else ["pending"]
        if retry:
            states.append("ocr-error" if ocr else "error")
        failing = "ocr-error" if ocr else "error"
        marks = ",".join("?" * len(states))
        waiting_of = lambda file: db.execute(
            f"SELECT d.id FROM files f JOIN docs d ON d.id=f.doc WHERE f.id=? AND d.state IN ({marks})", (file, *states)).fetchone()
        charged, tried, measured, halted = set(), set(), set(), False
        wanted = lambda doc, state: state in states and doc not in charged
        try:
            while not halted and (limit is None or done < limit):
                # A pass takes a snapshot of its queue. One document may be read through any
                # of its files: the others are tried when one fails, or turns out to hold
                # something else. What a pass reads, and what the sources say afterwards, may
                # leave other documents waiting, which the next pass takes up. No reading is
                # tried twice through one file in a run; a file that a pass only measured,
                # its document needing no reading then, may be read by a later pass, and is
                # taken at most twice, so that a run always ends.
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
                if exchange is not None:
                    # An order of this data folder's own, the same from one run to the next.
                    candidates = dict(sorted(candidates.items(), key=lambda item: hashlib.sha256(
                        "\0".join((exchange.writer or exchange.label, item[1][0][0]["name"], item[1][0][1]["rel"])).encode(
                            "utf-8", "surrogatepass")).digest()))
                if hold is None:
                    try:
                        hold = copies_folder(db, directory)
                    except Unplaced as exc:
                        progress(f"No folder for the copies of the documents could be made in {directory}: {exc}")
                        failures += 1
                        break
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
                            if update_text(db, root, row, ocr, languages, wanted, hold,
                                           exchange.offer if exchange is not None else None):
                                done += 1
                                tried.add(row["id"])
                            else:
                                if row["id"] in measured:
                                    tried.add(row["id"])    # copied twice without a reading: not a third time
                                else:
                                    measured.add(row["id"])
                                if exchange is not None and row["id"] in exchange.held:
                                    break                   # its text is on its way: no other file of it is copied
                        except sqlite3.Error:
                            raise
                        except Unplaced as exc:     # no room for the copy: nothing is wrong with the documents
                            tried.add(row["id"])
                            progress(f"No copy of {row['rel']} could be made in {hold}: {exc}; the reading stops here")
                            failures += 1
                            halted = True
                        except Exception as exc:    # one unreadable file must not stop the others
                            tried.add(row["id"])
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
                        finally:
                            if exchange is not None:
                                exchange.measured(row["id"])        # its document waits for the sources to be asked
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
                    if exchange is not None and done > before and exchange.due():
                        exchange.run("receive", "send", "apply")    # documents read elsewhere meanwhile are skipped
                if not taken:
                    break
                # What was just read may be what other files hold, and what the sources know by
                # its checksum: the files of its size are compared with it now, and the
                # sources are asked about the files just measured, so that the next run finds
                # nothing left to do.
                if exchange is not None:
                    exchange.run("receive")
                    offered = exchange.sizes()
                failures += acquaint(True, True)
                if exchange is not None:
                    failures += exchanged()
            if exchange is not None:
                exchange.run("send", wait=True)
        finally:
            failures += forget_copies(db, directory, progress)
    return done, failures
