# Copyright 2026 Eudaemonic Enterprises Inc.
"""Keys are read the same way whatever the size of the pieces in which their bytes arrive."""
import unittest

from shelffind.tui.keys import Decoder, Key, Paste, Report


def named(events):
    """A compact form: the names of the keys, the characters themselves, pastes in brackets."""
    out = []
    for event in events:
        if isinstance(event, Key):
            out.append(event.text if event.name == "char" else f"<{event.name}>")
        elif isinstance(event, Paste):
            out.append(f"[{event.text}]")
        else:
            out.append(f"({event.kind})")
    return "".join(out)


CASES = [
    (b"abc", "abc"),
    ("é漢🙂".encode(), "é漢🙂"),
    (b"\r\t\x7f\x08", "<enter><tab><backspace><backspace>"),
    (b"\x01\x05\x12\x15\x17\x0a\x00", "<ctrl-a><ctrl-e><ctrl-r><ctrl-u><ctrl-w><ctrl-j><ctrl-space>"),
    (b"\x1b[A\x1b[B\x1b[C\x1b[D", "<up><down><right><left>"),
    (b"\x1bOA\x1bOB\x1bOH\x1bOF", "<up><down><home><end>"),
    (b"\x1b[H\x1b[F\x1b[1~\x1b[4~\x1b[7~\x1b[8~", "<home><end><home><end><home><end>"),
    (b"\x1b[5~\x1b[6~\x1b[2~\x1b[3~", "<pgup><pgdn><insert><delete>"),
    (b"\x1bOP\x1bOQ\x1bOR\x1bOS\x1b[15~\x1b[17~\x1b[24~", "<f1><f2><f3><f4><f5><f6><f12>"),
    (b"\x1b[1;5C\x1b[1;3D\x1b[1;2A\x1b[3;5~", "<ctrl-right><alt-left><shift-up><ctrl-delete>"),
    (b"\x1b[Z", "<shift-tab>"),
    (b"\x1b1\x1bx\x1b\r\x1b\x7f", "<alt-1><alt-x><alt-enter><alt-backspace>"),
    ("\x1bé".encode(), "<alt-é>"),
    (b"\x1b[200~two\nlines \x1b[31m\x1b[201~x", "[two\nlines \x1b[31m]x"),
    (b"\x1b[24;80Rq", "(cursor)q"),
    (b"\x1b[1;2R", "<shift-f3>"),
    (b"\x1b[97;5u\x1b[13;2u\x1b[65;2u\x1b[27u", "<ctrl-a><shift-enter>A<esc>"),
    (b"\x1b[97;6u", "<ctrl-a>"),
    (b"\x1b[I\x1b[O", "(focus)(focus)"),
    (b"\x1b[<0;10;5M\x1b[M !!", "(mouse)(mouse)"),
    (b"\xffa", "�a"),
]


class Keys(unittest.TestCase):
    def test_whole_and_cut_at_every_byte(self):
        for data, expected in CASES:
            self.assertEqual(named(Decoder().feed(data)), expected, data)
            for cut in range(1, len(data)):
                decoder = Decoder()
                events = decoder.feed(data[:cut]) + decoder.feed(data[cut:])
                self.assertEqual(named(events), expected, (data, cut))
            decoder = Decoder()
            events = [e for i in range(len(data)) for e in decoder.feed(data[i:i + 1])]
            self.assertEqual(named(events), expected, (data, "byte by byte"))

    def test_a_lone_escape_waits_and_is_then_flushed(self):
        decoder = Decoder()
        self.assertEqual(decoder.feed(b"\x1b"), [])
        self.assertTrue(decoder.waiting())
        self.assertEqual(named(decoder.flush()), "<esc>")
        self.assertFalse(decoder.waiting())
        self.assertEqual(named(decoder.feed(b"a")), "a")

    def test_escape_followed_later_by_a_key_is_two_keys(self):
        decoder = Decoder()
        decoder.feed(b"\x1b")
        self.assertEqual(named(decoder.flush() + decoder.feed(b"[A")), "<esc>[A")
        decoder = Decoder()
        decoder.feed(b"\x1b[")
        self.assertEqual(named(decoder.flush()), "<esc>[")
        self.assertEqual(named(decoder.feed(b"\x1b\x1b[A")), "<esc><up>")

    def test_an_unfinished_paste_keeps_waiting(self):
        decoder = Decoder()
        self.assertEqual(decoder.feed(b"\x1b[200~first"), [])
        self.assertEqual(decoder.flush(), [])
        self.assertFalse(decoder.waiting())
        self.assertEqual(named(decoder.feed(b" second\x1b[201~")), "[first second]")

    def test_reports_are_not_keys(self):
        events = Decoder().feed(b"\x1b[12;40R")
        self.assertEqual(events, [Report("cursor")])


if __name__ == "__main__":
    unittest.main()
