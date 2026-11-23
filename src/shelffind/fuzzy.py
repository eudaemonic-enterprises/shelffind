# Copyright 2026 Eudaemonic Enterprises Inc.
"""Indexed spelling candidates and bounded Damerau (adjacent transposition) distance.

The known words come in two tiers. The words of names and folders are all kept. A word of the
text is offered as a spelling only once two documents contain it, which leaves out the misprints
of scans, by far the most numerous words of a large collection. Among the words offered, those
of names and those that many documents contain are searched first, so that a slip of the hand
is corrected into the usual word and not into a rare neighbour.
"""
import re
import unicodedata

SMALL_VOCABULARY = 250_000      # below this many known words, every word may be a spelling
COMMON = 16                     # a word of the text is common once this many documents hold it
CANDIDATES = 200                # words compared with the misspelt one, per index


def fold(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(c))


def words(text):
    return re.findall(r"[^\W_]+", fold(text))


def pieces(word, n):
    padded = "0" + word + "0"
    return {padded[i:i + n] for i in range(len(padded) - n + 1)}


def grams(word):
    """What the spelling index holds for a word: its pairs of letters when it is short, its
    triples when it is long, and both for five and six letters, so that a word is found from
    a misspelling one letter shorter or longer on either side of that limit."""
    keys = set()
    if len(word) <= 6:
        keys |= pieces(word, 2)
    if len(word) >= 5:
        keys |= pieces(word, 3)
    return " ".join(sorted(keys))


def size(word):
    return f"len{min(len(word), 49)}"


def remember(db, found, tier, count=True):
    """Enter words among the known ones, then into the spelling indexes those that have become
    eligible: the words of names, the words that two documents contain, and every word while the
    collection is small enough for the misprints of scans not to matter. The cost follows the
    number of words given, never the number of words known. The number of documents kept with
    a word counts readings: it only orders the suggestions, and is not lowered when a document
    leaves the index."""
    if not found:
        return
    db.execute("CREATE TEMP TABLE IF NOT EXISTS arriving(term TEXT PRIMARY KEY) WITHOUT ROWID")
    db.execute("DELETE FROM arriving")
    db.executemany("INSERT INTO arriving VALUES(?)", ((word,) for word in sorted(found)))
    change = "DO UPDATE SET tier=0" if tier == 0 else "DO UPDATE SET df=df+1" if count else "DO NOTHING"
    db.execute("INSERT INTO words(term,tier,df) SELECT term,?,1 FROM arriving WHERE true "
               f"ON CONFLICT(term) {change}", (tier,))
    mine = "term IN (SELECT term FROM arriving)"
    ready = f"listed=0 AND {mine}"
    if (db.execute("SELECT max(id) FROM words").fetchone()[0] or 0) >= SMALL_VOCABULARY:
        ready += " AND (tier=0 OR df>=2)"
    db.execute(f"INSERT INTO spelling(rowid,grams,size) SELECT id,word_grams(term),word_size(term) "
               f"FROM words WHERE {ready}")
    db.execute(f"UPDATE words SET listed=1 WHERE {ready}")
    usual = f"listed=1 AND {mine} AND (tier=0 OR df>=?)"
    db.execute(f"INSERT INTO common(rowid,grams,size) SELECT id,word_grams(term),word_size(term) "
               f"FROM words WHERE {usual}", (COMMON,))
    db.execute(f"UPDATE words SET listed=2 WHERE {usual}", (COMMON,))


def relist(db):
    """Build both spelling indexes again from the known words, by the same rules."""
    ready = "(tier=0 OR df>=2)"
    if (db.execute("SELECT max(id) FROM words").fetchone()[0] or 0) < SMALL_VOCABULARY:
        ready = "true"
    for index in ("spelling", "common"):
        db.execute(f"INSERT INTO {index}({index}) VALUES('delete-all')")
    db.execute(f"UPDATE words SET listed=CASE WHEN NOT {ready} THEN 0 WHEN tier=0 OR df>=? THEN 2 ELSE 1 END",
               (COMMON,))
    db.execute("INSERT INTO spelling(rowid,grams,size) SELECT id,word_grams(term),word_size(term) "
               "FROM words WHERE listed>=1")
    db.execute("INSERT INTO common(rowid,grams,size) SELECT id,word_grams(term),word_size(term) "
               "FROM words WHERE listed=2")


def remember_names(db, texts, known):
    """Keep the words of names and folders; `known` spares the words already seen in this run."""
    pending = set()
    for text in texts:
        pending.update(w for w in words(text) if 3 <= len(w) <= 48 and w not in known)
    remember(db, pending, 0)
    if len(known) > 200_000:
        known.clear()
    known.update(pending)


def text_words(texts):
    """The words of a text that may serve as spellings: made of letters, of 3 to 24 of them."""
    seen = set()
    for text in texts:
        seen.update(w for w in words(text) if 3 <= len(w) <= 24 and w.isalpha())
    return seen


def remember_text(db, texts):
    """Count one more document for each distinct word of a document's text."""
    remember(db, text_words(texts), 1)


def remember_more(db, texts):
    """Keep the words of text added to a document already counted, such as a page recognised
    later: the new words become known, and no word is counted twice for one document."""
    remember(db, text_words(texts), 1, count=False)


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
    the words that most documents contain. The usual words are searched first, and the others
    only when these give too little."""
    if not 3 <= len(term) <= 48 or term.isdigit():
        return []
    bound = 1 if len(term) < 7 else 2
    letters = " OR ".join(f'"{gram}"' for gram in sorted(pieces(term, 2 if len(term) <= 5 else 3)))
    sizes = " OR ".join(f"len{n}" for n in range(max(1, len(term) - bound), min(49, len(term) + bound) + 1))
    match = f"grams:({letters}) AND size:({sizes})"
    good = {}
    for index in ("common", "spelling"):
        candidates = db.execute(
            f"SELECT w.term,w.tier,w.df FROM {index} JOIN words w ON w.id={index}.rowid "
            f"WHERE {index} MATCH ? ORDER BY bm25({index},1.0,0.0) LIMIT ?", (match, CANDIDATES))
        for word, tier, df in candidates:
            if word != term and word not in good:
                cost = distance(term, word, bound)
                if cost <= bound:
                    good[word] = (cost, tier, -df, word)
        if len(good) >= 5:
            break
    return [entry[3] for entry in sorted(good.values())[:5]]
