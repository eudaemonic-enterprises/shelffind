# Copyright 2026 Eudaemonic Enterprises Inc.
"""What must hold in an index at rest, whatever happened before.

`structure(db)` lists the breaches of the rules that tie files, documents, checksums, pages and
the statements of sources together; `canonical(db)` computes, from what is measured and said
now, the documents the files ought to form, to be compared with those they do form: the two
agree only when the placing of the files does not depend on the order of the events.

The rules are written here on their own, without the functions of the program under test. The
histories they are used on never give two different files one MD5."""


def kinds(keys):
    """Kind -> the checksums of that kind among `keys`."""
    found = {}
    for key in keys:
        if key.startswith(("md5:", "sha256:")):
            found.setdefault(key.split(":")[0], set()).add(key)
    return found


def against(one, other):
    one, other = kinds(one), kinds(other)
    return any(kind in other and not one[kind] & other[kind] for kind in one)


def measured(db, doc):
    return [row["key"] for row in db.execute("SELECT key FROM doc_keys WHERE doc=? AND basis='content'", (doc,))]


def holder(db, key, fmt):
    row = db.execute("SELECT doc FROM doc_keys WHERE key=? AND format=? AND basis='content'", (key, fmt)).fetchone()
    return row["doc"] if row else None


def said(db, file):
    """One checksum of each kind among those the sources give for a file, or none when two of
    their statements contradict each other."""
    given = [row["keys"].split() for row in db.execute("SELECT keys FROM records WHERE file=? ORDER BY id", (file,))]
    for number, one in enumerate(given):
        for other in given[number + 1:]:
            if against(one, other):
                return []
    found = {}
    for keys in given:
        for kind, values in kinds(keys).items():
            found.setdefault(kind, sorted(values)[0])
    return [found[kind] for kind in sorted(found)]


def refuted(db, size, fmt, keys):
    """(Whether a measure refutes that a file of this size and format has these checksums, the
    document that a measure knows by them otherwise)."""
    if any(len(values) > 1 for values in kinds(keys).values()):
        return True, None
    known = {holder(db, key, fmt) for key in keys} - {None}
    if not known:
        return False, None
    if len(known) > 1:
        return True, None
    doc = min(known)
    if db.execute("SELECT size FROM docs WHERE id=?", (doc,)).fetchone()["size"] != size or against(keys, measured(db, doc)):
        return True, None
    return False, doc


