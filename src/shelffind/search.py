# Copyright 2026 Eudaemonic Enterprises Inc.
"""Rank filename, folder, abbreviated name, and full-text matches. One result per document."""
import re
from pathlib import Path

from . import fuzzy, store

# Marks that surround a matched word inside a snippet while it travels through SQLite; they
# are removed before the snippet leaves this module, which reports the matched ranges instead.
OPEN, CLOSE = "\x02", "\x03"
# Passages are ranked exactly when at most this many pages match. Beyond it the words are too
# common to rank in good time: a sample of the matching pages is ranked instead.
SAMPLE = 4000


def query_terms(query):
    parts = []
    for match in re.finditer(r'"([^"]*)(?:"|$)|([^"\s]+)', query):
        literal = match.group(1) is not None
        tokens = fuzzy.words(match.group(1) if literal else match.group(2))
        if tokens:
            parts.append((" ".join(tokens), literal))
    if len(parts) > 24 or any(len(t) > 256 for t, _ in parts):
        raise ValueError("Use a shorter query")
    return parts


def expression(db, parts, expand, exact):
    clauses = []
    for term, literal in parts:
        suffix = "" if exact or literal else "*"
        group = [f'"{term}"' + suffix]
        if expand and not literal and " " not in term:
            group.extend(f'"{alt}"' for alt in fuzzy.spellings(db, term))
        clauses.append("(" + " OR ".join(group) + ")")
    return " AND ".join(clauses)


def unmark(text):
    """Split a marked snippet into its plain text and the [start, end) ranges of its matches."""
    plain, ranges, start = [], [], None
    length = 0
    for piece in re.split(f"([{OPEN}{CLOSE}])", text):
        if piece == OPEN:
            start = length
        elif piece == CLOSE:
            if start is not None and length > start:
                ranges.append([start, length])
            start = None
        else:
            plain.append(piece)
            length += len(piece)
    return "".join(plain), ranges


def folded_map(text):
    """The folded form of text, with the position in text that each folded character came from."""
    folded, origin = [], []
    for index, char in enumerate(text):
        for piece in fuzzy.fold(char):
            folded.append(piece)
            origin.append(index)
    return "".join(folded), origin


def spans(text, query, kind=""):
    """Ranges [start, end) of `text` that the words of `query` account for: the words themselves
    where they occur, else the closest word of the name, else the letters of an abbreviation."""
    folded, origin = folded_map(text)
    found = []

    def keep(start, end):
        if end > start:
            found.append([origin[start], origin[end - 1] + 1])

    # The words of the name, a name in CamelCase being cut where a capital begins a word.
    pieces = [(m.start(), m.end()) for m in re.finditer(
        r"[^\W\d_]?[^\W\d_A-Z]+|[A-Z]+(?![^\W\d_A-Z])|\d+", text)]
    for term, _ in query_terms(query):
        for word in term.split():
            hits = [m.start() for m in re.finditer(re.escape(word), folded)]
            for start in hits:
                keep(start, start + len(word))
            if hits or len(word) < 3:
                continue
            bound = 1 if len(word) < 7 else 2
            close = [(fuzzy.distance(word, fuzzy.fold(text[a:b]), bound), a, b) for a, b in pieces]
            close = [c for c in close if c[0] <= bound]
            if close:
                _, a, b = min(close)
                found.append([a, b])
    if not found and "abbreviated" in kind:
        position = 0
        for char in "".join(fuzzy.words(query)):
            position = folded.find(char, position)
            if position < 0:
                break
            keep(position, position + 1)
            position += 1
    merged = []
    for start, end in sorted(found):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def document_id(db, hit):
    """The document of a result, or None when the result is not a file of the folders."""
    if hit.get("doc"):
        return hit["doc"]
    if not hit.get("root") or not hit.get("path"):
        return None
    for root in store.roots(db):
        if root["path"] == hit["root"]:
            found = db.execute("SELECT doc FROM files WHERE root=? AND rel=?",
                               (root["id"], str(Path(hit["path"]).relative_to(hit["root"])))).fetchone()
            return found["doc"] if found else None
    return None


