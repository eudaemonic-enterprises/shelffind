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


def document(db, name, row, only=None):
    """The document of the folders that a result of the source `name` stands for, or None.

    The checksums of the result come first, all of them together and whatever their order:
    they are those of the bytes the source holds now, while its reference may have been given
    to a file of the folders that held other bytes. The reference serves when no checksum is
    known here. The same work, or the same bytes, may be kept under several formats, each a
    document of its own: the one of the format of the result is preferred, then one known by a
    measure, then one that has a file. With `only`, the format the search is limited to, no
    document of another format stands for the result."""
    wanted = str(row.get("format") or "").lower().lstrip(".")
    only = str(only).lower().lstrip(".") if only else None
    filed = "EXISTS (SELECT 1 FROM files f WHERE f.doc=d.id) AS filed"

    def best(found):
        found = [one for one in found if only is None or one["format"] == only]
        if not found:
            return None
        return min(found, key=lambda one: (one["format"] != wanted, not one["measured"], not one["filed"], one["doc"]))["doc"]

    found = []
    for key in row.get("keys") or ():
        found += db.execute(f"SELECT k.doc,d.format,k.basis='content' AS measured,{filed} FROM doc_keys k "
                            "JOIN docs d ON d.id=k.doc WHERE k.key>? AND k.key<?", (str(key) + "@", str(key) + "A")).fetchall()
    known = best(found)
    if known is None:
        known = best(db.execute(f"SELECT r.doc,d.format,0 AS measured,{filed} FROM records r JOIN docs d ON d.id=r.doc "
                                "WHERE r.source=? AND r.ref=?", (name, row["id"])).fetchall())
    return known


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


def fuse(db, groups, limit, only=None):
    """Merge lists of results, given as (name of the source, rows) with the files first, into
    one list with one row per document. `only` is the format the search is limited to."""
    places = {root["id"]: root["path"] for root in store.roots(db)}
    merged, worth = {}, {}
    for name, rows in groups:
        for rank, found in enumerate(rows):
            if name == "files":
                key = ("files", found["doc"])
                merged.setdefault(key, dict(found))
            else:
                doc = document(db, name, found, only)
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
