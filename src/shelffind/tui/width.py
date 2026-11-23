# Copyright 2026 Eudaemonic Enterprises Inc.
"""Width of text in terminal cells, and cuts that never separate a letter from its marks."""
import unicodedata

ELLIPSIS = "…"


def cell_width(char):
    """Cells one character occupies: 0 for marks that sit on the previous letter, 2 for wide
    characters, 1 otherwise. Characters of ambiguous width (Cyrillic, Greek) count 1."""
    if unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def cells(text):
    return sum(cell_width(c) for c in text)


def clusters(text):
    """Split text into letters, each with the marks that follow it."""
    current = ""
    for char in text:
        if current and cell_width(char) == 0:
            current += char
        else:
            if current:
                yield current
            current = char
    if current:
        yield current


def take(text, width):
    """The longest beginning of text that fits in `width` cells, and the cells it uses."""
    out, used = [], 0
    for cluster in clusters(text):
        size = cells(cluster)
        if used + size > width:
            break
        out.append(cluster)
        used += size
    return "".join(out), used


def clip(text, width, tail=ELLIPSIS):
    """Cut text to at most `width` cells, ending with `tail` when something was removed."""
    if width <= 0:
        return ""
    if cells(text) <= width:
        return text
    room = width - cells(tail)
    if room <= 0:
        return take(tail, width)[0]
    return take(text, room)[0] + tail


def clip_start(text, width, head=ELLIPSIS):
    """Cut text to at most `width` cells by removing its beginning, as for a long path."""
    if width <= 0:
        return ""
    if cells(text) <= width:
        return text
    room = width - cells(head)
    if room <= 0:
        return take(head, width)[0]
    kept, used = [], 0
    for cluster in reversed(list(clusters(text))):
        size = cells(cluster)
        if used + size > room:
            break
        kept.append(cluster)
        used += size
    return head + "".join(reversed(kept))


def pad(text, width):
    """Text followed by the spaces that bring it to `width` cells (never cut)."""
    return text + " " * max(0, width - cells(text))


def wrap(text, width, lines):
    """Break text into at most `lines` lines of `width` cells, at spaces where possible; the
    last line ends with an ellipsis when text remains."""
    out, rest = [], " ".join(text.split())
    while rest and len(out) < lines:
        if cells(rest) <= width:
            out.append(rest)
            rest = ""
            break
        if len(out) == lines - 1:
            out.append(clip(rest, width))
            rest = ""
            break
        head, _ = take(rest, width)
        cut = head.rfind(" ")
        if cut > width // 3:
            head = head[:cut]
        out.append(head.rstrip())
        rest = rest[len(head):].lstrip()
    return out
