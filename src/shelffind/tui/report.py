# Copyright 2026 Eudaemonic Enterprises Inc.
"""What the commands print at a terminal: results with colours and links, the table of the
folders, and one live line while documents are read. A pipe receives the plain forms instead."""
from pathlib import Path

from ..text import safe
from .width import cells, clip, clip_start

LINK, UNLINK = "\x1b]8;;{}\x1b\\", "\x1b]8;;\x1b\\"
STATES = {"read": ("indexed", "needs-ocr"), "waiting": ("pending",), "scans": ("needs-ocr",),
          "problems": ("error", "ocr-error")}


def grouped(number):
    return f"{number:,}".replace(",", " ")


def format_role(hit):
    if hit["source"] != "files":
        return "format.source"
    kind = hit.get("format") or ""
    if kind in ("pdf", "djvu", "epub"):
        return f"format.{kind}"
    if kind.startswith("ps"):
        return "format.ps"
    return "format.text" if kind in ("txt", "md", "rst", "tex") else "format.other"


def marked(text, ranges, role, hot="match"):
    """Text as (text, role) pieces, the given ranges in the `hot` role, everything cleaned."""
    out, position = [], 0
    for start, end in ranges:
        start, end = max(position, start), min(len(text), end)
        if start > position:
            out.append((safe(text[position:start]), role))
        if end > start:
            out.append((safe(text[start:end]), hot))
            position = end
    if position < len(text):
        out.append((safe(text[position:]), role))
    return out


def results(hits, theme, links=True):
    """Lines of `search --list` at a terminal: each address can be clicked where the terminal
    follows links, and the words of the query are lit in the passage."""
    for hit in hits:
        if hit["source"] == "files":
            label = theme.render([((hit.get("format") or "file").ljust(5), format_role(hit))])
            address = theme.render([(safe(hit["path"]), "text")])
            if links:
                address = LINK.format(Path(hit["path"]).as_uri()) + address + UNLINK
            yield f"{label} {address}"
        else:
            status = hit.get("availability") or hit.get("match") or ""
            yield theme.render([(safe(hit["source"]).ljust(5) + " ", "format.source"),
                                (safe(hit["name"]), "title"), (f"  ({safe(status)})", "muted")])
            if hit.get("byline"):
                yield theme.render([("      " + safe(hit["byline"]), "muted")])
        if hit.get("snippet"):
            page = [(f"p. {hit['page']}  ", "page")] if hit.get("page") else []
            yield theme.render([("      ", None)] + page + marked(hit["snippet"], hit.get("marks") or [], "passage"))


def bar(part, whole, width, unicode=True):
    full = round(width * part / whole) if whole else 0
    full = max(1, full) if part else 0
    return ("█" if unicode else "#") * full, ("░" if unicode else ".") * (width - full)


def status(rows, theme, version, sources, unicode=True, width=100):
    """Lines of `status` at a terminal: one row per folder with a bar for the text at hand."""
    dot = " · " if unicode else " - "
    total = sum(row["files"] for row in rows)
    yield theme.render([(f"ShelfFind {version}", "title"),
                        (f"{dot}{grouped(total)} files in {len(rows)} folder{'s' * (len(rows) != 1)}", "muted")])
    if not rows:
        yield theme.render([("No folder is registered: shelffind roots add FOLDER", "warning")])
        return
    yield ""
    names = [safe(Path(row["path"]).name) for row in rows]
    name_room = min(max(cells("Folder"), *(cells(n) for n in names)), max(8, width - 70))
    head = f"{'Folder':<{name_room}}  {'Files':>8}  {'Text at hand':<28}  {'Waiting':>8}  {'Scans':>7}  {'Problems':>8}"
    yield theme.render([(head, "muted")])
    for row, name in zip(rows, names):
        count = {key: sum(row["states"].get(state, 0) for state in states) for key, states in STATES.items()}
        readable = row["files"] - row["states"].get("name-only", 0)
        done, rest = bar(count["read"], readable, 20, unicode)
        shown = clip(name, name_room)
        pieces = [(shown + " " * (name_room - cells(shown)), "title"), (f"  {grouped(row['files']):>8}  ", None),
                  (done, "ok"), (rest, "rule"), (f" {grouped(count['read']):>7}", None),
                  (f"  {grouped(count['waiting']):>8}", None),
                  (f"  {grouped(count['scans']):>7}", "warning" if count["scans"] else None),
                  (f"  {grouped(count['problems']):>8}", "error" if count["problems"] else None)]
        yield theme.render(pieces)
        if not row["available"] or row["problem"]:
            yield theme.render([("  " + safe(row["problem"] or "not reachable now; its last inventory stays searchable"),
                                 "warning")])
        for fault in row.get("errors", []):
            yield theme.render([("  " + clip(safe(f"{fault['rel']}: {fault['error']}"), max(20, width - 3)), "muted")])
    yield ""
    yield theme.render([("Sources".ljust(name_room + 2), "muted"), (dot.join(["files", *sources]), None)])


class Progress:
    """One line that follows the reading of documents; messages scroll above it."""

    def __init__(self, write, size, theme):
        self.write, self.size, self.theme = write, size, theme
        self.shown = False

    def document(self, done, total, name):
        cols = max(20, self.size()[0])
        share = f"{100 * done // total:>3}%" if total else ""
        head = f"Reading {grouped(done + 1)} of {grouped(total)}  {share}  "
        tail = clip_start(safe(name), max(8, cols - 1 - cells(head)))
        self.write("\r" + self.theme.render([(head, "accent"), (tail, "muted")]) + "\x1b[K")
        self.shown = True

    def message(self, text):
        self.clear()
        self.write(safe(text) + "\n")

    def clear(self):
        if self.shown:
            self.write("\r\x1b[K")
            self.shown = False