def structure(db):
    """The breaches found, as sentences; an empty list when the index is sound."""
    found = []
    docs = {row["id"]: row for row in db.execute("SELECT * FROM docs")}
    files = db.execute("SELECT * FROM files ORDER BY id").fetchall()
    members = {}
    for file in files:
        members.setdefault(file["doc"], []).append(file)
    pages = {}
    for row in db.execute("SELECT doc,count(*) AS pages,coalesce(sum(needs_ocr),0) AS blank FROM pages GROUP BY doc"):
        pages[row["doc"]] = (row["pages"], row["blank"])
    keys = {}
    for row in db.execute("SELECT key,format,size,basis,doc FROM doc_keys"):
        keys.setdefault(row["doc"], []).append(row)
    content = lambda doc: [row["key"] for row in keys.get(doc, ()) if row["basis"] == "content"]
    source = lambda doc: {(row["key"], row["format"], row["size"]) for row in keys.get(doc, ()) if row["basis"] == "source"}

    for broken in db.execute("PRAGMA foreign_key_check").fetchall():
        found.append(f"a reference to nothing: {tuple(broken)}")
    for doc, held in keys.items():
        if doc not in docs:
            found.append(f"keys of a document that is not there: {doc}")
            continue
        if any(len(values) > 1 for values in kinds(content(doc)).values()):
            found.append(f"document {doc} is known by two checksums of one kind: {sorted(content(doc))}")
        if source(doc) and (content(doc) or doc in pages):
            found.append(f"document {doc} holds checksums on a word beside measured ones or pages")
        for row in held:
            if (row["format"], row["size"]) != (docs[doc]["format"], docs[doc]["size"]):
                found.append(f"document {doc} is known by a checksum kept under another size or format: {row['key']}")
    for file in files:
        doc = docs.get(file["doc"])
        if doc is None:
            found.append(f"file {file['rel']} is in no document")
            continue
        if (doc["size"], doc["format"]) != (file["size"], file["format"]):
            found.append(f"file {file['rel']} is in a document of another size or format")
        mine = (file["sum"] or "").split()
        for key in mine:
            known = holder(db, key, file["format"])
            if known != file["doc"] and not (known is not None and against(mine, measured(db, known))):
                found.append(f"file {file['rel']} was measured {key} and its document is not the one known by it")
        if file["read"] and file["doc"] not in pages:
            found.append(f"file {file['rel']} is marked as read and its document has no pages")
    for doc, told in docs.items():
        count, blank = pages.get(doc, (0, 0))
        inside = members.get(doc, [])
        if count:
            readers = [file for file in inside if file["read"]]
            if len(readers) > 1:
                found.append(f"document {doc} was read from two files")
            if told["origin"] is None:
                if len(inside) != 1 or not readers or inside[0]["sum"] or content(doc):
                    found.append(f"document {doc} has pages that nothing ties to bytes, and is not one unmeasured file")
            elif set(kinds(told["origin"].split())) != {"md5", "sha256"} or not set(told["origin"].split()) <= set(content(doc)):
                found.append(f"document {doc} has pages of bytes it is not known by a reading to hold: {told['origin']}")
            if told["state"] not in ("indexed", "needs-ocr", "ocr-error", "covered"):
                found.append(f"document {doc} has pages and is {told['state']}")
            if (told["state"] == "indexed" and blank) or (told["state"] in ("needs-ocr", "ocr-error") and not blank):
                found.append(f"document {doc} is {told['state']} with {blank} of {count} pages to recognise")
        else:
            if told["state"] in ("indexed", "needs-ocr", "ocr-error"):
                found.append(f"document {doc} is {told['state']} and has no pages")
            if told["origin"] is not None:
                found.append(f"document {doc} has no pages and keeps the checksums of some")
        if not inside:
            if not (count or db.execute("SELECT 1 FROM records WHERE doc=? LIMIT 1", (doc,)).fetchone()):
                found.append(f"document {doc} has no file, no pages and no record")
            if told["gone"] is None:
                found.append(f"document {doc} has no file and is not marked as gone")
        best = db.execute("SELECT title,byline,year FROM records WHERE doc=? AND coalesce(title,'')!='' "
                          "ORDER BY text DESC,source,id LIMIT 1", (doc,)).fetchone()
        if (told["title"], told["byline"], told["year"]) != (tuple(best) if best else (None, None, None)):
            found.append(f"document {doc} is titled {told['title']!r}, which is not what the sources say of it now")
    for file in files:
        if file["sum"] or (file["read"] and file["doc"] in pages):
            continue
        told = said(db, file["id"])
        wrong, known = refuted(db, file["size"], file["format"], told)
        alone = len(members[file["doc"]]) == 1 and not keys.get(file["doc"]) and file["doc"] not in pages
        names = {(key, file["format"], file["size"]) for key in told}
        if wrong or not told:
            if not alone:
                found.append(f"file {file['rel']}, of which nothing usable is said, is not alone in a document without keys")
        elif known is not None:
            if file["doc"] != known:
                found.append(f"file {file['rel']} is said to be document {known} and is in {file['doc']}")
        elif not names <= source(file["doc"]):
            found.append(f"file {file['rel']} is not in the document known by what is said of it")
    for doc, inside in members.items():
        if not source(doc):
            continue
        told = set()
        for file in inside:
            told |= {(key, file["format"], file["size"]) for key in said(db, file["id"])}
        if source(doc) != told:
            found.append(f"document {doc} holds on a word checksums that no file of it is said to have")
        if db.execute("SELECT 1 FROM records WHERE doc=? AND file IS NULL LIMIT 1", (doc,)).fetchone():
            found.append(f"document {doc}, held together by words, keeps what was said of a file that is gone")
    for record in db.execute("SELECT r.*,f.sum,f.size,f.format,f.doc AS home FROM records r LEFT JOIN files f ON f.id=r.file").fetchall():
        given = record["keys"].split()
        indexed = sorted(row[0] for row in db.execute("SELECT key FROM record_keys WHERE record=?", (record["id"],)))
        if indexed != sorted(set(given)):
            found.append(f"record {record['id']} is not indexed by its checksums")
        if given != sorted(given):
            found.append(f"record {record['id']} keeps its checksums in the order they came")
        if record["file"] is None:
            told = docs[record["doc"]]
            if not keys.get(record["doc"]):
                found.append(f"record {record['id']} of a file that is gone stays with a document known by nothing")
            elif refuted(db, told["size"], told["format"], sorted({*given, *content(record["doc"])}))[0]:
                found.append(f"record {record['id']} of a file that is gone is kept although a measure refutes it")
            continue
        if record["home"] != record["doc"]:
            found.append(f"record {record['id']} is not with the document of its file")
        mine = (record["sum"] or "").split()
        if against(given, mine) or refuted(db, record["size"], record["format"], sorted({*given, *mine}))[0]:
            found.append(f"record {record['id']} is kept although a measure refutes it")
    named = {row["rowid"]: (row["title"], row["byline"]) for row in db.execute("SELECT rowid,title,byline FROM names")}
    if set(named) != {file["id"] for file in files}:
        found.append("the names searched are not those of the files")
    for file in files:
        told = docs.get(file["doc"])
        if told is not None and file["id"] in named and named[file["id"]] != (told["title"] or "", told["byline"] or ""):
            found.append(f"file {file['rel']} is searched under the title {named[file['id']][0]!r}, which is not that of its document")
    return found


