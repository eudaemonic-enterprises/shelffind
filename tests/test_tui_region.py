# Copyright 2026 Eudaemonic Enterprises Inc.
"""The band under the cursor: it scrolls only what it needs, redraws only what changed, and
leaves nothing behind."""
import unittest

from shelffind.tui.region import Region
from shelffind.tui.style import Theme, parse, theme
from shelffind.tui.width import cells, clip, clip_start, pad, take, wrap

from vt import Forbidden, Screen


class Band(unittest.TestCase):
    def band(self, height, cols=40, rows=10, before=0):
        screen = Screen(cols, rows)
        for number in range(before):
            screen.feed(f"earlier {number}\r\n")
        written = []

        def write(text):
            written.append(text)
            screen.feed(text)

        region = Region(write, lambda: (screen.cols, screen.rows), height)
        return screen, region, written

    def test_the_band_opens_under_the_cursor_and_keeps_what_was_above(self):
        screen, region, _ = self.band(4, before=3)
        region.open()
        region.paint(["one", "two", "three", "four"])
        self.assertEqual(screen.text()[:7], ["earlier 0", "earlier 1", "earlier 2",
                                             "one", "two", "three", "four"])
        self.assertEqual((screen.row, screen.col, screen.modes["cursor"]), (3, 0, False))
        self.assertEqual(screen.scrolled, [])
        self.assertTrue(screen.modes["wrap"])

    def test_at_the_bottom_it_scrolls_exactly_what_it_needs(self):
        screen, region, _ = self.band(4, rows=10, before=9)
        self.assertEqual(screen.row, 9)
        region.open()
        region.paint(["one", "two", "three", "four"])
        self.assertEqual(screen.scrolled, ["earlier 0", "earlier 1", "earlier 2"])
        self.assertEqual(screen.text()[6:], ["one", "two", "three", "four"])
        self.assertEqual(screen.row, 6)

    def test_only_changed_lines_are_written_again(self):
        screen, region, written = self.band(3)
        region.open()
        region.paint(["alpha", "beta", "gamma"])
        del written[:]
        region.paint(["alpha", "BETA", "gamma"])
        again = "".join(written)
        self.assertIn("BETA", again)
        self.assertNotIn("alpha", again)
        self.assertNotIn("gamma", again)
        self.assertEqual(screen.text()[:3], ["alpha", "BETA", "gamma"])
        self.assertEqual((screen.row, screen.col), (0, 0))
        del written[:]
        region.paint(["alpha", "BETA", "gamma"])
        self.assertEqual(written, [])

    def test_a_shorter_line_erases_the_rest_of_the_old_one(self):
        screen, region, _ = self.band(2)
        region.open()
        region.paint(["a long line of text", "second"])
        region.paint(["short", "second"])
        self.assertEqual(screen.text()[:2], ["short", "second"])

    def test_leaving_erases_the_band_and_shows_the_cursor_where_it_began(self):
        screen, region, _ = self.band(4, before=2)
        region.open()
        region.paint(["one", "two", "three", "four"])
        region.close()
        self.assertEqual(screen.text()[:6], ["earlier 0", "earlier 1", "", "", "", ""])
        self.assertEqual((screen.row, screen.col, screen.modes["cursor"]), (2, 0, True))

    def test_the_band_never_takes_the_whole_screen_and_survives_a_new_size(self):
        screen, region, _ = self.band(40, rows=10, before=1)
        region.open()
        self.assertEqual(region.height, 9)
        region.paint([f"line {n}" for n in range(9)])
        screen.cols = 30
        region.resized()
        self.assertEqual(region.cols, 30)
        region.paint([f"again {n}" for n in range(9)])
        self.assertEqual(screen.text()[1:], [f"again {n}" for n in range(9)])

    def test_the_strict_terminal_refuses_the_last_column_and_unknown_sequences(self):
        screen = Screen(10, 3)
        with self.assertRaises(Forbidden):
            screen.feed("0123456789")
        with self.assertRaises(Forbidden):
            Screen().feed("\x1b[?1049h")
        with self.assertRaises(Forbidden):
            Screen().feed("\x1b[2J")
        with self.assertRaises(Forbidden):
            Screen().feed("\x1b[6n")


class Text(unittest.TestCase):
    def test_width_counts_cells_not_characters(self):
        self.assertEqual(cells("abc"), 3)
        self.assertEqual(cells("été"), 3)          # letters with separate accents
        self.assertEqual(cells("Теория"), 6)
        self.assertEqual(cells("漢字"), 4)
        self.assertEqual(cells("a‍b"), 2)

    def test_cuts_keep_a_letter_with_its_marks(self):
        self.assertEqual(clip("étés", 2), "é…")
        self.assertEqual(clip("abcdef", 4), "abc…")
        self.assertEqual(clip("abc", 3), "abc")
        self.assertEqual(clip("漢字漢字", 5), "漢字…")
        self.assertEqual(clip("abc", 0), "")
        self.assertEqual(take("漢字", 3), ("漢", 2))
        self.assertEqual(clip_start("/a/long/path/file.pdf", 10), "…/file.pdf")
        self.assertEqual(pad("ab", 4), "ab  ")
        self.assertEqual(pad("abcdef", 4), "abcdef")

    def test_wrapping_breaks_at_spaces_and_ends_with_an_ellipsis(self):
        text = "the dispersion relation gives the group velocity of the wave"
        self.assertEqual(wrap(text, 24, 2), ["the dispersion relation", "gives the group velocit…"])
        self.assertEqual(wrap("short", 24, 2), ["short"])
        self.assertEqual(wrap("", 24, 2), [])
        self.assertTrue(all(cells(line) <= 10 for line in wrap(text, 10, 9)))


class Colours(unittest.TestCase):
    def test_styles_are_read_from_words(self):
        style = parse("bold yellow on #102030")
        self.assertEqual((style.fg, style.bg, style.attributes), (3, (16, 32, 48), frozenset({"bold"})))
        self.assertEqual(parse("bright-black").fg, 8)
        with self.assertRaises(ValueError):
            parse("mauve")

    def test_depth_follows_the_environment(self):
        full = {"TERM": "xterm-256color", "COLORTERM": "truecolor"}
        self.assertIn("38;2;16;32;48", Theme({"x": "#102030"}, 1 << 24).render([("a", "x")]))
        self.assertIn("38;5;", Theme({"x": "#102030"}, 256).render([("a", "x")]))
        self.assertEqual(theme("terminal", {}, full).colours, 1 << 24)
        self.assertEqual(theme("terminal", {}, {"TERM": "xterm-256color"}).colours, 256)
        plain = theme("terminal", {}, {**full, "NO_COLOR": "1"})
        self.assertEqual(plain.colours, 0)
        self.assertNotIn("3", plain.render([("match", "match")]).replace("\x1b[0;1m", ""))
        self.assertEqual(theme("terminal", {}, {"TERM": "dumb"}).colours, 0)
        self.assertEqual(theme("terminal", {}, full, terminal=False).colours, 0)
        self.assertEqual(theme("mine", {"mine": {"match": "red"}}, full).style("match").fg, 1)
        with self.assertRaises(ValueError):
            theme("unknown", {}, full)

    def test_text_never_takes_the_colour_of_its_background(self):
        shown = Theme().render([("folder", "folder")], base="selected")
        self.assertIn("\x1b[0;100m", shown)
        self.assertNotIn(";90", shown)


if __name__ == "__main__":
    unittest.main()
