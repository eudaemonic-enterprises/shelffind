# Copyright 2026 Eudaemonic Enterprises Inc.
"""A ShelfFind source for a local Zotero library.

The program is run once per question. It reads one JSON object from standard input and writes one
JSON answer to standard output. It opens zotero.sqlite read-only and never writes. The data folder
is the option --data-dir, otherwise the environment variable ZOTERO_DATA_DIR, otherwise the Zotero
folder in the home directory.

Search covers the ordinary items of the library: not attachments, notes or annotations, and not
the bin. A question whose terms are all met by the title, the creators, the year or the venue is
answered from those columns. Resolve opens the best file, or the item in Zotero. Identify
recognises a file by its md5 or by its address. The action select opens the item, and cite copies
its citation key.
"""
import argparse
import os
import re
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

try:
    from shelffind.provider import Refusal, serve
except ImportError:                     # a copy of provider.py placed beside this file
    from provider import Refusal, serve


# What hello tells ShelfFind. On a result, cite is offered only when a citation key is known.
ACTIONS = (
    {"name": "select", "label": "Show in Zotero", "result": "open"},
    {"name": "cite", "label": "Copy the citation key", "result": "copy"},
)
# Attachments, notes and annotations are children of a work, not works themselves.
EXCLUDED = frozenset({"attachment", "note", "annotation"})
# The best file of an item: a pdf, then a djvu, then an epub, then anything else.
RANK = {"pdf": 0, "djvu": 1, "epub": 2}
# Field names in the Zotero schema, and the role each one plays in an answer.
ROLES = {
    "title": "title", "date": "date", "publicationTitle": "publication", "publisher": "publisher",
    "extra": "extra", "citationKey": "citation",
}
WORD = re.compile(r"[^\W_]+")
YEAR = re.compile(r"\d{4}")
HEX = frozenset("0123456789abcdef")
MISSING_YEAR = 10 ** 12             # Sorts after every real year when the newest come first.


def fold(text):
    """Lower case, with accents removed. A question's terms arrive already folded this way."""
    return "".join(char for char in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(char))


def words_of(text):
    """The words of a catalogue field, folded, in order."""
    return WORD.findall(fold(text or ""))


def phrase_matches(text, words, prefix):
    """Whether the folded term meets this sequence of words, as a whole word or, when prefix is
    set, as the beginning of one. A term of several words must occupy consecutive words."""
    parts = words_of(text)
    if not parts:
        return False
    if len(parts) == 1:
        part = parts[0]
        if prefix:
            return any(word.startswith(part) for word in words)
        return part in words
    width = len(parts)
    for start in range(len(words) - width + 1):
        if list(words[start:start + width - 1]) != parts[:-1]:
            continue
        last = words[start + width - 1]
        if last == parts[-1] or (prefix and last.startswith(parts[-1])):
            return True
    return False


def term_matches(term, words, prefix):
    """Whether the term or one of its alternatives meets these words."""
    if not isinstance(term, dict):
        return False
    choices = []
    text = term.get("text")
    if isinstance(text, str) and text:
        choices.append(text)
    for alternative in term.get("alternatives") or ():
        if isinstance(alternative, str) and alternative:
            choices.append(alternative)
    return any(phrase_matches(choice, words, prefix) for choice in choices)


def allows_prefix(request, term):
    """A term that is not literal may be the beginning of a word, unless the question asks for
    whole words only."""
    return not request.get("exact") and not (isinstance(term, dict) and term.get("literal"))


def relation(conn, *names):
    """The first of these tables or views that the database has, or None."""
    for name in names:
        found = conn.execute("SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')",
                             (name,)).fetchone()
        if found:
            return name
    return None


