# Copyright 2026 Eudaemonic Enterprises Inc.
"""Filters typed inside a query narrow the search and never count as words of it."""
import json
import shutil
import subprocess
import unittest

from shelffind import sources
from shelffind.query import split
from shelffind.search import file_id, page_of, passages_of, search, spans, unmark

from support import Shelf


class Typed(unittest.TestCase):
    def test_filters_are_taken_out_of_the_words(self):
        typed = split('whitham ext:DjVu in:Books -draft "in:quoted phrase" src:catalogue waves')
        self.assertEqual(typed.words, 'whitham "in:quoted phrase" waves')
        self.assertEqual((typed.fmt, typed.place, typed.source, typed.without),
                         ("djvu", "Books", "catalogue", ["draft"]))
        self.assertEqual(split("fmt:.pdf format:ps").fmt, "ps")
        plain = split("well-posed x - y -5 a:b ext:")
        self.assertEqual(plain.words, "well-posed x - y a:b ext:")
        self.assertEqual(plain.without, ["5"])
        self.assertEqual(split("").words, "")

    def test_matches_are_reported_as_ranges(self):
        self.assertEqual(unmark("the \x02group\x03 velocity of \x02waves\x03"),
                         ("the group velocity of waves", [[4, 9], [22, 27]]))
        self.assertEqual(spans("Lamb-Hydrodynamics-CUP-1932", "hydro lamb"), [[0, 4], [5, 10]])
        self.assertEqual(spans("Théorie des ondes", "theorie ond"), [[0, 7], [12, 15]])
        self.assertEqual(spans("OrchardCalendar", "orchadr"), [[0, 7]])           # the closest word
        self.assertEqual(spans("HTTPServerNotes2024", "sevrer"), [[4, 10]])
        self.assertEqual(spans("OrchardCalendar", "orclnd", "abbreviated name"),
                         [[0, 3], [9, 10], [11, 13]])
        self.assertEqual(spans("Lamb", ""), [])


class Narrowing(Shelf):
    def setUp(self):
        super().setUp()
        for name, text in {"Waves/TideTables.txt": "Spring tides at the harbour.",
                           "Waves/TideTables-draft.txt": "Spring tides, a draft.",
                           "Charts/TideAtlas.md": "An atlas of spring tides.",
                           "Charts/Soundings.txt": "Depths in fathoms."}.items():
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(text)
        self.build()

    def names(self, query, **options):
        found = []
        opts = {"root": None, "fmt": None, "exact": False, "limit": 25, "source": None, "where": "all"}
        sources.find(self.db, self.data, query, {**opts, **options}, lambda rows, message: found.append(rows))
        return sorted(row["name"] for row in found[-1])

    def test_format_place_and_exclusion(self):
        self.assertEqual(self.names("tide"), ["TideAtlas.md", "TideTables-draft.txt", "TideTables.txt"])
        self.assertEqual(self.names("tide ext:md"), ["TideAtlas.md"])
        self.assertEqual(self.names("tide in:waves"), ["TideTables-draft.txt", "TideTables.txt"])
        self.assertEqual(self.names("tide in:waves -draft"), ["TideTables.txt"])
        self.assertEqual(self.names("spring -atlas -draft"), ["TideTables.txt"])
        self.assertEqual(self.names('in:"Reading Room"'.replace('"', "")), [])     # two words: not a filter
        self.assertEqual(self.names("in:charts"), ["Soundings.txt", "TideAtlas.md"])  # filters alone list files
        self.assertEqual(self.names("ext:txt -tide"), ["Soundings.txt"])
        self.assertEqual(self.names("tide", fmt="md"), ["TideAtlas.md"])            # the option still works
        self.assertEqual(self.names("tide ext:txt", fmt="md"), ["TideTables-draft.txt", "TideTables.txt"])

    def test_a_registered_folder_is_named_by_in(self):
        annex = self.base / "Annex"
        annex.mkdir()
        (annex / "TideGauge.txt").write_text("A gauge for tides.")
        self.run_cli("roots", "add", str(annex))
        self.build()
        self.assertEqual(self.names("tide in:annex"), ["TideGauge.txt"])
        self.assertEqual(len(self.names("tide")), 4)
        code, out, _ = self.run_cli("search", "--json", "tide in:Annex -atlas")
        self.assertEqual([row["name"] for row in json.loads(out)], ["TideGauge.txt"])

    def test_special_characters_of_a_filter_are_literal(self):
        (self.root / "Charts" / "100%_wet.txt").write_text("Rain.")
        self.build()
        self.assertEqual(self.names("in:100%_"), ["100%_wet.txt"])
        self.assertEqual(search(self.db, "", folder="_"), search(self.db, "", folder="_", without=[]))
        self.assertEqual([hit["name"] for hit in search(self.db, "", folder="%_")], ["100%_wet.txt"])


@unittest.skipUnless(shutil.which("gs") and shutil.which("pdftotext"), "Ghostscript and Poppler are required")
class OneDocument(Shelf):
    """The passages of one document, in the order of its pages, and the text of a page."""

    def setUp(self):
        super().setUp()
        pages = ["Tides of the northern harbours", "The spring tide follows the new moon",
                 "Charts and soundings", "A neap tide comes a week later", "Index of harbours"]
        source = self.base / "tides.ps"
        source.write_text("%!PS-Adobe-3.0\n/Helvetica findfont 16 scalefont setfont\n" + "".join(
            f"72 700 moveto ({text}) show showpage\n" for text in pages))
        self.pdf = self.root / "TideTables.pdf"
        subprocess.run(["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
                        f"-sOutputFile={self.pdf}", "-f", str(source)], check=True)
        (self.root / "notes.txt").write_text("One page of notes about a tide.")
        self.assertEqual(self.build(), (2, 0))
        self.hit = search(self.db, "tidetables")[0]

    def test_passages_come_in_the_order_of_the_pages(self):
        found = passages_of(self.db, self.hit, "tide")
        self.assertEqual([(item["part"], item["page"]) for item in found], [(1, 1), (2, 2), (4, 4)])
        self.assertEqual(found[1]["snippet"], "The spring tide follows the new moon")
        self.assertEqual(found[1]["marks"], [[11, 15]])
        self.assertEqual([item["page"] for item in passages_of(self.db, self.hit, '"neap tide"')], [4])
        self.assertEqual([item["page"] for item in passages_of(self.db, self.hit, "tyde")], [2, 4])   # by spelling
        self.assertEqual(passages_of(self.db, self.hit, "glacier"), [])
        every = passages_of(self.db, self.hit, "")
        self.assertEqual([item["page"] for item in every], [1, 2, 3, 4, 5])
        self.assertTrue(every[2]["snippet"].startswith("Charts and soundings"))
        note = search(self.db, "notes")[0]
        self.assertEqual([(item["part"], item["page"]) for item in passages_of(self.db, note, "tide")], [(1, None)])
        self.assertEqual(passages_of(self.db, {"root": None, "path": None}, "tide"), [])

    def test_a_page_is_read_by_its_rank_in_the_document(self):
        page = page_of(self.db, self.hit, 4)
        self.assertEqual((page["page"], page["parts"]), (4, 5))
        self.assertIn("A neap tide comes a week later", page["text"])
        self.assertIsNone(page_of(self.db, self.hit, 6))
        self.assertIsNone(page_of(self.db, self.hit, 0))
        self.assertEqual(file_id(self.db, self.hit), self.db.execute(
            "SELECT id FROM files WHERE name='TideTables.pdf'").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