def passages_of(db, hit, query, exact=False, limit=200):
    """The passages of one document that match `query`, in the order of its pages. Each is a
    dict with part (its rank among the pages, from 1), page, section, snippet and marks. With no
    words in the query, the beginning of every page is listed instead."""
    doc = document_id(db, hit)
    if doc is None:
        return []
    first = db.execute("SELECT min(id),max(id) FROM pages WHERE doc=?", (doc,)).fetchone()
    if first[0] is None:
        return []
    order = "(SELECT count(*) FROM pages q WHERE q.doc=p.doc AND q.id<=p.id)"
    parts = query_terms(query)
    if not parts:
        rows = db.execute(f"SELECT {order} AS part,p.page,p.section,substr(sf_inflate(p.ztext),1,240) AS snippet "
                          "FROM pages p WHERE p.doc=? ORDER BY p.id LIMIT ?", (doc, limit)).fetchall()
    else:
        # The pages of a document follow one another in the index: the search is kept to them.
        sql = (f"SELECT {order} AS part,p.page,p.section,"
               f"snippet(passages,0,'{OPEN}','{CLOSE}',' … ',28) AS snippet FROM passages "
               "JOIN pages p ON p.id=passages.rowid WHERE passages MATCH ? AND passages.rowid BETWEEN ? AND ? "
               "AND p.doc=? ORDER BY p.id LIMIT ?")
        rows = db.execute(sql, (expression(db, parts, False, exact), first[0], first[1], doc, limit)).fetchall()
        if not rows and not exact:
            rows = db.execute(sql, (expression(db, parts, True, False), first[0], first[1], doc, limit)).fetchall()
    found = []
    for row in rows:
        snippet, marks = unmark(" ".join(row["snippet"].split()))
        found.append({"part": row["part"], "page": row["page"], "section": row["section"],
                      "snippet": snippet, "marks": marks})
    return found


def page_of(db, hit, part):
    """The text of the `part`-th page of a document (from 1), with its page number, its section
    and the number of parts; None when there is no such part."""
    doc = document_id(db, hit)
    if doc is None or part < 1:
        return None
    row = db.execute("SELECT page,section,sf_inflate(ztext) AS text FROM pages WHERE doc=? ORDER BY id "
                     "LIMIT 1 OFFSET ?", (doc, part - 1)).fetchone()
    if row is None:
        return None
    total = db.execute("SELECT count(*) FROM pages WHERE doc=?", (doc,)).fetchone()[0]
    return {"part": part, "page": row["page"], "section": row["section"], "text": row["text"], "parts": total}


COLUMNS = "f.id,f.doc,f.root AS place,f.name,f.format,f.rel,f.size,f.mtime,d.title,d.byline,d.year"
FILES = "files f JOIN docs d ON d.id=f.doc"


