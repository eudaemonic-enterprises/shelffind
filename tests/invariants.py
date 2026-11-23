# Copyright 2026 Eudaemonic Enterprises Inc.
"""What must hold in an index at rest, whatever happened before.

`structure(db)` lists the breaches of the rules that tie files, documents, checksums, pages and
the statements of sources together; `canonical(db)` computes, from what is measured and said
now, the documents the files ought to form, to be compared with those they do form: the two
agree only when the placing of the files does not depend on the order of the events."""
from shelffind import store


def structure(db):
    """The breaches found, as sentences; an empty list when the index is sound."""
    found = []
    docs = {row["id"]: row for row in db.execute("SELECT * FROM docs")}
    files = db.execute("SELECT * FROM files ORDER BY id").fetchall()
    members, paged = {}, {row["doc"] for row in db.execute("SELECT DISTINCT doc FROM pages")}
    for file in files:
        members.setdefault(file["doc"], []).append(file)
    keys = {}
    for row in db.execute("SELECT key,doc,basis FROM doc_keys"):
        keys.setdefault(row["doc"], []).append((row["key"], row["basis"]))
    content = lambda doc: [key.split("@")[0] for key, basis in keys.get(doc, ()) if basis == "content"]
    source = lambda doc: [key for key, basis in keys.get(doc, ()) if basis == "source"]

    for broken in db.execute("PRAGMA foreign_key_check").fetchall():
        found.append(f"a reference to nothing: {tuple(broken)}")
    for doc, held in keys.items():
        if doc not in docs:
            found.append(f"keys of a document that is not there: {doc}")
            continue
        kinds = store.kinds(content(doc))
        if any(len(values) > 1 for values in kinds.values()):
            found.append(f"document {doc} is known by two checksums of one kind: {sorted(content(doc))}")
        if source(doc) and (content(doc) or doc in paged):
            found.append(f"document {doc} holds checksums on a word beside measured ones or pages")
        for key in source(doc):
            if not key.endswith(f"@{docs[doc]['format']}#{docs[doc]['size']}"):
                found.append(f"document {doc} holds on a word a checksum of another size or format: {key}")
    for file in files:
        doc = docs.get(file["doc"])
        if doc is None:
            found.append(f"file {file['rel']} is in no document")
            continue
        if (doc["size"], doc["format"]) != (file["size"], file["format"]):
            found.append(f"file {file['rel']} is in a document of another size or format")
        for key in (file["sum"] or "").split():
            if store.holder(db, key, file["format"]) != file["doc"]:
                found.append(f"file {file['rel']} was measured {key} and its document is not the one known by it")
        if file["read"] and file["doc"] not in paged:
            found.append(f"file {file['rel']} is marked as read and its document has no pages")
    for doc in paged:
        told, inside = docs[doc], members.get(doc, [])
        readers = [file for file in inside if file["read"]]
        if len(readers) > 1:
            found.append(f"document {doc} was read from two files")
        if told["origin"] is None:
            if len(inside) != 1 or not readers or inside[0]["sum"] or content(doc):
                found.append(f"document {doc} has pages that nothing ties to bytes, and is not one unmeasured file")
        elif "sha256:" not in told["origin"] or not set(told["origin"].split()) <= set(content(doc)):
            found.append(f"document {doc} has pages of bytes it is not known by a reading to hold: {told['origin']}")
    for doc, told in docs.items():
        if doc in members:
            continue
        if not (doc in paged or db.execute("SELECT 1 FROM records WHERE doc=? LIMIT 1", (doc,)).fetchone()):
            found.append(f"document {doc} has no file, no pages and no record")
        if told["gone"] is None:
            found.append(f"document {doc} has no file and is not marked as gone")
    for file in files:
        if file["sum"] or (file["read"] and file["doc"] in paged):
            continue
        said = store.said(db, file["id"])
        wrong, known = store.refuted(db, file["size"], file["format"], said)
        alone = len(members[file["doc"]]) == 1 and not keys.get(file["doc"]) and file["doc"] not in paged
        names = {f"{key}@{file['format']}#{file['size']}" for key in said}
        if wrong or not said:
            if not alone:
                found.append(f"file {file['rel']}, of which nothing usable is said, is not alone in a document without keys")
        elif known is not None:
            if file["doc"] != known:
                found.append(f"file {file['rel']} is said to be document {known} and is in {file['doc']}")
        elif not names <= set(source(file["doc"])):
            found.append(f"file {file['rel']} is not in the document known by what is said of it")
    for doc, inside in members.items():
        if not source(doc):
            continue
        said = set()
        for file in inside:
            said |= {f"{key}@{file['format']}#{file['size']}" for key in store.said(db, file["id"])}
        if set(source(doc)) != said:
            found.append(f"document {doc} holds on a word checksums that no file of it is said to have")
        if db.execute("SELECT 1 FROM records WHERE doc=? AND file IS NULL LIMIT 1", (doc,)).fetchone():
            found.append(f"document {doc}, held together by words, keeps what was said of a file that is gone")
    for record in db.execute("SELECT r.*,f.sum,f.size,f.format,f.doc AS home FROM records r LEFT JOIN files f ON f.id=r.file").fetchall():
        given = record["keys"].split()
        indexed = sorted(row[0] for row in db.execute("SELECT key FROM record_keys WHERE record=?", (record["id"],)))
        if indexed != sorted(set(given)):
            found.append(f"record {record['id']} is not indexed by its checksums")
        if record["file"] is None:
            if not keys.get(record["doc"]):
                found.append(f"record {record['id']} of a file that is gone stays with a document known by nothing")
            continue
        if record["home"] != record["doc"]:
            found.append(f"record {record['id']} is not with the document of its file")
        mine = (record["sum"] or "").split()
        if store.against(given, mine) or (mine and store.refuted(db, record["size"], record["format"], sorted({*given, *mine}))[0]):
            found.append(f"record {record['id']} is kept although a measure refutes it")
    if db.execute("SELECT count(*) FROM names").fetchone()[0] != len(files):
        found.append("the names searched are not those of the files")
    return found


def canonical(db):
    """The documents that the files ought to form, from what is measured and said now, as a
    set of sets of file numbers."""
    files = db.execute("SELECT * FROM files ORDER BY id").fetchall()
    paged = {row["doc"] for row in db.execute("SELECT DISTINCT doc FROM pages")}
    blocks, loose = {}, {}
    for file in files:
        mine = store.sums(file["sum"])
        if mine:
            blocks.setdefault(("measured", mine["md5"], file["format"]), set()).add(file["id"])
        elif file["read"] and file["doc"] in paged:
            blocks[("reader", file["id"])] = {file["id"]}
        else:
            said = store.said(db, file["id"])
            wrong, known = store.refuted(db, file["size"], file["format"], said)
            if wrong or not said:
                blocks[("alone", file["id"])] = {file["id"]}
            elif known is not None:
                md5 = store.sums(" ".join(store.keys_of(db, known, "content")))["md5"]
                blocks.setdefault(("measured", md5, file["format"]), set()).add(file["id"])
            else:
                loose[file["id"]] = {f"{key}@{file['format']}#{file['size']}" for key in said}
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