def column_names(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def clean_hash(value):
    """An md5 as 32 lower-case hex digits, or None when the stored value is not one."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text.startswith("md5:"):
        text = text[4:]
    if len(text) == 32 and set(text) <= HEX:
        return text
    return None


def citation_from_extra(extra):
    """The value after a line 'Citation Key:' in the extra field, or None."""
    for line in extra.splitlines():
        label, separator, rest = line.strip().partition(":")
        if separator and label == "Citation Key" and rest.strip():
            return rest.strip()
    return None


def extension_of(name):
    suffix = Path(name).suffix
    return suffix[1:].lower() if len(suffix) > 1 else ""


def describe_file(folder, key, path):
    """The file an attachment points at, or None when the path is not a stored or absolute file.

    A stored attachment has a path of the form storage:FILENAME and lives at
    storage/<attachment key>/FILENAME in the data folder. A linked attachment has an absolute
    path. The relative form attachments:... is ignored.
    """
    if not isinstance(path, str) or not path:
        return None
    if path.startswith("storage:"):
        relative = path[len("storage:"):].lstrip("/")
        if not relative or any(part == ".." for part in Path(relative).parts):
            return None
        absolute = os.path.normpath(str(folder / "storage" / key / relative))
        name = Path(relative).name
    elif path.startswith("/"):
        absolute = os.path.normpath(path)
        name = Path(absolute).name
    else:
        return None
    if not name:
        return None
    return {"name": name, "format": extension_of(name), "path": absolute}


def creator_words(row):
    """The name ShelfFind may match: the single field, or the given name and the surname."""
    if (row["fieldMode"] or 0) == 1:
        return row["lastName"] or ""
    return f"{row['firstName'] or ''} {row['lastName'] or ''}".strip()


def surname(row):
    """The name shown in a byline. A single-field creator uses that field."""
    if (row["fieldMode"] or 0) == 1:
        return (row["lastName"] or row["firstName"] or "").strip()
    return (row["lastName"] or "").strip()


def byline_of(creators):
    """One surname, 'Surname and Surname', or 'Surname et al.'"""
    names = [name for name in (surname(row) for row in creators) if name]
    if not names:
        return None
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]} et al."


def year_of(dates):
    """The first four digits in the date field, as an integer, when there are any."""
    for text in dates:
        found = YEAR.search(text)
        if found:
            return int(found.group())
    return None


def texts_of(grouped, role):
    """The values of one role, canonical fields before fields merely mapped onto them."""
    found = sorted(grouped.get(role, ()), key=lambda pair: pair[0])
    return [text.strip() for _, text in found if isinstance(text, str) and text.strip()]


def normal_folder(chosen):
    path = Path(chosen).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.normpath(str(path)))


def data_folder(argv=None):
    """The Zotero data folder named by the command, the environment, or the home directory."""
    parser = argparse.ArgumentParser(description="Answer ShelfFind from a local Zotero library.")
    parser.add_argument("--data-dir", help="The Zotero data folder, instead of ZOTERO_DATA_DIR or ~/Zotero.")
    args = parser.parse_args(argv)
    chosen = args.data_dir or os.environ.get("ZOTERO_DATA_DIR") or (Path.home() / "Zotero")
    return normal_folder(chosen)


class Library:
    """The columns a question needs, read once from a read-only database."""

    def __init__(self, folder):
        self.folder = folder
        self.items = {}
        self.by_key = {}
        self.searchable = []
        self.by_hash = {}
        self.by_path = {}

    @classmethod
    def read(cls, folder):
        path = folder / "zotero.sqlite"
        if not path.is_file():
            raise Refusal("unavailable", "The Zotero database was not found.")
        uri = "file:" + quote(str(path), safe="/") + "?mode=ro&immutable=1"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=1)
        except sqlite3.Error:
            raise Refusal("unavailable", "The Zotero database could not be opened.") from None
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            library = cls(folder)
            library.load(conn)
            return library
        except Refusal:
            raise
        except sqlite3.Error:
            raise Refusal("unavailable", "The Zotero database could not be read.") from None
        finally:
            conn.close()

    def load(self, conn):
        types = self.type_names(conn)
        roles, own = self.field_roles(conn)
        deleted = self.identifiers(conn, "deletedItems")
        retracted = self.identifiers(conn, "retractedItems") if relation(conn, "retractedItems") else set()
        hidden = set()
        for table in ("itemAttachments", "itemNotes", "itemAnnotations"):
            if relation(conn, table):
                hidden.update(row[0] for row in conn.execute(f"SELECT itemID FROM {table}"))
        library_type = self.library_types(conn)
        values = self.field_values(conn, roles, own)
        creators = self.creators(conn)
        notes = self.note_counts(conn)
        has_library = "libraryID" in column_names(conn, "items")
        library_sql = "libraryID" if has_library else "NULL"
        for row in conn.execute(f"SELECT itemID, itemTypeID, key, {library_sql} AS libraryID FROM items"):
            item_id = row["itemID"]
            kind = types.get(row["itemTypeID"], "")
            grouped = values.get(item_id, {})
            title_values = texts_of(grouped, "title")
            title = title_values[0] if title_values else ""
            publication = texts_of(grouped, "publication")
            publisher = texts_of(grouped, "publisher")
            year = year_of(texts_of(grouped, "date"))
            people = creators.get(item_id, [])
            cite = next(iter(texts_of(grouped, "citation")), None)
            if not cite:
                for extra in texts_of(grouped, "extra"):
                    cite = citation_from_extra(extra)
                    if cite:
                        break
            where = library_type.get(row["libraryID"])
            openable = kind not in EXCLUDED and item_id not in hidden
            item = {
                "id": item_id, "key": row["key"], "title": title, "title_fold": fold(title),
                "byline": byline_of(people), "year": year,
                "venue": (publication[0] if publication else publisher[0] if publisher else None),
                "notes": notes.get(item_id, 0), "retracted": item_id in retracted, "cite": cite,
                "deleted": item_id in deleted, "user": where == "user", "openable": openable,
                "best": None,
            }
            if openable and item_id not in deleted and where != "feed":
                title_words = words_of(title)
                places = [title_words]
                places.extend(words_of(creator_words(person)) for person in people)
                if year is not None:
                    places.append((str(year),))
                for text in publication:
                    places.append(words_of(text))
                for text in publisher:
                    places.append(words_of(text))
                item["title_words"] = title_words
                item["places"] = places
                self.searchable.append(item)
            self.items[item_id] = item
            if openable:
                self.keep_key(item)
        self.attachments(conn)

    def keep_key(self, item):
        """Remember one item per key. The user library wins over a group, and a live item over
        one in the bin. The same key in two libraries is rare, and the select address names a key
        of the user library."""
        current = self.by_key.get(item["key"])
        if current is None or self.prefer(item, current):
            self.by_key[item["key"]] = item

    @staticmethod
    def prefer(item, current):
        def rank(candidate):
            return (0 if candidate["user"] else 1, 0 if not candidate["deleted"] else 1, candidate["id"])
        return rank(item) < rank(current)

    def type_names(self, conn):
        table = relation(conn, "itemTypesCombined", "itemTypes")
        if not table:
            return {}
        return {row["itemTypeID"]: row["typeName"] for row in conn.execute(
            f"SELECT itemTypeID, typeName FROM {table}")}

    def field_roles(self, conn):
        """Each field id is given a role.

        A field mapped onto a base field takes that role too: bookTitle onto publicationTitle,
        and the same for the date and the publisher.
        """
        table = relation(conn, "fieldsCombined", "fields")
        roles, own = {}, {}
        if not table:
            return roles, own
        for row in conn.execute(f"SELECT fieldID, fieldName FROM {table}"):
            own[row["fieldID"]] = row["fieldName"]
            role = ROLES.get(row["fieldName"])
            if role:
                roles[row["fieldID"]] = role
        mapping = relation(conn, "baseFieldMappingsCombined", "baseFieldMappings")
        if mapping:
            for row in conn.execute(
                    f"SELECT map.fieldID AS fieldID, base.fieldName AS fieldName FROM {mapping} AS map "
                    f"JOIN {table} AS base ON base.fieldID = map.baseFieldID"):
                role = ROLES.get(row["fieldName"])
                if role and row["fieldID"] not in roles:
                    roles[row["fieldID"]] = role
        return roles, own

    def field_values(self, conn, roles, own):
        grouped = defaultdict(lambda: defaultdict(list))
        if not roles or not relation(conn, "itemData") or not relation(conn, "itemDataValues"):
            return grouped
        marks = ",".join("?" * len(roles))
        rows = conn.execute(
            f"SELECT d.itemID AS itemID, d.fieldID AS fieldID, v.value AS value FROM itemData AS d "
            f"JOIN itemDataValues AS v ON v.valueID = d.valueID WHERE d.fieldID IN ({marks})",
            tuple(roles))
        for row in rows:
            value = row["value"]
            if not isinstance(value, str) or not value:
                continue
            priority = 0 if own.get(row["fieldID"]) in ROLES else 1
            grouped[row["itemID"]][roles[row["fieldID"]]].append((priority, value))
        return grouped

    def creators(self, conn):
        grouped = defaultdict(list)
        if not relation(conn, "itemCreators") or not relation(conn, "creators"):
            return grouped
        for row in conn.execute(
                "SELECT ic.itemID AS itemID, c.firstName AS firstName, c.lastName AS lastName, "
                "c.fieldMode AS fieldMode FROM itemCreators AS ic "
                "JOIN creators AS c ON c.creatorID = ic.creatorID "
                "ORDER BY ic.itemID, ic.orderIndex, ic.creatorID"):
            grouped[row["itemID"]].append(row)
        return grouped

    def note_counts(self, conn):
        if not relation(conn, "itemNotes"):
            return {}
        return {row[0]: row[1] for row in conn.execute(
            "SELECT parentItemID, COUNT(*) FROM itemNotes WHERE parentItemID IS NOT NULL GROUP BY parentItemID")}

    def library_types(self, conn):
        if "libraryID" not in column_names(conn, "items") or not relation(conn, "libraries"):
            return {}
        if "type" not in column_names(conn, "libraries"):
            return {}
        return {row["libraryID"]: row["type"] for row in conn.execute("SELECT libraryID, type FROM libraries")}

    def identifiers(self, conn, table):
        if not relation(conn, table):
            return set()
        return {row[0] for row in conn.execute(f"SELECT itemID FROM {table}")}

    def attachments(self, conn):
        if not relation(conn, "itemAttachments"):
            return
        grouped = defaultdict(list)
        indexed = []
        for row in conn.execute(
                "SELECT a.itemID AS itemID, a.parentItemID AS parentItemID, a.path AS path, "
                "a.storageHash AS storageHash, i.key AS key FROM itemAttachments AS a "
                "JOIN items AS i ON i.itemID = a.itemID"):
            described = describe_file(self.folder, row["key"], row["path"])
            if described is None:
                continue
            described.update({"item_id": row["itemID"], "parent": row["parentItemID"], "key": row["key"],
                              "md5": clean_hash(row["storageHash"])})
            indexed.append(described)
            if row["parentItemID"] is not None:
                grouped[row["parentItemID"]].append(described)
        for item_id, found in grouped.items():
            item = self.items.get(item_id)
            if item is not None:
                item["best"] = min(found, key=lambda att: (RANK.get(att["format"], 9), att["key"]))
        for att in sorted(indexed, key=lambda att: att["key"]):
            if att["md5"]:
                self.by_hash.setdefault(att["md5"], att)
            self.by_path.setdefault(att["path"], att)

    def target(self, att):
        """The parent work of an attachment, or the attachment itself when it has no parent."""
        parent = self.items.get(att["parent"])
        if parent is not None and parent["openable"]:
            return parent
        return self.items.get(att["item_id"])

    def search(self, request):
        # Passages of one record, and a search of the full text, have nothing to look in here.
        if request.get("within") or request.get("where") == "text":
            return []
        terms = request.get("terms") or []
        if not isinstance(terms, list):
            raise Refusal("invalid", "The search needs a list of terms.")
        wanted = request.get("format")
        wanted = wanted.lower().lstrip(".") if isinstance(wanted, str) else None
        wanted = wanted or None
        limit = request.get("limit", 25)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            limit = 25
        found = []
        for item in self.searchable:
            best = item["best"]
            if wanted and (best is None or best["format"] != wanted):
                continue
            if not self.meets(item["places"], terms, request):
                continue
            found.append((item, self.meets([item["title_words"]], terms, request)))
        found.sort(key=lambda pair: (0 if pair[1] else 1,
                                     -(pair[0]["year"] if pair[0]["year"] is not None else -MISSING_YEAR),
                                     pair[0]["title_fold"], pair[0]["key"]))
        return [self.row(item) for item, _ in found[:limit]]

    @staticmethod
    def meets(places, terms, request):
        """Every term is met, by its text or by one alternative, in at least one of these fields."""
        for term in terms:
            prefix = allows_prefix(request, term)
            if not any(term_matches(term, place, prefix) for place in places):
                return False
        return True

    def row(self, item):
        record = {"id": item["key"], "match": "catalogue", "actions": ["select"]}
        if item["cite"]:
            record["actions"].append("cite")
        if item["title"]:
            record["title"] = item["title"]
        if item["byline"]:
            record["byline"] = item["byline"]
        if item["year"] is not None:
            record["year"] = item["year"]
        if item["venue"]:
            record["venue"] = item["venue"]
        if item["notes"]:
            record["notes"] = item["notes"]
        if item["retracted"]:
            record["flags"] = ["retracted"]
        best = item["best"]
        if best is not None:
            record["name"] = best["name"]
            if best["format"]:
                record["format"] = best["format"]
            if os.path.isfile(best["path"]):
                record["path"] = best["path"]
            if best["md5"]:
                record["keys"] = ["md5:" + best["md5"]]
        if not record.get("title") and not record.get("name"):
            record["title"] = item["key"]
        return record

    def item(self, key):
        if not isinstance(key, str) or not key:
            return None
        return self.by_key.get(key)

    def resolve(self, request):
        item = self.item(request.get("id"))
        if item is None:
            raise Refusal("not_found", "No such item.")
        best = item["best"]
        if best is not None and os.path.isfile(best["path"]):
            return {"path": best["path"]}
        return {"uri": self.address(item["key"])}

    def identify(self, request):
        files = request.get("files") or []
        if not isinstance(files, list):
            raise Refusal("invalid", "The question has no list of files.")
        records = []
        for file in files:
            if not isinstance(file, dict):
                continue
            number = file.get("n")
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                continue
            att, basis = self.recognise(file)
            if att is None:
                continue
            item = self.target(att)
            if item is None:
                continue
            record = {"n": number, "id": item["key"], "basis": basis, "text": False}
            if item["title"]:
                record["title"] = item["title"]
            if item["byline"]:
                record["byline"] = item["byline"]
            if item["year"] is not None:
                record["year"] = item["year"]
            if item["retracted"]:
                record["flags"] = ["retracted"]
            if att["md5"]:
                record["keys"] = ["md5:" + att["md5"]]
            if os.path.isfile(att["path"]):
                record["path"] = att["path"]
            records.append(record)
        return records

    def recognise(self, file):
        """An attachment whose md5 is one of the file's keys, or else one whose address is the
        file's path."""
        for key in file.get("keys") or ():
            if not isinstance(key, str):
                continue
            digest = clean_hash(key) if key.startswith("md5:") else None
            att = self.by_hash.get(digest) if digest else None
            if att is not None:
                return att, "md5"
        path = file.get("path")
        if isinstance(path, str) and path.startswith("/"):
            att = self.by_path.get(os.path.normpath(path))
            if att is not None:
                return att, "path"
        return None, None

    def act(self, request):
        item = self.item(request.get("id"))
        if item is None:
            raise Refusal("not_found", "No such item.")
        action = request.get("action")
        if action == "select":
            return {"result": "open", "uri": self.address(item["key"])}
        if action == "cite":
            if not item["cite"]:
                raise Refusal("not_found", "This item has no citation key.")
            return {"result": "copy", "text": item["cite"]}
        raise Refusal("invalid", "The action is unknown.")

    @staticmethod
    def address(key):
        return f"zotero://select/library/items/{key}"


def main(argv=None):
    folder = data_folder(argv)

    def search(request):
        return Library.read(folder).search(request)

    def resolve(request):
        return Library.read(folder).resolve(request)

    def identify(request):
        return Library.read(folder).identify(request)

    def act(request):
        return Library.read(folder).act(request)

    serve(name="Zotero", search=search, resolve=resolve, identify=identify, act=act,
          actions=ACTIONS, text=False, network=False)


if __name__ == "__main__":
    main()
