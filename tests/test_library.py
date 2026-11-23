# Copyright 2026 Eudaemonic Enterprises Inc.
"""A source that knows files of the folders: one row per document, the text left to the source."""
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from shelffind import extract, indexer, sources, store
from shelffind.search import search

from support import Shelf

SOURCE = Path(__file__).resolve().parent.parent / "src"
# A small library of full texts: its catalogue is the JSON file beside the program.
LIBRARY = """#!/usr/bin/env python3
import json, os, sys
sys.path.insert(0, {source!r})
from shelffind.provider import serve, Refusal

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "catalogue.json")) as catalogue:
    STATE = json.load(catalogue)
WORKS = STATE["works"]
if STATE.get("closed") and STATE["closed"] != "refuse":
    raise SystemExit(STATE["closed"])

def shown(work, **more):
    row = {{key: work[key] for key in ("id", "title", "byline", "year", "keys", "flags") if key in work}}
    if work.get("path") and os.path.exists(work["path"]):
        row["path"] = work["path"]
    return {{**row, "fetch": True, "actions": ["cite"], "notes": len(work.get("notes", [])) or None, **more}}

def search(request):
    if STATE.get("closed") == "refuse":
        raise Refusal("unavailable", "The library index is being rebuilt", retry=True)
    words = [[term["text"], *term["alternatives"]] for term in request["terms"]]
    found = []
    for work in WORKS:
        if request["within"] and work["id"] != request["within"]:
            continue
        name = (work["title"] + " " + work["byline"]).lower()
        if request["where"] != "text" and not request["within"] and all(any(w in name for w in group) for group in words):
            found.append(shown(work, match="bibliography"))
            continue
        for number, page in enumerate(work.get("pages", []), 1):
            if request["where"] != "name" and all(any(w in page.lower() for w in group) for group in words):
                start = page.lower().find(words[0][0])
                found.append(shown(work, match="passage", page=number, page_label=str(number + 10), snippet=page,
                                   marks=[[start, start + len(words[0][0])]]))
                if not request["within"]:
                    break
    return found[:request["limit"]]

def identify(request):
    records = []
    for file in request["files"]:
        for work in WORKS:
            by_key = set(file["keys"]) & set(work.get("keys", []))
            if by_key or (work.get("size") == file["size"] and work.get("file") == os.path.basename(file["path"])):
                records.append({{"n": file["n"], "id": work["id"], "basis": "sha256" if by_key else "name and size",
                                "keys": work.get("keys", []), "title": work["title"], "byline": work["byline"],
                                "year": work["year"], "flags": work.get("flags", []), "text": bool(work.get("pages"))}})
                break
    return records

def pages(request):
    work = next(w for w in WORKS if w["id"] == request["id"])
    first = request.get("first", 1)
    chosen = work["pages"][first - 1:first - 1 + request.get("count", 1)]
    return {{"total": len(work["pages"]), "pages": [{{"page": first + i, "label": str(first + i + 10), "text": text}}
                                                  for i, text in enumerate(chosen)]}}

def resolve(request):
    work = next((w for w in WORKS if w["id"] == request["id"]), None)
    if work is None:
        raise Refusal("not_found", "No such work")
    return {{"path": work["path"]}} if work.get("path") else {{"uri": "https://example.org/works/" + work["id"]}}

def act(request):
    return {{"result": "copy", "text": request["id"].lower() + "-key"}}

serve(name="Library", search=search, identify=identify, pages=pages, resolve=resolve, act=act, text=True,
      network=STATE.get("network", False),
      actions=[{{"name": "cite", "label": "Copy the citation key", "result": "copy"}}])
"""
BOOK = "Chapter eleven. " * 5000        # stands for a scanned book that only the library has read


