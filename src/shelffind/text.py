# Copyright 2026 Eudaemonic Enterprises Inc.
"""Text that came from a file name, a document or a source, made fit to print."""
import json
import re
import unicodedata

JOINERS = {"‌", "‍"}
# Characters that JSON may carry literally but that a terminal could obey: the C1 controls,
# the line and paragraph separators, and the invisible formatting marks.
RISKY = re.compile("[\u007f-\u009f؜​-‏ -‮⁠-⁯﻿]")


def safe(text):
    """Return text that a terminal can display without obeying anything inside it.

    File names and extracted passages are untrusted: an escape sequence, or a mark that
    reverses the direction of writing, would otherwise act on the terminal that shows it.
    Control characters and separators become a space, formatting marks are dropped (the two
    joiners that some scripts need are kept), and letters are written in composed form.
    """
    out = []
    for char in unicodedata.normalize("NFC", str(text)):
        kind = unicodedata.category(char)
        if kind in ("Cc", "Zl", "Zp", "Zs"):
            out.append(" ")
        elif kind == "Cf":
            if char in JOINERS:
                out.append(char)
        elif kind == "Cs":
            out.append("�")
        else:
            out.append(char)
    return "".join(out)


def json_text(value):
    """Return value as indented JSON whose risky characters are written as \\uXXXX escapes.

    A program that parses the result reads exactly the same data, and a person who prints it
    in a terminal is not exposed to the control characters it may contain.
    """
    dumped = json.dumps(value, ensure_ascii=False, indent=2)
    return RISKY.sub(lambda m: f"\\u{ord(m.group()):04x}", dumped)
