# Copyright 2026 Eudaemonic Enterprises Inc.
"""The panel: what each key does to its state, and what it shows for a given state."""
import os
import time
import unittest

from shelffind.config import KEYS, defaults
from shelffind.picker.model import Model, update
from shelffind.picker.view import Look, frame, plain, wrapped
from shelffind.picker.worker import Reply
from shelffind.tui.keys import Key, Paste
from shelffind.tui.width import cells

KEYMAP = defaults().keymap()


def hit(name, folder, fmt="pdf", page=None, snippet=None, marks=(), match="name or path"):
    return {"path": f"/lib/Books/{folder}/{name}", "root": "/lib/Books", "name": name, "format": fmt,
            "match": match, "page": page, "section": None, "snippet": snippet,
            "marks": [list(m) for m in marks], "size": 1234567, "mtime": 1700000000 * 10**9,
            "source": "files"}


def hits():
    return [
        hit("Lamb-Hydrodynamics-CUP-1932.pdf", "Waves", page=3, match="passage", marks=[(28, 38)],
            snippet="For waves on deep water the dispersion relation gives the frequency in terms "
                    "of the wavenumber."),
        hit("Rayleigh-OnProgressiveWaves-LondonMathSoc-1877.pdf", "Dispersion"),
        hit("Stokes-OnTheTheoryOfOscillatoryWaves-CambPhilSoc-1847.djvu", "Solitons", fmt="djvu", page=12,
            match="passage", snippet="The dispersion relation is corrected at the third order.",
            marks=[(4, 14)]),
        {"path": None, "root": None, "name": "On the dispersion of waves", "format": "pdf",
         "match": "bibliography", "page": None, "section": None, "snippet": None, "marks": [],
         "size": None, "mtime": None, "source": "library", "id": "K1/abc", "byline": "Kelvin (1887).",
         "availability": "published full text"},
    ]


def press(model, *names):
    done = []
    for name in names:
        event = Key("char", name) if len(name) == 1 else Key(name)
        done.extend(update(model, event, KEYMAP))
    return done


