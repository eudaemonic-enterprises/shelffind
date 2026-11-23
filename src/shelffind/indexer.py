# Copyright 2026 Eudaemonic Enterprises Inc.
"""Inventory first. Commit extracted text per document and OCR per page.

Between the inventory and the reading, the files are compared with the documents already known:
a file that holds what a known document holds joins it and is not read again. Every comparison
is of content (see `store`), and each is made once for a file as it is: `files.print` and
`files.sum` keep what was measured.
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

BLOCK = 64 * 1024
SAMPLES = 8
ASKED = 400             # files in one question to the storage of a folder
CHANGED = "File changed during indexing; run index again"


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


def fingerprint(path, fmt):
    """A key drawn from the content of a file: all of it up to 4 MiB; for a larger file its
    size, its first and last 64 KiB and eight samples between. Two files of one format with
    the same key are taken for the same document. The format belongs to the key because a
    document is read by the reader of its format."""
    size = path.stat().st_size
    digest = hashlib.sha256(str(size).encode())
    with path.open("rb") as source:
        if size <= store.WHOLE:
            while block := source.read(1 << 20):
                digest.update(block)
        else:
            step = (size - 2 * BLOCK) // (SAMPLES + 1)
            for offset in (0, *(BLOCK + number * step for number in range(1, SAMPLES + 1)), size - BLOCK):
                source.seek(offset)
                digest.update(source.read(BLOCK))
    return f"fp:{size}:{digest.hexdigest()[:32]}:{fmt}"


def proven(path, row, same=None):
    """The fingerprint of a file that stayed as the inventory saw it, and itself, while it was
    read."""
    same = check_stamp(path, row, same)
    key = fingerprint(path, row["format"])
    check_stamp(path, row, same)
    return key


def whole_checksums(path, row):
    """The MD5 and the SHA-256 of a file read in full, which stayed as the inventory saw it."""
    same = check_stamp(path, row)
    sums = hashlib.md5(), hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1 << 20):
            for one in sums:
                one.update(block)
    check_stamp(path, row, same)
    return [f"md5:{sums[0].hexdigest()}", f"sha256:{sums[1].hexdigest()}"]


def storage(arguments, listing, remote, seconds):
    """What rclone prints about the files named in `listing`, or None when it gave no answer."""
    try:
        done = subprocess.run(["rclone", *arguments, "--retries", "1", "--files-from-raw", str(listing), remote],
                              capture_output=True, text=True, timeout=seconds)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def stored_checksums(remote, rows):
    """The MD5 that the storage of a folder gives for some of its files, as address -> key,
    without any of them being read; None when the storage gives no answer at all. A file is
    answered for only when the storage holds it with the size and the date the inventory saw:
    a twin that lags behind the folder, or runs ahead of it, says nothing of the file. The
    storage is believed beyond that: a twin that held other bytes under the same size and the
    same date would not be noticed."""
    wanted = {row["rel"]: row for row in rows if "\n" not in row["rel"] and "\r" not in row["rel"]}
    if not wanted:
        return {}
    with tempfile.TemporaryDirectory(prefix="shelffind-") as work:
        listing = Path(work) / "files"
        listing.write_text("".join(rel + "\n" for rel in wanted), encoding="utf-8")
        told = storage(["lsjson", "-R", "--files-only", "--no-mimetype"], listing, remote, 900)
        try:
            entries = json.loads(told) if told is not None else None
        except ValueError:
            entries = None
        if not isinstance(entries, list):
            return None
        agreeing = []
        for entry in entries:
            row = wanted.get(entry.get("Path")) if isinstance(entry, dict) else None
            if row is None or entry.get("Size") != row["size"]:
                continue
            try:                                # an instant with its offset, whatever the time zone it is written in
                moment = datetime.datetime.fromisoformat(str(entry.get("ModTime")))
            except ValueError:
                continue
            if moment.tzinfo is not None and math.floor(moment.timestamp()) == row["mtime"] // 1_000_000_000:
                agreeing.append(entry["Path"])
        if not agreeing:
            return {}
        listing.write_text("".join(rel + "\n" for rel in agreeing), encoding="utf-8")
        told = storage(["hashsum", "md5"], listing, remote, 1800)
    if told is None:
        return None
    found = {}
    for line in told.splitlines():
        word, _, rel = line.partition("  ")
        word = word.strip().lower()
        if rel in wanted and len(word) == 32 and all(char in "0123456789abcdef" for char in word):
            found[rel] = f"md5:{word}"
    return found


def joined(db, file, keys):
    """Keep the checksums `keys` of the whole of a file, taken from its bytes or given by its
    storage, and let the file join the document that carries one of them.

    A document speaks for the file it was read from, or, when it was never read, for its files
    alike. The checksums of such a file become those of the document; should the document carry
    another checksum of the same kind, on the word of a source, that word is withdrawn when the
    document was read from this very file, and otherwise the file leaves the document. A file
    that joined a document read from another file keeps its checksums to itself: they confirm
    that document once it is known by its own, and until then only show whether the file is
    some other document. The caller holds the transaction."""
    row = db.execute("SELECT doc,sum,read FROM files WHERE id=?", (file,)).fetchone()
    if row is None:
        return
    db.execute("UPDATE files SET sum=? WHERE id=?", (" ".join(sorted({*(row["sum"] or "").split(), *keys})), file))
    doc = row["doc"]
    if store.against(keys, store.keys_of(db, doc)):
        if row["read"]:
            store.disown(db, doc, file, measured=True)
        else:
            doc = store.set_apart(db, file)
    elif not row["read"] and db.execute("SELECT 1 FROM pages WHERE doc=? LIMIT 1", (doc,)).fetchone():
        marks = ",".join("?" * (2 * len(keys)))
        fmt = db.execute("SELECT format FROM docs WHERE id=?", (doc,)).fetchone()["format"]
        elsewhere = db.execute(f"SELECT 1 FROM doc_keys WHERE key IN ({marks}) AND doc!=? LIMIT 1",
                               (*keys, *(f"{key}@{fmt}" for key in keys), doc)).fetchone()
        if not elsewhere:
            return
        doc = store.set_apart(db, file)
    for key in keys:
        doc = store.claim(db, doc, key, "content")
    # The document is now known by these checksums: a file of it that was measured otherwise
    # is not that document.
    for other in db.execute("SELECT id,sum FROM files WHERE doc=? AND id!=? AND sum IS NOT NULL", (doc, file)).fetchall():
        if store.against(other["sum"].split(), store.keys_of(db, doc)):
            apart = store.set_apart(db, other["id"])
            for key in other["sum"].split():
                apart = store.claim(db, apart, key, "content")


def bearers(db, found):
    """The documents that carry the fingerprint `found`: one as a rule, several when files that
    agree on every sample of a fingerprint were shown to differ by their checksums."""
    return [row["doc"] for row in db.execute("SELECT doc FROM doc_keys WHERE key=? OR (key>? AND key<?) ORDER BY key",
                                             (found, found + "#", found + "$"))]


def keep_print(db, doc, found):
    """Let a document be known by the fingerprint of the file it was read from."""
    carrying = bearers(db, found)
    if doc not in carrying:
        db.execute("INSERT INTO doc_keys(key,doc,basis) VALUES(?,?,'content')",
                   (f"{found}#{doc}" if carrying else found, doc))


def join_known(db, file, found, whole):
    """Let a file join the document that its fingerprint `found` shows it to be; True when it
    joined one. When several documents carry that fingerprint, only the checksums of the whole
    file tell which of them it is: `whole()` reads them, and without it the file waits. The
    caller holds the transaction."""
    own = db.execute("SELECT doc FROM files WHERE id=?", (file,)).fetchone()
    if own is None:
        return False
    carrying = bearers(db, found)
    others = [doc for doc in carrying if doc != own["doc"]]
    if not others or own["doc"] in carrying:
        return False
    if len(others) == 1:
        return store.merge(db, others[0], own["doc"]) is not None
    if whole is None:
        return False
    joined(db, file, whole())
    now = db.execute("SELECT doc FROM files WHERE id=?", (file,)).fetchone()
    return now is not None and now["doc"] in bearers(db, found)


COMPARABLE = {
    "fp": "f.print IS NULL AND k.key LIKE 'fp:%'",
    "md5": "instr(coalesce(f.sum,''),'md5:')=0 AND k.key LIKE 'md5:%'",
    "sha256": "instr(coalesce(f.sum,''),'sha256:')=0 AND k.key LIKE 'sha256:%'",
}
FILE = "f.id,f.rel,f.size,f.mtime,f.format"


def unsettled(db, root, kinds, only=None):
    """The files of a folder that may be a known document of their size and format, by a
    comparison of one of `kinds` that has not been made of them yet."""
    tests = " OR ".join(f"({COMPARABLE[kind]})" for kind in kinds)
    rows = db.execute(
        f"SELECT {FILE} FROM files f WHERE f.root=? AND EXISTS (SELECT 1 FROM docs d JOIN doc_keys k ON k.doc=d.id "
        f"WHERE d.size=f.size AND d.format=f.format AND d.id!=f.doc AND ({tests})) ORDER BY f.id", (root["id"],)).fetchall()
    return [row for row in rows if only is None or row["rel"] in only]


def ask_storage(db, root, rows, progress):
    """Keep what the storage of a folder says of the files `rows`, and let each join the
    document that carries its checksum. No file is read. Returns False when the storage gave
    no answer."""
    for start in range(0, len(rows), ASKED):
        batch = rows[start:start + ASKED]
        if len(rows) > ASKED:
            progress(f"{root['name']}: the storage is asked about {min(start + ASKED, len(rows)):,} of {len(rows):,} files")
        told = stored_checksums(root["remote"], batch)
        if told is None:
            progress(f"The storage of {root['name']} gives no answer ({root['remote']}): "
                     "its files will be compared when it does")
            return False
        for row in batch:
            given = told.get(row["rel"])
            if not given:
                continue
            try:                                # the file itself is still what the inventory saw
                check_stamp(Path(root["path"]) / row["rel"], row)
            except (OSError, ValueError):
                continue
            with db:
                joined(db, row["id"], [given])
    return True


def prints(db, root, progress, only=None, failed=None):
    """Take the fingerprint of every file a document was read from and that has none yet, as
    happens to the documents an earlier version read: the file, unchanged since, is the one
    proof of what the pages hold. Returns the number of files that could not be read."""
    failures = 0
    failed = set() if failed is None else failed
    bare = db.execute(f"SELECT {FILE} FROM files f WHERE f.root=? AND f.read=1 AND f.print IS NULL AND EXISTS "
                      "(SELECT 1 FROM pages p WHERE p.doc=f.doc) ORDER BY f.id", (root["id"],)).fetchall()
    for row in bare:
        if (only is not None and row["rel"] not in only) or row["id"] in failed:
            continue
        path = Path(root["path"]) / row["rel"]
        try:
            found = proven(path, row)
        except ValueError:
            continue                            # changed since the inventory: the next one will see it
        except OSError as fault:
            progress(f"Could not fingerprint {row['rel']}: {fault}")
            failed.add(row["id"])
            failures += 1
            continue
        with db:
            now = db.execute("SELECT doc FROM files WHERE id=? AND read=1 AND print IS NULL", (row["id"],)).fetchone()
            if now is None:
                continue
            db.execute("UPDATE files SET print=? WHERE id=?", (found, row["id"]))
            carried = {key.split("#")[0] for key in store.keys_of(db, now["doc"]) if key.startswith("fp:")}
            if carried and found not in carried:
                store.set_apart(db, row["id"])      # the document was read from another file than this one
            elif not join_known(db, row["id"], found, lambda: whole_checksums(path, row)):
                keep_print(db, db.execute("SELECT doc FROM files WHERE id=?", (row["id"],)).fetchone()["doc"], found)
    return failures


def settle(db, root, progress, reads=True, only=None, failed=None):
    """Compare the files of one folder with the documents already known, as far as it has not
    been done: by the checksum its storage gives, when the folder has an rclone twin, which
    costs no reading; then, when `reads` allows, by their fingerprint; and, in a folder without
    a twin, by a full reading for the checksums of the whole file, when a source described a
    document of that size by them. A file joins the document that has the same content.
    `only` limits the work to some addresses. Returns the number of files that could not be
    compared; `failed` collects them, so that one run says a failure once."""
    failures = 0
    failed = set() if failed is None else failed
    # A fingerprint taken earlier may be that of a document read since: no reading is needed
    # to see it.
    known = db.execute(f"SELECT {FILE},f.print FROM files f JOIN doc_keys k ON k.key=f.print "
                       "WHERE f.root=? AND k.doc!=f.doc ORDER BY f.id", (root["id"],)).fetchall()
    for row in known:
        if only is None or row["rel"] in only:
            path = Path(root["path"]) / row["rel"]
            try:
                with db:
                    join_known(db, row["id"], row["print"], (lambda: whole_checksums(path, row)) if reads else None)
            except (OSError, ValueError):
                pass                            # it could not be read in full now: another run will tell
    if root.get("remote"):
        # Also asked: the checksum of every file a document was read from, so that the file is
        # recognised after a move without being read again; and that of every large file which
        # joined a document on the samples of its fingerprint, to confirm that the whole agrees.
        asked = set()
        for _ in range(2):                      # what the first answers tell may make other files worth asking about
            more = db.execute(
                f"SELECT {FILE} FROM files f WHERE f.root=? AND instr(coalesce(f.sum,''),'md5:')=0 AND (f.read=1 OR "
                "(f.size>? AND f.print IS NOT NULL AND EXISTS (SELECT 1 FROM doc_keys k WHERE k.doc=f.doc AND "
                "k.key LIKE 'md5:%'))) ORDER BY f.id", (root["id"], store.WHOLE)).fetchall()
            wanted = {row["id"]: row for row in unsettled(db, root, ["md5"], only)}
            wanted.update((row["id"], row) for row in more if only is None or row["rel"] in only)
            rows = [row for number, row in wanted.items() if number not in asked]
            if not rows or not ask_storage(db, root, rows, progress):
                break
            asked.update(wanted)
    if not reads:
        return failures

    def compare(row, measure, keep):
        nonlocal failures
        if row["id"] in failed:
            return
        try:
            found = measure(Path(root["path"]) / row["rel"], row)
        except ValueError:
            progress(f"Changed while it was compared: {row['rel']}; run index again")
        except OSError as fault:
            progress(f"Could not compare {row['rel']}: {fault}")
        else:
            with db:
                keep(row, found)
            return
        failed.add(row["id"])
        failures += 1

    def by_print(row, found):
        if db.execute("UPDATE files SET print=? WHERE id=? AND print IS NULL", (found, row["id"])).rowcount:
            path = Path(root["path"]) / row["rel"]
            join_known(db, row["id"], found, lambda: whole_checksums(path, row))

    for row in unsettled(db, root, ["fp"], only):
        compare(row, proven, by_print)
    if not root.get("remote"):
        # No storage tells the checksums here: the file is read once in full to know them.
        for row in unsettled(db, root, ["md5", "sha256"], only):
            compare(row, whole_checksums, lambda row, found: joined(db, row["id"], found))
    return failures


def update_text(db, root, row, ocr, languages):
    """Read one document through one of its files. Returns False when the file turned out to be
    a document already read, in which case nothing was extracted."""
    path = Path(root["path"]) / row["rel"]
    doc = row["doc"]
    file = db.execute("SELECT id,read FROM files WHERE root=? AND rel=? AND doc=?", (root["id"], row["rel"], doc)).fetchone()
    if file is None:
        raise AnotherDocument(row["rel"])
    same = check_stamp(path, row)
    key = proven(path, row, same)
    with db:
        db.execute("UPDATE files SET print=? WHERE id=?", (key, file["id"]))
    carried = {name.split("#")[0] for name in store.keys_of(db, doc) if name.startswith("fp:")}
    if carried and key not in carried:
        # The document is known by the fingerprint of the file it was read from, and this file
        # has another: whatever put the two together, this file is not that document.
        with db:
            store.set_apart(db, file["id"])
        raise AnotherDocument(row["rel"])
    if ocr:
        if not carried and not file["read"]:
            raise ValueError("The file its pages were read from is not at hand, and no other is proved to be the same")
    else:
        with db:
            merged = join_known(db, file["id"], key, lambda: whole_checksums(path, row))
        if merged:
            doc = db.execute("SELECT doc FROM files WHERE id=?", (file["id"],)).fetchone()["doc"]
            if db.execute("SELECT 1 FROM pages WHERE doc=? LIMIT 1", (doc,)).fetchone():
                return False
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
                keep_print(db, doc, key)
                db.execute("UPDATE files SET read=1 WHERE id=?", (file["id"],))
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
            # The fingerprint is kept once the reading has succeeded, and not before.
            keep_print(db, doc, key)
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
    folder tells their checksums. Returns the number of documents read, a file found to be a
    document already read not being one, and the number of problems."""
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
        if not names_only:
            # First of all, so that the comparisons that follow know the documents by them.
            for root in ready:
                failures += prints(db, root, progress, chosen[root["id"]], failed)
        for root in ready:
            failures += settle(db, root, progress, not names_only, chosen[root["id"]], failed)
        if link.link(db, directory, ready, progress, everything=relink):
            for root in ready:                  # what the sources said may show more files to be known documents
                failures += settle(db, root, progress, not names_only, chosen[root["id"]], failed)
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
            fault, tried = None, False
            for root, row in copies:
                if root["id"] in offline:
                    continue
                if not store.available(root):       # the other folders go on being read
                    progress(f"Disconnected: {root['path']}; saved completed work")
                    offline.add(root["id"])
                    failures += 1
                    continue
                try:
                    fault, tried = None, update_text(db, root, row, ocr, languages)
                    break
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
                    fault, tried = fault or (row["rel"], exc), True
            if fault:
                with db:
                    db.execute("UPDATE docs SET state=?,error=? WHERE id=?",
                               ("ocr-error" if ocr else "error", str(fault[1])[:1200], doc))
                progress(f"Could not read {fault[0]}: {fault[1]}")
                failures += 1
            if tried:
                done += 1
                if done % 100 == 0:
                    progress(f"{done:,} documents processed")
        if done:
            # What was just read may be what other files hold, and its checksum is asked where
            # a storage tells it: neither needs a reading.
            for root in ready:
                if root["id"] not in offline:
                    settle(db, root, progress, False, chosen[root["id"]], failed)
    return done, failures
