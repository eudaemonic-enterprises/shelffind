# Copyright 2026 Eudaemonic Enterprises Inc.
"""A band of lines drawn under the cursor, on the ordinary screen.

The band is reserved by scrolling, with no question put to the terminal. Between two frames the
real cursor stays hidden on the first cell of the band. A terminal keeps the cursor on its cell
when it rewraps its lines, so one sequence is right after a change of size, on leaving and on
suspension alike: go to the line start, erase everything below, reserve again, repaint.

Lines given to `paint` must be at most `cols - 1` cells wide, so that nothing ever wraps.
"""

HIDE, SHOW = "\x1b[?25l", "\x1b[?25h"
SYNC_ON, SYNC_OFF = "\x1b[?2026h", "\x1b[?2026l"      # one frame appears at once where supported
WRAP_OFF, WRAP_ON = "\x1b[?7l", "\x1b[?7h"            # a mismeasured character can never scroll
ERASE_BELOW, ERASE_LINE, RESET = "\x1b[J", "\x1b[K", "\x1b[0m"


class Region:
    def __init__(self, write, size, height):
        self.write = write          # write(text)
        self.size = size            # size() -> (columns, rows)
        self.wanted = height
        self.cols = self.height = 0
        self.shown = None           # the lines on screen, None when all must be drawn
        self.active = False

    def _measure(self):
        cols, rows = self.size()
        self.cols = max(2, cols)
        self.height = max(1, min(self.wanted, rows - 1))

    def _reserve(self):
        down = self.height - 1
        return "\r" + ("\n" * down + f"\x1b[{down}A" if down else "")

    def open(self):
        self._measure()
        self.write(HIDE + self._reserve())
        self.shown, self.active = None, True

    def paint(self, lines):
        lines = list(lines)[:self.height]
        lines += [""] * (self.height - len(lines))
        out, row = [], 0
        for index, line in enumerate(lines):
            if self.shown is not None and self.shown[index] == line:
                continue
            if index != row:
                out.append(f"\x1b[{index - row}B")
                row = index
            out.append("\r" + line + ERASE_LINE + RESET)
        if out:
            back = f"\x1b[{row}A" if row else ""
            self.write(SYNC_ON + WRAP_OFF + "".join(out) + back + "\r" + WRAP_ON + SYNC_OFF)
        self.shown = lines

    def resized(self):
        """The terminal changed size: erase the band where the cursor rests and take it again."""
        self.write("\r" + ERASE_BELOW)
        self._measure()
        self.write(self._reserve())
        self.shown = None

    def close(self):
        if self.active:
            self.write("\r" + ERASE_BELOW + RESET + SHOW)
            self.active, self.shown = False, None
