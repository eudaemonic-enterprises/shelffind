# Copyright 2026 Eudaemonic Enterprises Inc.
"""Rank filename, folder, abbreviated name, and full-text matches."""
import re
from pathlib import Path

from . import fuzzy, store

# Marks that surround a matched word inside a snippet while it travels through SQLite; they
# are removed before the snippet leaves this module, which reports the matched ranges instead.
OPEN, CLOSE = "\x02", "\x03"


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

    words = [(m.start(), m.end()) for m in re.finditer(r"[^\W_]+", folded)]
    for term, _ in query_terms(query):
        for word in term.split():
            hits = [m.start() for m in re.finditer(re.escape(word), folded)]
            for start in hits:
                keep(start, start + len(word))
            if hits or len(word) < 3:
                continue
            bound = 1 if len(word) < 7 else 2
            close = [(fuzzy.distance(word, folded[a:b], bound), a, b) for a, b in words]
            close = [c for c in close if c[0] <= bound]
            if close:
                _, a, b = min(close)
                keep(a, b)
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


COLUMNS = "f.id,f.name,f.format,f.rel,f.size,f.mtime,r.path AS root"


def search(db, query, *, root=None, fmt=None, limit=25, exact=False, where="all"):
    if not 1 <= limit <= 500:
        raise ValueError("Choose a limit from 1 to 500")
    filters, bindings = "", []
    if root:
        filters += " AND f.root=?"
        bindings.append(store.roots(db, root)[0]["id"])
    if fmt:
        filters += " AND f.format=?"
        bindings.append(fmt.lower().lstrip("."))
    parts = query_terms(query)
    found = {}
    def add(row, kind, score):
        key = row["id"]
        item = found.get(key)
        if item is None:
            item = {'path': str(Path(row["root"]) / row["rel"]), 'root': row["root"],
                        'name': row["name"], 'format': row["format"], 'match': kind, 'page': None,
                        'section': None, 'snippet': None, 'marks': [], 'size': row["size"],
                        'mtime': row["mtime"]}
            found[key] = (score, item)
        elif score < item[0]:
            item = item[1]
            item["match"] = kind
            found[key] = (score, item)
        else:
            item = item[1]
        if kind.endswith("passage") and item["snippet"] is None:
            snippet, marks = unmark(" ".join(row["snippet"].split()))
            item.update(snippet=snippet, marks=marks, page=row["page"], section=row["section"])

    if not parts:
        if query.strip():
            return []
        sql = f"SELECT {COLUMNS} FROM files f JOIN roots r ON r.id=f.root WHERE 1" + filters
        for i, row in enumerate(db.execute(sql + " ORDER BY f.mtime DESC,f.id LIMIT ?", (*bindings, limit))):
            add(row, "recent", i)
    else:
        def matches(expr, approximate):
            name_sql = (f"SELECT {COLUMNS} FROM names JOIN files f ON f.id=names.rowid "
                        "JOIN roots r ON r.id=f.root WHERE names MATCH ?" + filters)
            if where != "text":
                for i, row in enumerate(db.execute(name_sql + " ORDER BY rank LIMIT ?",
                                                    (expr, *bindings, limit * 2))):
                    name = fuzzy.fold(row["name"])
                    label = "fuzzy name or path" if approximate else "name or path"
                    tier = 3 if approximate else 1
                    if name == fuzzy.fold(query) or fuzzy.fold(Path(row["name"]).stem) == fuzzy.fold(query):
                        tier = 0
                    add(row, label, tier + i / (2 * limit + 1))
            if where == "name":
                return
            text_sql = (f"SELECT {COLUMNS},p.page,p.section,"
                        f"snippet(passages,0,'{OPEN}','{CLOSE}',' … ',18) AS snippet "
                        "FROM passages JOIN pages p ON p.id=passages.rowid JOIN files f ON f.id=p.file "
                        "JOIN roots r ON r.id=f.root WHERE passages MATCH ?" + filters + " ORDER BY rank")
            seen = set()
            for row in db.execute(text_sql, (expr, *bindings)):
                if row["id"] in seen:
                    continue
                add(row, "fuzzy passage" if approximate else "passage",
                    (4 if approximate else 2) + len(seen) / (limit + 1))
                seen.add(row["id"])
                if len(seen) >= limit:
                    break
        primary = expression(db, parts, False, exact)
        matches(primary, False)
        if not exact and len(found) < limit:
            expanded = expression(db, parts, True, False)
            if expanded != primary:
                matches(expanded, True)
            if where != "text" and len(parts) == 1 and not parts[0][1] and 2 <= len(parts[0][0]) <= 24:
                term = parts[0][0].replace(" ", "")
                pattern = "%" + "%".join(term) + "%"
                sql = (f"SELECT {COLUMNS} FROM files f JOIN roots r ON r.id=f.root "
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
    return [item for _, item in ranked[:limit]]
