# Copyright 2026 Eudaemonic Enterprises Inc.
"""The Zotero source, run as a program against a small invented library.

The works, the people and the files are made up. The people are long dead, or fictional. The real
Zotero database of this machine is never opened: the home directory of the child process is the
temporary folder.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
PROGRAM = REPOSITORY / "contrib" / "zotero.py"
PDF_HASH = "ab" * 16
EPUB_HASH = "cd" * 16

SCHEMA = """
CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INT NOT NULL, key TEXT NOT NULL);
CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
CREATE TABLE itemData (itemID INT, fieldID INT, valueID INT, PRIMARY KEY (itemID, fieldID));
CREATE TABLE creators (
    creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT, fieldMode INT);
CREATE TABLE itemCreators (
    itemID INT NOT NULL, creatorID INT NOT NULL, creatorTypeID INT NOT NULL DEFAULT 1,
    orderIndex INT NOT NULL DEFAULT 0);
CREATE TABLE itemAttachments (
    itemID INTEGER PRIMARY KEY, parentItemID INT, path TEXT, storageHash TEXT);
CREATE TABLE itemNotes (itemID INTEGER PRIMARY KEY, parentItemID INT, note TEXT, title TEXT);
CREATE TABLE itemAnnotations (itemID INTEGER PRIMARY KEY, parentItemID INT);
CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
CREATE TABLE retractedItems (itemID INTEGER PRIMARY KEY);
CREATE TABLE baseFieldMappings (itemTypeID INT, baseFieldID INT, fieldID INT);
"""

TYPES = {"journalArticle": 1, "book": 2, "bookSection": 3, "attachment": 4, "note": 5, "annotation": 6}
FIELDS = {"title": 1, "date": 2, "publicationTitle": 3, "publisher": 4, "extra": 5, "citationKey": 6,
          "bookTitle": 7}


class ZoteroSource(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.library = self.folder / "library"
        self.library.mkdir()
        self.pdf = self.library / "storage" / "PDFONDE1" / "ondes.pdf"
        self.pdf.parent.mkdir(parents=True)
        self.pdf.write_bytes(b"%PDF-1.4\n% invented\n")
        self.djvu = self.library / "files" / "treatise.djvu"
        self.djvu.parent.mkdir()
        self.djvu.write_bytes(b"not a document")
        self.build_library()

    def build_library(self):
        conn = sqlite3.connect(self.library / "zotero.sqlite")
        try:
            self.fill(conn)
        finally:
            conn.close()

    def fill(self, conn):
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO itemTypes (itemTypeID, typeName) VALUES (?, ?)",
                         [(type_id, name) for name, type_id in TYPES.items()])
        conn.executemany("INSERT INTO fields (fieldID, fieldName) VALUES (?, ?)",
                         [(field_id, name) for name, field_id in FIELDS.items()])
        # The bookTitle field is the publication title of a chapter, through the base-field mapping.
        conn.execute("INSERT INTO baseFieldMappings (itemTypeID, baseFieldID, fieldID) VALUES (?, ?, ?)",
                     (TYPES["bookSection"], FIELDS["publicationTitle"], FIELDS["bookTitle"]))
        works = [
            (1, "journalArticle", "ONDE1895", "Étude des ondes longues", "1895",
             {"publicationTitle": "Journal de mathématiques",
              "extra": "Citation Key: poincare1895\nA remark that is not a key."},
             [(0, 0)]),
            # Stokes is inserted before Thomson, with a later order, so orderIndex has to be read.
            (2, "book", "WAVE1907", "A Treatise on the Motion of Waves", "1907",
             {"publisher": "Cambridge Press"}, [(1, 1), (2, 0)]),
            # Airy is inserted before Lamb, so the byline needs the creator order.
            (3, "journalArticle", "CHAN1880", "Wave motion in channels", "1880",
             {"publicationTitle": "Proceedings of the Society", "extra": "A remark about the channels."},
             [(3, 2), (4, 1), (5, 0)]),
            (4, "journalArticle", "RETR2001", "A false theory of tides", "May 2001",
             {"publicationTitle": "Annals of Error", "extra": "Citation Key: not-this-one",
              "citationKey": "halley2001"}, [(6, 0)]),
            (5, "book", "HARB1999", "Records of the northern harbours", "1999",
             {"publisher": "Harbour House"}, [(7, 0)]),
            (6, "book", "GONE1990", "Discarded notes on harbours", "1990", {}, [(8, 0)]),
            (7, "bookSection", "CHAP1844", "On the solitary swell", "1844",
             {"bookTitle": "A Treatise on the Motion of Waves"}, [(9, 0)]),
        ]
        children = [
            (10, "note", "NOTEONDE", None), (11, "attachment", "PDFONDE1", None),
            (12, "attachment", "DJONDE01", None), (13, "attachment", "TXTONDE1", None),
            (14, "attachment", "DJVUWAVE", None), (15, "attachment", "RELWAVE1", None),
            (16, "attachment", "EPUBRETR", None),
            (17, "attachment", "DECOYATT", "Wave motion in channels"),
            (18, "note", "DECOYNOT", "Étude des ondes longues"),
            (19, "annotation", "DECOYANN", "Wave motion in channels"),
        ]
        items, values = [], []
        for item_id, type_name, key, title, date, fields, _people in works:
            items.append((item_id, TYPES[type_name], key))
            values.append((item_id, "title", title))
            values.append((item_id, "date", date))
            values.extend((item_id, name, text) for name, text in fields.items())
        for item_id, type_name, key, title in children:
            items.append((item_id, TYPES[type_name], key))
            if title:
                values.append((item_id, "title", title))
        conn.executemany("INSERT INTO items (itemID, itemTypeID, key) VALUES (?, ?, ?)", items)
        conn.executemany("INSERT INTO creators (creatorID, firstName, lastName, fieldMode) VALUES (?, ?, ?, ?)", (
            (0, "Augustin", "Poincaré", 0), (1, "George", "Stokes", 0), (2, "William", "Thomson", 0),
            (3, "George", "Airy", 0), (4, "George", "Green", 0), (5, "Horace", "Lamb", 0),
            (6, None, "Halley", 1), (7, "Walter", "Waverley", 0), (8, "Anders", "Celsius", 0),
            (9, "John", "Russell", 0)))
        conn.executemany(
            "INSERT INTO itemCreators (itemID, creatorID, creatorTypeID, orderIndex) VALUES (?, ?, 1, ?)",
            [(item_id, creator_id, order) for item_id, *_rest, people in works for creator_id, order in people])
        conn.executemany("INSERT INTO itemDataValues (valueID, value) VALUES (?, ?)",
                         [(index, text) for index, (_, _, text) in enumerate(values, 1)])
        conn.executemany("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (?, ?, ?)",
                         [(item_id, FIELDS[name], index) for index, (item_id, name, _) in enumerate(values, 1)])
        conn.executemany(
            "INSERT INTO itemAttachments (itemID, parentItemID, path, storageHash) VALUES (?, ?, ?, ?)", (
                (11, 1, "storage:ondes.pdf", PDF_HASH),
                (12, 1, "storage:copy.djvu", None),
                (13, 1, "storage:notes.txt", None),
                (14, 2, str(self.djvu), None),
                (15, 2, "attachments:papers/treatise.pdf", None),
                (16, 4, "storage:tides.epub", EPUB_HASH.upper()),
                (17, None, "storage:decoy.pdf", None)))
        conn.execute("INSERT INTO itemNotes (itemID, parentItemID, note, title) VALUES (?, ?, ?, ?)",
                     (10, 1, "A marginal remark.", "On the long waves"))
        conn.execute("INSERT INTO itemNotes (itemID, parentItemID, note, title) VALUES (?, ?, ?, ?)",
                     (18, None, "Not a work.", "Étude des ondes longues"))
        conn.execute("INSERT INTO itemAnnotations (itemID, parentItemID) VALUES (?, ?)", (19, 11))
        conn.execute("INSERT INTO deletedItems (itemID) VALUES (?)", (6,))
        conn.execute("INSERT INTO retractedItems (itemID) VALUES (?)", (4,))
        conn.commit()

    def ask(self, request, folder="library", env=None, program=None, pythonpath=True):
        if folder == "library":
            folder = self.library
        # Without the package, the interpreter must not see an installed copy either.
        command = [sys.executable, "-W", "error", str(program or PROGRAM)]
        if not pythonpath:
            command.insert(1, "-S")
        if folder is not None:
            command.extend(["--data-dir", str(folder)])
        environment = os.environ.copy()
        # A stray data folder in the environment must not point the child at the real library.
        environment["HOME"] = str(self.folder)
        environment.pop("ZOTERO_DATA_DIR", None)
        if pythonpath:
            environment["PYTHONPATH"] = str(REPOSITORY / "src")
        else:
            environment.pop("PYTHONPATH", None)
        if env:
            environment.update(env)
        completed = subprocess.run(command, input=json.dumps(request), capture_output=True, text=True,
                                   env=environment, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertEqual(completed.stderr, "")
        return json.loads(completed.stdout)

    def query(self, terms, **options):
        request = {"version": options.get("version", 2), "op": "search",
                   "query": " ".join(text for text, _, _ in terms),
                   "terms": [{"text": text, "literal": literal, "alternatives": list(alternatives)}
                             for text, literal, alternatives in terms],
                   "exact": options.get("exact", False), "limit": options.get("limit", 25),
                   "format": options.get("format"), "where": options.get("where", "all")}
        if "within" in options:
            request["within"] = options["within"]
        return request

    def found(self, terms, **options):
        told = self.ask(self.query(terms, **options))
        self.assertTrue(told["ok"], told)
        self.assertIs(told["partial"], False)
        return told["results"]

    def ids(self, terms, **options):
        return [row["id"] for row in self.found(terms, **options)]

    def test_hello_describes_the_zotero_source(self):
        told = self.ask({"version": 2, "op": "hello", "client": "shelffind/test"})
        self.assertEqual(told, {"ok": True, "version": 2, "name": "Zotero",
                                "ops": ["search", "resolve", "identify", "act"],
                                "text": False, "network": False,
                                "actions": [{"name": "select", "label": "Show in Zotero", "result": "open"},
                                            {"name": "cite", "label": "Copy the citation key", "result": "copy"}]})

    def test_search_reads_the_title_the_names_the_year_and_the_venue(self):
        rows = self.found([("etude", False, [])])
        self.assertEqual([row["id"] for row in rows], ["ONDE1895"])
        row = rows[0]
        self.assertEqual(row["title"], "Étude des ondes longues")
        self.assertEqual(row["byline"], "Poincaré")
        self.assertEqual((row["year"], row["venue"], row["match"]), (1895, "Journal de mathématiques", "catalogue"))
        self.assertEqual(row["notes"], 1)
        self.assertEqual(row["actions"], ["select", "cite"])
        self.assertEqual((row["name"], row["format"], row["keys"]), ("ondes.pdf", "pdf", ["md5:" + PDF_HASH]))
        self.assertEqual(Path(row["path"]).resolve(), self.pdf.resolve())
        self.assertNotIn("flags", row)
        self.assertEqual(self.ids([("augustin", False, [])]), ["ONDE1895"])
        self.assertEqual(self.ids([("1907", False, [])]), ["WAVE1907"])
        self.assertEqual(self.ids([("cambridge", False, [])]), ["WAVE1907"])
        self.assertEqual(self.ids([("mathematiques", False, [])]), ["ONDE1895"])
        self.assertEqual(self.ids([("ondes longues", True, [])]), ["ONDE1895"])
        self.assertEqual(self.ids([("longues ondes", True, [])]), [])
        # A chapter keeps its book title in bookTitle, which is the publication title there.
        treatise = {row["id"]: row for row in self.found([("treatise", False, [])])}
        self.assertEqual(list(treatise), ["WAVE1907", "CHAP1844"])
        self.assertEqual(treatise["CHAP1844"]["venue"], "A Treatise on the Motion of Waves")
        self.assertEqual(treatise["CHAP1844"]["byline"], "Russell")

    def test_an_alternative_matches_and_the_bin_is_left_out(self):
        self.assertEqual(self.ids([("kelvin", False, [])]), [])
        self.assertEqual(self.ids([("kelvin", False, ["thomson"])]), ["WAVE1907"])
        self.assertEqual(self.ids([("harbours", True, [])]), ["HARB1999"])
        self.assertEqual(self.ids([("discarded", False, [])]), [])
        self.assertEqual(self.ids([("celsius", False, [])]), [])

    def test_a_prefix_matches_unless_the_word_must_be_whole(self):
        self.assertEqual(self.ids([("ond", False, [])]), ["ONDE1895"])
        self.assertEqual(self.ids([("ond", False, [])], exact=True), [])
        self.assertEqual(self.ids([("ond", True, [])]), [])
        self.assertEqual(self.ids([("ondes", True, [])]), ["ONDE1895"])
        self.assertEqual(self.ids([("wave", False, [])], exact=True), ["CHAN1880"])
        self.assertEqual(self.ids([("harbor", False, ["harbour"])]), ["HARB1999"])

    def test_accents_are_ignored(self):
        self.assertEqual(self.ids([("etude", False, [])]), ["ONDE1895"])
        self.assertEqual(self.ids([("poincare", False, [])]), ["ONDE1895"])
        self.assertEqual(self.ids([("etude", False, [])], where="name"), ["ONDE1895"])

    def test_title_matches_come_first_then_the_newest_year(self):
        rows = {row["id"]: row for row in self.found([("wave", False, [])])}
        self.assertEqual(list(rows), ["WAVE1907", "CHAN1880", "HARB1999", "CHAP1844"])
        self.assertEqual(rows["WAVE1907"]["byline"], "Thomson and Stokes")
        self.assertEqual(rows["WAVE1907"]["venue"], "Cambridge Press")
        self.assertEqual((rows["WAVE1907"]["name"], rows["WAVE1907"]["format"]), ("treatise.djvu", "djvu"))
        self.assertEqual(Path(rows["WAVE1907"]["path"]).resolve(), self.djvu.resolve())
        self.assertEqual(rows["CHAN1880"]["byline"], "Lamb et al.")
        self.assertEqual(rows["CHAN1880"]["actions"], ["select"])
        self.assertNotIn("name", rows["CHAN1880"])
        self.assertEqual(rows["HARB1999"]["byline"], "Waverley")
        retracted = self.found([("halley", False, [])])
        self.assertEqual(retracted[0]["id"], "RETR2001")
        self.assertEqual(retracted[0]["flags"], ["retracted"])
        self.assertEqual((retracted[0]["byline"], retracted[0]["year"], retracted[0]["format"]),
                         ("Halley", 2001, "epub"))
        self.assertEqual(retracted[0]["actions"], ["select", "cite"])
        self.assertNotIn("path", retracted[0])

    def test_the_limit_and_the_format_keep_only_what_was_asked(self):
        self.assertEqual(self.ids([("wave", False, [])], limit=1), ["WAVE1907"])
        self.assertEqual(self.ids([("wave", False, [])], format="djvu"), ["WAVE1907"])
        self.assertEqual(self.ids([("wave", False, [])], format="pdf"), [])
        self.assertEqual(self.ids([("etude", False, [])], format="pdf"), ["ONDE1895"])
        self.assertEqual(self.ids([("etude", False, [])], format="DJVU"), [])
        self.assertEqual(self.ids([("etude", False, [])], where="text"), [])
        self.assertEqual(self.ids([("etude", False, [])], within="ONDE1895"), [])

    def test_resolve_opens_the_file_or_the_item_in_zotero(self):
        opened = self.ask({"version": 2, "op": "resolve", "id": "WAVE1907"})
        self.assertTrue(opened["ok"])
        self.assertEqual(Path(opened["path"]).resolve(), self.djvu.resolve())
        shown = self.ask({"version": 2, "op": "resolve", "id": "RETR2001"})
        self.assertEqual(shown, {"ok": True, "uri": "zotero://select/library/items/RETR2001"})
        missing = self.ask({"version": 2, "op": "resolve", "id": "NOSUCH01"})
        self.assertEqual(missing["error"]["code"], "not_found")
        self.assertFalse(missing["ok"])

    def test_identify_recognises_a_file_by_its_md5_or_its_path(self):
        told = self.ask({"version": 2, "op": "identify", "files": [
            {"n": 0, "path": "/nowhere/copy.pdf", "size": 4, "mtime": 0, "keys": ["md5:" + PDF_HASH]},
            {"n": 1, "path": str(self.djvu), "size": 4, "mtime": 0, "keys": ["md5:" + "00" * 16]},
            {"n": 2, "path": "/nowhere/missing.pdf", "size": 1, "mtime": 0, "keys": []},
            {"n": 3, "path": str(self.pdf), "size": 4, "mtime": 0, "keys": []},
            {"n": 4, "path": "/nowhere/tides.epub", "size": 1, "mtime": 0, "keys": ["sha256:" + "ab" * 32,
                                                                                    "md5:" + EPUB_HASH]},
        ]})
        self.assertTrue(told["ok"], told)
        records = {row["n"]: row for row in told["records"]}
        self.assertEqual(set(records), {0, 1, 3, 4})
        self.assertEqual(records[0]["basis"], "md5")
        self.assertEqual(records[0]["id"], "ONDE1895")
        self.assertNotEqual(records[0]["id"], "PDFONDE1")
        self.assertEqual(records[0]["keys"], ["md5:" + PDF_HASH])
        self.assertEqual(records[0]["title"], "Étude des ondes longues")
        self.assertIs(records[0]["text"], False)
        self.assertEqual(records[0]["byline"], "Poincaré")
        self.assertEqual(records[0]["year"], 1895)
        self.assertEqual(Path(records[0]["path"]).resolve(), self.pdf.resolve())
        self.assertEqual((records[1]["basis"], records[1]["id"]), ("path", "WAVE1907"))
        self.assertNotIn("keys", records[1])
        self.assertEqual(Path(records[1]["path"]).resolve(), self.djvu.resolve())
        self.assertEqual((records[3]["basis"], records[3]["id"]), ("path", "ONDE1895"))
        self.assertEqual(records[4]["id"], "RETR2001")
        self.assertEqual(records[4]["basis"], "md5")
        self.assertEqual(records[4]["flags"], ["retracted"])
        self.assertEqual(records[4]["keys"], ["md5:" + EPUB_HASH])
        self.assertNotIn("path", records[4])
        self.assertIs(records[4]["text"], False)

    def test_select_opens_the_item_and_cite_copies_the_key(self):
        selected = self.ask({"version": 2, "op": "act", "id": "ONDE1895", "action": "select"})
        self.assertEqual(selected, {"ok": True, "result": "open",
                                    "uri": "zotero://select/library/items/ONDE1895"})
        cited = self.ask({"version": 2, "op": "act", "id": "ONDE1895", "action": "cite"})
        self.assertEqual(cited, {"ok": True, "result": "copy", "text": "poincare1895"})
        field = self.ask({"version": 2, "op": "act", "id": "RETR2001", "action": "cite"})
        self.assertEqual(field["text"], "halley2001")
        refused = self.ask({"version": 2, "op": "act", "id": "CHAN1880", "action": "cite"})
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"]["code"], "not_found")
        unknown = self.ask({"version": 2, "op": "act", "id": "NOSUCH01", "action": "select"})
        self.assertEqual(unknown["error"]["code"], "not_found")

    def test_version_1_search_and_resolve_keep_their_old_shape(self):
        terms = [{"text": "etude", "literal": False, "alternatives": []}]
        found = self.ask({"version": 1, "op": "search", "query": "etude", "terms": terms,
                          "exact": False, "limit": 25, "format": None, "where": "all"})
        self.assertEqual([(row["id"], row["name"]) for row in found], [("ONDE1895", "Étude des ondes longues")])
        bare = self.ask({"version": 1, "op": "search", "query": "channels",
                         "terms": [{"text": "channels", "literal": False, "alternatives": []}],
                         "exact": False, "limit": 25, "format": None, "where": "all"})
        self.assertEqual([(row["id"], row["name"]) for row in bare], [("CHAN1880", "Wave motion in channels")])
        opened = self.ask({"version": 1, "op": "resolve", "id": "WAVE1907"})
        self.assertEqual(set(opened), {"path"})
        self.assertEqual(Path(opened["path"]).resolve(), self.djvu.resolve())
        shown = self.ask({"version": 1, "op": "resolve", "id": "CHAN1880"})
        self.assertEqual(shown, {"uri": "zotero://select/library/items/CHAN1880"})

    def test_a_missing_database_is_refused(self):
        empty = self.folder / "empty"
        empty.mkdir()
        hello = self.ask({"version": 2, "op": "hello"}, folder=empty)
        self.assertEqual(hello["name"], "Zotero")
        refused = self.ask(self.query([("etude", False, [])]), folder=empty)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"]["code"], "unavailable")
        self.assertTrue(refused["error"]["message"].startswith("The "))

    def test_the_data_folder_comes_from_the_option_or_the_environment(self):
        from_env = self.ask(self.query([("etude", False, [])]), folder=None,
                            env={"ZOTERO_DATA_DIR": str(self.library)})
        self.assertEqual(from_env["results"][0]["id"], "ONDE1895")
        empty = self.folder / "empty-option"
        empty.mkdir()
        refused = self.ask(self.query([("etude", False, [])]), folder=empty,
                           env={"ZOTERO_DATA_DIR": str(self.library)})
        self.assertEqual(refused["error"]["code"], "unavailable")

    def test_the_source_runs_beside_a_copy_of_the_helper(self):
        alone = self.folder / "alone"
        alone.mkdir()
        shutil.copy(PROGRAM, alone / "zotero.py")
        shutil.copy(REPOSITORY / "src" / "shelffind" / "provider.py", alone / "provider.py")
        told = self.ask({"version": 2, "op": "hello"}, program=alone / "zotero.py", pythonpath=False)
        self.assertEqual(told["name"], "Zotero")
        found = self.ask(self.query([("etude", False, [])]), program=alone / "zotero.py", pythonpath=False)
        self.assertEqual([row["id"] for row in found["results"]], ["ONDE1895"])


if __name__ == "__main__":
    unittest.main()
