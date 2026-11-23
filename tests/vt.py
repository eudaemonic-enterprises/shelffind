# Copyright 2026 Eudaemonic Enterprises Inc.
"""A small, strict terminal for the tests.

It understands exactly the sequences ShelfFind is allowed to send and fails on any other. It
also fails when a character is written in the last column, which the drawing code must never do.
"""
import re

from shelffind.tui.width import cell_width

CSI = re.compile(r"\x1b\[([?<>=]?)([0-9;]*)([A-Za-z~])")
OSC = re.compile(r"\x1b\]([0-9]+);([^\x07\x1b]*)(?:\x07|\x1b\\)")
PRIVATE_MODES = {25: "cursor", 7: "wrap", 2026: "sync", 2004: "paste"}


class Forbidden(AssertionError):
    pass


class Screen:
    def __init__(self, cols=80, rows=24):
        self.cols, self.rows = cols, rows
        self.grid = [[" "] * cols for _ in range(rows)]
        self.row = self.col = 0
        self.scrolled = []                      # lines pushed off the top
        self.modes = {"cursor": True, "wrap": True, "sync": False, "paste": False}
        self.clipboard = None
        self.alternate = False

    def text(self):
        return ["".join(line).rstrip() for line in self.grid]

    def _scroll(self):
        self.scrolled.append("".join(self.grid.pop(0)).rstrip())
        self.grid.append([" "] * self.cols)

    def _put(self, char):
        width = cell_width(char)
        if width == 0:
            if self.col:
                self.grid[self.row][self.col - 1] += char
            return
        if self.col + width > self.cols - 1:
            raise Forbidden(f"a character reaches the last column of row {self.row}: {char!r}")
        self.grid[self.row][self.col] = char
        for extra in range(1, width):
            self.grid[self.row][self.col + extra] = ""
        self.col += width

    def feed(self, data):
        position = 0
        while position < len(data):
            char = data[position]
            if char == "\x1b":
                match = CSI.match(data, position) or OSC.match(data, position)
                if not match:
                    raise Forbidden(f"unknown sequence: {data[position:position + 12]!r}")
                if match.re is CSI:
                    self._csi(*match.groups())
                else:
                    self._osc(*match.groups())
                position = match.end()
                continue
            position += 1
            if char == "\r":
                self.col = 0
            elif char == "\n":
                if self.row == self.rows - 1:
                    self._scroll()
                else:
                    self.row += 1
            elif char < " " or char == "\x7f":
                raise Forbidden(f"control character {char!r}")
            else:
                self._put(char)

    def _csi(self, private, body, final):
        numbers = [int(n) if n else 0 for n in body.split(";")] if body else []
        count = numbers[0] if numbers else 1
        if private == "?" and final in "hl" and numbers and numbers[0] in PRIVATE_MODES:
            if numbers[0] == 1049:
                self.alternate = True
            self.modes[PRIVATE_MODES[numbers[0]]] = final == "h"
        elif private:
            raise Forbidden(f"private sequence ?{body}{final}")
        elif final == "A":
            if count < 1:
                raise Forbidden("a move of zero lines")
            self.row = max(0, self.row - count)
        elif final == "B":
            if count < 1:
                raise Forbidden("a move of zero lines")
            self.row = min(self.rows - 1, self.row + count)
        elif final == "G":
            self.col = min(self.cols - 1, max(1, count) - 1)
        elif final == "K" and not body:
            for col in range(self.col, self.cols):
                self.grid[self.row][col] = " "
        elif final == "J" and not body:
            for col in range(self.col, self.cols):
                self.grid[self.row][col] = " "
            for row in range(self.row + 1, self.rows):
                self.grid[row] = [" "] * self.cols
        elif final == "m":
            pass
        else:
            raise Forbidden(f"sequence {body}{final}")

    def _osc(self, number, body):
        if number == "52":
            self.clipboard = body
        elif number != "8":
            raise Forbidden(f"operating system command {number}")
