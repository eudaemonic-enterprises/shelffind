# Copyright 2026 Eudaemonic Enterprises Inc.
"""The one-file helper for writing a source, and the example of the reference page."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shelffind import provider
from shelffind.provider import Fault, Refusal, answer, check_answer, check_record

REPOSITORY = Path(__file__).resolve().parent.parent
SHA = "sha256:" + "9f" * 32


def search(request):
    if request["query"] == "refuse":
        raise Refusal("unavailable", "The catalogue is closed", retry=True)
    if request["query"] == "break":
        raise KeyError("shelf")
    if request["query"] == "faulty":
        return [{"id": "a1", "title": "Tide Tables", "keys": ["SHA256:XYZ"]}]
    return [{"id": "a1", "title": "Tide Tables", "byline": "Admiralty", "year": 1936, "keys": [SHA],
             "match": "passage", "page": 12, "snippet": "the spring tide", "marks": [[4, 10]],
             "within": request["within"]}]


class Answers(unittest.TestCase):
    def ask(self, request, **more):
        return answer(request, name="Catalogue", search=search, **more)

    def test_hello_tells_what_the_source_can_do(self):
        told, failure = self.ask({"version": 2, "op": "hello", "client": "shelffind/test"},
                                 resolve=lambda request: {"uri": "https://example.org/" + request["id"]},
                                 actions=[{"name": "cite", "label": "Copy the citation key", "result": "copy"}],
                                 text=True)
        self.assertIsNone(failure)
        self.assertEqual(told, {"ok": True, "version": 2, "name": "Catalogue", "ops": ["search", "resolve"],
                                "text": True, "network": False,
                                "actions": [{"name": "cite", "label": "Copy the citation key", "result": "copy"}]})
        self.assertEqual(check_answer("hello", told), told)

    def test_a_search_is_answered_in_both_versions(self):
        new, _ = self.ask({"version": 2, "op": "search", "query": "tide", "terms": []})
        self.assertEqual((new["ok"], new["partial"], new["results"][0]["title"]), (True, False, "Tide Tables"))
        self.assertIsNone(new["results"][0]["within"])           # the fields a caller left out are filled
        old, failure = self.ask({"version": 1, "op": "search", "query": "tide", "terms": [], "limit": 5})
        self.assertIsNone(failure)
        self.assertEqual((old[0]["id"], old[0]["name"]), ("a1", "Tide Tables"))

    def test_a_refusal_an_accident_and_a_faulty_answer_are_all_answers(self):
        refused, _ = self.ask({"version": 2, "op": "search", "query": "refuse", "terms": []})
        self.assertEqual(refused, {"ok": False, "error": {"code": "unavailable", "retry": True,
                                                          "message": "The catalogue is closed"}})
        broken, _ = self.ask({"version": 2, "op": "search", "query": "break", "terms": []})
        self.assertEqual((broken["ok"], broken["error"]["code"]), (False, "failed"))
        self.assertIn("KeyError", broken["error"]["message"])
        faulty, _ = self.ask({"version": 2, "op": "search", "query": "faulty", "terms": []})
        self.assertIn("neither sha256: nor md5:", faulty["error"]["message"])
        unknown, _ = self.ask({"version": 2, "op": "pages", "id": "a1"})
        self.assertEqual(unknown["error"]["code"], "invalid")
        self.assertEqual(self.ask({"version": 3, "op": "search"})[0]["error"]["code"], "invalid")
        self.assertEqual(self.ask(None)[0]["error"]["code"], "invalid")
        # Version 1 cannot refuse: the failure becomes the message of a failed program.
        self.assertEqual(self.ask({"version": 1, "op": "search", "query": "refuse", "terms": []}),
                         (None, "The catalogue is closed"))
        self.assertEqual(self.ask({"version": 1, "op": "identify"})[1], "Unknown question: identify")

    def test_answers_are_checked_field_by_field(self):
        good = {"id": "a1", "title": "Tide Tables", "keys": [SHA, "md5:" + "0a" * 16], "path": "/srv/a1.pdf",
                "flags": ["unverified"], "notes": 2, "marks": [[0, 4]], "year": "1936"}
        self.assertIs(check_record(good), good)
        faults = [{"id": "a1"}, {"title": "no id"}] + [{**good, **change} for change in (
            {"id": 7}, {"keys": ["sha256:abc"]}, {"path": "relative/a.pdf"}, {"marks": [[4, 2]]}, {"page": -1},
            {"notes": "two"}, {"flags": "retracted"}, {"byline": 3}, {"year": 19.36})]
        for fault in faults:
            with self.assertRaises(Fault, msg=fault):
                check_record(fault)
        self.assertEqual(check_record({"id": "a1", "n": 0}, "record")["n"], 0)       # identify needs no title
        for op, body in (("resolve", {"path": "a.pdf"}), ("resolve", {}), ("pages", {"pages": [{"page": 1}]}),
                         ("act", {"result": "dance"}), ("act", {"result": "open"}), ("identify", {"records": [{"id": "a"}]}),
                         ("search", {})):
            with self.assertRaises(Fault, msg=(op, body)):
                check_answer(op, {"ok": True, **body})
        self.assertTrue(check_answer("act", {"ok": True, "result": "copy", "text": "tide1936"}))
        self.assertTrue(check_answer("resolve", {"ok": True, "uri": "zotero://select/library/items/A1"}))
        self.assertTrue(check_answer("pages", {"ok": True, "total": 1, "pages": [{"page": 1, "text": "Tides"}]}))


class Programs(unittest.TestCase):
    """A source run as ShelfFind runs it: a program, a question on its input, an answer on its output."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.environment = {**os.environ, "PYTHONPATH": str(REPOSITORY / "src")}

    def run_source(self, text, request):
        program = self.folder / "source.py"
        program.write_text(text)
        return subprocess.run([sys.executable, str(program)], input=json.dumps(request), capture_output=True,
                              text=True, env=self.environment, timeout=20)

    def test_the_example_of_the_reference_page_works_as_written(self):
        page = (REPOSITORY / "docs" / "sources.md").read_text()
        example = re.search(r"```python\n(.*?)```", page, re.S).group(1)
        told = json.loads(self.run_source(example, {"version": 2, "op": "hello", "client": "test"}).stdout)
        self.assertEqual((told["name"], told["ops"]), ("Example", ["search", "resolve"]))
        found = self.run_source(example, {"version": 2, "op": "search", "query": "tide",
                                          "terms": [{"text": "tide", "literal": False, "alternatives": []}]})
        self.assertEqual(found.returncode, 0)
        self.assertEqual([row["id"] for row in json.loads(found.stdout)["results"]], ["a1"])
        nothing = self.run_source(example, {"version": 2, "op": "search", "query": "glacier",
                                            "terms": [{"text": "glacier", "literal": False, "alternatives": []}]})
        self.assertEqual(json.loads(nothing.stdout)["results"], [])
        place = self.run_source(example, {"version": 1, "op": "resolve", "id": "a1"})
        self.assertEqual(json.loads(place.stdout), {"uri": "https://example.org/records/a1"})

    def test_a_failure_of_version_1_ends_the_program_with_its_message(self):
        text = ("from shelffind.provider import serve, Refusal\n"
                "def search(request):\n    raise Refusal('unavailable', 'The catalogue is closed')\n"
                "serve(search=search)\n")
        failed = self.run_source(text, {"version": 1, "op": "search", "query": "x", "terms": []})
        self.assertEqual((failed.returncode, failed.stdout, failed.stderr.strip()), (1, "", "The catalogue is closed"))
        refused = self.run_source(text, {"version": 2, "op": "search", "query": "x", "terms": []})
        self.assertEqual((refused.returncode, json.loads(refused.stdout)["ok"]), (0, False))
        garbled = subprocess.run([sys.executable, str(self.folder / "source.py")], input="{", capture_output=True,
                                 text=True, env=self.environment, timeout=20)
        self.assertEqual(json.loads(garbled.stdout)["error"]["code"], "invalid")

    def test_the_helper_stands_alone(self):
        copied = self.folder / "provider.py"
        copied.write_text(Path(provider.__file__).read_text())
        text = "from provider import serve\nserve(name='Alone', search=lambda request: [])\n"
        (self.folder / "source.py").write_text(text)
        alone = subprocess.run([sys.executable, str(self.folder / "source.py")], input='{"version":2,"op":"hello"}',
                               capture_output=True, text=True, cwd=self.folder, timeout=20,
                               env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
        self.assertEqual(alone.stderr, "")
        self.assertEqual(json.loads(alone.stdout)["name"], "Alone")


if __name__ == "__main__":
    unittest.main()
