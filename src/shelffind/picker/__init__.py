# Copyright 2026 Eudaemonic Enterprises Inc.
"""The search panel: a band under the prompt, in the terminal's ordinary screen."""
import json
import locale
import os
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from .. import desktop, history, machine, parcels, search, sharing, sources, store
from ..config import KEYS
from ..query import split
from ..text import safe
from ..tui import report, style
from ..tui.app import App, Terminal
from ..tui.keys import Key
from .model import Model, update
from .view import Look, frame
from .worker import Reply, Searches

EXCHANGE = 600          # seconds between two exchanges started by the panel, as on the command line

__all__ = ["panel"]


@dataclass(frozen=True)
class Brought:
    """An exchange started by the panel has written something: the band looks again."""


def stamps(directory):
    """The dates of the two notes an exchange rewrites, so a change can be seen without reading them."""
    found = []
    for name in ("share.json", parcels.TEXTS):
        try:
            found.append((directory / name).stat().st_mtime_ns)
        except OSError:
            found.append(0)
    return tuple(found)


def follow_exchange(directory, settings):
    """Start an exchange when the last one is old, in a process that outlives the band.
    Returns the dates of the notes as they were before it, or None when none was started."""
    if sharing.carrier(settings) is None or time.time() - sharing.last(directory).get("synced", 0) < EXCHANGE:
        return None
    seen = stamps(directory)
    try:
        subprocess.Popen([sys.executable, "-m", "shelffind", "--data-dir", str(directory), "sync", "--quiet"],
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        return None
    return seen


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
    model.waiting = db.execute("SELECT count(*) FROM docs WHERE state IN ('pending','error')").fetchone()[0]
    if sharing.carrier(settings) is not None:
        try:
            model.company = report.company(machine.identity(directory), sharing.last(directory),
                                           names=settings.tables["machines"], texts=sharing.texts_last(directory),
                                           unicode=unicode_signs(settings))
        except OSError:
            pass
    try:
        model.channels.extend(sources.registered(directory))
    except (OSError, ValueError):
        pass            # the first answer of a search carries the fault of the configuration
    model.history = history.queries(directory)
    model.offers = sources.offers(directory)
    return model


def panel(db, directory, query, options, settings, enter="open", quote=False, widget=False):
    """Run the panel. Returns (exit code, text for standard output, name of the document that
    was opened). The code is 0 when a document was opened or its address chosen, 1 when the
    panel was left without a choice, 130 on Ctrl-C."""
    theme = style.theme(settings["panel.theme"], settings.tables["theme"], os.environ)
    keymap = settings.keymap()
    slow = store.slow_mounts()
    look = Look(preview=settings["panel.preview"], help=settings["panel.help"],
                unicode=unicode_signs(settings), home=os.path.expanduser("~"),
                keys=tuple((action, settings[f"keys.{action}"]) for action in KEYS), slow=tuple(slow))
    model = prepare(db, directory, query, options, settings)
    terminal = Terminal(paste=not widget)
    app = App(terminal, settings.height(terminal.size()[1]), suspendable=not widget)
    searches = Searches(directory, app.post)
    result = {"code": 1, "text": "", "trace": ""}
    viewers = settings.tables["open"]

    def place(hit, fastest=False):
        """Where a result is: its file, or the address of its record. To open a document, the
        copy on a disk of this machine is preferred to one that comes through a network."""
        path = hit.get("path")
        if path and hit["source"] != "files" and not os.path.isfile(path):
            path = None                             # the copy of the source has gone since the search
        if path:
            quick = hit.get("fast")
            if fastest and quick and os.path.isfile(quick) and not store.at_hand(path, slow):
                return {"path": quick}
            return {"path": path}
        return sources.resolve(directory, hit["source"], hit.get("id") or hit["path"])

    def address(hit):
        target = place(hit)
        return target.get("path") or target["uri"]

    def work(action, hit, application=None):
        """What an action does outside the panel; it may take time (a source may fetch a file)."""
        try:
            if action in ("address", "copy"):
                text = address(hit)
                app.post(Done(action, hit, text=text))
                return
            if action == "act":
                name, identifier = sources.record_of(hit)
                told = sources.question(directory, name, "act", id=identifier, action=application)
                app.post(Done(action, hit, text=json.dumps(told)))
                return
            target = place(hit, fastest=action in ("open", "launch"))
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
        if done.action == "act":
            told = json.loads(done.text)
            if told["result"] == "copy":
                used = desktop.copy(told["text"], terminal.write)
                model.message = f"Copied: {told['text']}" if used else "No clipboard could be reached"
            elif told["result"] == "open":
                desktop.open_uri(told["uri"])
            elif told["result"] == "show":
                model.document, model.back, model.scroll, model.mode = done.hit, "search", 0, "reading"
                model.reading = {"part": 1, "page": None, "section": told.get("title"), "text": told["text"],
                                 "parts": 1}
            else:
                model.message = told["text"]
            return False
        if settings["history.record"] and not model.query.startswith(" "):     # a leading blank: off the record
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
        """Ctrl-O: the passages of the chosen document, the one of the result selected first.
        The text read by ShelfFind is searched first, then the text its source holds."""
        words, exact = split(model.query).words, model.options.get("exact", False)
        found = []
        if hit["source"] == "files":
            found = search.passages_of(db, hit, words, exact) or search.passages_of(db, hit, "")
        if not found:
            try:
                found = sources.passages(db, directory, hit, words, exact) or \
                    sources.passages(db, directory, hit, "")
            except (OSError, ValueError) as fault:
                model.message = str(fault)
                return
        if not found:
            model.message = "No text of this document has been read yet"
            return
        model.document, model.passages, model.mode, model.back = hit, found, "passages", "passages"
        model.passage = next((i for i, item in enumerate(found) if item["page"] == hit.get("page")), 0)

    def read_page(hit, part):
        page = search.page_of(db, hit, part) if hit["source"] == "files" else None
        if page is None:
            try:
                page = sources.page(directory, hit, part)
            except (OSError, ValueError) as fault:
                model.message = str(fault)
        return page

    def handle(event):
        if isinstance(event, Done):
            return finished(event)
        if isinstance(event, Brought):
            try:
                model.company = report.company(machine.identity(directory), sharing.last(directory),
                                               names=settings.tables["machines"], texts=sharing.texts_last(directory),
                                               unicode=look.unicode)
            except OSError:
                pass
            note = machine.read(directory / parcels.TEXTS)
            if note.get("held"):
                model.notice = "An indexing is running, so the pages from the other machines wait"
            elif note.get("received"):
                model.notice = ""
                if model.mode == "search":
                    model.version = searches.submit(model.query, model.options)
                    model.busy, model.fresh, model.message = True, True, ""
            return False
        if isinstance(event, Reply):
            model.offers = sources.offers(directory)    # a source just asked may have told its actions
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
                page = read_page(effect[1], effect[2])
                if page:
                    model.reading, model.scroll, model.mode, model.back = page, 0, "reading", "passages"
            elif kind in ("launch", "act"):
                start(kind, effect[1], effect[2])
            else:
                start(kind, effect[1])
        return False

    def draw(cols, rows):
        return [theme.render(pieces, base) for pieces, base in frame(model, cols, rows, look)]

    stop = threading.Event()
    seen = follow_exchange(directory, settings)

    def follow():
        current = seen
        while not stop.wait(0.3):
            now = stamps(directory)
            if now != current:
                current = now
                app.post(Brought())

    watcher = threading.Thread(target=follow, daemon=True) if seen is not None else None
    try:
        model.version = searches.submit(model.query, model.options)
        model.busy = True
        if watcher is not None:
            watcher.start()
        app.run(handle, draw)
    finally:
        stop.set()
        searches.close()
        terminal.close()
    if app.stopped_by:
        return 128 + app.stopped_by, "", ""
    return result["code"], result["text"], safe(result["trace"])
