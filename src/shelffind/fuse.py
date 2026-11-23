# Copyright 2026 Eudaemonic Enterprises Inc.
"""One document, one row: the results of the folders and of the sources merged into one list.

A result of a source and a file of the folders are the same document when an earlier
`identify` linked the file to the record, or when they share a checksum. The merged row keeps
the file of the folders, takes the title, the authors, the marks and the actions of the
record, and shows the passage of whichever found one.
"""
import os
from pathlib import Path

from . import store
from .search import COLUMNS, result

# The worth of a result is 1 / (WEIGHT + its rank in its own list); a document found by
# several lists adds its worths, and so rises above those found by one.
WEIGHT = 20


def document(db, name, row):
    """The document of the folders that a result of the source `name` stands for, or None."""
    known = db.execute("SELECT doc FROM records WHERE source=? AND ref=?", (name, row["id"])).fetchone()
    if known:
        return known[0]
    for key in row.get("keys") or ():
        known = db.execute("SELECT doc FROM doc_keys WHERE key=?", (key,)).fetchone()
        if known:
            return known[0]
    return None


def copies(db, doc, places):
    """The addresses of the files of a document, in the order they were found."""
    return [str(Path(places[r["root"]]) / r["rel"])
            for r in db.execute("SELECT root,rel FROM files WHERE doc=? ORDER BY id", (doc,)) if r["root"] in places]


def shelf_row(db, doc, kind, places):
    """A result for a document of the folders that only a source found; None when no file of
    the folders holds that document any more."""
    row = db.execute(f"SELECT {COLUMNS} FROM files f JOIN docs d ON d.id=f.doc WHERE f.doc=? ORDER BY f.id LIMIT 1",
                     (doc,)).fetchone()
    if row is None or row["place"] not in places:
        return None
    shown = result(row, places[row["place"]], kind)
    shown["source"] = "files"
    shown["copies"] = [path for path in copies(db, doc, places) if path != shown["path"]]
    return shown


def adopt(shown, name, found):
    """Give the row of a file what a source knows of the same document."""
    shown.setdefault("record", {"source": name, "id": found["id"], "actions": list(found.get("actions") or [])})
    for field in ("title", "byline", "year", "venue"):
        if not shown.get(field) and found.get(field):
            shown[field] = found[field]
    marks = sorted(set(shown.get("flags") or []) | set(found.get("flags") or []))
    if marks:
        shown["flags"] = marks
    if found.get("notes"):
        shown["notes"] = found["notes"]
    if found.get("path") and found["path"] != shown.get("path") and os.path.isfile(found["path"]):
        shown.setdefault("fast", found["path"])     # the copy of the source on this machine
    if shown.get("snippet") is None and found.get("snippet"):
        shown.update(page=found.get("page"), page_label=found.get("page_label"), section=found.get("section"),
                     snippet=found["snippet"], marks=found.get("marks") or [])


def fuse(db, groups, limit):
    """Merge lists of results, given as (name of the source, rows) with the files first, into
    one list with one row per document."""
    places = {root["id"]: root["path"] for root in store.roots(db)}
    merged, worth = {}, {}
    for name, rows in groups:
        for rank, found in enumerate(rows):
            if name == "files":
                key = ("files", found["doc"])
                merged.setdefault(key, dict(found))
            else:
                doc = document(db, name, found)
                key = ("files", doc)
                if doc is not None and key not in merged:
                    built = shelf_row(db, doc, found["match"], places)
                    if built is not None:
                        merged[key] = built
                if doc is not None and key in merged:
                    adopt(merged[key], name, found)
                else:
                    key = (name, found["id"])
                    merged.setdefault(key, dict(found))
            worth[key] = worth.get(key, 0) + 1 / (WEIGHT + rank)
    order = {key: index for index, key in enumerate(merged)}
    ranked = sorted(merged, key=lambda key: (-worth[key], order[key]))
    return [merged[key] for key in ranked[:limit]]