def canonical(db):
    """The documents that the files ought to form, from what is measured and said now, as a
    set of sets of file numbers."""
    files = db.execute("SELECT * FROM files ORDER BY id").fetchall()
    paged = {row["doc"] for row in db.execute("SELECT DISTINCT doc FROM pages")}
    blocks, loose = {}, {}
    for file in files:
        mine = kinds((file["sum"] or "").split())
        if mine:
            blocks.setdefault(("measured", min(mine["md5"]), file["format"]), set()).add(file["id"])
        elif file["read"] and file["doc"] in paged:
            blocks[("reader", file["id"])] = {file["id"]}
        else:
            told = said(db, file["id"])
            wrong, known = refuted(db, file["size"], file["format"], told)
            if wrong or not told:
                blocks[("alone", file["id"])] = {file["id"]}
            elif known is not None:
                md5 = min(kinds(measured(db, known))["md5"])
                blocks.setdefault(("measured", md5, file["format"]), set()).add(file["id"])
            else:
                loose[file["id"]] = {(key, file["format"], file["size"]) for key in told}
    # The files that words alone hold together: those that share a checksum said of them.
    leader = {file: file for file in loose}

    def find(file):
        while leader[file] != file:
            leader[file] = leader[leader[file]]
            file = leader[file]
        return file

    owner = {}
    for file, names in loose.items():
        for name in names:
            if name in owner:
                leader[find(file)] = find(owner[name])
            else:
                owner[name] = file
    for file in loose:
        blocks.setdefault(("said", find(file)), set()).add(file)
    return {frozenset(block) for block in blocks.values()}


def actual(db):
    """The documents that the files do form."""
    formed = {}
    for file in db.execute("SELECT id,doc FROM files"):
        formed.setdefault(file["doc"], set()).add(file["id"])
    return {frozenset(block) for block in formed.values()}
