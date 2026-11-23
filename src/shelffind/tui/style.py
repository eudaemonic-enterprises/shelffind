# Copyright 2026 Eudaemonic Enterprises Inc.
"""Colours by role. A theme maps roles (title, match, selected ...) to styles written as text,
for instance "bold yellow", "bright-black", "#e0af68 on #1f2335" or "reverse"."""
import re
from dataclasses import dataclass, replace

NAMES = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]
ATTRIBUTES = {"bold": 1, "dim": 2, "italic": 3, "underline": 4, "reverse": 7}

# The default theme uses the sixteen colours of the terminal itself, so the panel always
# agrees with the colour scheme the user has chosen there.
TERMINAL = {
    "text": "",
    "title": "bold",
    "muted": "bright-black",
    "hint": "bright-black",
    "accent": "cyan",
    "match": "bold yellow",
    "selected": "on bright-black",
    "marker": "bold cyan",
    "folder": "bright-black",
    "page": "green",
    "kind": "bright-black",
    "scope": "bold cyan",
    "prompt": "bold",
    "cursor": "reverse",
    "count": "bright-black",
    "rule": "bright-black",
    "passage": "",
    "path": "bright-black",
    "ok": "green",
    "warning": "yellow",
    "error": "bold red",
    "format.pdf": "red",
    "format.djvu": "blue",
    "format.epub": "green",
    "format.ps": "magenta",
    "format.text": "yellow",
    "format.other": "bright-black",
    "format.source": "cyan",
}
THEMES = {
    "terminal": TERMINAL,
    "plain": {role: ("reverse" if role in ("selected", "cursor") else
                     "bold" if role in ("title", "match", "prompt", "scope", "marker", "error") else "")
              for role in TERMINAL},
}


@dataclass(frozen=True)
class Style:
    fg: object = None       # None, an index 0..15, or an (r, g, b) triple
    bg: object = None
    attributes: frozenset = frozenset()

    def over(self, base):
        """This style laid over `base`: what it leaves unset shows the base through. Text that
        would take the colour of its own background falls back to the base foreground."""
        fg = self.fg if self.fg is not None else base.fg
        bg = self.bg if self.bg is not None else base.bg
        if fg is not None and fg == bg:
            fg = base.fg if base.fg != bg else None
        return Style(fg, bg, self.attributes | base.attributes)


def colour(word):
    if re.fullmatch(r"#[0-9a-fA-F]{6}", word):
        return tuple(int(word[i:i + 2], 16) for i in (1, 3, 5))
    bright = word.startswith("bright-")
    name = word[7:] if bright else word
    if name in NAMES:
        return NAMES.index(name) + (8 if bright else 0)
    raise ValueError(f"Unknown colour: {word}")


def parse(spec):
    """Read a style such as "bold yellow on #202020"."""
    style, background = Style(), False
    for word in str(spec).lower().split():
        if word == "on":
            background = True
        elif word in ATTRIBUTES:
            style = replace(style, attributes=style.attributes | {word})
        elif word in ("default", "none"):
            background = False
        elif background:
            style, background = replace(style, bg=colour(word)), False
        else:
            style = replace(style, fg=colour(word))
    return style


def depth(environ, terminal=True):
    """How many colours may be used: 0, 16, 256 or 2**24."""
    if not terminal or environ.get("NO_COLOR") or environ.get("TERM", "dumb") == "dumb":
        return 0
    if environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return 1 << 24
    return 256 if "256" in environ.get("TERM", "") else 16


def nearest(rgb):
    """The closest of the 256 indexed colours to a full colour."""
    r, g, b = rgb
    if abs(r - g) < 10 and abs(g - b) < 10:
        level = round((r + g + b) / 3)
        return 16 if level < 8 else 231 if level > 238 else 232 + round((level - 8) / 10)
    scale = lambda v: 0 if v < 48 else 1 if v < 115 else (v - 35) // 40
    return 16 + 36 * scale(r) + 6 * scale(g) + scale(b)


def sixteen(rgb):
    r, g, b = rgb
    bright = max(r, g, b) > 170
    code = (r > 100) | ((g > 100) << 1) | ((b > 100) << 2)
    return code + (8 if bright and code else 0)


class Theme:
    def __init__(self, roles=None, colours=1 << 24):
        merged = {**TERMINAL, **(roles or {})}
        self.colours = colours
        self.styles = {role: parse(spec) for role, spec in merged.items()}
        self.cache = {}

    def style(self, role):
        return self.styles.get(role) or self.styles["text"]

    def _colour(self, value, background):
        if isinstance(value, tuple):
            if self.colours >= 1 << 24:
                return f"{48 if background else 38};2;{value[0]};{value[1]};{value[2]}"
            if self.colours >= 256:
                return f"{48 if background else 38};5;{nearest(value)}"
            value = sixteen(value)
        base = (40 if background else 30) if value < 8 else (100 if background else 90)
        return str(base + value % 8)

    def sgr(self, style):
        """The escape sequence that sets exactly this style, starting from a reset."""
        if self.colours == 0:
            style = Style(attributes=style.attributes & {"bold", "underline", "reverse"})
        found = self.cache.get(style)
        if found is None:
            codes = ["0"] + [str(ATTRIBUTES[a]) for a in sorted(style.attributes)]
            if style.fg is not None:
                codes.append(self._colour(style.fg, False))
            if style.bg is not None:
                codes.append(self._colour(style.bg, True))
            found = self.cache[style] = "\x1b[" + ";".join(codes) + "m"
        return found

    def render(self, spans, base=None):
        """Turn [(text, role), ...] into a string with its escape sequences."""
        ground = self.style(base) if base else Style()
        out = []
        for text, role in spans:
            if text:
                out.append(self.sgr(self.style(role).over(ground) if role else ground))
                out.append(text)
        out.append("\x1b[0m")
        return "".join(out)


def theme(name, custom, environ, terminal=True):
    """The theme called `name`: a built-in one, or a table of roles from the settings."""
    colours = depth(environ, terminal)
    if name in (custom or {}):
        return Theme(custom[name], colours)
    if name not in THEMES:
        raise ValueError(f"Unknown theme: {name}")
    return Theme(THEMES["plain" if colours == 0 else name], colours)
