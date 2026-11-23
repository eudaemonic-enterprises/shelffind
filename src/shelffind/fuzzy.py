# Copyright 2026 Eudaemonic Enterprises Inc.
"""Indexed spelling candidates and bounded Damerau (adjacent transposition) distance."""
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


def remember_words(db, texts, known):
    pending = set()
    for text in texts:
        pending.update(w for w in words(text) if 3 <= len(w) <= 48 and w not in known)
    db.executemany("INSERT OR IGNORE INTO words(term) VALUES(?)", ((w,) for w in sorted(pending)))
    if len(known) > 200_000:
        known.clear()
    known.update(pending)


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
    if not 3 <= len(term) <= 48 or term.isdigit():
        return []
    bound = 1 if len(term) < 7 else 2
    match = " OR ".join(grams(term).split())
    candidates = db.execute("SELECT w.term FROM spelling JOIN words w ON w.rowid=spelling.rowid "
                            "WHERE spelling MATCH ? ORDER BY rank LIMIT 64", (match,))
    good = []
    for (word,) in candidates:
        if word == term:
            continue
        cost = distance(term, word, bound)
        if cost <= bound:
            good.append((cost, word))
    return [w for _, w in sorted(good)[:5]]
