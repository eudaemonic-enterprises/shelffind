# Copyright 2026 Eudaemonic Enterprises Inc.
"""The state of the panel and what each key does to it.

`update` changes the state and returns the things to be done outside it (a search to run, a
document to open); it reads no file and starts no program, so it can be tested alone.
"""
from dataclasses import dataclass, field

from ..tui.keys import Key, Paste
from .worker import Reply

SCOPES = ["all", "name", "text"]


@dataclass
class Model:
    query: str = ""
    caret: int = 0                       # position of the cursor inside the query
    options: dict = field(default_factory=lambda: {"root": None, "fmt": None, "source": None,
                                                    "exact": False, "limit": 50, "where": "all"})
    results: list = field(default_factory=list)
    selected: int = 0                    # 0 is the best match
    message: str = ""
    busy: bool = False
    seconds: float | None = None
    version: int = 0                     # number of the search whose answers are awaited
    fresh: bool = True                   # no answer to that search has come yet
    mode: str = "search"                 # search, apps, help, actions, passages or reading
    apps: list = field(default_factory=list)        # the applications offered for the result, as identifiers
    app_names: list = field(default_factory=list)   # the same, as the names people know them by
    app: int = 0
    actions: list = field(default_factory=list)     # (label, thing to do) for the chosen result
    action: int = 0
    document: dict | None = None         # the result whose passages are listed or read
    passages: list = field(default_factory=list)
    passage: int = 0
    reading: dict | None = None          # the page being read: part, page, section, text, parts
    back: str = "passages"               # where leaving the page leads
    offers: dict = field(default_factory=dict)      # source -> {action: label}, as the sources declare
    scroll: int = 0                      # first line of the page shown
    history: list = field(default_factory=list)     # earlier searches, newest first
    recalled: int = -1
    draft: str = ""                      # what was typed before recalling an earlier search
    roots: list = field(default_factory=lambda: [None])
    formats: list = field(default_factory=lambda: [None])
    channels: list = field(default_factory=lambda: [None, "files"])
    files: int = 0
    bottom: bool = True                  # the prompt is under the list, best match next to it
    page: int = 10                       # rows of the list, for PgUp and PgDn

    def hit(self):
        return self.results[self.selected] if self.results else None

    def edit(self, query, caret=None):
        self.query = query
        self.caret = len(query) if caret is None else max(0, min(caret, len(query)))


def word_start(text, position):
    """Where the word that ends at `position` begins."""
    while position > 0 and text[position - 1].isspace():
        position -= 1
    while position > 0 and not text[position - 1].isspace():
        position -= 1
    return position


def word_end(text, position):
    while position < len(text) and text[position].isspace():
        position += 1
    while position < len(text) and not text[position].isspace():
        position += 1
    return position


def typing(model, event):
    """Line editing of the query. Returns True when the query changed, None when the event is
    not an editing key."""
    query, caret = model.query, model.caret
    if isinstance(event, Paste):
        text = " ".join(event.text.split())
        model.edit(query[:caret] + text + query[caret:], caret + len(text))
        return bool(text)
    name = event.name
    if name == "char":
        model.edit(query[:caret] + event.text + query[caret:], caret + len(event.text))
        return True
    if name == "backspace":
        if caret:
            model.edit(query[:caret - 1] + query[caret:], caret - 1)
        return bool(caret)
    if name == "delete":
        model.edit(query[:caret] + query[caret + 1:], caret)
        return caret < len(query)
    if name in ("ctrl-w", "alt-backspace"):
        start = word_start(query, caret)
        model.edit(query[:start] + query[caret:], start)
        return start != caret
    if name == "ctrl-u":
        model.edit(query[caret:], 0)
        return bool(caret)
    moves = {"left": caret - 1, "right": caret + 1, "home": 0, "ctrl-a": 0, "end": len(query),
             "ctrl-e": len(query), "ctrl-left": word_start(query, caret), "alt-b": word_start(query, caret),
             "ctrl-right": word_end(query, caret), "alt-f": word_end(query, caret)}
    if name in moves:
        model.edit(query, moves[name])
        return False
    return None


def cycle(values, current):
    return values[(values.index(current) + 1) % len(values)] if current in values else values[0]


def move(model, step):
    """Move the selection by `step` rows on screen (negative is upwards)."""
    if model.results:
        towards_worse = -step if model.bottom else step
        model.selected = max(0, min(len(model.results) - 1, model.selected + towards_worse))


