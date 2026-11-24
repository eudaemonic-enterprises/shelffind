# Copyright 2026 Eudaemonic Enterprises Inc.
"""Rank filename, folder, abbreviated name, and full-text matches. One result per document."""
import itertools
import re
from pathlib import Path

from . import extract, fuzzy, store

# Passages are ranked exactly when at most this many pages match. Beyond it the words are too
# common for that to be done in good time: pages taken at regular intervals across the whole
# index are ranked instead.
SAMPLE = 20_000
SPREAD = 2_000          # the number of pages such a sample aims at
# A query whose words are shared between the name of a document and its text is answered from the
# documents that the names give: at most this many are looked into for all the ways of sharing
# together, the ways that give the name more words first, so that a long query stays quick.
SHARED = 120
# What the names give for one way of sharing is read to this many documents at most.
LISTED = 300
# What is a document, as against the programs, pictures and data that some folders hold: among
# matches of the same kind, documents are listed first.
DOCUMENTS = frozenset("pdf djvu ps ps.gz epub txt md rst tex bib typ doc docx odt rtf html htm fb2 mobi azw3 chm "
                      "ppt pptx".split())
SHARES = 5              # beyond this many words, only one word, or all but one, goes to the name
MOST = 8                # a query of more words than this is not shared between the name and the text
# In the text only the last word of a query may be the beginning of a longer one, and only
# from this many letters: completing every short word of a large collection costs seconds.
PREFIX = 4
WORD = re.compile(r"[^\W_]+(?:[̀-ͯ]+[^\W_]*)*")


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


def variants(db, parts, expand, exact=False):
    """For each part of a query, the known words close to it when `expand` asks for them."""
    return [fuzzy.spellings(db, term) if expand and not exact and not literal and " " not in term else []
            for term, literal in parts]


def open_ended(parts, index, exact, text):
    """Whether part `index` of a query may be the beginning of a longer word."""
    term, literal = parts[index]
    if exact or literal:
        return False
    return not text or (index == len(parts) - 1 and len(term.split()[-1]) >= PREFIX)


def clause(parts, close, index, exact, text=False):
    """The full-text expression of one part of a query: the part itself, or a word close to it."""
    group = [f'"{parts[index][0]}"' + ("*" if open_ended(parts, index, exact, text) else "")]
    group.extend(f'"{other}"' for other in close[index])
    return "(" + " OR ".join(group) + ")"


def expression(parts, close, exact, text=False):
    """The full-text expression of a query: every part must be met, by itself or by one of the
    words close to it. `text` chooses the rule of the passages for unfinished words."""
    return " AND ".join(clause(parts, close, index, exact, text) for index in range(len(parts)))


def shares(count):
    """The ways of sharing the parts of a query, numbered from 0, between the name of a document
    (the parts of the first set) and its text (the others): both sets are never empty, and the
    ways that give the name more parts come first."""
    if count < 2:
        return []
    indexes = range(count)
    if count <= SHARES:
        names = [chosen for size in range(count - 1, 0, -1) for chosen in itertools.combinations(indexes, size)]
    else:
        names = [tuple(other for other in indexes if other != index) for index in indexes] + [(index,) for index in indexes]
    return [(chosen, tuple(index for index in indexes if index not in chosen)) for chosen in names]


def targets(parts, close, exact):
    """What a page is searched for, part by part: sequences of words, each with the mark that
    its last word may be longer."""
    return [[(tuple(term.split()), open_ended(parts, index, exact, True))] + [((other,), False) for other in others]
            for index, ((term, _), others) in enumerate(zip(parts, close))]


def occurrences(text, wanted):
    """Where the parts of a query occur in `text`: (start, end, part), in the order of the text."""
    words = []
    for match in WORD.finditer(text):
        word = match.group()
        words.append((match.start(), match.end(), word.lower() if word.isascii() else fuzzy.fold(word)))
    first, loose = {}, []
    for part, options in enumerate(wanted):
        for sequence, longer in options:
            if longer and len(sequence) == 1:
                loose.append((sequence[0], part))
            else:
                first.setdefault(sequence[0], []).append((sequence, longer, part))
    found = []
    for index, (start, end, word) in enumerate(words):
        for beginning, part in loose:
            if word.startswith(beginning):
                found.append((start, end, part))
        for sequence, longer, part in first.get(word, ()):
            last = index + len(sequence) - 1
            if last >= len(words) or any(words[index + k][2] != sequence[k] for k in range(1, len(sequence) - 1)):
                continue
            closing = words[last][2]
            if closing.startswith(sequence[-1]) if longer else closing == sequence[-1]:
                found.append((start, words[last][1], part))
    return found