def containing(text):
    """A LIKE pattern that finds `text` anywhere, its own % and _ taken literally."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search(db, query, *, root=None, fmt=None, limit=25, exact=False, where="all", folder=None, without=()):
    """Rank the documents that match `query`, one result per document: a document with several
    copies is shown through the copy that matched best, and names the others. `folder` keeps the
    files whose path contains it, and `without` drops those whose name or path contains one of
    its words."""
    if not 1 <= limit <= 500:
        raise ValueError("Choose a limit from 1 to 500")
    places = {row["id"]: row["path"] for row in store.roots(db)}
    filters, bindings = "", []
    if root:
        filters += " AND f.root=?"
        bindings.append(store.roots(db, root)[0]["id"])
    if fmt:
        filters += " AND f.format=?"
        bindings.append(fmt.lower().lstrip("."))
    if folder:
        filters += " AND lower(f.rel) LIKE ? ESCAPE '\\'"
        bindings.append(containing(folder.lower()))
    for word in without:
        filters += " AND lower(f.rel) NOT LIKE ? ESCAPE '\\' AND f.folded NOT LIKE ? ESCAPE '\\'"
        bindings += [containing(word.lower()), containing(fuzzy.fold(word))]
    parts = query_terms(query)
    found = {}

    def add(row, kind, score):
        """Returns the result of the document of `row`, created or improved by this match."""
        place = places[row["place"]]
        path = str(Path(place) / row["rel"])
        entry = found.get(row["doc"])
        if entry is None:
            item = {'path': path, 'root': place, 'name': row["name"], 'format': row["format"],
                    'match': kind, 'page': None, 'section': None, 'snippet': None, 'marks': [],
                    'size': row["size"], 'mtime': row["mtime"], 'doc': row["doc"], 'copies': [],
                    'title': row["title"], 'byline': row["byline"], 'year': row["year"]}
            found[row["doc"]] = (score, item)
            return item
        item = entry[1]
        if score < entry[0]:
            item["match"] = kind
            found[row["doc"]] = (score, item)
        return item

    def passages(expr, approximate):
        """Add the documents whose text matches: ranked exactly, or through a sample when the
        words are too common."""
        probe = [r[0] for r in db.execute("SELECT rowid FROM passages WHERE passages MATCH ? LIMIT ?",
                                          (expr, SAMPLE + 1))]
        if not probe:
            return
        wanted = max(200, limit * 8)
        if len(probe) <= SAMPLE:
            ranked = [r[0] for r in db.execute(
                "SELECT rowid FROM passages WHERE passages MATCH ? ORDER BY rank LIMIT ?", (expr, wanted))]
        else:
            ranked = probe[:wanted]
        marks = ",".join("?" * len(ranked))
        rows = db.execute(f"SELECT p.id AS page_id,p.page,p.section,{COLUMNS} FROM pages p "
                          "JOIN files f ON f.doc=p.doc JOIN docs d ON d.id=f.doc "
                          f"WHERE p.id IN ({marks})" + filters, (*ranked, *bindings)).fetchall()
        position = {page: index for index, page in enumerate(ranked)}
        seen = {}
        for row in sorted(rows, key=lambda r: (position[r["page_id"]], r["id"])):
            if row["doc"] in seen:
                continue
            if len(seen) >= limit:
                break
            item = add(row, "fuzzy passage" if approximate else "passage",
                       (4 if approximate else 2) + len(seen) / (limit + 1))
            seen[row["doc"]] = True
            if item["snippet"] is None:
                text = db.execute(f"SELECT snippet(passages,0,'{OPEN}','{CLOSE}',' … ',18) FROM passages "
                                  "WHERE passages MATCH ? AND rowid=?", (expr, row["page_id"])).fetchone()
                snippet, lit = unmark(" ".join((text[0] if text else "").split()))
                item.update(snippet=snippet, marks=lit, page=row["page"], section=row["section"])

    if not parts:
        if query.strip():
            return []
        sql = f"SELECT {COLUMNS} FROM {FILES} WHERE 1" + filters
        for i, row in enumerate(db.execute(sql + " ORDER BY f.mtime DESC,f.id LIMIT ?", (*bindings, limit * 2))):
            add(row, "recent", i)
    else:
        def matches(expr, approximate):
            name_sql = (f"SELECT {COLUMNS} FROM names JOIN files f ON f.id=names.rowid "
                        "JOIN docs d ON d.id=f.doc WHERE names MATCH ?" + filters)
            if where != "text":
                for i, row in enumerate(db.execute(name_sql + " ORDER BY bm25(names,10.0,3.0,6.0,3.0) LIMIT ?",
                                                    (expr, *bindings, limit * 2))):
                    name = fuzzy.fold(row["name"])
                    label = "fuzzy name or path" if approximate else "name or path"
                    tier = 3 if approximate else 1
                    if name == fuzzy.fold(query) or fuzzy.fold(Path(row["name"]).stem) == fuzzy.fold(query):
                        tier = 0
                    add(row, label, tier + i / (2 * limit + 1))
            if where != "name":
                passages(expr, approximate)
        primary = expression(db, parts, False, exact)
        matches(primary, False)
        if not exact and len(found) < limit:
            expanded = expression(db, parts, True, False)
            if expanded != primary:
                matches(expanded, True)
            if where != "text" and len(parts) == 1 and not parts[0][1] and 2 <= len(parts[0][0]) <= 24:
                term = parts[0][0].replace(" ", "")
                pattern = "%" + "%".join(term) + "%"
                sql = (f"SELECT {COLUMNS} FROM {FILES} "
                       "WHERE f.folded LIKE ?" + filters + " ORDER BY length(f.name),f.id LIMIT ?")
                for i, row in enumerate(db.execute(sql, (pattern, *bindings, limit * 2))):
                    add(row, "abbreviated name", 5 + i / (2 * limit + 1))
    order = lambda entry: (entry[0], entry[1]["path"])
    ranked = sorted(found.values(), key=order)
    if where == "all" and len(ranked) > limit:
        # Names would fill the list alone: a third of it is kept for passages, which no key
        # could reach otherwise.
        texts = [entry for entry in ranked if entry[1]["match"].endswith("passage")]
        names = [entry for entry in ranked if not entry[1]["match"].endswith("passage")]
        names = names[:limit - min(len(texts), limit // 3)]
        ranked = sorted(names + texts[:limit - len(names)], key=order)
    results = [item for _, item in ranked[:limit]]
    for item in results:
        others = db.execute("SELECT root,rel FROM files WHERE doc=? ORDER BY id", (item["doc"],))
        item["copies"] = [path for path in (str(Path(places[r["root"]]) / r["rel"]) for r in others)
                          if path != item["path"]]
    return results


def by_paths(db, paths, kind="opened"):
    """Results for the files at the given addresses that the index still knows, in that order."""
    places = store.roots(db)
    found, seen = [], set()
    for path in paths:
        for root in places:
            if path.startswith(root["path"].rstrip("/") + "/"):
                row = db.execute(f"SELECT {COLUMNS} FROM {FILES} WHERE f.root=? AND f.rel=?",
                                 (root["id"], path[len(root["path"].rstrip("/")) + 1:])).fetchone()
                if row and row["doc"] not in seen:
                    seen.add(row["doc"])
                    found.append({'path': path, 'root': root["path"], 'name': row["name"],
                                  'format': row["format"], 'match': kind, 'page': None, 'section': None,
                                  'snippet': None, 'marks': [], 'size': row["size"], 'mtime': row["mtime"],
                                  'doc': row["doc"], 'copies': [], 'title': row["title"],
                                  'byline': row["byline"], 'year': row["year"]})
                break
    return found
