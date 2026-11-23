# Copyright 2026 Eudaemonic Enterprises Inc.
"""A document is recognised by its content: copies share one text, and a file that is moved,
renamed or written anew elsewhere keeps it."""
import os
import shutil
import unittest
from unittest.mock import patch

from shelffind import extract, fuzzy, history, search as searching, sources, store
from shelffind.search import search

from support import Shelf

LARGE = "Tide tables of the northern harbours. " * 3000        # beyond the size of a small file
OTHER = "Wind roses of the southern capes. " * 3000
OTHER += "." * (len(LARGE) - len(OTHER))                       # another text of the same length


class Reading(Shelf):
    """Counts how many documents are really read."""

    def setUp(self):
        super().setUp()
        self.read = []
        original = extract.passages

        def counted(path, fmt, work):
            self.read.append(path.name)
            return original(path, fmt, work)

        watcher = patch("shelffind.extract.passages", side_effect=counted)
        watcher.start()
        self.addCleanup(watcher.stop)
        self.assertEqual(len(LARGE), len(OTHER))
        self.assertGreater(len(LARGE), store.SMALL)

    def write(self, name, text=LARGE, when=1_700_000_000):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.utime(path, (when, when))
        return path

    def documents(self):
        return self.db.execute("SELECT count(*) FROM docs").fetchone()[0]


class Copies(Reading):
    def test_two_copies_are_one_document_read_once(self):
        first = self.write("Harbours/TideTables.txt")
        second = self.write("Desk/TideTables.txt")
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(len(self.read), 1)
        found = search(self.db, "northern harbours")
        self.assertEqual(len(found), 1)
        self.assertEqual({found[0]["path"], *found[0]["copies"]}, {str(first), str(second)})
        self.assertEqual(len(search(self.db, "tidetables")), 1)
        only_desk = search(self.db, "tidetables", folder="desk")
        self.assertEqual((only_desk[0]["path"], only_desk[0]["copies"]), (str(second), [str(first)]))

    def test_the_same_stamp_under_another_name_is_compared_before_it_is_shared(self):
        self.write("TideTables.txt")
        self.write("WindRoses.txt", OTHER)                  # same size, same date, another content
        self.write("TideTables-second.txt")                 # the same content as the first
        self.assertEqual(self.documents(), 0)
        self.assertEqual(self.build(), (3, 0))
        self.assertEqual(len(self.read), 2)                 # one of the two equal files, and the other text
        self.assertIn("WindRoses.txt", self.read)
        self.assertEqual(self.documents(), 2)
        self.assertEqual(len(search(self.db, "northern harbours")[0]["copies"]), 1)
        self.assertEqual(search(self.db, "southern capes")[0]["copies"], [])

    def test_small_files_of_the_same_second_stay_apart(self):
        for name in ("a.txt", "b.txt"):
            self.write(name, "same length " + name[0])
        self.assertEqual(self.build(), (2, 0))
        self.assertEqual(self.documents(), 2)
        self.assertEqual([hit["name"] for hit in search(self.db, '"same length b"')], ["b.txt"])


