# Copyright 2026 Eudaemonic Enterprises Inc.
"""HTML, Word, OpenDocument, FB2, Typst and BibTeX documents, and formats read by a program of the
user's choice."""
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from shelffind import config, extract, store
from shelffind.search import search

from support import Shelf

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
T = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"


class Formats(Shelf):
    def found(self, query, **opts):
        return [(row["name"], row["page"], row["section"]) for row in search(self.db, query, where="text", **opts)]

    def test_an_html_page_gives_its_text_in_the_encoding_that_it_names_and_not_its_scripts(self):
        (self.root / "page.html").write_bytes(
            '<html><head><title>Cover</title><meta charset="iso-8859-1"></head><body><p>Café orchids</p>'
            '<script>var hidden = 1</script></body></html>'.encode("latin-1"))
        (self.root / "second.htm").write_text("<body><p>Brass compass</p></body>")
        self.assertEqual(self.build(), (2, 0))
        self.assertEqual([name for name, *_ in self.found("café orchids")], ["page.html"])
        self.assertEqual([name for name, *_ in self.found("brass compass")], ["second.htm"])
        self.assertFalse(self.found("hidden"))

    def test_a_word_processor_file_gives_its_paragraphs_across_the_runs_that_cut_them(self):
        body = (f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>Solar</w:t></w:r><w:r><w:tab/></w:r>'
                f'<w:r><w:t>winds blow</w:t></w:r></w:p><w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p></w:body></w:document>')
        with ZipFile(self.root / "letter.docx", "w") as z:
            z.writestr("word/document.xml", body)
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(self.found('"solar winds blow"'), [("letter.docx", None, "part 1")])
        self.assertTrue(self.found("second paragraph"))

    def test_an_opendocument_file_gives_its_headings_paragraphs_and_spaces(self):
        body = (f'<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                f'xmlns:text="{T}"><office:body><office:text><text:h>Brass compass</text:h>'
                f'<text:p>Winds<text:s/>and <text:span>tides</text:span> turn</text:p></office:text></office:body>'
                f'</office:document-content>')
        with ZipFile(self.root / "essay.odt", "w") as z:
            z.writestr("content.xml", body)
        self.assertEqual(self.build(), (1, 0))
        self.assertTrue(self.found('"winds and tides turn"'))
        self.assertTrue(self.found("brass compass"))

    def test_an_fb2_book_gives_its_sections_under_their_titles(self):
        (self.root / "novel.fb2").write_text(
            '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><body>'
            '<section><title><p>The harbour</p></title><p>Lanterns swing in the dark.</p></section>'
            '<section><title><p>The market</p></title><p>Oranges and copper kettles.</p></section>'
            '</body></FictionBook>')
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(self.found("lanterns swing"), [("novel.fb2", None, "The harbour")])
        self.assertEqual(self.found("copper kettles"), [("novel.fb2", None, "The market")])

    def test_typst_and_bibtex_files_are_read_as_they_are(self):
        (self.root / "notes.typ").write_text("= Waves\nThe dispersion relation of water waves.")
        (self.root / "refs.bib").write_text("@article{whitham1967, title={Nonlinear dispersion of water waves}}")
        self.assertEqual(self.build(), (2, 0))
        self.assertEqual([n for n, *_ in self.found("dispersion relation")], ["notes.typ"])
        self.assertEqual([n for n, *_ in self.found("whitham1967")], ["refs.bib"])

    def test_a_long_document_without_pages_is_cut_into_passages_of_whole_paragraphs(self):
        paragraphs = [f"Paragraph {n} " + "word " * 120 for n in range(40)]
        pieces = list(extract.pieces(paragraphs, "part"))
        self.assertGreater(len(pieces), 3)
        self.assertEqual([p.section for p in pieces], [f"part {n}" for n in range(1, len(pieces) + 1)])
        self.assertEqual("\n".join(p.text for p in pieces), "\n".join(paragraphs))
        self.assertTrue(all(len(p.text) < extract.CHUNK + 800 for p in pieces))
        self.assertEqual([p.text for p in extract.pieces([], "part")], [""])

    def test_an_archive_that_unpacks_to_too_much_is_refused(self):
        with ZipFile(self.root / "bomb.docx", "w", ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", b"<a>" + b"x" * (extract.MAX_TEXT + 10) + b"</a>")
        self.assertEqual(self.build(), (1, 1))
        self.assertEqual(self.db.execute("SELECT state FROM docs").fetchone()[0], "error")

    def test_files_of_a_format_read_since_are_promoted_from_their_names_alone(self):
        (self.root / "novel.fb2").write_text('<FictionBook><body><section><p>Lanterns swing.</p></section></body></FictionBook>')
        with patch.object(extract, "SUPPORTED", extract.SUPPORTED - {"fb2"}):
            self.assertEqual(self.build(), (0, 0))
            self.assertEqual(self.db.execute("SELECT state FROM docs").fetchone()[0], "name-only")
        self.assertEqual(self.build(), (1, 0))
        self.assertTrue(self.found("lanterns swing"))


class Converters(Shelf):
    def declared(self, **tables):
        extract.declare(tables)
        self.addCleanup(extract.declare, {})

    def test_a_format_read_by_a_program_of_the_users_choice(self):
        folder = self.fake_tool("shout", 'tr a-z A-Z < "$1"\n')
        self.declared(zzz={"command": [str(folder / "shout"), "{path}"]})
        (self.root / "note.zzz").write_text("quiet harbour")
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual([r["name"] for r in search(self.db, "harbour", where="text")], ["note.zzz"])
        self.assertEqual(search(self.db, "harbour", where="text")[0]["section"], "part 1")

    def test_a_converter_that_fails_is_the_fault_of_the_document(self):
        folder = self.fake_tool("broken", "echo 'cannot read it' >&2\nexit 3\n")
        self.declared(zzz={"command": [str(folder / "broken"), "{path}"]})
        (self.root / "note.zzz").write_text("quiet harbour")
        self.assertEqual(self.build(), (1, 1))
        error = self.db.execute("SELECT error FROM docs").fetchone()[0]
        self.assertIn("cannot read it", error)

    def test_without_the_converter_the_file_stays_a_name(self):
        (self.root / "note.zzz").write_text("quiet harbour")
        self.assertEqual(self.build(), (0, 0))
        self.assertEqual(self.db.execute("SELECT state FROM docs").fetchone()[0], "name-only")

    def test_the_settings_accept_a_command_that_names_the_document_and_no_other(self):
        folder = self.base / "settings"
        folder.mkdir()
        for text, good in (('[convert.doc]\ncommand = ["antiword", "{path}"]\n', True),
                           ('[convert.doc]\ncommand = ["antiword"]\n', False),
                           ('[convert.doc]\ncommand = "antiword {path}"\n', False),
                           ('[convert.doc]\ncommand = ["antiword", "{path}"]\nextra = 1\n', False)):
            (folder / "config.toml").write_text(text)
            if good:
                settings = config.load(folder, True, environ={})
                self.assertEqual(settings.tables["convert"], {"doc": {"command": ["antiword", "{path}"]}})
                self.assertTrue(any(line.startswith("convert.doc") for line in config.report(settings)))
            else:
                with self.assertRaises(config.ConfigError, msg=text):
                    config.load(folder, True, environ={})


if __name__ == "__main__":
    unittest.main()
