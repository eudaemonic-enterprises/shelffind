# Copyright 2026 Eudaemonic Enterprises Inc.
"""HTML, Word, OpenDocument, FB2, Typst and BibTeX documents, and formats read by a program of the
user's choice."""
import os
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


class Faults(Shelf):
    """What a review found: readers that lost or doubled text, and what a removed converter left."""

    def found(self, query):
        return [(row["name"], row["section"]) for row in search(self.db, query, where="text")]

    def test_an_html_page_whose_head_is_not_closed_keeps_its_body_and_its_title(self):
        (self.root / "open.html").write_text("<html><head><title>Tide tables</title><body><p>Hello world body text</p>")
        self.assertEqual(self.build(), (1, 0))
        self.assertTrue(self.found('"hello world body text"'))
        self.assertTrue(self.found("tide tables"))

    def test_html_blocks_do_not_run_their_words_together(self):
        (self.root / "blocks.html").write_text(
            "<table><tr><th>Name</th><th>Age</th></tr></table><dl><dt>Term</dt><dd>Definition</dd></dl>"
            "<h4>Subsection</h4>Following text<blockquote>Quoted</blockquote>after<pre>code</pre>tail"
            "<template><p>hidden template</p></template><noscript>hidden fallback</noscript>")
        self.assertEqual(self.build(), (1, 0))
        for word in ("age", "definition", "following", "after", "tail"):
            self.assertTrue(self.found(word), word)
        self.assertFalse(self.found("nameage") or self.found("termdefinition") or self.found("subsectionfollowing"))
        self.assertFalse(self.found("template") or self.found("fallback"))

    def test_a_text_box_a_nested_paragraph_deleted_words_and_field_codes_in_a_docx(self):
        mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
        body = (f'<w:document xmlns:w="{W}" xmlns:mc="{mc}"><w:body>'
                f'<w:p><w:r><w:t>Before box.</w:t></w:r><mc:AlternateContent><mc:Choice><w:txbxContent>'
                f'<w:p><w:r><w:t>Box text</w:t></w:r></w:p></w:txbxContent></mc:Choice><mc:Fallback><w:pict>'
                f'<w:p><w:r><w:t>Box text</w:t></w:r></w:p></w:pict></mc:Fallback></mc:AlternateContent>'
                f'<w:r><w:t>After box.</w:t></w:r></w:p>'
                f'<w:p><w:del><w:r><w:delText>deleted words</w:delText></w:r></w:del><w:r><w:instrText>PAGEREF _Toc12345</w:instrText></w:r>'
                f'<w:r><w:t>Kept words</w:t></w:r></w:p></w:body></w:document>')
        with ZipFile(self.root / "box.docx", "w") as z:
            z.writestr("word/document.xml", body)
        self.assertEqual(self.build(), (1, 0))
        text = " ".join(store_text(self.db))
        self.assertEqual(text.count("Box text"), 1)
        self.assertNotIn("box.Box", text)
        self.assertIn("Before box.", text)
        self.assertIn("After box.", text)
        self.assertIn("Kept words", text)
        self.assertFalse(self.found("deleted") or self.found("pageref"))

    def test_an_odt_footnote_is_a_passage_of_its_own_and_does_not_glue_to_its_paragraph(self):
        body = (f'<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="{T}">'
                f'<office:body><office:text><text:p>The wave equation<text:note><text:note-citation>1</text:note-citation>'
                f'<text:note-body><text:p>See Whitham</text:p></text:note-body></text:note> is hyperbolic.</text:p>'
                f'</office:text></office:body></office:document-content>')
        with ZipFile(self.root / "note.odt", "w") as z:
            z.writestr("content.xml", body)
        self.assertEqual(self.build(), (1, 0))
        text = "\n".join(store_text(self.db))
        self.assertIn("The wave equation is hyperbolic.", text)
        self.assertEqual(text.count("See Whitham"), 1)
        self.assertNotIn("equation1See", text)

    def test_an_fb2_title_outside_its_sections_is_kept_and_a_large_one_is_refused_by_its_size(self):
        (self.root / "front.fb2").write_text(
            '<FictionBook><body><title><p>Collected tales</p></title><section><p>Lanterns swing.</p></section></body></FictionBook>')
        self.assertEqual(self.build(), (1, 0))
        self.assertTrue(self.found("collected tales"))
        with patch.object(extract, "MAX_XML", 100):
            with self.assertRaisesRegex(ValueError, "exceeds 32 MiB"):
                list(extract.fb2_pages(self.root / "front.fb2"))

    def test_an_fb2_book_with_large_pictures_is_read_and_its_text_is_what_the_limit_is_about(self):
        body = "<section><p>Lanterns swing.</p></section>"
        picture = '<binary id="cover.jpg" content-type="image/jpeg">' + "QUJD" * 2000 + "</binary>"
        (self.root / "pictured.fb2").write_text(f"<FictionBook><body>{body}</body>{picture}</FictionBook>")
        with patch.object(extract, "MAX_XML", 1000):
            self.assertEqual(self.build(), (1, 0))
            self.assertTrue(self.found("lanterns swing"))
            (self.root / "wordy.fb2").write_text("<FictionBook><body><section><p>" + "word " * 400 + "</p></section></body></FictionBook>")
            with self.assertRaisesRegex(ValueError, "exceeds 32 MiB"):
                list(extract.fb2_pages(self.root / "wordy.fb2"))

    def test_tracked_deletions_and_the_author_of_a_comment_are_not_text_of_the_odt_paragraph(self):
        office = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
        dc = "http://purl.org/dc/elements/1.1/"
        body = (f'<office:document-content xmlns:office="{office}" xmlns:text="{T}" xmlns:dc="{dc}"><office:body><office:text>'
                f'<text:tracked-changes><text:changed-region><text:deletion><text:change-info><dc:creator>Jones</dc:creator></text:change-info>'
                f'<text:p>Deleted sentence about vortices.</text:p></text:deletion></text:changed-region></text:tracked-changes>'
                f'<text:p>Kept text<office:annotation><dc:creator>Commenter Jones</dc:creator><dc:date>2026-03-04T11:00:00</dc:date>'
                f'<text:p>The comment body.</text:p></office:annotation> after comment.</text:p></office:text></office:body></office:document-content>')
        with ZipFile(self.root / "marked.odt", "w") as z:
            z.writestr("content.xml", body)
        self.assertEqual(self.build(), (1, 0))
        text = "\n".join(store_text(self.db))
        self.assertNotIn("vortices", text)
        self.assertIn("Kept text after comment.", text)
        self.assertNotIn("Jones2026", text)
        self.assertEqual(text.count("The comment body."), 1)

    def test_an_fb2_subtitle_is_text(self):
        (self.root / "sub.fb2").write_text("<FictionBook><body><section><subtitle>Interlude of the tides</subtitle><p>Waves.</p></section></body></FictionBook>")
        self.assertEqual(self.build(), (1, 0))
        self.assertTrue(self.found("interlude of the tides"))

    def test_a_document_of_a_format_no_longer_read_is_not_read_as_plain_text(self):
        folder = self.fake_tool("shout", 'tr a-z A-Z < "$1"\n')
        extract.declare({"zzz": {"command": [str(folder / "shout"), "{path}"]}})
        self.addCleanup(extract.declare, {})
        (self.root / "note.zzz").write_bytes(b"\x00\x01\x02 binary")
        self.build(names_only=True)
        self.assertEqual(self.db.execute("SELECT state FROM docs").fetchone()[0], "pending")
        extract.declare({})
        self.build(names_only=True)
        self.assertEqual(self.db.execute("SELECT state FROM docs").fetchone()[0], "name-only")
        self.assertEqual(self.build(), (0, 0))
        with self.assertRaisesRegex(ValueError, "No program reads the format zzz"):
            list(extract.passages(self.root / "note.zzz", "zzz", self.base))

    def test_a_format_read_through_a_converter_ranks_like_the_documents_that_ShelfFind_reads(self):
        folder = self.fake_tool("keep", 'cat "$1"\n')
        extract.declare({"xlsx": {"command": [str(folder / "keep"), "{path}"]}})
        self.addCleanup(extract.declare, {})
        (self.root / "weak.txt").write_text("lantern " + "filler " * 800)
        (self.root / "strong.xlsx").write_text("lantern lantern lantern lantern")
        self.assertEqual(self.build(), (2, 0))
        order = [r["name"] for r in search(self.db, "lantern", where="text")]
        self.assertEqual(order[0], "strong.xlsx")

    def test_a_converter_given_by_a_relative_address_is_found_from_where_it_was_asked_for(self):
        folder = self.fake_tool("hello", "echo hello from the converter\n")
        here = os.getcwd()
        os.chdir(folder)
        self.addCleanup(os.chdir, here)
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.assertEqual(extract.run_tool(["./hello"], elsewhere).strip(), "hello from the converter")

    def test_several_programs_run_at_once_only_where_they_are_limited_by_prlimit(self):
        with patch("shelffind.extract.shutil.which", return_value="/usr/bin/prlimit"):
            self.assertEqual(extract.threads(4), 4)
        with patch("shelffind.extract.shutil.which", return_value=None):
            self.assertEqual(extract.threads(4), 1)

    def test_the_words_of_a_long_query_are_shared_with_all_but_one_first_and_a_query_of_many_is_not_shared(self):
        from shelffind import search as engine
        six = engine.shares(6)
        self.assertEqual(six[0], ((1, 2, 3, 4, 5), (0,)))
        self.assertEqual(six[6], ((0,), (1, 2, 3, 4, 5)))
        (self.root / "Whitham-Notes.txt").write_text("dispersion " * 3)
        self.build()
        words = " ".join(["whitham dispersion"] + [f"extra{n}" for n in range(engine.MOST)])
        self.assertFalse([r for r in search(self.db, words) if r["match"] == "name and passage"])

    def test_the_empty_query_keeps_the_order_of_the_most_recent_files(self):
        newest, second = self.root / "zzz-newest.py", self.root / "aaa-second.png"
        for path, when in ((self.root / "old.txt", 1_600_000_000), (second, 1_700_000_000), (newest, 1_800_000_000)):
            path.write_text("x " + path.name)
            os.utime(path, (when, when))
        self.build()
        self.assertEqual([r["name"] for r in search(self.db, "", limit=3)], ["zzz-newest.py", "aaa-second.png", "old.txt"])


def store_text(db):
    """The text of every page of an index, in the order of the pages."""
    return [store.inflate(row["ztext"]) for row in db.execute("SELECT ztext FROM pages ORDER BY id")]


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