class Moves(Reading):
    def setUp(self):
        super().setUp()
        self.first = self.write("Unsorted/tides.txt")
        self.assertEqual(self.build(), (1, 0))
        self.read.clear()

    def test_a_renamed_file_keeps_its_text(self):
        target = self.root / "Harbours" / "TideTables-Admiralty-1936.txt"
        target.parent.mkdir()
        self.first.rename(target)
        self.assertEqual(self.build(), (0, 0))
        self.assertEqual(self.read, [])
        found = search(self.db, "northern harbours")
        self.assertEqual([(hit["path"], hit["copies"]) for hit in found], [(str(target), [])])
        self.assertEqual(self.documents(), 1)

    def test_a_file_written_anew_elsewhere_takes_its_text_back(self):
        target = self.root / "Harbours" / "TideTables.txt"
        target.parent.mkdir()
        shutil.copyfile(self.first, target)                 # a fresh copy: today's date
        self.first.unlink()
        self.assertEqual(self.build(), (0, 0))
        self.assertEqual(self.read, [])
        self.assertEqual(search(self.db, "northern harbours")[0]["path"], str(target))
        bases = {row[0] for row in self.db.execute("SELECT basis FROM doc_keys")}
        self.assertNotIn("size", bases)                     # the reading confirmed it by fingerprint
        self.assertIn("content", bases)

    def test_another_document_of_the_same_size_is_not_mistaken_for_it(self):
        self.first.unlink()
        self.write("Capes/WindRoses.txt", OTHER, when=1_800_000_000)
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(self.read, ["WindRoses.txt"])
        self.assertEqual([hit["name"] for hit in search(self.db, "southern capes")], ["WindRoses.txt"])
        self.assertEqual(search(self.db, "northern harbours"), [])

    def test_a_new_date_on_the_same_content_reads_nothing_again(self):
        os.utime(self.first, (1_800_000_000, 1_800_000_000))
        self.assertEqual(self.build(), (1, 0))              # looked at, and found to be known
        self.assertEqual(self.read, [])
        self.assertEqual(len(search(self.db, "northern harbours")), 1)

    def test_changed_content_is_read_again_and_the_old_text_is_gone(self):
        self.write("Unsorted/tides.txt", OTHER, when=1_800_000_000)
        self.assertEqual(self.build(), (1, 0))
        self.assertEqual(self.read, ["tides.txt"])
        self.assertEqual(search(self.db, "northern harbours"), [])
        self.assertEqual(len(search(self.db, "southern capes")), 1)

    def test_forgetting_a_folder_keeps_what_another_folder_holds(self):
        annex = self.base / "Annex"
        annex.mkdir()
        shutil.copy2(self.first, annex / "tides.txt")        # the same content and date
        (annex / "only-here.txt").write_text("A note kept only in the annex.")
        self.run_cli("roots", "add", str(annex))
        self.build()
        self.assertEqual(len(search(self.db, "northern harbours")[0]["copies"]), 1)
        self.run_cli("roots", "remove", "Annex")
        found = search(self.db, "northern harbours")
        self.assertEqual([(hit["path"], hit["copies"]) for hit in found], [(str(self.first), [])])
        self.assertEqual(search(self.db, "annex"), [])
        self.assertEqual(self.documents(), 1)


class Words(Reading):
    def test_a_word_of_the_text_is_a_spelling_once_two_documents_hold_it(self):
        with patch.object(fuzzy, "SMALL_VOCABULARY", 0):
            self.write("one.txt", "The ultraviolet horizon. " * 3000)
            self.build()
            self.assertEqual(fuzzy.spellings(self.db, "ultravoilet"), [])
            self.assertEqual(search(self.db, "ultravoilet"), [])
            self.write("two.txt", "An ultraviolet lamp, and more. " * 3000)
            self.build()
            self.assertEqual(fuzzy.spellings(self.db, "ultravoilet"), ["ultraviolet"])
            self.assertEqual(len(search(self.db, "ultravoilet")), 2)
            self.assertEqual(fuzzy.spellings(self.db, "horizno"), [])           # still in one document
        self.write("three.txt", "Zodiacal light. " * 5000)
        self.build()
        self.assertEqual(fuzzy.spellings(self.db, "zodiacla"), ["zodiacal"])    # a small collection keeps all

    def test_very_common_words_are_answered_from_a_sample(self):
        for number in range(12):
            self.write(f"note{number:02}.txt", f"The tide of harbour {number}. " * 3000, when=1_700_000_000 + number)
        self.build()
        exact = search(self.db, "tide", where="text", limit=5)
        with patch.object(searching, "SAMPLE", 3):
            sampled = search(self.db, "tide", where="text", limit=5)
        self.assertEqual((len(exact), len(sampled)), (5, 5))
        self.assertTrue(all(hit["snippet"] and hit["marks"] for hit in sampled))

    def test_nothing_asked_lists_the_documents_opened_lately_first(self):
        old = self.write("old.txt", when=1_600_000_000)
        self.write("recent.txt", OTHER, when=1_800_000_000)
        self.build()
        history.record(self.data, "open", "tides", {"path": str(old), "source": "files", "name": "old.txt"})
        answers = []
        options = {"root": None, "fmt": None, "exact": False, "limit": 5, "source": None, "where": "all"}
        sources.find(self.db, self.data, "", options, lambda rows, message: answers.append(rows))
        self.assertEqual([(hit["name"], hit["match"]) for hit in answers[-1]],
                         [("old.txt", "opened"), ("recent.txt", "recent")])
        self.assertEqual(history.documents(self.data), [str(old)])
        self.assertEqual(history.queries(self.data), ["tides"])


if __name__ == "__main__":
    unittest.main()
