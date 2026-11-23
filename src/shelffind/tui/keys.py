# Copyright 2026 Eudaemonic Enterprises Inc.
"""Bytes typed at a terminal, turned into named keys.

`Decoder.feed` may be given the bytes in pieces of any size: a sequence cut in the middle waits
for its end. A lone Esc cannot be told from the start of a sequence until some time has passed;
the caller then calls `flush`.
"""
import codecs
from dataclasses import dataclass


@dataclass(frozen=True)
class Key:
    name: str            # "enter", "up", "ctrl-r", "alt-1", "f2" ... or "char"
    text: str = ""       # the character typed, for name == "char"


@dataclass(frozen=True)
class Paste:
    text: str


@dataclass(frozen=True)
class Report:
    """An answer of the terminal (cursor position, focus change), not something typed."""
    kind: str


CONTROL = {0: "ctrl-space", 8: "backspace", 9: "tab", 13: "enter", 27: "esc", 127: "backspace",
           28: "ctrl-\\", 29: "ctrl-]", 30: "ctrl-^", 31: "ctrl-_"}
FINAL = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
         "Z": "shift-tab", "P": "f1", "Q": "f2", "R": "f3", "S": "f4"}
TILDE = {1: "home", 2: "insert", 3: "delete", 4: "end", 5: "pgup", 6: "pgdn", 7: "home", 8: "end",
         11: "f1", 12: "f2", 13: "f3", 14: "f4", 15: "f5", 17: "f6", 18: "f7", 19: "f8", 20: "f9",
         21: "f10", 23: "f11", 24: "f12"}
CODEPOINTS = {9: "tab", 13: "enter", 27: "esc", 127: "backspace", 8: "backspace"}
PASTE_END = b"\x1b[201~"


def modified(name, modifier):
    """Add the modifiers of an xterm-style parameter (1 + shift 1, alt 2, ctrl 4)."""
    bits = max(0, modifier - 1)
    prefix = ("ctrl-" if bits & 4 else "") + ("alt-" if bits & 2 else "") + ("shift-" if bits & 1 else "")
    return prefix + name


def control(byte):
    if byte in CONTROL:
        return CONTROL[byte]
    return "ctrl-" + chr(byte + 96)       # 1..26 are ctrl-a..ctrl-z


class Decoder:
    def __init__(self):
        self.pending = b""
        self.pasting = None
        self.utf8 = codecs.getincrementaldecoder("utf-8")("replace")

    def waiting(self):
        """True when an Esc, or the start of a sequence, waits to be completed or flushed."""
        return bool(self.pending) and self.pasting is None

    def feed(self, data):
        self.pending += data
        events = []
        while self.pending:
            used = self._paste(events) if self.pasting is not None else self._one(events)
            if not used:
                break
            self.pending = self.pending[used:]
        return events

    def flush(self):
        """Time has passed: what is pending is complete as it stands."""
        if self.pasting is not None:        # a paste is under way: its end will come
            return []
        events, data, self.pending = [], self.pending, b""
        if data[:1] == b"\x1b":
            events.append(Key("esc"))
            data = data[1:]
        if data:
            events.extend(self.feed(data))
            if self.pending:        # an unfinished sequence that will never end: drop it
                self.pending = b""
        return events

    def _paste(self, events):
        end = self.pending.find(PASTE_END)
        if end < 0:
            keep = len(PASTE_END) - 1          # the end mark may itself arrive in pieces
            self.pasting += self.pending[:-keep] if len(self.pending) > keep else b""
            used = max(0, len(self.pending) - keep)
            return used
        text = (self.pasting + self.pending[:end]).decode("utf-8", "replace")
        self.pasting = None
        events.append(Paste(text))
        return end + len(PASTE_END)

    def _one(self, events):
        """Decode one key from the start of `pending`; return the bytes used, 0 to wait."""
        data = self.pending
        first = data[0]
        if first == 0x1b:
            return self._escape(data, events)
        if first < 0x20 or first == 0x7f:
            events.append(Key(control(first)))
            return 1
        text = self.utf8.decode(data[:1])
        if text:
            events.append(Key("char", text))
        return 1

    def _escape(self, data, events):
        if len(data) == 1:
            return 0
        second = data[1]
        if second == 0x5b:                                    # ESC [ : a control sequence
            end = 2
            while end < len(data) and 0x20 <= data[end] <= 0x3f:
                end += 1
            if end == len(data):
                return 0
            final = chr(data[end])
            body = data[2:end].decode("ascii", "replace")
            if final == "M" and not body:                     # an old-style mouse report
                if len(data) < end + 4:
                    return 0
                events.append(Report("mouse"))
                return end + 4
            self._sequence(body, final, events)
            return end + 1
        if second == 0x4f:                                    # ESC O : keypad and F1 to F4
            if len(data) < 3:
                return 0
            name = FINAL.get(chr(data[2]))
            events.append(Key(name) if name else Report("unknown"))
            return 3
        if second == 0x1b:                                    # Esc typed twice, or Alt-Esc
            events.append(Key("esc"))
            return 1
        if second < 0x20 or second == 0x7f:
            events.append(Key("alt-" + control(second)))
            return 2
        size = 2 if second < 0x80 else 3 if second < 0xe0 else 4 if second < 0xf0 else 5
        if len(data) < size:
            return 0
        events.append(Key("alt-" + data[1:size].decode("utf-8", "replace")))
        return size

    def _sequence(self, body, final, events):
        private = body[:1] in ("?", "<", ">", "=")
        numbers = []
        for part in body.lstrip("?<>=").split(";"):
            head = part.split(":")[0]
            numbers.append(int(head) if head.isdigit() else 0)
        if final == "~" and not private:
            code = numbers[0] if numbers else 0
            if code == 200:
                self.pasting = b""
            elif code in TILDE:
                events.append(Key(modified(TILDE[code], numbers[1] if len(numbers) > 1 else 1)))
            elif code != 201:
                events.append(Report("unknown"))
        elif final == "u" and not private:                    # the "CSI u" form of a key
            code = numbers[0] if numbers else 0
            modifier = numbers[1] if len(numbers) > 1 else 1
            if code in CODEPOINTS:
                events.append(Key(modified(CODEPOINTS[code], modifier)))
            elif code >= 32:
                bits = max(0, modifier - 1)
                if bits & ~1:                                 # with Ctrl or Alt: a named key
                    events.append(Key(modified(chr(code).lower(), (bits & ~1) + 1)))
                else:
                    events.append(Key("char", chr(code)))
            else:
                events.append(Report("unknown"))
        elif final == "R" and len(numbers) == 2 and numbers[0] > 1 and not private:
            events.append(Report("cursor"))                   # an answer, not Shift-F3
        elif final in FINAL and not private:
            modifier = numbers[1] if len(numbers) > 1 else 1
            events.append(Key(modified(FINAL[final], modifier)))
        elif final in ("I", "O") and not body:
            events.append(Report("focus"))
        elif final in ("M", "m") and private:
            events.append(Report("mouse"))
        else:
            events.append(Report("unknown"))
