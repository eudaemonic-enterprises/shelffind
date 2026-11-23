# Copyright 2026 Eudaemonic Enterprises Inc.
"""Type to search, then open or reveal the selected document."""
import curses
import queue
import sqlite3
import subprocess
import threading
import time

from . import desktop, sources, store


def line(screen, y, text, attr=0):
    height, width = screen.getmaxyx()
    if y < height and width > 1:
        safe = "".join(c if c.isprintable() else " " for c in str(text))
        try:
            screen.addnstr(y, 0, safe, width - 1, attr)
        except curses.error:
            pass


class Searches:
    def __init__(self, directory):
        self.directory = directory
        self.requests = queue.Queue()
        self.replies = queue.Queue()
        self.version = 0
        self.thread = threading.Thread(target=self.run, args=(directory,), daemon=True)
        self.thread.start()

    def submit(self, query, options):
        self.version += 1
        self.requests.put((self.version, query, dict(options)))

    def run(self, directory):
        db = store.connect(directory)
        try:
            while (request := self.requests.get()) is not None:
                version, query, opts = request
                if version != self.version:
                    continue
                def cancelled(ticket=version):
                    return ticket != self.version

                db.set_progress_handler(cancelled, 2000)
                try:
                    def publish(rows, message, ticket=version):
                        self.replies.put((ticket, rows, message))
                    sources.find(db, self.directory, query, opts, publish, cancelled)
                except sources.Cancelled:
                    continue
                except (sqlite3.Error, ValueError) as exc:
                    self.replies.put((version, [], str(exc)))
        finally:
            db.close()

    def close(self):
        self.version += 1
        self.requests.put(None)
        self.thread.join(timeout=1)


def choose_app(screen, apps):
    position = 0
    screen.timeout(-1)
    try:
        while apps:
            screen.erase()
            line(screen, 0, "Open with    ↑↓ choose    Enter open    Esc cancel", curses.A_BOLD)
            height = max(1, screen.getmaxyx()[0] - 2)
            start = max(0, position - height + 1)
            for n, name in enumerate(apps[start:start + height], start):
                line(screen, n - start + 1, name, curses.A_REVERSE if n == position else 0)
            key = screen.get_wch()
            if key in {"\n", "\r", curses.KEY_ENTER}:
                return apps[position]
            if key == "\x1b":
                return None
            if key == curses.KEY_DOWN:
                position = min(len(apps) - 1, position + 1)
            if key == curses.KEY_UP:
                position = max(0, position - 1)
    finally:
        screen.timeout(60)
    return None


def picker(screen, db, directory, query, options):
    screen.keypad(True)
    screen.timeout(60)
    try:
        curses.set_escdelay(25)  # The default waits a full second before Esc acts.
        curses.curs_set(0)
    except curses.error:
        pass
    roots = [None] + [r["path"] for r in store.roots(db)]
    formats = [None] + [r[0] for r in db.execute("SELECT DISTINCT format FROM files WHERE format!='' ORDER BY format")]
    history = [r[0] for r in db.execute("SELECT query FROM history ORDER BY used DESC LIMIT 100")]
    channels = [None, "files"]
    try:
        channels.extend(sources.registered(directory))
    except (OSError, ValueError):
        pass  # The search response carries the configuration diagnostic.
    history_pos = -1
    searches = Searches(directory)
    dirty, results, selected, message = True, [], 0, ""
    try:
        while True:
            if dirty:
                searches.submit(query, options)
                results, selected, message, dirty = [], 0, "Searching…", False
            while not searches.replies.empty():
                version, items, error = searches.replies.get_nowait()
                if version == searches.version:
                    current = results[selected] if results else None
                    results, message = items, error or ("" if items else "No matches")
                    if current:
                        selected = next((i for i,v in enumerate(items) if
                                         (v["source"],v.get("id"),v["path"]) ==
                                         (current["source"],current.get("id"),current["path"])), 0)
            screen.erase()
            height, _ = screen.getmaxyx()
            line(screen, 0, "Find  " + query + "▏", curses.A_BOLD)
            facets = [str(options[k]) for k in ("root", "fmt", "source") if options.get(k)]
            line(screen, 1, "   ".join(facets) or "All folders and formats", curses.A_DIM)
            slots = max(1, height - 7)
            start = max(0, selected - slots + 1)
            for i, item in enumerate(results[start:start + slots], start):
                label = item["name"] if item["source"] == "files" else f"[{item['source']}] {item['name']}"
                line(screen, 2 + i - start, label, curses.A_REVERSE if i == selected else 0)
            if results:
                item = results[selected]
                line(screen, max(2, height - 4), item["path"] or f"{item['source']}  {item.get('byline') or ''}  {item['availability'] or item['match']}")
                where = f"page {item['page']}  " if item["page"] else ""
                line(screen, max(3, height - 3), where + (item["snippet"] or item["match"]))
            line(screen, max(4, height - 2), message)
            line(screen, max(5, height - 1), "Enter open  F2 apps  F3 reveal  F4 folder  F5 format  F6 source  Ctrl-P history  Esc quit")
            screen.refresh()
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if key in {"\x1b", "\x03"}:
                break
            if key in (curses.KEY_UP, curses.KEY_DOWN):
                selected = max(0, min(len(results) - 1, selected + (1 if key == curses.KEY_DOWN else -1)))
            elif key in (curses.KEY_F4, curses.KEY_F5, curses.KEY_F6):
                name, values = {curses.KEY_F4: ("root", roots), curses.KEY_F5: ("fmt", formats),
                                curses.KEY_F6: ("source", channels)}[key]
                old = options.get(name)
                options[name] = values[(values.index(old) + 1) % len(values)] if old in values else None
                if name == "root" and options[name]:
                    options["source"] = "files"
                if name == "source" and options[name] != "files":
                    options["root"] = None
                dirty = True
            elif key == "\x10" and history:
                history_pos = (history_pos + 1) % len(history)
                query, dirty = history[history_pos], True
            elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                query, dirty = query[:-1], True
            elif key == "\x15":
                query, dirty = "", True
            elif key in ("\r", "\n", curses.KEY_ENTER, curses.KEY_F2, curses.KEY_F3) and results:
                hit = results[selected]
                try:
                    target = sources.resolve(directory, hit["source"], hit.get("id") or hit["path"])
                    path = target.get("path")
                    if not path:
                        if key in (curses.KEY_F2, curses.KEY_F3):
                            message = "This result is a citation. Press Enter to view it."
                            continue
                        desktop.open_uri(target["uri"])
                    elif key == curses.KEY_F3:
                        desktop.reveal(path)
                    elif key == curses.KEY_F2:
                        apps = desktop.applications(path)
                        if not apps:
                            message = "No registered applications"
                            continue
                        app = choose_app(screen, apps)
                        if not app:
                            continue
                        desktop.launch(path, app)
                    else:
                        desktop.launch(path)
                    if query:
                        with db:
                            db.execute("INSERT OR REPLACE INTO history VALUES(?,?)", (query, time.time_ns()))
                    break
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    message = str(exc)
            elif isinstance(key, str) and key.isprintable():
                query, dirty = query + key, True
    finally:
        searches.close()
