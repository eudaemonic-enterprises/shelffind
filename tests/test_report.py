# Copyright 2026 Eudaemonic Enterprises Inc.
"""What `search --list`, `status` and `index` print at a terminal, and that a pipe still receives
the plain forms."""
import re
import sys
import unittest

from shelffind.tui import report
from shelffind.tui.style import Theme

from support import Shelf
from test_panel_terminal import Session, SOURCE

SGR = re.compile(r"\x1b\[[0-9;]*m")
OSC = re.compile(r"\x1b\][^\x1b\x07]*(?:\x07|\x1b\\)")


def text(line):
    return OSC.sub("", SGR.sub("", line))


ROWS = [
    {"path": "/lib/Articles", "available": True, "scanned": "2026-11-23", "problem": None, "files": 1000,
     "states": {"indexed": 420, "needs-ocr": 30, "pending": 500, "error": 10, "name-only": 40}},
    {"path": "/lib/Books", "available": False, "scanned": None, "problem": None, "files": 12345,
     "states": {"pending": 12345}},
]


class Printed(unittest.TestCase):
    def test_results_carry_a_link_the_page_and_the_lit_words(self):
        hits = [{"source": "files", "path": "/lib/Books/a b.pdf", "format": "pdf", "name": "a b.pdf",
                 "page": 12, "snippet": "the group velocity", "marks": [[4, 9]], "match": "passage"},
                {"source": "library", "path": None, "format": "pdf", "name": "On waves\x1b[2J", "page": None,
                 "snippet": None, "marks": [], "match": "bibliography", "byline": "Kelvin (1887).",
                 "availability": "published full text"}]
        lines = list(report.results(hits, Theme(colours=16)))
        self.assertIn("\x1b]8;;file:///lib/Books/a%20b.pdf\x1b\\", lines[0])
        self.assertEqual([text(line) for line in lines],
                         ["pdf   /lib/Books/a b.pdf", "      p. 12  the group velocity",
                          "library On waves [2J  (published full text)", "      Kelvin (1887)."])
        self.assertIn("\x1b[0;1;33mgroup", lines[1])                # the match is bold and yellow
        plain = list(report.results(hits, Theme(colours=0), links=False))
        self.assertNotIn("\x1b]8", plain[0])
        self.assertNotIn("\x1b[2J", "".join(lines))

    def test_the_status_table_has_a_bar_and_the_counts_of_each_folder(self):
        lines = [text(line) for line in report.status(ROWS, Theme(colours=0), "0.2.1", ["library"])]
        self.assertEqual(lines[0], "ShelfFind 0.2.1 · 13 345 files in 2 folders")
        self.assertEqual(lines[2].split(), ["Folder", "Files", "Text", "at", "hand", "Waiting", "Scans", "Problems"])
        self.assertEqual(lines[3], "Articles     1 000  █████████░░░░░░░░░░░     450       500       30        10")
        self.assertEqual(lines[4], "Books       12 345  ░░░░░░░░░░░░░░░░░░░░       0    12 345        0         0")
        self.assertIn("not reachable now", lines[5])
        self.assertEqual(lines[-1], "Sources   files · library")
        ascii_lines = [text(line) for line in report.status(ROWS, Theme(colours=0), "0.2.1", [], unicode=False)]
        self.assertTrue("\n".join(ascii_lines).isascii())
        self.assertIn("#########...........", ascii_lines[3])
        empty = [text(line) for line in report.status([], Theme(colours=0), "0.2.1", [])]
        self.assertIn("shelffind roots add FOLDER", empty[1])

    def test_the_live_line_is_rewritten_in_place_and_cleared(self):
        written = []
        live = report.Progress(written.append, lambda: (60, 20), Theme(colours=0))
        live.document(4, 200, "Waves/" + "a very long name " * 6 + ".pdf")
        line = text(written[-1])
        self.assertTrue(line.startswith("\rReading 5 of 200    2%  …"))
        self.assertLessEqual(len(line.rstrip("\x1b[K").strip("\r")), 59)
        live.message("Could not read x.pdf: damaged\x1b[2J")
        self.assertEqual(written[-2:], ["\r\x1b[K", "Could not read x.pdf: damaged [2J\n"])
        live.clear()
        self.assertEqual(len(written), 3)                   # nothing more to clear


class AtATerminal(Shelf):
    def setUp(self):
        super().setUp()
        (self.root / "OrchardCalendar.txt").write_text("Brass compass below the deck.")
        (self.root / "notes.bin").write_bytes(b"data")
        self.build()
        (self.root / "HarbourTides.txt").write_text("The tide turns at noon.")

    def run_at_terminal(self, *arguments):
        argv = [sys.executable, "-m", "shelffind", "--data-dir", str(self.data), *arguments]
        env = {"PYTHONPATH": SOURCE, "TERM": "xterm-256color", "LC_ALL": "C.UTF-8", "HOME": str(self.base)}
        session = Session(argv, cols=110, rows=20, env=env)
        self.addCleanup(lambda: session.process.kill() if session.process.poll() is None else None)
        code, _ = session.finish()
        return code, session

    def test_status_search_and_index_draw_for_a_person(self):
        code, session = self.run_at_terminal("status")
        shown = session.screen.text()
        self.assertEqual(code, 0)
        self.assertTrue(shown[0].startswith("ShelfFind") and "2 files in 1 folder" in shown[0])
        self.assertTrue(any(line.startswith("Reading Room") and "█" in line for line in shown), shown)
        code, session = self.run_at_terminal("search", "--list", "--where", "text", "compass")
        self.assertEqual(code, 0)
        self.assertIn(b"\x1b]8;;file://", session.raw)
        self.assertTrue(any(line.endswith("OrchardCalendar.txt") for line in session.screen.text()))
        self.assertTrue(any("Brass compass below the deck." in line for line in session.screen.text()))
        code, session = self.run_at_terminal("index")
        shown = [line for line in session.screen.text() if line]
        self.assertEqual(code, 0)
        self.assertIn(b"Reading 1 of 1", session.raw)
        self.assertEqual(shown[-1], "1 documents processed, 0 problems")
        self.assertFalse(any(line.startswith("Reading 1 of") for line in shown))    # the live line is gone

    def test_a_pipe_still_receives_the_plain_forms(self):
        code, out, _ = self.run_cli("status")
        self.assertEqual(out, f"{self.root}   2 files   1 indexed, 1 name-only\n")
        code, out, _ = self.run_cli("search", "--list", "--where", "text", "compass")
        self.assertEqual(out.splitlines()[0], str(self.root / "OrchardCalendar.txt"))
        self.assertNotIn("\x1b", out)


if __name__ == "__main__":
    unittest.main()