class Keys(unittest.TestCase):
    def test_typing_edits_the_query_at_the_cursor(self):
        model = Model()
        self.assertEqual(press(model, "w", "a", "v", "e"), [("search",)] * 4)
        press(model, "left", "left", "X")
        self.assertEqual((model.query, model.caret), ("waXve", 3))
        press(model, "backspace", "home", "delete", "end", "s")
        self.assertEqual((model.query, model.caret), ("aves", 4))
        update(model, Paste("deep\nwater  waves"), KEYMAP)
        self.assertEqual(model.query, "avesdeep water waves")
        press(model, "ctrl-w")
        self.assertEqual(model.query, "avesdeep water ")
        press(model, "ctrl-left", "ctrl-left")
        self.assertEqual(model.caret, 0)
        press(model, "ctrl-right", "ctrl-u")
        self.assertEqual((model.query, model.caret), (" water ", 0))
        self.assertEqual(press(model, "left", "home"), [])      # moving the cursor searches nothing

    def test_the_selection_follows_the_side_of_the_prompt(self):
        model = Model(results=hits())
        press(model, "up")
        self.assertEqual(model.selected, 1)         # prompt at the bottom: upwards is the next best
        press(model, "down", "down", "down")
        self.assertEqual(model.selected, 0)
        model.bottom = False
        press(model, "down", "down")
        self.assertEqual(model.selected, 2)
        model.page = 10
        press(model, "pgdn")
        self.assertEqual(model.selected, 3)
        press(model, "pgup")
        self.assertEqual(model.selected, 0)

    def test_scope_folder_format_and_source_cycle(self):
        model = Model(roots=[None, "/lib/Books"], formats=[None, "djvu", "pdf"],
                      channels=[None, "files", "library"])
        seen = []
        for _ in range(3):
            self.assertEqual(press(model, "ctrl-r"), [("search",)])
            seen.append(model.options["where"])
        self.assertEqual(seen, ["name", "text", "all"])
        press(model, "f6", "f6")
        self.assertEqual(model.options["source"], "library")
        press(model, "f4")
        self.assertEqual((model.options["root"], model.options["source"]), ("/lib/Books", "files"))
        press(model, "f6")
        self.assertEqual((model.options["root"], model.options["source"]), (None, "library"))
        press(model, "f5", "f5")
        self.assertEqual(model.options["fmt"], "pdf")

    def test_one_key_turns_the_approximate_search_off_and_on(self):
        model = Model(query="whitham", caret=7)
        self.assertFalse(model.options["exact"])
        self.assertEqual(press(model, "f7"), [("search",)])     # the search is made again, exactly
        self.assertTrue(model.options["exact"])
        self.assertEqual(press(model, "f7"), [("search",)])
        self.assertFalse(model.options["exact"])

    def test_earlier_searches_come_back_and_the_draft_is_kept(self):
        model = Model(history=["solitary wave", "bessel"])
        press(model, "t", "i", "d")
        press(model, "ctrl-p")
        self.assertEqual(model.query, "solitary wave")
        press(model, "ctrl-p", "ctrl-p")
        self.assertEqual(model.query, "bessel")
        press(model, "ctrl-n", "ctrl-n")
        self.assertEqual((model.query, model.caret), ("tid", 3))

    def test_actions_are_asked_for_and_the_other_screens_close(self):
        model = Model(results=hits())
        self.assertEqual(press(model, "enter"), [("open", model.results[0])])
        self.assertEqual(press(model, "tab"), [("address", model.results[0])])
        self.assertEqual(press(model, "f3"), [("reveal", model.results[0])])
        self.assertEqual(press(model, "ctrl-y"), [("copy", model.results[0])])
        self.assertEqual(press(model, "f2"), [("apps", model.results[0])])
        model.apps, model.mode = ["one.desktop", "two.desktop"], "apps"
        self.assertEqual(press(model, "down", "enter"), [("launch", model.results[0], "two.desktop")])
        self.assertEqual(model.mode, "search")
        press(model, "f1")
        self.assertEqual(model.mode, "help")
        self.assertEqual(press(model, "esc"), [])
        self.assertEqual(model.mode, "search")
        self.assertEqual(press(model, "esc"), [("quit",)])
        self.assertEqual(press(Model(), "enter", "tab", "f2"), [])     # nothing to act on

    def test_an_answer_keeps_the_chosen_document_and_old_answers_are_dropped(self):
        model = Model(results=hits(), selected=2, version=7, busy=True, fresh=False)
        update(model, Reply(6, [], "", 0.1), KEYMAP)
        self.assertEqual(len(model.results), 4)
        later = [hits()[3], hits()[2]]
        update(model, Reply(7, later, "", 0.02), KEYMAP)
        self.assertEqual((model.selected, model.busy, model.seconds), (1, False, 0.02))
        model.fresh = True                          # a new search: every answer starts at the best match
        update(model, Reply(7, hits(), "", 0.02), KEYMAP)
        update(model, Reply(7, hits()[::-1], "", 0.03), KEYMAP)     # a later answer that changes the order
        self.assertEqual((model.selected, model.fresh), (0, True))
        press(model, "up")                          # ... until the selection is moved: then it follows its document
        chosen = model.hit()
        update(model, Reply(7, hits(), "", 0.04), KEYMAP)
        self.assertEqual((model.hit(), model.fresh), (chosen, False))
        model.query = "nothing"
        update(model, Reply(7, [], "", 0.01), KEYMAP)
        self.assertEqual(model.message, "No matches")
        model.waiting = 4
        update(model, Reply(7, [], "", 0.01), KEYMAP)
        self.assertEqual(model.message, "No matches. 4 documents still have no text")
        model.waiting = 1
        update(model, Reply(7, [], "", 0.01), KEYMAP)
        self.assertEqual(model.message, "No matches. 1 document still has no text")
        model.options["where"] = "name"
        update(model, Reply(7, [], "", 0.01), KEYMAP)
        self.assertEqual(model.message, "No matches")


