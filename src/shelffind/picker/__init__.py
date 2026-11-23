# Copyright 2026 Eudaemonic Enterprises Inc.
"""The search panel: a band under the prompt, in the terminal's ordinary screen."""
import json
import locale
import os
import shlex
import sqlite3
import subprocess
import threading
from dataclasses import dataclass

from .. import desktop, history, search, sources, store
from ..config import KEYS
from ..query import split
from ..text import safe
from ..tui import style
from ..tui.app import App, Terminal
from ..tui.keys import Key
from .legacy import picker                      # the earlier full-screen screen, kept as a fallback
from .model import Model, update
from .view import Look, frame
from .worker import Searches

__all__ = ["panel", "picker"]


@dataclass(frozen=True)
class Done:
    """An action started by a key has ended, in a thread of its own."""
    action: str
    hit: dict
    error: str = ""
    text: str = ""              # what to print for `address`


def unicode_signs(settings):
    choice = settings["panel.glyphs"]
    if choice != "auto":
        return choice == "unicode"
    return "utf" in (locale.getpreferredencoding(False) or "").lower()


def prepare(db, directory, query, options, settings):
    model = Model(query=query, caret=len(query), bottom=settings["panel.prompt"] == "bottom")
    model.options.update(options)
    model.roots = [None] + [r["path"] for r in store.roots(db)]
    model.formats = [None] + [r[0] for r in db.execute(
        "SELECT DISTINCT format FROM files WHERE format!='' ORDER BY format")]
    model.files = db.execute("SELECT count(*) FROM files").fetchone()[0]
    try:
        model.channels.extend(sources.registered(directory))
    except (OSError, ValueError):
        pass            # the first answer of a search carries the fault of the configuration
    earlier = [r[0] for r in db.execute("SELECT query FROM history ORDER BY used DESC LIMIT 100")]
    model.history = history.queries(directory, earlier)
    return model


def panel(db, directory, query, options, settings, enter="open", quote=False, widget=False):
    """Run the panel. Returns (exit code, text for standard output, name of the document that
    was opened). The code is 0 when a document was opened or its address chosen, 1 when the
    panel was left without a choice, 130 on Ctrl-C."""
    theme = style.theme(settings["panel.theme"], settings.tables["theme"], os.environ)
    keymap = settings.keymap()
    look = Look(preview=settings["panel.preview"], help=settings["panel.help"],
                unicode=unicode_signs(settings), home=os.path.expanduser("~"),
                keys=tuple((action, settings[f"keys.{action}"]) for action in KEYS))
    model = prepare(db, directory, query, options, settings)
    terminal = Terminal(paste=not widget)
    app = App(terminal, settings.height(terminal.size()[1]), suspendable=not widget)
    searches = Searches(directory, app.post)
    result = {"code": 1, "text": "", "trace": ""}
    viewers = settings.tables["open"]

    def address(hit):
        target = sources.resolve(directory, hit["source"], hit.get("id") or hit["path"])
        return target.get("path") or target["uri"]

    def work(action, hit, application=None):
        """What an action does outside the panel; it may take time (a source may fetch a file)."""
        try:
            if action in ("address", "copy"):
                text = address(hit)
                app.post(Done(action, hit, text=text))
                return
            target = sources.resolve(directory, hit["source"], hit.get("id") or hit["path"])
            path = target.get("path")
            if not path:
                if action != "open":
                    raise ValueError("This result is a citation: Enter shows it.")
                desktop.open_uri(target["uri"])
            elif action == "reveal":
                desktop.reveal(path)
            elif action == "apps":
                found = desktop.choices(path)
                if not found:
                    raise ValueError("No application is registered for this kind of file")
                app.post(Done(action, hit, text=json.dumps(found)))
                return
            else:
                custom = viewers.get(hit.get("format") or "")
                custom = custom if isinstance(custom, list) else None
                phrase = hit["snippet"][hit["marks"][0][0]:hit["marks"][0][1]] if hit.get("marks") else None
                desktop.launch(path, application, page=hit.get("page"), phrase=phrase, custom=custom)
            app.post(Done(action, hit))
        except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
            app.post(Done(action, hit, error=str(error)))

    def start(action, hit, application=None):
        model.message = {"open": "Opening", "launch": "Opening", "reveal": "Showing"}.get(action, "")
        model.message += look.sign("dots") if model.message else ""
        threading.Thread(target=work, args=(action, hit, application), daemon=True).start()

    def finished(done):
        """Returns True when the panel must close."""
        model.message = ""
        if done.error:
            model.message = done.error
            return False
        if done.action == "apps":
            found = json.loads(done.text)
            model.apps, model.app_names = [name for name, _ in found], [shown for _, shown in found]
            model.app, model.mode = 0, "apps"
            return False
        if done.action == "copy":
            used = desktop.copy(done.text, terminal.write)
            model.message = "Address copied" if used else "No clipboard could be reached"
            return False
        if settings["history.record"]:
            history.record(directory, done.action, model.query, done.hit, done.hit.get("page"))
        if done.action == "address":
            result.update(code=0, text=shlex.quote(done.text) if quote else done.text)
            return True
        if done.action in ("open", "launch"):
            result.update(code=0, trace=done.hit["name"])
            if settings["panel.after_open"] == "stay":
                model.message = "Opened"
                return False
            return True
        return False

    def list_passages(hit):
        """Ctrl-O: the passages of the chosen document, the one of the result selected first."""
        if hit["source"] != "files":
            model.message = "Passages are listed for the files of the folders"
            return
        found = search.passages_of(db, hit, split(model.query).words, model.options.get("exact", False))
        if not found:
            found = search.passages_of(db, hit, "")         # no match in the text: every page
        if not found:
            model.message = "No text of this document has been read yet"
            return
        model.document, model.passages, model.mode = hit, found, "passages"
        model.passage = next((i for i, item in enumerate(found) if item["page"] == hit.get("page")), 0)

    def handle(event):
        if isinstance(event, Done):
            return finished(event)
        for effect in update(model, event, keymap):
            kind = effect[0]
            if kind == "quit":
                result["code"] = 130 if event == Key("ctrl-c") else 1
                return True
            if kind == "search":
                model.version = searches.submit(model.query, model.options)
                model.busy, model.fresh, model.message = True, True, ""
            elif kind == "open" and enter == "print":
                start("address", effect[1])
            elif kind == "passages":
                list_passages(effect[1])
            elif kind == "read":
                page = search.page_of(db, effect[1], effect[2])
                if page:
                    model.reading, model.scroll, model.mode = page, 0, "reading"
            elif kind == "launch":
                start("launch", effect[1], effect[2])
            else:
                start(kind, effect[1])
        return False

    def draw(cols, rows):
        return [theme.render(pieces, base) for pieces, base in frame(model, cols, rows, look)]

    try:
        model.version = searches.submit(model.query, model.options)
        model.busy = True
        app.run(handle, draw)
    finally:
        searches.close()
        terminal.close()
    if app.stopped_by:
        return 128 + app.stopped_by, "", ""
    return result["code"], result["text"], safe(result["trace"])
