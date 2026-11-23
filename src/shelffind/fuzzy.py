# Copyright 2026 Eudaemonic Enterprises Inc.
"""Indexed spelling candidates and bounded Damerau (adjacent transposition) distance.

The known words come in two tiers. The words of names and folders are all kept. A word of the
text is offered as a spelling only once two documents contain it, which leaves out the misprints
of scans, by far the most numerous words of a large collection.
"""
import re
import unicodedata


def fold(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(c))


def words(text):
    return re.findall(r"[^\W_]+", fold(text))


def grams(word):
    padded = "0" + word + "0"
    n = 2 if len(word) <= 5 else 3
    keys = {padded[i:i + n] for i in range(len(padded) - n + 1)}
    # Shared boundary bigrams also find neighbours across the 5/6-letter cutoff.
    keys.update((padded[:2], padded[-2:]))
    return " ".join(sorted(keys))


SMALL_VOCABULARY = 250_000      # below this many known words, every word may be a spelling


def list_words(db):
    """Enter into the spelling index the words that have just become eligible: the words of
    names, the words that two documents contain, and every word while the collection is small
    enough for the misprints of scans not to matter."""
    if (db.execute("SELECT max(id) FROM words").fetchone()[0] or 0) < SMALL_VOCABULARY:
        ready = db.execute("SELECT id,term FROM words WHERE listed=0").fetchall()
    else:
        ready = db.execute("SELECT id,term FROM words WHERE listed=0 AND (tier=0 OR df>=2)").fetchall()
    db.executemany("INSERT INTO spelling(rowid,grams) VALUES(?,?)", ((row[0], grams(row[1])) for row in ready))
    db.executemany("UPDATE words SET listed=1 WHERE id=?", ((row[0],) for row in ready))


def remember_names(db, texts, known):
    """Keep the words of names and folders; `known` spares the words already seen in this run."""
    pending = set()
    for text in texts:
        pending.update(w for w in words(text) if 3 <= len(w) <= 48 and w not in known)
    db.executemany("INSERT INTO words(term,tier,df) VALUES(?,0,1) ON CONFLICT(term) DO UPDATE SET tier=0",
                   ((w,) for w in sorted(pending)))
    if pending:
        list_words(db)
    if len(known) > 200_000:
        known.clear()
    known.update(pending)


def remember_text(db, texts):
    """Count one more document for each distinct word of a text. Only words made of letters,
    of 3 to 24 of them, are counted."""
    seen = set()
    for text in texts:
        seen.update(w for w in words(text) if 3 <= len(w) <= 24 and w.isalpha())
    db.executemany("INSERT INTO words(term,tier,df) VALUES(?,1,1) ON CONFLICT(term) DO UPDATE SET df=df+1",
                   ((w,) for w in sorted(seen)))
    if seen:
        list_words(db)


def distance(a, b, bound):
    if abs(len(a) - len(b)) > bound:
        return bound + 1
    previous = list(range(len(b) + 1))
    older = previous
    for i, char in enumerate(a, 1):
        current = [i]
        for j, other in enumerate(b, 1):
            value = min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (char != other))
            if i > 1 and j > 1 and char == b[j - 2] and a[i - 2] == other:
                value = min(value, older[j - 2] + 1)
            current.append(value)
        if min(current) > bound:
            return bound + 1
        older, previous = previous, current
    return previous[-1]


def spellings(db, term):
    """Up to five known words close to `term`: the closest first, then the words of names, then
    the words that most documents contain."""
    if not 3 <= len(term) <= 48 or term.isdigit():
        return []
    bound = 1 if len(term) < 7 else 2
    match = " OR ".join(grams(term).split())
    candidates = db.execute("SELECT w.term,w.tier,w.df FROM spelling JOIN words w ON w.id=spelling.rowid "
                            "WHERE spelling MATCH ? ORDER BY rank LIMIT 64", (match,))
    good = []
    for word, tier, df in candidates:
        if word == term:
            continue
        cost = distance(term, word, bound)
        if cost <= bound:
            good.append((cost, tier, -df, word))
    return [entry[3] for entry in sorted(good)[:5]]