class Frames(unittest.TestCase):
    def setUp(self):
        self.zone = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.look = Look(preview=3, keys=tuple(KEYS.items()), home="/lib")

    def tearDown(self):
        if self.zone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.zone
        time.tzset()

    def model(self, **changes):
        model = Model(query="dispersion", caret=10, results=hits(), files=39033, seconds=0.018)
        model.channels = [None, "files", "library"]
        for name, value in changes.items():
            setattr(model, name, value)
        return model

    def test_prompt_at_the_bottom_with_the_best_match_next_to_it(self):
        self.assertEqual(plain(frame(self.model(), 80, 10, self.look)), [
            "",
            "ShelfFind  39 033 files · library       enter open · tab address · ctrl-r scope",
            "4 library  On the dispersion of waves            Kelvin (1887). · publ…",
            "3 djvu     Stokes-OnTheTheoryOfOscillatoryWave…  Books › Solitons         p. 12",
            "2 pdf      Rayleigh-OnProgressiveWaves-LondonM…  Books › Dispersion",
            "▌ pdf      Lamb-Hydrodynamics-CUP-1932           Books › Waves             p. 3",
            "[ ALL ] dispersion                                               1 of 4 · 18 ms",
            "│ p. 3  For waves on deep water the dispersion relation gives the frequency in…",
            "│ ~/Books/Waves/Lamb-Hydrodynamics-CUP-1932.pdf",
            "│ 1.2 MB · 2023-11-14 · passage",
        ])

    def test_the_first_line_names_the_machines(self):
        model = self.model(company="home (here) · texts 2 h ago", notice="An indexing is running, so the pages from the other machines wait")
        line = plain(frame(model, 160, 10, self.look))[1]
        self.assertIn("home (here)", line)
        self.assertIn("pages from the other machines wait", line)

    def test_the_prompt_says_when_the_search_is_exact(self):
        model = self.model()
        model.options["exact"] = True
        self.assertIn("[ ALL ] exact dispersion", plain(frame(model, 80, 10, self.look))[6])
        model.mode = "help"
        self.assertTrue(any("f7" in line and "exactly as typed" in line for line in plain(frame(model, 100, 24, self.look))))

    def test_prompt_at_the_top(self):
        self.assertEqual(plain(frame(self.model(selected=2, bottom=False), 80, 10, self.look)), [
            "[ ALL ] dispersion                                               3 of 4 · 18 ms",
            "1 pdf      Lamb-Hydrodynamics-CUP-1932           Books › Waves             p. 3",
            "2 pdf      Rayleigh-OnProgressiveWaves-LondonM…  Books › Dispersion",
            "▌ djvu     Stokes-OnTheTheoryOfOscillatoryWave…  Books › Solitons         p. 12",
            "4 library  On the dispersion of waves            Kelvin (1887). · publ…",
            "│ p. 12  The dispersion relation is corrected at the third order.",
            "│ ~/Books/Solitons/Stokes-OnTheTheoryOfOscillatoryWaves-CambPhilSoc-1847.djvu",
            "│ 1.2 MB · 2023-11-14 · passage",
            "ShelfFind  39 033 files · library       enter open · tab address · ctrl-r scope",
            "",
        ])

    def test_a_record_of_a_source_and_a_narrow_window(self):
        self.assertEqual(plain(frame(self.model(selected=3, query="disp", caret=4), 50, 7, self.look)), [
            "ShelfFind  39 033 files · library      enter open",
            "▌ library  On the dispersion of waves",
            "3 djvu     Stokes-OnTheTheoryOfOscillato…   p. 12",
            "2 pdf      Rayleigh-OnProgressiveWaves-L…",
            "[ ALL ] disp                       4 of 4 · 18 ms",
            "│ library · Kelvin (1887). · published full text",
            "│ bibliography",
        ])

    def test_roles_mark_the_chosen_row_the_matches_and_the_cursor(self):
        lines = frame(self.model(), 80, 10, self.look)
        chosen = lines[5]
        self.assertEqual(chosen[1], "selected")
        self.assertEqual([base for _, base in lines].count("selected"), 1)
        folder_row = lines[4][0]                    # the folder "Dispersion" explains this match
        self.assertIn(("Dispersion", "match"), folder_row)
        passage = lines[7][0]
        self.assertIn(("dispersion", "match"), passage)
        self.assertIn((" ", "cursor"), lines[6][0])
        middle = self.model(caret=3)
        self.assertIn(("p", "cursor"), frame(middle, 80, 10, self.look)[6][0])

    def test_no_line_ever_reaches_the_last_column(self):
        long = self.model(query="a very long query " * 12, caret=40, message="A long message " * 20)
        for model in (self.model(), self.model(bottom=False), long, self.model(mode="help"),
                      self.model(mode="apps", apps=["a.desktop", "b" * 200 + ".desktop"]),
                      self.model(results=[], query="")):
            for cols in (2, 5, 13, 24, 40, 79, 80, 132, 200):
                for rows in (1, 2, 3, 4, 6, 9, 15, 40):
                    lines = frame(model, cols, rows, self.look)
                    self.assertEqual(len(lines), rows, (cols, rows))
                    for pieces, _ in lines:
                        used = sum(cells(text) for text, _ in pieces)
                        self.assertLessEqual(used, max(1, cols - 1), (cols, rows, pieces))

    def test_other_screens_and_plain_signs(self):
        shown = plain(frame(self.model(mode="help"), 80, 20, self.look))
        self.assertTrue(any("ctrl-r" in line and "passages only" in line for line in shown))
        apps = plain(frame(self.model(mode="apps", apps=["evince.desktop", "okular.desktop"], app=1),
                           80, 10, self.look))
        self.assertIn("▌ okular.desktop", apps)
        self.assertTrue(any(line.startswith("[ OPEN WITH ] okular.desktop") for line in apps))
        named = self.model(mode="apps", apps=["org.gnome.Papers.desktop", "com.google.Chrome.desktop"], app=1,
                           app_names=["Document Viewer (Papers)", "Google Chrome (opens it by default)"])
        shown = plain(frame(named, 80, 10, self.look))
        self.assertIn("  Document Viewer (Papers)", shown)
        self.assertIn("▌ Google Chrome (opens it by default)", shown)
        self.assertTrue(any(line.startswith("[ OPEN WITH ] Google Chrome") for line in shown))
        self.assertEqual(press(named, "enter"), [("launch", named.results[0], "com.google.Chrome.desktop")])
        ascii_look = Look(preview=3, unicode=False, keys=tuple(KEYS.items()), home="/lib")
        text = "\n".join(plain(frame(self.model(), 80, 10, ascii_look)))
        self.assertTrue(text.isascii(), text)
        self.assertIn("> pdf      Lamb", text)
        empty = plain(frame(self.model(results=[], query=""), 80, 10, self.look))
        self.assertIn("│ Type a name, a few letters or a passage.", empty)

    def test_passages_wrap_between_words_and_keep_their_marks(self):
        pieces = [("the ", None), ("dispers", "match"), ("ion relation of deep water waves", None)]
        lines = wrapped(pieces, 20, 2)
        self.assertEqual(["".join(t for t, _ in line) for line in lines],
                         ["the dispersion", "relation of deep wa…"])
        self.assertIn(("dispers", "match"), lines[0])

    def test_actions_passages_and_a_page_read_in_the_panel(self):
        model = self.model()
        self.assertEqual(press(model, "alt-3"), [("open", model.results[2])])     # the third best at once
        self.assertEqual(press(model, "alt-9"), [])
        model.selected = 0
        press(model, "ctrl-x")
        shown = plain(frame(model, 80, 14, self.look))
        self.assertEqual(model.mode, "actions")
        self.assertIn("▌ Open", shown)
        self.assertTrue(any(line.startswith("[ ACTIONS ] Lamb-Hydrodynamics-CUP-1932") for line in shown))
        self.assertEqual(press(model, "down", "down", "enter"), [("reveal", model.results[0])])
        self.assertEqual(model.mode, "search")
        self.assertEqual(press(model, "ctrl-o"), [("passages", model.results[0])])

        document = model.results[0]
        model.document, model.mode, model.passage = document, "passages", 1
        model.passages = [
            {"part": 3, "page": 3, "section": None, "snippet": "the dispersion relation gives", "marks": [[4, 14]]},
            {"part": 7, "page": 7, "section": None, "snippet": "anomalous dispersion of waves", "marks": [[10, 20]]},
            {"part": 9, "page": None, "section": "Appendix", "snippet": "dispersion once more", "marks": [[0, 10]]},
        ]
        shown = plain(frame(model, 80, 12, self.look))
        self.assertEqual(shown[-8:], [
            "Lamb-Hydrodynamics-CUP-1932 · 3 passages   enter open at the page · ctrl-o read",
            "  p. 3      the dispersion relation gives",
            "▌ p. 7      anomalous dispersion of waves",
            "  Appendix  dispersion once more",
            "[ PASSAGES ] dispersion                                                  2 of 3",
            "│ p. 7  anomalous dispersion of waves",
            "│ ~/Books/Waves/Lamb-Hydrodynamics-CUP-1932.pdf",
            "│",
        ])
        self.assertEqual(press(model, "x", "backspace"), [])            # the query rests in this screen
        self.assertEqual(press(model, "enter"),
                         [("open", {**document, "page": 7, "snippet": "anomalous dispersion of waves",
                                    "marks": [[10, 20]]})])
        self.assertEqual(press(model, "tab"), [("address", document)])
        self.assertEqual(press(model, "up", "ctrl-o"), [("read", document, 3)])

        model.mode, model.scroll = "reading", 0
        model.reading = {"part": 3, "page": 3, "section": None, "parts": 40,
                         "text": "Chapter 2\n\n\nFor waves on deep water the dispersion relation\ngives the frequency.\n"
                                 + "\n".join(f"line {n}" for n in range(30))}
        shown = plain(frame(model, 60, 9, self.look))
        self.assertEqual(shown[:5], [
            "Lamb-Hydrodynamics-CUP-1932 · p. 3 · p…      pgdn next page",
            "  Chapter 2",
            "",
            "  For waves on deep water the dispersion relation",
            "  gives the frequency.",
        ])
        self.assertEqual(shown[-1], "[ P. 3 ] dispersion                            line 1 of 34")
        self.assertIn(("dispersion", "match"), frame(model, 60, 9, self.look)[3][0])
        press(model, "down", "down")
        self.assertTrue(plain(frame(model, 60, 9, self.look))[1].startswith("  For waves"))
        for _ in range(60):
            press(model, "down")
        frame(model, 60, 9, self.look)
        self.assertEqual(model.scroll, 34 - 7)                  # the last screen of the page, no further
        self.assertEqual(press(model, "pgdn"), [("read", document, 4)])
        self.assertEqual(press(model, "pgup"), [("read", document, 2)])
        self.assertEqual(press(model, "enter"), [("open", {**document, "page": 3, "snippet": None, "marks": []})])
        press(model, "esc")
        self.assertEqual(model.mode, "passages")
        press(model, "esc")
        self.assertEqual(model.mode, "search")

    def test_a_file_known_to_a_source_shows_the_work_and_what_the_source_says(self):
        merged = {**hit("scan-0042.pdf", "Waves", page=380, match="passage", marks=[(4, 14)],
                        snippet="the dispersion relation gives the group velocity"),
                  "title": "Linear and Nonlinear Waves", "byline": "G. B. Whitham", "year": "1974",
                  "page_label": "363", "copies": ["/lib/Desk/scan-0042.pdf"], "fast": "/var/library/9f2c.pdf",
                  "flags": ["unverified"], "notes": 2,
                  "record": {"source": "library", "id": "W1", "actions": ["cite", "notes", "unknown"]}}
        only = {"path": None, "root": None, "name": "Waves in Fluids", "title": "Waves in Fluids", "format": "pdf",
                "byline": "J. Lighthill", "year": 1978, "venue": "Cambridge University Press", "match": "bibliography",
                "page": None, "section": None, "snippet": None, "marks": [], "size": None, "mtime": None,
                "source": "library", "id": "W2", "actions": ["cite"], "availability": "published full text"}
        model = self.model(results=[merged, only], query="whitham dispersion", caret=18)
        model.offers = {"library": {"cite": "Copy the citation key", "notes": "Show the notes"}}
        look = Look(preview=3, keys=tuple(KEYS.items()), home="/lib", slow=("/lib/",))
        self.assertEqual(plain(frame(model, 100, 8, look)), [
            "",
            "ShelfFind  39 033 files · library                 enter open · tab address · ctrl-r scope · f1 keys",
            "2 library  J. Lighthill (1978) Waves in Fluids               Cambridge University Press · …",
            "▌ pdf      G. B. Whitham (1974) Linear and Nonlinear Waves   Books › Waves                   p. 363",
            "[ ALL ] whitham dispersion                                                           1 of 2 · 18 ms",
            "│ p. 363  the dispersion relation gives the group velocity",
            "│ ~/Books/Waves/scan-0042.pdf",
            "│ 1.2 MB · 2023-11-14 · 2 copies · a copy on this disk · passage · 2 notes · unverified",
        ])
        lines = frame(model, 100, 8, look)
        self.assertIn(("Whitham", "match"), lines[3][0])            # the words typed are lit in the title too
        self.assertIn(("unverified", "warning"), lines[7][0])
        elsewhere = plain(frame(model, 100, 8, self.look))          # a shelf on a disk: nothing to remark
        self.assertEqual(elsewhere[-1], "│ 1.2 MB · 2023-11-14 · 2 copies · passage · 2 notes · unverified")

        press(model, "ctrl-x")
        self.assertEqual([text for text, _ in model.actions][-2:], ["Copy the citation key", "Show the notes"])
        self.assertEqual(press(model, *["down"] * 7, "enter"), [("act", merged, "notes")])
        model.selected = 1
        press(model, "ctrl-x")
        self.assertEqual(model.actions[-1], ("Copy the citation key", ("act", only, "cite")))
        press(model, "esc")
        # A text shown by a source is read like a page, and leaving it returns to the list.
        model.document, model.mode, model.back, model.scroll = merged, "reading", "search", 0
        model.reading = {"part": 1, "page": None, "section": "Notes", "parts": 1, "text": "A note.\n" * 40}
        self.assertEqual(press(model, "pgdn", "enter"), [])
        self.assertGreater(model.scroll, 0)
        press(model, "esc")
        self.assertEqual(model.mode, "search")

    def test_the_nine_best_rows_carry_their_rank(self):
        lines = frame(self.model(), 80, 10, self.look)
        self.assertEqual(lines[4][0][0], ("2 ", "hint"))
        many = self.model(results=hits() * 4)
        shown = plain(frame(many, 80, 24, self.look))
        self.assertTrue(any(line.startswith("9 ") for line in shown))
        self.assertFalse(any(line.startswith("10") for line in shown))


if __name__ == "__main__":
    unittest.main()