class Linked(Shelf):
    def setUp(self):
        super().setUp()
        self.book = self.root / "Waves" / "scan-0042.txt"
        self.book.parent.mkdir()
        self.book.write_text(BOOK)
        (self.root / "Waves" / "notes.txt").write_text("My notes on the group velocity.")
        self.checksum = "sha256:" + hashlib.sha256(BOOK.encode()).hexdigest()
        self.folder = self.base / "library"
        self.folder.mkdir()
        self.works = [
            {"id": "W1", "title": "Linear and Nonlinear Waves", "byline": "G. B. Whitham", "year": 1974,
             "file": "scan-0042.txt", "size": len(BOOK), "keys": [self.checksum], "flags": ["unverified"],
             "notes": ["a note"], "pages": ["Preface", "The dispersion relation gives the group velocity."]},
            {"id": "W2", "title": "Waves in Fluids", "byline": "J. Lighthill", "year": 1978,
             "pages": ["Internal waves and the group velocity of a wave packet."]}]
        self.catalogue()
        program = self.folder / "library.py"
        program.write_text(LIBRARY.format(source=str(SOURCE)))
        program.chmod(0o700)
        sources.configure(self.data, "library", program)
        self.read = []
        original = extract.passages

        def counted(path, fmt, work):
            self.read.append(path.name)
            return original(path, fmt, work)

        watcher = patch("shelffind.extract.passages", side_effect=counted)
        watcher.start()
        self.addCleanup(watcher.stop)

    def catalogue(self, **state):
        (self.folder / "catalogue.json").write_text(json.dumps({"works": self.works, **state}))

    def find(self, query, **options):
        seen = []
        opts = {"root": None, "fmt": None, "exact": False, "limit": 25, "source": None, "where": "all", **options}
        sources.find(self.db, self.data, query, opts, lambda rows, message: seen.append((rows, message)))
        return seen[-1]

    def states(self):
        return dict(self.db.execute("SELECT f.name,d.state FROM files f JOIN docs d ON d.id=f.doc"))

    def test_a_file_the_library_holds_is_not_read_and_is_found_by_its_title(self):
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(self.read, ["notes.txt"])
        self.assertEqual(self.states(), {"scan-0042.txt": "covered", "notes.txt": "indexed"})
        found = search(self.db, "nonlinear whitham")
        self.assertEqual([(hit["name"], hit["title"], hit["byline"], hit["year"]) for hit in found],
                         [("scan-0042.txt", "Linear and Nonlinear Waves", "G. B. Whitham", "1974")])
        keys = {row[0] for row in self.db.execute("SELECT key FROM doc_keys WHERE basis='source'")}
        self.assertEqual(keys, {self.checksum})
        self.assertEqual(self.build(), (0, 0))              # nothing more to ask, nothing to read
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(json.loads(out)[0]["states"], {"covered": 1, "indexed": 1})

    def test_a_passage_of_the_library_appears_on_the_row_of_the_shelf_file(self):
        self.build()
        rows, message = self.find("dispersion relation")
        self.assertEqual(message, "")
        self.assertEqual(len(rows), 1)
        hit = rows[0]
        self.assertEqual((hit["source"], hit["path"], hit["title"]), ("files", str(self.book), "Linear and Nonlinear Waves"))
        self.assertEqual((hit["match"], hit["page"], hit["page_label"]), ("passage", 2, "12"))
        self.assertEqual(hit["snippet"][hit["marks"][0][0]:hit["marks"][0][1]], "dispersion")
        self.assertEqual(hit["record"], {"source": "library", "id": "W1", "actions": ["cite"]})
        self.assertEqual((hit["flags"], hit["notes"]), (["unverified"], 1))
        # Both know the group velocity: the note of the shelf, and two works of the library, of
        # which one is the shelf file again and the other exists only there.
        rows, _ = self.find("group velocity")
        self.assertEqual(sorted((hit["source"], hit["name"]) for hit in rows),
                         [("files", "notes.txt"), ("files", "scan-0042.txt"), ("library", "Waves in Fluids")])
        code, out, _ = self.run_cli("search", "--json", "whitham")
        listed = json.loads(out)
        self.assertEqual([(hit["name"], hit["source"]) for hit in listed], [("scan-0042.txt", "files")])
        self.assertNotIn("doc", listed[0])                  # what is private to the index stays in it

    def test_the_text_is_read_again_once_the_library_is_gone_but_not_when_it_falters(self):
        self.build()
        self.read.clear()
        for closed in ("The library is closed for the night", "refuse"):
            self.catalogue(closed=closed)
            self.assertEqual(self.build(), (0, 0))
            self.assertEqual((self.read, self.states()["scan-0042.txt"]), ([], "covered"))
        rows, message = self.find("dispersion relation")
        self.assertEqual(rows, [])
        self.assertIn("The library index is being rebuilt", message)
        sources.configure(self.data, "library", None)
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual((self.read, self.states()["scan-0042.txt"]), (["scan-0042.txt"], "indexed"))
        self.assertEqual(search(self.db, "chapter eleven")[0]["name"], "scan-0042.txt")

    def test_the_settings_can_keep_the_reading_at_home(self):
        program = sources.registered(self.data)["library"]
        sources.configure(self.data, "library", None)
        sources.declare({"library": {"command": program, "text": "ignore"}})
        self.addCleanup(sources.declare, {})
        self.assertEqual(self.build(), (2, 0))
        self.assertEqual(self.states(), {"scan-0042.txt": "indexed", "notes.txt": "indexed"})
        self.assertEqual(search(self.db, "whitham")[0]["name"], "scan-0042.txt")    # the title is taken all the same

    def test_two_files_with_one_checksum_are_one_document(self):
        second = self.root / "Desk" / "scan-0042.txt"
        second.parent.mkdir()
        second.write_text(BOOK)
        os.utime(second, (1_600_000_000, 1_600_000_000))        # another date: the inventory keeps them apart
        self.build()
        self.assertEqual(self.db.execute("SELECT count(DISTINCT doc) FROM files WHERE name='scan-0042.txt'").fetchone()[0], 1)
        found = search(self.db, "whitham")
        self.assertEqual((len(found), len(found[0]["copies"])), (1, 1))

    def test_a_source_that_needs_the_network_is_asked_only_by_name(self):
        self.build()
        self.catalogue(network=True)
        (self.folder / "library.py").touch()                # its program changed: it is asked again what it can do
        os.utime(self.folder / "library.py", (1_900_000_000, 1_900_000_000))
        self.assertEqual(self.find("lighthill"), ([], ""))
        rows, _ = self.find("lighthill", source="library")
        self.assertEqual([hit["name"] for hit in rows], ["Waves in Fluids"])
        rows, _ = self.find("lighthill src:library")
        self.assertEqual(len(rows), 1)

    def test_the_lists_for_keeping_the_folders_in_order(self):
        second = self.root / "Desk" / "scan-0042.txt"
        second.parent.mkdir()
        second.write_text(BOOK)
        os.utime(second, (self.book.stat().st_mtime, self.book.stat().st_mtime))
        named = self.root / "Waves" / "Lighthill-WavesInFluids-CUP-1978.txt"
        named.write_text("Internal waves. " * 5000)
        self.works[1].update(file=named.name, size=named.stat().st_size)
        self.catalogue()
        self.build()
        code, out, err = self.run_cli("report", "copies", "--json")
        self.assertEqual([(row["title"], sorted(row["paths"])) for row in json.loads(out)],
                         [("Linear and Nonlinear Waves", sorted([str(self.book), str(second)]))])
        self.assertEqual(json.loads(self.run_cli("report", "copies", "--json", "--root", "Reading Room")[1]),
                         json.loads(out))
        code, out, err = self.run_cli("report", "unlinked")
        self.assertEqual((out.split("\n")[0], err.strip()), (str(self.root / "Waves" / "notes.txt"), "1 files no source knows"))
        listed = json.loads(self.run_cli("report", "names", "--json")[1])
        self.assertEqual([(Path(row["path"]).name, row["lacks"]) for row in listed],
                         [("scan-0042.txt", ["author", "title", "year"])] * 2)   # the well-named file is not listed
        code, out, _ = self.run_cli("report", "names")
        self.assertIn("    lacks author, title, year: G. B. Whitham (1974) Linear and Nonlinear Waves", out)

    def test_a_source_is_checked_question_by_question(self):
        code, out, _ = self.run_cli("sources", "check", "library", "group", "velocity")
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("[ ok ] hello     version 2 · search, resolve, identify, pages, act"))
        self.assertIn("holds text · works offline", lines[0])
        self.assertIn("[ ok ] action    cite: Copy the citation key (copy)", lines)
        self.assertTrue(any(line.startswith('[ ok ] search    2 results for "group velocity"') for line in lines))
        self.assertTrue(any(line.startswith("[ ok ] resolve") for line in lines))
        self.assertIn("[ ok ] identify  a file that does not exist is not recognised", lines)
        self.assertIn("[ ok ] pages     1 page of 2", lines)
        self.catalogue(closed="refuse")
        code, out, _ = self.run_cli("sources", "check", "library", "group")
        self.assertEqual(code, 1)
        self.assertIn("[fail] search    The library index is being rebuilt", out.splitlines())
        old = self.base / "old-source"
        old.write_text("#!/bin/sh\ncat > /dev/null\necho '[{\"id\": \"a\", \"name\": \"A record\"}]'\n")
        old.chmod(0o700)
        sources.configure(self.data, "old", old)
        code, out, _ = self.run_cli("sources", "check", "old")
        self.assertIn("version 1", out.splitlines()[0])
        self.assertIn('1 results for "the"', out)
        self.assertEqual(self.run_cli("sources", "check", "nobody")[0], 1)

    def test_the_copy_of_the_library_on_this_machine_is_offered_for_opening(self):
        kept = self.folder / "9f2c.txt"
        kept.write_text(BOOK)
        self.works[0]["path"] = str(kept)
        self.catalogue()
        self.build()
        rows, _ = self.find("dispersion relation")
        self.assertEqual((rows[0]["path"], rows[0]["fast"]), (str(self.book), str(kept)))
        self.assertEqual(sources.resolve(self.data, "library", "W1"), {"path": str(kept)})
        self.assertEqual(sources.resolve(self.data, "library", "W2"), {"uri": "https://example.org/works/W2"})
        with self.assertRaises(ValueError) as refused:
            sources.resolve(self.data, "library", "W9")
        self.assertEqual(str(refused.exception), "No such work")
        self.assertEqual(sources.question(self.data, "library", "act", id="W1", action="cite")["text"], "w1-key")
        told = sources.question(self.data, "library", "pages", id="W1", first=2, count=1)
        self.assertEqual((told["total"], told["pages"][0]["label"]), (2, "12"))
        self.assertEqual(sources.actions(self.data, "library"), {"cite": ("Copy the citation key", "copy")})


if __name__ == "__main__":
    unittest.main()