def update(model, event, keymap):
    """Apply one event; return the list of things to do, each a tuple whose first item is one of
    search, open, address, reveal, copy, apps, launch, quit."""
    if isinstance(event, Reply):
        if event.version == model.version:
            # The first answer to a new search selects the best match; later answers to the
            # same search (those of outside sources) leave the selection on its document.
            current = None if model.fresh else model.hit()
            model.fresh = False
            model.results, model.busy, model.seconds = event.rows, False, event.seconds
            model.message = event.message or ("" if event.rows or not model.query.strip() else "No matches")
            model.selected = 0
            if current:
                same = lambda hit: (hit["source"], hit.get("id"), hit["path"]) == \
                    (current["source"], current.get("id"), current["path"])
                model.selected = next((i for i, hit in enumerate(event.rows) if same(hit)), 0)
        return []
    if not isinstance(event, (Key, Paste)):
        return []
    action = keymap.get(event.name) if isinstance(event, Key) else None

    if model.mode == "help":
        model.mode = "search"
        return []
    step = {"up": -1, "down": 1, "page-up": -model.page, "page-down": model.page}.get(action, 0)
    if model.mode == "apps":
        if action == "quit":
            model.mode = "search"
        elif step and model.apps:
            model.app = max(0, min(len(model.apps) - 1, model.app + step))
        elif action == "open" and model.apps:
            model.mode = "search"
            return [("launch", model.hit(), model.apps[model.app])]
        return []
    if model.mode == "actions":
        if action == "quit":
            model.mode = "search"
        elif step and model.actions:
            model.action = max(0, min(len(model.actions) - 1, model.action + step))
        elif action == "open" and model.actions:
            model.mode = "search"
            return [model.actions[model.action][1]]
        return []
    if model.mode == "passages":
        chosen = model.passages[model.passage] if model.passages else None
        if action == "quit":
            model.mode = "search"
        elif step and model.passages:
            model.passage = max(0, min(len(model.passages) - 1, model.passage + step))
        elif action == "open" and chosen:
            return [("open", {**model.document, "page": chosen["page"], "snippet": chosen["snippet"],
                              "marks": chosen["marks"]})]
        elif action == "passages" and chosen:
            return [("read", model.document, chosen["part"])]
        elif action in ("address", "reveal", "copy"):
            return [(action, model.document)]
        return []
    if model.mode == "reading":
        page = model.reading
        if action in ("quit", "passages"):
            model.mode = model.back
        elif action in ("up", "down"):
            model.scroll = max(0, model.scroll + step)
        elif action in ("page-up", "page-down") and model.back == "passages":
            return [("read", model.document, page["part"] + (1 if action == "page-down" else -1))]
        elif action in ("page-up", "page-down"):
            model.scroll = max(0, model.scroll + step)
        elif action == "open" and model.back == "passages":
            return [("open", {**model.document, "page": page["page"], "snippet": None, "marks": []})]
        elif action in ("address", "reveal", "copy"):
            return [(action, model.document)]
        return []

    if action == "quit":
        return [("quit",)]
    if isinstance(event, Key) and event.name.startswith("alt-") and event.name[4:] in "123456789" \
            and len(event.name) == 5:
        index = int(event.name[4:]) - 1         # Alt-1 is the best match, next to the prompt
        if index < len(model.results):
            model.selected = index
            return [("open", model.results[index])]
        return []
    if action == "actions" and model.hit():
        hit = model.hit()
        model.actions = [("Open", ("open", hit)), ("Open with another program", ("apps", hit)),
                         ("Show in the file manager", ("reveal", hit)), ("Copy the address", ("copy", hit)),
                         ("Give the address", ("address", hit)), ("List its passages", ("passages", hit))]
        # What the source that knows this document offers besides.
        record = hit.get("record") or ({"source": hit["source"], "actions": hit.get("actions") or []}
                                       if hit["source"] != "files" else {"source": None, "actions": []})
        labels = model.offers.get(record["source"], {})
        model.actions += [(labels[name], ("act", hit, name)) for name in record["actions"] if name in labels]
        model.mode, model.action = "actions", 0
        return []
    if action == "passages" and model.hit():
        return [("passages", model.hit())]
    if action == "help":
        model.mode = "help"
        return []
    if action in ("up", "down"):
        move(model, -1 if action == "up" else 1)
        return []
    if action in ("page-up", "page-down"):
        move(model, -model.page if action == "page-up" else model.page)
        return []
    if action in ("open", "address", "reveal", "copy") and model.hit():
        return [(action, model.hit())]
    if action == "open-with" and model.hit():
        return [("apps", model.hit())]
    if action == "scope":
        model.options["where"] = cycle(SCOPES, model.options.get("where", "all"))
        return [("search",)]
    if action in ("folder", "format", "source"):
        name, values = {"folder": ("root", model.roots), "format": ("fmt", model.formats),
                        "source": ("source", model.channels)}[action]
        model.options[name] = cycle(values, model.options.get(name))
        if name == "root" and model.options[name]:
            model.options["source"] = "files"       # a folder belongs to the local files
        if name == "source" and model.options[name] != "files":
            model.options["root"] = None
        return [("search",)]
    if action in ("earlier-search", "later-search") and model.history:
        if model.recalled < 0:
            model.draft = model.query
        step = 1 if action == "earlier-search" else -1
        model.recalled = max(-1, min(len(model.history) - 1, model.recalled + step))
        model.edit(model.draft if model.recalled < 0 else model.history[model.recalled])
        return [("search",)]
    if action:
        return []
    changed = typing(model, event)
    if changed:
        model.recalled = -1
        return [("search",)]
    return []
