# Copyright 2026 Eudaemonic Enterprises Inc.
"""What the panel shows for a given state: a list of lines, each a list of (text, role) pieces.

Nothing here touches the terminal. A line never exceeds `cols - 1` cells.
"""
import re
import time
from dataclasses import dataclass
from pathlib import Path

from ..query import split
from ..search import spans
from ..text import safe
from ..tui.width import cells, clip, clip_start, take

SCOPE_LABELS = {"all": "ALL", "name": "NAMES", "text": "TEXT"}
TEXT_FORMATS = {"txt", "md", "rst", "tex"}
SIGNS = {
    True: {"marker": "▌", "crumb": " › ", "bar": "│", "dots": "…", "dot": " · "},
    False: {"marker": ">", "crumb": " > ", "bar": "|", "dots": "...", "dot": " - "},
}
HELP = [
    ("open", "open the document, at the page of the passage"),
    ("address", "put its address on the command line"),
    ("scope", "everything, names only, passages only"),
    ("passages", "the passages of the document; again, read the page"),
    ("actions", "everything that can be done with the result"),
    ("up", "move up"), ("down", "move down"),
    ("earlier-search", "recall an earlier search"),
    ("open-with", "open with another program"),
    ("reveal", "show in the file manager"),
    ("copy", "copy the address"),
    ("folder", "one folder or all"), ("format", "one format or all"), ("source", "one source or all"),
    ("quit", "leave"),
]


@dataclass(frozen=True)
class Look:
    preview: int = 4
    help: bool = True
    unicode: bool = True
    home: str = ""                       # written as ~ in addresses
    keys: tuple = ()                     # (action, keys) pairs, for the hints and the help

    def sign(self, name):
        return SIGNS[self.unicode][name]

    def key(self, action):
        return dict(self.keys).get(action, "").split(" ")[0]


def width_of(pieces):
    return sum(cells(text) for text, _ in pieces)


def cut(pieces, width, dots="…"):
    """Keep the pieces that fit in `width` cells; end with `dots` when something was removed."""
    if width_of(pieces) <= width:
        return list(pieces)
    out, room = [], max(0, width - cells(dots))
    for text, role in pieces:
        if cells(text) <= room:
            out.append((text, role))
            room -= cells(text)
        else:
            head, _ = take(text, room)
            if head:
                out.append((head, role))
            break
    if width >= cells(dots):
        out.append((dots, out[-1][1] if out else None))
    return out


def fill(pieces, width):
    """Pad with spaces to exactly `width` cells."""
    missing = width - width_of(pieces)
    return list(pieces) + ([(" " * missing, None)] if missing > 0 else [])


def sides(left, right, width, dots="…"):
    """`left` at the start of the line and `right` at its end; the left part gives way."""
    right = cut(right, width, dots)
    left = cut(left, max(0, width - width_of(right) - (1 if right else 0)), dots)
    gap = width - width_of(left) - width_of(right)
    return left + [(" " * gap, None)] + right


def marked(text, ranges, role, hot="match"):
    """Text as pieces, the given ranges in the `hot` role."""
    out, position = [], 0
    for start, end in ranges:
        start, end = max(position, start), min(len(text), end)
        if start > position:
            out.append((text[position:start], role))
        if end > start:
            out.append((text[start:end], hot))
            position = end
    if position < len(text):
        out.append((text[position:], role))
    return out


def wrapped(pieces, width, lines, dots="…"):
    """Break pieces into at most `lines` lines of `width` cells, at spaces where possible."""
    words, apart = [], True                       # (text, role, joined to the previous word)
    for text, role in pieces:
        for run in re.findall(r"\s+|\S+", text):
            if run.isspace():
                apart = True
            else:
                words.append((run, role, not apart))
                apart = False
    out, current, used = [], [], 0
    for word, role, joined in words:
        space = 0 if not current or joined else 1
        gap = [(" ", None)] * space              # a space never takes the colour of a match
        if used + space + cells(word) > width and current:
            if len(out) == lines - 1:
                out.append(cut(current + gap + [(word, role)], width, dots))
                return out
            out.append(current)
            current, used, space, gap = [], 0, 0, []
        current.extend(gap + [(word, role)])
        used += space + cells(word)
    if current and len(out) < lines:
        out.append(cut(current, width, dots))
    return out


