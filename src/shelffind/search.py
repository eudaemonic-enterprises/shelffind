# Copyright 2026 Eudaemonic Enterprises Inc.
"""Rank filename, folder, abbreviated name, and full-text matches."""
import re
from pathlib import Path

from . import fuzzy, store


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


COLUMNS = "f.id,f.name,f.format,f.rel,r.path AS root"


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
                        'section': None, 'snippet': None}
            found[key] = (score, item)
        elif score < item[0]:
            item = item[1]
            item["match"] = kind
            found[key] = (score, item)
        else:
            item = item[1]
        if kind.endswith("passage") and item["snippet"] is None:
            item.update(snippet=" ".join(row["snippet"].split()), page=row["page"], section=row["section"])

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
            if where == "name" or (where == "all" and len(found) >= limit):
                return
            text_sql = (f"SELECT {COLUMNS},p.page,p.section,snippet(passages,0,'','',' … ',18) AS snippet "
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
    return [item for _, item in sorted(found.values(), key=lambda v: (v[0], v[1]["path"]))[:limit]]