def one_line(text, start, end, ranges):
    """`text[start:end]` on one line, its runs of blanks reduced to one space, with `ranges`
    (in the positions of `text`) moved to the positions of the result."""
    out, where = [], {}
    blank = True
    for position in range(start, end):
        char = text[position]
        where[position] = len(out)
        if char.isspace():
            if not blank:
                out.append(" ")
            blank = True
        else:
            out.append(char)
            blank = False
    where[end] = len(out)
    line = "".join(out).rstrip()
    marks = [[where[a], min(where[b], len(line))] for a, b in ranges if start <= a and b <= end]
    return line, [mark for mark in marks if mark[1] > mark[0]]


def excerpt(text, wanted, width=120):
    """A stretch of about `width` characters of a page, chosen where the most parts of the query
    meet, with the ranges of the matched words in it. Without a match, the page's beginning."""
    found = occurrences(text, wanted) if wanted else []
    start = 0
    if found:
        # The window that begins at an occurrence and holds the most distinct parts; the first
        # such window wins.
        best, inside, counts, right = 0, 0, {}, 0
        for left, (begin, _, _) in enumerate(found):
            while right < len(found) and found[right][1] <= begin + width:
                counts[found[right][2]] = counts.get(found[right][2], 0) + 1
                right += 1
            if len(counts) > inside:
                best, inside = left, len(counts)
            part = found[left][2]
            if right > left:
                counts[part] -= 1
                if not counts[part]:
                    del counts[part]
            else:
                right = left + 1
        begin = found[best][0]
        reach = max(end for a, end, _ in found if begin <= a and end <= begin + width) if inside else found[best][1]
        start = max(0, begin - max(12, (width - (reach - begin)) // 2))
    end = min(len(text), start + width)
    # Words are not cut: each end moves to the nearest blank, within a few letters.
    if start > 0:
        blank = max(text.rfind(" ", max(0, start - 16), start + 1), text.rfind("\n", max(0, start - 16), start + 1))
        if blank >= 0:
            start = blank + 1
        else:
            ahead = re.search(r"\s", text[start:start + 16])
            start += ahead.end() if ahead else 0
        if found:
            start = min(start, found[best][0])
    if end < len(text):
        ahead = re.search(r"\s", text[end:end + 16])
        if ahead:
            end += ahead.start()
        else:
            back = max(text.rfind(" ", start, end), text.rfind("\n", start, end))
            end = back if back > start + width // 2 else end
    line, marks = one_line(text, start, end, [(a, b) for a, b, _ in found])
    lead = "… " if start > 0 and line else ""
    line = lead + line + (" …" if end < len(text) and line else "")
    merged = []
    for a, b in sorted([a + len(lead), b + len(lead)] for a, b in marks):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return line, merged


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
    first, last = db.execute("SELECT min(id),max(id) FROM pages WHERE doc=?", (doc,)).fetchone()
    if first is None:
        return []
    order = "(SELECT count(*) FROM pages q WHERE q.doc=p.doc AND q.id<=p.id)"
    parts = query_terms(query)
    wanted, chosen = [], None
    if parts:
        # The pages of a document follow one another in the index: the search is kept to them.
        for expand in (False, True):
            close = variants(db, parts, expand, exact)
            chosen = [row[0] for row in db.execute(
                "SELECT rowid FROM passages WHERE passages MATCH ? AND rowid BETWEEN ? AND ? LIMIT ?",
                (expression(parts, close, exact, text=True), first, last, limit))]
            wanted = targets(parts, close, exact)
            if chosen or exact:
                break
        if not chosen:
            return []
        marks = ",".join("?" * len(chosen))
        rows = db.execute(f"SELECT {order} AS part,p.page,p.section,p.ztext FROM pages p "
                          f"WHERE p.doc=? AND p.id IN ({marks}) ORDER BY p.id", (doc, *chosen)).fetchall()
    else:
        rows = db.execute(f"SELECT {order} AS part,p.page,p.section,p.ztext FROM pages p "
                          "WHERE p.doc=? ORDER BY p.id LIMIT ?", (doc, limit)).fetchall()
    found = []
    for row in rows:
        snippet, marks = excerpt(store.inflate(row["ztext"]), wanted, 190 if wanted else 240)
        found.append({"part": row["part"], "page": row["page"], "section": row["section"],
                      "snippet": snippet, "marks": marks})
    return found


def page_of(db, hit, part):
    """The text of the `part`-th page of a document (from 1), with its page number, its section
    and the number of parts; None when there is no such part."""
    doc = document_id(db, hit)
    if doc is None or part < 1:
        return None
    row = db.execute("SELECT page,section,ztext FROM pages WHERE doc=? ORDER BY id LIMIT 1 OFFSET ?",
                     (doc, part - 1)).fetchone()
    if row is None:
        return None
    total = db.execute("SELECT count(*) FROM pages WHERE doc=?", (doc,)).fetchone()[0]
    return {"part": part, "page": row["page"], "section": row["section"], "text": store.inflate(row["ztext"]),
            "parts": total}


COLUMNS = "f.id,f.doc,f.root AS place,f.name,f.format,f.rel,f.size,f.mtime,d.title,d.byline,d.year"
FILES = "files f JOIN docs d ON d.id=f.doc"


def containing(text):
    """A LIKE pattern that finds `text` anywhere, its own % and _ taken literally."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def result(row, place, kind):
    return {'path': str(Path(place) / row["rel"]), 'root': place, 'name': row["name"], 'format': row["format"],
            'match': kind, 'page': None, 'section': None, 'snippet': None, 'marks': [], 'size': row["size"],
            'mtime': row["mtime"], 'doc': row["doc"], 'copies': [], 'title': row["title"],
            'byline': row["byline"], 'year': row["year"]}


def scored_pages(db, expr):
    """The pages that match `expr` with their scores, the best first: all of them, or pages
    taken at regular intervals across the index when too many match."""
    scored = db.execute("SELECT rowid,bm25(passages) FROM passages WHERE passages MATCH ? LIMIT ?",
                        (expr, SAMPLE + 1)).fetchall()
    if len(scored) > SAMPLE:
        # They come in the order of the index: their density tells how many there are in all.
        last = db.execute("SELECT max(id) FROM pages").fetchone()[0]
        covered = max(1, scored[-1][0] - scored[0][0] + 1)
        expected = len(scored) * (last - scored[0][0] + 1) / covered
        step = max(2, int(expected / SPREAD))
        spread = db.execute("SELECT rowid,bm25(passages) FROM passages WHERE passages MATCH ? AND rowid % ?=0 "
                            "LIMIT ?", (expr, step, SPREAD * 20)).fetchall()
        scored = spread or scored[:SPREAD]
    return sorted(scored, key=lambda row: (row[1], row[0]))


def search(db, query, *, root=None, fmt=None, limit=25, exact=False, where="all", folder=None, without=(),
           interim=None, often=None):
    """Rank the documents that match `query`, one result per document: a document with several
    copies is shown through the copy that matched best, and names the others. `folder` keeps the
    files whose path contains it, and `without` drops those whose name or path contains one of
    its words. `interim(results)` receives the matches by name as soon as they are known, while
    the passages are still being searched. `often` tells how many times each document was opened
    lately, as document -> number: among matches of the same kind, those opened often come first."""
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
    found, spots = {}, {}

    def add(row, kind, score):
        """The result of the document of `row`, created or improved by this match."""
        entry = found.get(row["doc"])
        if entry is None:
            item = result(row, places[row["place"]], kind)
            found[row["doc"]] = (score, item)
            return item
        item = entry[1]
        if score < entry[0]:
            item["match"] = kind
            found[row["doc"]] = (score, item)
        return item

    def by_name(expr, approximate):
        sql = (f"SELECT {COLUMNS} FROM names JOIN files f ON f.id=names.rowid JOIN docs d ON d.id=f.doc "
               "WHERE names MATCH ?" + filters + " ORDER BY bm25(names,10.0,3.0,6.0,3.0) LIMIT ?")
        for i, row in enumerate(db.execute(sql, (expr, *bindings, limit * 2))):
            name = fuzzy.fold(row["name"])
            tier = 3 if approximate else 1
            if name == fuzzy.fold(query) or fuzzy.fold(Path(row["name"]).stem) == fuzzy.fold(query):
                tier = 0
            add(row, "fuzzy name or path" if approximate else "name or path", tier + i / (2 * limit + 1))

    def by_text(expr, wanted, approximate):
        """Add the documents whose text matches, in the order of their best page."""
        seen = set()
        ranked = [row[0] for row in scored_pages(db, expr)]
        for start in range(0, len(ranked), 400):
            chunk = ranked[start:start + 400]
            position = {page: index for index, page in enumerate(chunk)}
            rows = db.execute(f"SELECT p.id AS page_id,p.page,p.section,{COLUMNS} FROM pages p "
                              "JOIN files f ON f.doc=p.doc JOIN docs d ON d.id=f.doc "
                              f"WHERE p.id IN ({','.join('?' * len(chunk))})" + filters, (*chunk, *bindings)).fetchall()
            for row in sorted(rows, key=lambda r: (position[r["page_id"]], r["id"])):
                if row["doc"] in seen:
                    continue
                seen.add(row["doc"])
                item = add(row, "fuzzy passage" if approximate else "passage",
                           (4 if approximate else 2) + len(seen) / (limit + 1))
                if row["doc"] not in spots:
                    spots[row["doc"]] = (row["page_id"], wanted)
                    item.update(page=row["page"], section=row["section"])
                if len(seen) >= limit:
                    return

    def by_both(close, approximate):
        """Add the documents whose name, folder, title or author holds some words of the query
        and whose text holds the others (`whitham dispersion`: one in the name, one in the page).
        They come before the documents found by their text alone, and after those found by
        their names, and the page shown is the one that holds the words asked of the text. Only
        documents that have text are looked into. The ways of sharing the words are tried the
        one with the fewest candidates first, each with its share of the budget of looks and
        what the others left, so that a word that is in many names cannot take all of it."""
        if len(parts) > MOST:
            return
        crossed, aimed, ways = set(), targets(parts, close, exact), []
        for chosen, rest in shares(len(parts)):
            names_expr = " AND ".join(clause(parts, close, index, exact) for index in chosen)
            text_expr = " AND ".join(clause(parts, close, index, exact, True) for index in rest)
            sql = (f"SELECT {COLUMNS} FROM names JOIN files f ON f.id=names.rowid JOIN docs d ON d.id=f.doc "
                   "WHERE names MATCH ? AND EXISTS (SELECT 1 FROM pages WHERE pages.doc=d.id)" + filters
                   + " ORDER BY bm25(names,10.0,3.0,6.0,3.0) LIMIT ?")
            rows = db.execute(sql, (names_expr, *bindings, LISTED)).fetchall()
            if rows:
                ways.append((len(rows), -len(chosen), rest, text_expr, rows))
        ways.sort(key=lambda way: way[:2])
        share, carry = max(10, SHARED // max(1, len(ways))), 0
        for _, _, rest, text_expr, rows in ways:
            if len(crossed) >= limit:
                break
            allowance, spent = share + carry, 0
            for row in rows:
                if row["doc"] in crossed or len(crossed) >= limit:
                    continue
                if spent >= allowance:
                    break
                spent += 1
                first, last = db.execute("SELECT min(id),max(id) FROM pages WHERE doc=?", (row["doc"],)).fetchone()
                if first is None:
                    continue
                page = db.execute("SELECT rowid FROM passages WHERE passages MATCH ? AND rowid BETWEEN ? AND ? "
                                  "ORDER BY bm25(passages) LIMIT 1", (text_expr, first, last)).fetchone()
                if page is None:
                    continue
                crossed.add(row["doc"])
                # Below one thousandth, so that they stay ahead of every document found by its text alone.
                item = add(row, "fuzzy name and passage" if approximate else "name and passage",
                           (4 if approximate else 2) + len(crossed) / (2 * limit + 1) / 1000)
                if row["doc"] not in spots:
                    found_page = db.execute("SELECT page,section FROM pages WHERE id=?", (page[0],)).fetchone()
                    spots[row["doc"]] = (page[0], [aimed[index] for index in rest])
                    item.update(page=found_page["page"], section=found_page["section"])
            carry = allowance - spent

    def ranked():
        """The results so far, the best first, with their copies and their passages."""
        def order(entry):
            # A document opened often moves up among the matches of its own kind, never past
            # a better kind of match.
            score, item = entry
            base = kind = int(score)
            plain = item["format"] not in DOCUMENTS and item["format"] not in extract.formats()
            # A program or a picture whose name is the query is not put before the documents that
            # have it in their names.
            if plain and base == 0 and item["match"] == "name or path":
                kind = 1
            return (kind, plain,
                    score - base - min(0.9, 0.3 * (often or {}).get(item["doc"], 0)), item["path"])
        listed = sorted(found.values(), key=order)
        if where == "all" and len(listed) > limit:
            # Names would fill the list alone: a third of it is kept for passages, which no key
            # could reach otherwise.
            texts = [entry for entry in listed if entry[1]["match"].endswith("passage")]
            names = [entry for entry in listed if not entry[1]["match"].endswith("passage")]
            names = names[:limit - min(len(texts), limit // 3)]
            listed = sorted(names + texts[:limit - len(names)], key=order)
        results = [dict(item) for _, item in listed[:limit]]
        for item in results:
            others = db.execute("SELECT root,rel FROM files WHERE doc=? ORDER BY id", (item["doc"],))
            item["copies"] = [path for path in (str(Path(places[r["root"]]) / r["rel"]) for r in others)
                              if path != item["path"]]
            if item["doc"] in spots and item["snippet"] is None:
                page, wanted = spots[item["doc"]]
                blob = db.execute("SELECT ztext FROM pages WHERE id=?", (page,)).fetchone()
                item["snippet"], item["marks"] = excerpt(store.inflate(blob[0]) if blob else "", wanted)
        return results

    if not parts:
        if query.strip():
            return []
        sql = f"SELECT {COLUMNS} FROM {FILES} WHERE 1" + filters
        for i, row in enumerate(db.execute(sql + " ORDER BY f.mtime DESC,f.id LIMIT ?", (*bindings, limit * 2))):
            add(row, "recent", i)
        return ranked()
    for approximate in (False, True):
        if approximate and (exact or len(found) >= limit):
            break
        close = variants(db, parts, approximate, exact)
        if approximate and not any(close):
            break
        if where != "text":
            by_name(expression(parts, close, exact), approximate)
            if interim and where == "all" and found and not approximate:
                interim(ranked())
        if where != "name":
            by_text(expression(parts, close, exact, text=True), targets(parts, close, exact), approximate)
        if where == "all":
            by_both(close, approximate)
    if not exact and len(found) < limit and where != "text" and len(parts) == 1 and not parts[0][1] \
            and 2 <= len(parts[0][0]) <= 24:
        term = parts[0][0].replace(" ", "")
        sql = (f"SELECT {COLUMNS} FROM {FILES} WHERE f.folded LIKE ?" + filters
               + " ORDER BY length(f.name),f.id LIMIT ?")
        for i, row in enumerate(db.execute(sql, ("%" + "%".join(term) + "%", *bindings, limit * 2))):
            add(row, "abbreviated name", 5 + i / (2 * limit + 1))
    return ranked()


def by_paths(db, paths, kind="opened"):
    """Results for the files at the given addresses that the index still knows, in that order."""
    places = store.roots(db)
    found, seen = [], set()
    for path in paths:
        for root in places:
            base = root["path"].rstrip("/") + "/"
            if path.startswith(base):
                row = db.execute(f"SELECT {COLUMNS} FROM {FILES} WHERE f.root=? AND f.rel=?",
                                 (root["id"], path[len(base):])).fetchone()
                if row and row["doc"] not in seen:
                    seen.add(row["doc"])
                    found.append(result(row, root["path"], kind))
                break
    return found
