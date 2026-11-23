# Copyright 2026 Eudaemonic Enterprises Inc.
"""Lists for whoever keeps the shelves in order: the documents kept in several places, the files
no source knows, and the files whose name does not show the work that a source says they are."""
from pathlib import Path

from . import fuzzy, store
from .extract import SUPPORTED


def places(db, root=None):
    """The folders to look at, as number -> address: one of them, or all."""
    return {row["id"]: row["path"] for row in store.roots(db, root)}


def copies(db, root=None):
    """The documents that several files hold, the largest first: each with its size, its title
    when a source told it, and the addresses of its files. With `root`, the documents that have
    at least one file there."""
    known = places(db)
    wanted = set(places(db, root))
    found = []
    for doc in db.execute("SELECT d.id,d.size,d.title FROM docs d WHERE (SELECT count(*) FROM files f "
                          "WHERE f.doc=d.id)>1 ORDER BY d.size DESC,d.id"):
        files = db.execute("SELECT root,rel FROM files WHERE doc=? ORDER BY id", (doc["id"],)).fetchall()
        if any(row["root"] in wanted for row in files):
            found.append({"size": doc["size"], "title": doc["title"] or None,
                          "paths": [str(Path(known[row["root"]]) / row["rel"]) for row in files if row["root"] in known]})
    return found


def unlinked(db, root=None, source=None):
    """The files of readable formats that no source recognised (or that `source` did not), in
    the order of their addresses: what a library of full texts does not hold yet."""
    wanted = places(db, root)
    only = " AND r.source=?" if source else ""
    found = []
    for row in db.execute(
            "SELECT f.root,f.rel,f.size,f.format,d.state FROM files f JOIN docs d ON d.id=f.doc WHERE NOT EXISTS "
            f"(SELECT 1 FROM records r WHERE r.doc=f.doc AND r.ref!=''{only}) ORDER BY f.root,f.rel",
            (source,) if source else ()):
        if row["root"] in wanted and row["format"] in SUPPORTED:
            found.append({"path": str(Path(wanted[row["root"]]) / row["rel"]), "size": row["size"], "state": row["state"]})
    return found


def names(db, root=None):
    """The files whose name does not show the work a source says they are: for each, what the
    name lacks among the author, the title and the year, and what the source says."""
    wanted = places(db, root)
    found = []
    for row in db.execute("SELECT f.root,f.rel,f.name,d.title,d.byline,d.year FROM files f JOIN docs d ON d.id=f.doc "
                          "WHERE d.title IS NOT NULL AND d.title!='' ORDER BY f.root,f.rel"):
        if row["root"] not in wanted:
            continue
        shown = fuzzy.fold(Path(row["name"]).stem)          # a name may run its words together
        authors = [word for word in fuzzy.words(row["byline"] or "") if len(word) >= 3 and word.isalpha()]
        title = [word for word in fuzzy.words(row["title"]) if len(word) >= 5]
        year = "".join(char for char in str(row["year"] or "") if char.isdigit())[:4]
        lacks = []
        if authors and not any(word in shown for word in authors):
            lacks.append("author")
        if title and not any(word in shown for word in title):
            lacks.append("title")
        if len(year) == 4 and year not in shown:
            lacks.append("year")
        if lacks:
            found.append({"path": str(Path(wanted[row["root"]]) / row["rel"]), "lacks": lacks, "title": row["title"],
                          "byline": row["byline"] or None, "year": row["year"] or None})
    return found
