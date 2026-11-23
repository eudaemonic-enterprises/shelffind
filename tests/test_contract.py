# Copyright 2026 Eudaemonic Enterprises Inc.
"""What other programs rely on: the commands, the JSON keys and the exit codes stay as they are."""
import io
import json
import sys
from unittest.mock import patch

from shelffind import __version__
from shelffind.cli import listing, main
from shelffind.search import search
from shelffind.text import json_text, safe

from support import Shelf

STATES = {"pending", "name-only", "indexed", "needs-ocr", "error", "ocr-error"}
HIT_KEYS = {"path", "root", "name", "format", "match", "page", "section", "snippet", "source"}


class Contract(Shelf):
    def setUp(self):
        super().setUp()
        self.doc = self.root / "OrchardCalendar.txt"
        self.doc.write_text("Brass compass below the deck.")
        self.assertEqual(self.build(), (1, 0))

    def test_search_json_keeps_the_file_name_and_a_plain_snippet(self):
        code, out, _ = self.run_cli("search", "--source", "files", "--where", "name", "--json", "orchadr")
        rows = json.loads(out)
        self.assertEqual((code, rows[0]["name"], rows[0]["path"]), (0, self.doc.name, str(self.doc)))
        self.assertLessEqual(HIT_KEYS, set(rows[0]))
        code, out, _ = self.run_cli("search", "--source", "files", "--where", "text", "--json",
                                    '"brass compass"')
        row = json.loads(out)[0]
        self.assertEqual(code, 0)
        self.assertIn("brass compass", row["snippet"].lower())
        self.assertEqual((row["match"], row["source"]), ("passage", "files"))

    def test_status_json_rows_carry_the_states(self):
        (self.root / "notes.bin").write_bytes(b"data")
        self.build()
        code, out, _ = self.run_cli("status", "--json")
        rows = json.loads(out)
        self.assertEqual((code, len(rows)), (0, 1))
        row = rows[0]
        self.assertEqual((row["path"], row["files"]), (str(self.root), 2))
        self.assertEqual(row["states"], {"indexed": 1, "name-only": 1})
        self.assertLessEqual(set(row["states"]), STATES)
        self.assertLessEqual({"available", "scanned", "problem"}, set(row))

    def test_list_prints_the_path_and_then_the_passage(self):
        code, out, _ = self.run_cli("search", "--list", "--where", "text", '"brass compass"')
        lines = out.splitlines()
        self.assertEqual((code, lines[0]), (0, str(self.doc)))
        self.assertTrue(lines[1].startswith("  ") and "compass" in lines[1])

    def test_exit_codes_and_version(self):
        self.assertEqual(self.run_cli("search", "--list", "orchard")[0], 0)
        code, _, err = self.run_cli("search", "--source", "nowhere", "--list", "orchard")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("shelffind: "))
        with patch("shelffind.cli.indexer.build", side_effect=KeyboardInterrupt):
            self.assertEqual(self.run_cli("index")[0], 130)
        code, out, _ = self.run_cli("--version")
        self.assertEqual((code, out.strip()), (0, f"ShelfFind {__version__}"))

    def test_leaving_the_search_screen_with_ctrl_c_is_silent(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        errors = io.StringIO()
        with patch.object(sys, "stdin", Terminal()), patch.object(sys, "stdout", Terminal()), \
                patch.object(sys, "stderr", errors), patch.dict("os.environ", {"SHELFFIND_UI": "curses"}), \
                patch("shelffind.cli.curses.wrapper", side_effect=KeyboardInterrupt):
            code = main(["--data-dir", str(self.data), "search", "orchard"])
        self.assertEqual((code, errors.getvalue()), (130, ""))

    def test_roots_remove_forgets_a_folder_and_leaves_its_documents(self):
        annex = self.base / "Annex"
        annex.mkdir()
        (annex / "lantern.txt").write_text("Copper lantern on the quay.")
        self.assertEqual(self.run_cli("roots", "add", str(annex))[0], 0)
        self.build()
        self.assertTrue(search(self.db, "lantern"))
        code, out, _ = self.run_cli("roots", "remove", "Annex")
        self.assertEqual((code, out.strip()), (0, str(annex)))
        self.assertFalse(search(self.db, "lantern"))
        self.assertFalse(search(self.db, '"copper lantern"'))
        self.assertTrue((annex / "lantern.txt").exists())
        self.assertEqual(len(json.loads(self.run_cli("status", "--json")[1])), 1)
        self.assertTrue(search(self.db, "orchard"))
        self.assertEqual(self.run_cli("roots", "remove", "Annex")[0], 1)


class Printing(Shelf):
    """Names and passages are untrusted text: nothing in them may reach the terminal as a command."""

    risky = "\x1b\x07\x9b‮⁦ "

    def setUp(self):
        super().setUp()
        self.doc = self.root / "tide\x1b[31m‮table.txt"
        self.doc.write_text("Harbour \x9b2J bell ⁦ rings \x1b]0;title\x07 at noon.")
        self.assertEqual(self.build(), (1, 0))

    def test_safe_text(self):
        self.assertEqual(safe("a\x1b[31mb\tc‮d e‍f"), "a [31mb cd e‍f")
        self.assertEqual(safe("é"), "é")
        self.assertEqual(safe(7), "7")

    def test_list_shown_in_a_terminal_carries_no_control_code(self):
        _, out, _ = self.run_cli("search", "--json", "--where", "text", "harbour")
        hits = json.loads(out)
        shown = "\n".join(listing(hits, terminal=True))
        self.assertFalse(set(shown) & set(self.risky))
        self.assertIn("tide", shown)
        self.assertIn("bell", shown)
        piped = list(listing(hits, terminal=False))
        self.assertEqual(piped[0], str(self.doc))
        self.assertFalse(set(piped[1]) & set(self.risky))

    def test_json_escapes_what_a_terminal_could_obey_and_keeps_the_data(self):
        code, out, _ = self.run_cli("search", "--json", "--where", "text", "harbour")
        self.assertEqual(code, 0)
        self.assertFalse(set(out) & set(self.risky))
        row = json.loads(out)[0]
        self.assertEqual(row["path"], str(self.doc))
        self.assertIn("\x9b2J", row["snippet"])
        value = {"text": "x\x7fy​z﻿"}
        self.assertEqual(json.loads(json_text(value)), value)

    def test_status_and_errors_are_cleaned(self):
        broken = self.root / "chart\x1b[2J.pdf"
        broken.write_bytes(b"not a PDF \x1b[5m")
        self.build()
        code, out, _ = self.run_cli("status", "--errors")
        self.assertEqual(code, 0)
        self.assertFalse(set(out) & set(self.risky))
        self.assertIn("chart", out)
