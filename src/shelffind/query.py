# Copyright 2026 Eudaemonic Enterprises Inc.
"""Filters typed inside a query: `ext:djvu`, `in:Books`, `src:catalogue` and `-word`."""
import re
from dataclasses import dataclass, field

TOKEN = re.compile(r'"[^"]*"?|\S+')
FILTER = re.compile(r"(ext|fmt|format|in|src|source):(.+)", re.IGNORECASE)


@dataclass
class Typed:
    words: str = ""                 # the query without its filters
    fmt: str | None = None          # ext:, fmt: or format:
    place: str | None = None        # in: a registered folder by its name, else a part of the path
    source: str | None = None       # src: or source:
    without: list = field(default_factory=list)     # -word: not in the name nor in the path


def split(query):
    """Separate the filters of a query from its words. A quoted phrase is never a filter."""
    typed, kept = Typed(), []
    for token in TOKEN.findall(query):
        found = None if token.startswith('"') else FILTER.fullmatch(token)
        if found:
            name, value = found.group(1).lower(), found.group(2)
            if name in ("ext", "fmt", "format"):
                typed.fmt = value.lower().lstrip(".")
            elif name == "in":
                typed.place = value
            else:
                typed.source = value
        elif len(token) > 1 and token[0] == "-" and (token[1].isalnum() or token[1] == '"'):
            word = token[1:].strip('"')
            if word:
                typed.without.append(word)
        else:
            kept.append(token)
    typed.words = " ".join(kept)
    return typed