def human_size(size):
    for unit in ("B", "kB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" or size >= 100 else f"{size:.1f} {unit}"
        size /= 1000


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
    return "format.text" if kind in TEXT_FORMATS else "format.other"


def crumbs(hit, look):
    """Where a result lives: its folders for a file, its byline for a record of a source."""
    if hit["source"] != "files":
        parts = [hit.get("byline"), hit.get("availability")]
        return look.sign("dot").join(safe(p) for p in parts if p)
    try:
        inner = Path(hit["path"]).relative_to(hit["root"]).parent.parts
    except ValueError:
        inner = ()
    return look.sign("crumb").join(safe(p) for p in (Path(hit["root"]).name, *inner))


def title(hit):
    name = safe(hit["name"])
    kind = hit.get("format") or ""
    if hit["source"] == "files" and kind and name.lower().endswith("." + kind):
        name = name[:-len(kind) - 1]
    return name


def label(hit):
    """The short word at the start of a row: the format of a file, the name of a source."""
    return safe((hit.get("format") or "file") if hit["source"] == "files" else hit["source"])


def row(hit, chosen, model, look, width, label_room=4, rank=0):
    """One result. The nine best carry their rank, which Alt and that digit open at once."""
    base = "selected" if chosen else None
    first = (look.sign("marker") + " ", "marker") if chosen else \
        (f"{rank} " if 0 < rank < 10 else "  ", "hint")
    lead = [first, (clip(label(hit), label_room, "").ljust(label_room + 2), format_role(hit))]
    where = (f"p. {hit['page']}" if hit.get("page") else "").rjust(8)
    room = width - width_of(lead) - cells(where)
    name = title(hit)
    try:
        ranges = spans(name, split(model.query).words, hit.get("match") or "")
    except ValueError:
        ranges = []
    place = crumbs(hit, look)
    name_room = room if room < 44 or not place else min(72, max(24, room * 3 // 5))
    name_pieces = fill(cut(marked(name, ranges, "title" if chosen else None), name_room, look.sign("dots")),
                       name_room)
    place_room = room - name_room - 2
    if place_room > 6:
        shorten = clip_start if hit["source"] == "files" else clip      # keep the deepest folders
        shown = shorten(place, place_room, look.sign("dots"))
        try:
            lit = spans(shown, split(model.query).words)
        except ValueError:
            lit = []
        place_pieces = [("  ", None)] + fill(marked(shown, lit, "folder"), place_room)
    else:
        place_pieces = [(" " * max(0, room - name_room), None)]
    return fill(lead + name_pieces + place_pieces + [(where, "page")], width), base


def application_names(model):
    """The applications offered, by the names people know them by where these are known."""
    return model.app_names if len(model.app_names) == len(model.apps) else model.apps


def listing(model, look, width, rows):
    """At most `rows` rows of the list. Under a prompt at the bottom the best match is the
    lowest row."""
    if model.mode == "help":
        table = [[(f"  {dict(look.keys).get(action, ''):<22}", "accent"), (text, None)] for action, text in HELP]
        return [(cut(line, width), None) for line in table[:rows]]
    if model.mode == "apps":
        first = max(0, model.app - rows + 1)
        lines = []
        for index, name in enumerate(application_names(model)[first:first + rows], first):
            chosen = index == model.app
            lines.append((fill([((look.sign("marker") if chosen else " ") + " ", "marker"),
                                (clip(safe(name), width - 2), None)], width), "selected" if chosen else None))
        return lines
    if model.mode == "actions":
        lines = []
        for index, (text, _) in enumerate(model.actions[:rows]):
            chosen = index == model.action
            lines.append((fill([((look.sign("marker") if chosen else " ") + " ", "marker"), (text, None)], width),
                          "selected" if chosen else None))
        return lines
    if model.mode == "passages":
        first = max(0, model.passage - rows + 1)
        lines = []
        for index, item in enumerate(model.passages[first:first + rows], first):
            chosen = index == model.passage
            lead = [((look.sign("marker") if chosen else " ") + " ", "marker"),
                    (clip(place(item), 9).ljust(10), "page")]
            text = [(safe(piece), role) for piece, role in marked(item["snippet"], item["marks"], None)]
            lines.append((fill(lead + cut(text, width - width_of(lead), look.sign("dots")), width),
                          "selected" if chosen else None))
        return lines                        # in the order of the pages, whichever side the prompt is
    if model.mode == "reading":
        lines = page_lines(model, width)
        model.scroll = max(0, min(model.scroll, len(lines) - rows))
        return [([("  ", None)] + line, None) for line in lines[model.scroll:model.scroll + rows]]
    first = max(0, model.selected - rows + 1)
    shown = model.results[first:first + rows]
    room = max(4, min(8, max((cells(label(hit)) for hit in shown), default=4)))
    lines = [row(hit, index == model.selected, model, look, width, room, index + 1)
             for index, hit in enumerate(shown, first)]
    return lines[::-1] if model.bottom else lines


def place(item):
    """Where a passage is: its page, else its section, else its rank in the document."""
    if item.get("page"):
        return f"p. {item['page']}"
    return safe(item["section"]) if item.get("section") else f"part {item['part']}"


def page_lines(model, width):
    """The page being read, wrapped to the width, the words of the query lit. The lines are kept
    with the page, since a page is long and the frame is drawn at every key."""
    page, inner = model.reading, max(8, width - 2)
    cached = page.get("lines")
    if cached is None or cached[0] != inner:
        words, lines = split(model.query).words, []
        for raw in safe_lines(page["text"]):
            try:
                lit = spans(raw, words) if words else []
            except ValueError:
                lit = []
            lines.extend(wrapped(marked(raw, lit, "passage"), inner, 1000) or [[]])
        while lines and not lines[-1]:
            lines.pop()
        page["lines"] = cached = (inner, lines)
    return cached[1]


def safe_lines(text):
    """The lines of a page, cleaned, with runs of empty lines reduced to one."""
    previous = True
    for raw in text.replace("\f", "\n").split("\n"):
        line = safe(raw).rstrip()
        if line or not previous:
            yield line
        previous = not line


def header(model, look, width):
    """The name and the size of the collection; the key hints give way first."""
    dot = look.sign("dot")
    if model.mode in ("passages", "reading") and model.document:
        left = [(title(model.document), "title")]
        if model.mode == "passages":
            count = len(model.passages)
            left.append((f"{dot}{count} passage{'s' * (count != 1)}", "muted"))
            hints = [("open", "open at the page"), ("passages", "read"), ("quit", "back")]
        else:
            page = model.reading
            left.append((f"{dot}{place(page)}{dot}part {page['part']} of {page['parts']}", "muted"))
            hints = [("page-down", "next page"), ("open", "open here"), ("quit", "back")]
    else:
        left = [("ShelfFind", "title"), (f"  {grouped(model.files)} files", "muted")]
        extra = [c for c in model.channels if c not in (None, "files")]
        if extra:
            left.append((dot + ", ".join(safe(c) for c in extra), "muted"))
        hints = [("open", "open"), ("address", "address"), ("scope", "scope"), ("help", "keys")]
    left = cut(left, max(1, width * 2 // 3 if model.mode in ("passages", "reading") else width), look.sign("dots"))
    words = [f"{look.key(action)} {word}" for action, word in hints if look.key(action)]
    while words and width_of(left) + 3 + cells(look.sign("dot").join(words)) > width:
        words.pop()
    right = [(look.sign("dot").join(words), "hint")] if words else []
    return sides(left, right, width, look.sign("dots")), None


def prompt(model, look, width):
    if model.mode == "apps":
        left = [("[ OPEN WITH ] ", "scope"),
                (clip(safe(application_names(model)[model.app]) if model.apps else "", width - 16), "prompt")]
        return sides(left, [("enter choose" + look.sign("dot") + "esc back", "hint")], width), None
    if model.mode == "actions":
        left = [("[ ACTIONS ] ", "scope"), (clip(title(model.hit()) if model.hit() else "", width - 14), "prompt")]
        return sides(left, [("enter choose" + look.sign("dot") + "esc back", "hint")], width), None
    if model.mode == "passages":
        left = [("[ PASSAGES ] ", "scope"), (clip(safe(model.query), max(4, width // 2)), "prompt")]
        count = [(f"{model.passage + 1} of {len(model.passages)}", "count")] if model.passages else []
        return sides(left, count, width, look.sign("dots")), None
    if model.mode == "reading":
        lines = len(page_lines(model, width))
        left = [(f"[ {place(model.reading).upper()} ] ", "scope"),
                (clip(safe(model.query), max(4, width // 2)), "prompt")]
        return sides(left, [(f"line {min(lines, model.scroll + 1)} of {lines}", "count")], width,
                     look.sign("dots")), None
    left = [(f"[ {SCOPE_LABELS[model.options.get('where', 'all')]} ] ", "scope")]
    facets = [Path(model.options["root"]).name if model.options.get("root") else None,
              model.options.get("fmt"), model.options.get("source")]
    for facet in facets:
        if facet:
            left.append((safe(facet) + " ", "accent"))
    if model.message:
        status = [(safe(model.message), "warning")]
    elif model.busy:
        status = [("searching" + look.sign("dots"), "count")]
    elif model.results:
        took = f"{look.sign('dot')}{model.seconds * 1000:.0f} ms" if model.seconds is not None else ""
        status = [(f"{model.selected + 1} of {len(model.results)}{took}", "count")]
    else:
        status = []
    status = cut(status, max(0, width // 2), look.sign("dots"))
    room = max(4, width - width_of(left) - width_of(status) - 2)
    query, caret = model.query, model.caret
    start = caret                                   # show the part of the query around the cursor
    while start > 0 and cells(query[start - 1:caret]) < room - 1:
        start -= 1
    shown, _ = take(query[start:], room - 1 if caret == len(query) else room)
    inside = caret - start
    under = shown[inside:inside + 1] or " "
    typed = [(shown[:inside], "prompt"), (under, "cursor"), (shown[inside + 1:], "prompt")]
    return sides(left + typed, status, width, look.sign("dots")), None


def address_of(hit, look):
    """The address of a file, written from the home folder when it lies there."""
    address = safe(hit["path"])
    if look.home and (address == look.home or address.startswith(look.home + "/")):
        address = "~" + address[len(look.home):]
    return address


def preview(model, look, width, rows):
    """Under the prompt: the passage in its context, the address, and a line of facts."""
    if rows <= 0:
        return []
    gutter = [(look.sign("bar") + " ", "rule")]
    inner = width - 2
    hit = model.hit() if model.mode != "help" else None
    lines = []
    if model.mode == "passages" and model.passages:
        item = model.passages[model.passage]
        label = [(place(item) + "  ", "page")]
        text = [(safe(piece), role) for piece, role in marked(item["snippet"], item["marks"], "passage")]
        indent = width_of(label)
        for index, line in enumerate(wrapped(text, inner - indent, max(1, rows - 1), look.sign("dots"))):
            lines.append((label if index == 0 else [(" " * indent, None)]) + line)
        if len(lines) < rows:
            lines.append([(clip_start(address_of(model.document, look), inner, look.sign("dots")), "path")])
    elif hit is None:
        hint = "Type a name, a few letters or a passage." if not model.query else ""
        lines.append([(hint, "muted")])
    else:
        facts = []
        if hit["source"] == "files":
            address = address_of(hit, look)
            if hit.get("size") is not None:
                facts.append(human_size(hit["size"]))
            if hit.get("mtime"):
                stamp = time.localtime(hit["mtime"] / 1e9)
                facts.append(time.strftime("%Y-%m-%d", stamp))
        else:
            address = look.sign("dot").join(safe(p) for p in (hit["source"], hit.get("byline"),
                                                                hit.get("availability")) if p)
        facts.append(safe(hit.get("match") or ""))
        passage_rows = max(0, rows - 2) if rows >= 3 else (1 if rows == 2 else 0)
        if hit.get("snippet") and passage_rows:
            label = [(f"p. {hit['page']}  ", "page")] if hit.get("page") else []
            text = [(safe(piece), role)
                    for piece, role in marked(hit["snippet"], hit.get("marks") or [], "passage")]
            indent = width_of(label)
            for index, line in enumerate(wrapped(text, inner - indent, passage_rows, look.sign("dots"))):
                lines.append((label if index == 0 else [(" " * indent, None)]) + line)
        if len(lines) < rows:
            lines.append([(clip_start(address, inner, look.sign("dots")), "path")])
        if len(lines) < rows:
            lines.append([(clip(look.sign("dot").join(f for f in facts if f), inner, look.sign("dots")), "muted")])
    lines = lines[:rows] + [[]] * (rows - len(lines))
    return [(gutter + line, None) for line in lines]


def frame(model, cols, rows, look):
    """The whole panel: `rows` lines of at most `cols - 1` cells. What is shown is kept together
    next to the prompt; the unused lines of the band stay empty on the far side."""
    width = max(1, cols - 1)
    below = 0 if model.mode in ("help", "reading") else min(look.preview, max(0, rows - 5))
    top = 1 if look.help and rows - below >= 4 else 0
    body = max(1, rows - top - 1 - below)
    model.page = max(1, body - 1)           # the step of PgUp and PgDn follows the list shown
    parts = {
        "header": [header(model, look, width)] if top else [],
        "list": listing(model, look, width, body),
        "prompt": [prompt(model, look, width)],
        "preview": preview(model, look, width, below),
    }
    order = ("header", "list", "prompt", "preview") if model.bottom else ("prompt", "list", "preview", "header")
    lines = [line for name in order for line in parts[name]]
    empty = [([], None)] * max(0, rows - len(lines))
    lines = empty + lines if model.bottom else lines + empty
    return [(cut(pieces, width, look.sign("dots")), base) for pieces, base in lines][:rows]


def plain(lines):
    """The text of a frame without its roles, for tests and for reading."""
    return ["".join(text for text, _ in pieces).rstrip() for pieces, _ in lines]
