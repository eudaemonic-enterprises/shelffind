# Copyright 2026 Eudaemonic Enterprises Inc.
"""Optional search sources registered explicitly by the local user.

A source is a program that answers for a collection ShelfFind does not index itself. The
exchange with it is described in docs/sources.md; this module is the asking side.
"""
import fcntl
import json
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import __version__, fuzzy, history, provider, store
from .fuse import fuse
from .query import split
from .search import by_paths, query_terms, search


# Sources declared in the settings file, as name -> command with its arguments. The command
# line fills this once the settings are read; a declared source replaces a registered one of
# the same name. `policies` holds what the settings say of each: whether its text is relied
# upon, and whether it is asked which files of the folders it knows.
declared = {}
policies = {}
NAME = "[a-z][a-z0-9_-]{0,39}"
SECONDS = {"hello": 5, "search": 45, "pages": 60, "identify": 120, "resolve": 180, "act": 180}
# What a source of the first version can do, and all that is supposed of a program that does
# not answer `hello`.
FIRST = {"version": 1, "name": None, "ops": ["search", "resolve"], "text": False, "network": False, "actions": []}
REMEMBERED = "sources-hello.json"


def stored(directory):
    """The sources registered with `sources add`: name -> path of the program."""
    config = directory / "sources.json"
    data = json.loads(config.read_text()) if config.exists() else {}
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                             for k, v in data.items()):
        raise ValueError("Invalid sources.json: expected source names and executable paths")
    return data


def declare(tables):
    """Take the [sources.NAME] tables of the settings: each needs `command`, a list of words.
    `text = "ignore"` makes ShelfFind read the files a source holds the text of, and
    `identify = false` stops it from asking the source which files it knows."""
    declared.clear()
    policies.clear()
    for name, table in tables.items():
        command = table.get("command") if isinstance(table, dict) else None
        if isinstance(command, str):
            command = [command]
        if not re.fullmatch(NAME, name) or name == "files" or not isinstance(command, list) \
                or not command or not all(isinstance(word, str) and word for word in command):
            raise ValueError(f"[sources.{name}] needs a short name and command = [\"program\", ...]")
        text, identify = table.get("text", "trust"), table.get("identify", True)
        if text not in ("trust", "ignore") or not isinstance(identify, bool):
            raise ValueError(f"[sources.{name}] text is \"trust\" or \"ignore\", and identify is true or false")
        if table.get("enabled", True) is not False:
            declared[name] = [os.path.expanduser(command[0]), *command[1:]]
            policies[name] = {"text": text, "identify": identify}


def policy(name):
    return policies.get(name, {"text": "trust", "identify": True})


def registered(directory):
    """Every source in force: name -> command with its arguments."""
    return {**{name: [path] for name, path in stored(directory).items()}, **declared}


def configure(directory, name, program):
    if not re.fullmatch(NAME, name) or name == "files":
        raise ValueError("Use a short source name, beginning with a letter; files is reserved")
    with (directory / "sources.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        sources = stored(directory)
        if program is None:
            if name not in sources:
                raise ValueError(f"Unknown source: {name}")
            del sources[name]
        else:
            path = program.expanduser().resolve(strict=True)
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError(f"Choose an executable provider: {path}")
            sources[name] = str(path)
        replace(directory / "sources.json", sources)


def replace(path, value):
    """Write a small JSON file so that a reader never sees it half written."""
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        json.dump(value, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(out.name, path)


class Cancelled(Exception):
    pass


def ask(program, request, cancelled=lambda: False):
    """Put one question to a source and return its answer. A failure of the program is a
    ValueError that carries its own words."""
    deadline = time.monotonic() + SECONDS.get(request["op"], 45)
    payload = json.dumps(request).encode()
    argv = [program] if isinstance(program, str) else list(program)
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True) as child:
        try:
            while time.monotonic() < deadline:
                if cancelled():
                    raise Cancelled()
                try:
                    out, err = child.communicate(payload, timeout=0.1)
                    if child.returncode:
                        raise ValueError(err.decode("utf-8", "replace").strip()[:600] or "Provider failed")
                    if len(out) > provider.LIMIT:
                        raise ValueError("Provider answered more than it may")
                    return json.loads(out)
                except subprocess.TimeoutExpired:
                    payload = None
            raise ValueError("Provider timed out")
        finally:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.communicate()


def call(program, request, cancelled=lambda: False):
    """A question of the first version."""
    return ask(program, {"version": 1, **request}, cancelled)


def stamp(argv):
    """What tells that the program of a source has changed: the size and date of its files."""
    marks = []
    for index, word in enumerate(argv):
        path = (shutil.which(word) or word) if index == 0 else word
        try:
            info = os.stat(path)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            marks.append([str(path), info.st_size, info.st_mtime_ns])
    return marks


def abilities(directory, name, argv, cancelled=lambda: False, fresh=False):
    """What the source `name` can do: asked once with `hello`, then remembered until its program
    changes. A program that does not answer `hello` is a source of the first version; one that
    refuses for the moment is not remembered, and the refusal is raised."""
    argv = list(argv)
    try:
        known = json.loads((directory / REMEMBERED).read_text())
    except (OSError, ValueError):
        known = {}
    entry, now = known.get(name) if isinstance(known, dict) else None, stamp(argv)
    if not fresh and isinstance(entry, dict) and entry.get("argv") == argv and entry.get("stamp") == now \
            and isinstance(entry.get("caps"), dict):
        return entry["caps"]
    try:
        told = ask(argv, {"version": 2, "op": "hello", "client": f"shelffind/{__version__}"}, cancelled)
    except (OSError, ValueError):
        told = None
    if isinstance(told, dict) and told.get("ok") is False and isinstance(told.get("error"), dict):
        raise ValueError(str(told["error"].get("message") or "The source refuses for the moment"))
    try:
        caps = provider.check_hello(told)
    except provider.Fault:
        caps = dict(FIRST)
    try:
        with (directory / "sources.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                known = json.loads((directory / REMEMBERED).read_text())
            except (OSError, ValueError):
                known = {}
            known = known if isinstance(known, dict) else {}
            known[name] = {"argv": argv, "stamp": now, "caps": caps}
            replace(directory / REMEMBERED, known)
    except OSError:
        pass
    return caps


def question(directory, name, op, cancelled=lambda: False, **fields):
    """Put one question of the second version to the source `name`; its checked answer."""
    argv = registered(directory).get(name)
    if not argv:
        raise ValueError(f"Unknown source: {name}")
    caps = abilities(directory, name, argv, cancelled)
    if caps["version"] < 2 or op not in caps["ops"]:
        raise ValueError(f"The source {name} does not answer {op}")
    answer = provider.check_answer(op, ask(argv, {"version": 2, "op": op, **fields}, cancelled))
    if not answer["ok"]:
        raise ValueError(answer["error"]["message"])
    return answer


def row(name, record):
    """A result of a source in the form the lists use."""
    copy = record.get("path")
    return {"source": name, "id": record["id"], "name": record.get("title") or record["name"],
            "path": copy if copy and os.path.isfile(copy) else None, "root": None,
            "format": record.get("format"), "match": record.get("match") or "catalogue",
            "page": record.get("page"), "page_label": record.get("page_label"), "section": record.get("section"),
            "snippet": record.get("snippet"), "marks": record.get("marks") or [], "size": record.get("size"),
            "mtime": None, "title": record.get("title"), "byline": record.get("byline"), "year": record.get("year"),
            "venue": record.get("venue"), "file": record.get("name"), "keys": record.get("keys") or [],
            "fetch": bool(record.get("fetch")), "flags": record.get("flags") or [], "notes": record.get("notes"),
            "actions": record.get("actions") or [], "availability": record.get("availability")}


def answers(directory, name, argv, request, limit, without, cancelled):
    """The results of one source for a search, in the form the lists use."""
    caps = abilities(directory, name, argv, cancelled)
    if caps["version"] >= 2:
        data = provider.check_answer("search", ask(argv, {"version": 2, **request, "within": None,
                                                          "budget_ms": 800}, cancelled))
        if not data["ok"]:
            raise ValueError(data["error"]["message"])
        records = data["results"]
    else:
        records = call(argv, request, cancelled)
        if not isinstance(records, list):
            raise TypeError("Expected an array of results")
        for record in records:
            if not isinstance(record, dict) or not all(isinstance(record.get(k), str) and record[k]
                                                       for k in ("id", "name")):
                raise ValueError("Each result needs a string id and name")
    rows = []
    for record in records[:limit]:
        shown = row(name, record)
        if not any(word in fuzzy.fold(shown["name"]) for word in without):
            rows.append(shown)
    return rows


def find(db, directory, query, options, publish, cancelled=lambda: False):
    """Search the files, then every outside source at the same time. Filters typed in the query
    (`ext:`, `in:`, `src:`, `-word`) are taken out of it first and win over the options.
    `publish(rows, message)` receives the list each time it grows: the matches by name, the
    passages, then the list merged with the answers of each source as they come."""
    opts = dict(options)
    source = opts.pop("source", None)
    pause = opts.pop("pause", 0)
    typed = split(query)
    query = typed.words
    source = typed.source or source
    if typed.fmt:
        opts["fmt"] = typed.fmt
    if typed.place:
        wanted = typed.place.lower()
        named = [r["path"] for r in store.roots(db) if wanted in (r["name"].lower(), Path(r["path"]).name.lower())]
        if len(named) == 1:
            opts["root"] = named[0]
        else:
            opts["folder"] = typed.place
    opts["without"] = typed.without
    groups, errors = [], []
    if source in (None, "files"):
        def early(rows):
            """The matches by name are shown while the passages are still being searched."""
            for shown in rows:
                shown["source"] = "files"
            publish(rows, "")
        local = search(db, query, **opts, interim=early)
        if not query.strip() and not (opts.get("root") or opts.get("fmt") or opts.get("folder") or typed.without):
            # Nothing is asked yet: the documents opened lately come first.
            lately = by_paths(db, history.documents(directory))
            seen = {shown["doc"] for shown in lately}
            local = (lately + [shown for shown in local if shown["doc"] not in seen])[:opts["limit"]]
        for shown in local:
            shown["source"] = "files"
        groups.append(("files", local))
        publish(local, "")
    folder = opts.pop("folder", None)
    without = [fuzzy.fold(word) for word in opts.pop("without")]
    if source == "files" or opts.get("root") or folder:
        if source not in (None, "files"):
            raise ValueError("A folder filter selects the files source")
        return
    try:
        programs = registered(directory)
    except (OSError, ValueError) as exc:
        publish(fuse(db, groups, opts["limit"]), f"Source configuration: {exc}")
        return
    if source:
        if source not in programs:
            raise ValueError(f"Unknown source: {source}")
        programs = {source: programs[source]}
    if not query.strip() or not programs:
        if not groups:
            publish([], "")
        return
    # A pause lets the next letter arrive before any program is started for this one.
    waited = time.monotonic() + pause
    while time.monotonic() < waited:
        if cancelled():
            raise Cancelled()
        time.sleep(0.02)
    terms = [{"text": t, "literal": literal,
              "alternatives": [] if literal or opts["exact"] else fuzzy.spellings(db, t)}
             for t, literal in query_terms(query)]
    request = {"op": "search", "query": query, "terms": terms, "exact": opts["exact"],
               "limit": opts["limit"], "format": opts.get("fmt"), "where": opts.get("where", "all")}
    arrived = queue.Queue()

    def one(name, argv):
        try:
            if source != name and abilities(directory, name, argv, cancelled)["network"]:
                arrived.put((name, None, None))         # asked only when the user names it
            else:
                arrived.put((name, answers(directory, name, argv, request, opts["limit"], without, cancelled), None))
        except Cancelled:
            arrived.put((name, None, None))
        except (OSError, TypeError, ValueError) as exc:
            arrived.put((name, None, f"{name}: {exc}"))

    for name, argv in programs.items():
        threading.Thread(target=one, args=(name, argv), daemon=True).start()
    place = {name: index for index, name in enumerate(programs)}
    for _ in programs:
        name, rows, fault = arrived.get()
        if cancelled():
            raise Cancelled()
        if fault:
            errors.append(fault)
        if rows is not None:
            groups.append((name, rows))
            groups.sort(key=lambda group: -1 if group[0] == "files" else place[group[0]])
        publish(fuse(db, groups, opts["limit"]), " | ".join(errors))


def resolve(directory, source, identifier):
    """Where a result can be opened: a file, which the source may fetch first, or an address."""
    if source in (None, "files"):
        return {"path": identifier}
    program = registered(directory).get(source)
    if not program:
        raise ValueError(f"Unknown source: {source}")
    version = abilities(directory, source, program)["version"]
    result = ask(program, {"version": version, "op": "resolve", "id": identifier})
    if isinstance(result, dict):
        if result.get("ok") is False and isinstance(result.get("error"), dict):
            raise ValueError(str(result["error"].get("message") or "The source refuses"))
        path = result.get("path")
        if isinstance(path, str) and Path(path).is_absolute() and Path(path).is_file():
            return {"path": path}
        uri = result.get("uri")
        if isinstance(uri, str) and re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", uri):
            return {"uri": uri}
    raise ValueError("Provider returned no available file or citation URI")


def check(directory, name, query):
    """Put to the source `name` the questions it says it answers and yield, for each, a status
    (ok, warn or fail), the question, and what came back. Nothing is changed, and no action of
    the source is carried out."""
    program = registered(directory).get(name)
    if not program:
        yield "fail", "source", f"{name} is not registered"
        return
    try:
        caps = abilities(directory, name, program, fresh=True)
    except (OSError, ValueError) as fault:
        yield "fail", "hello", str(fault)
        return
    if caps["version"] < 2:
        yield "ok", "hello", "version 1 (the program does not answer hello): search and resolve"
    else:
        told = [", ".join(caps["ops"]), "holds text" if caps["text"] else "no text",
                "needs the network" if caps["network"] else "works offline"]
        yield "ok", "hello", f"version 2 · {' · '.join(told)}"
        for action in caps["actions"]:
            yield "ok", "action", f"{action['name']}: {action['label']} ({action['result']})"
    terms = [{"text": t, "literal": literal, "alternatives": []} for t, literal in query_terms(query)]
    request = {"op": "search", "query": query, "terms": terms, "exact": False, "limit": 5, "format": None,
               "where": "all"}
    started = time.monotonic()
    try:
        rows = answers(directory, name, program, request, 5, [], lambda: False)
    except (OSError, TypeError, ValueError) as fault:
        yield "fail", "search", str(fault)
        return
    spent = time.monotonic() - started
    yield ("ok" if spent < 1 else "warn"), "search", f"{len(rows)} results for \"{query}\" in {spent:.2f} s" + \
        ("" if rows else "; try words the source surely knows")
    for found in rows[:1]:
        yield "ok", "result", " · ".join(str(part) for part in (found["name"], found.get("byline"), found.get("year"),
                                                                  found["match"]) if part)
        try:
            place = resolve(directory, name, found["id"])
            yield "ok", "resolve", place.get("path") or place["uri"]
        except (OSError, ValueError) as fault:
            yield "fail", "resolve", str(fault)
    try:
        if "identify" in caps["ops"]:
            unknown = {"n": 0, "path": "/nonexistent/shelffind-check.pdf", "size": 1, "mtime": 0, "keys": []}
            records = question(directory, name, "identify", files=[unknown])["records"]
            yield ("ok" if not records else "warn"), "identify", \
                "a file that does not exist is not recognised" if not records else "a made-up file was recognised"
        if "pages" in caps["ops"] and rows:
            told = question(directory, name, "pages", id=rows[0]["id"], first=rows[0].get("page") or 1, count=1)
            yield "ok", "pages", f"{len(told['pages'])} page of {told.get('total', '?')}"
    except (OSError, ValueError) as fault:
        yield "fail", "questions", str(fault)


def actions(directory, name):
    """The actions the source `name` declares, as name -> (label, kind of result)."""
    program = registered(directory).get(name)
    if not program:
        return {}
    return {action["name"]: (action["label"], action["result"])
            for action in abilities(directory, name, program)["actions"]}


def offers(directory):
    """The actions of every source as they are remembered, without asking anything: source ->
    {name of the action: its label}."""
    try:
        known = json.loads((directory / REMEMBERED).read_text())
        return {name: {action["name"]: action["label"] for action in entry["caps"]["actions"]}
                for name, entry in known.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def record_of(hit):
    """The source and the identifier under which a result is known to a source, or None."""
    if hit.get("record"):
        return hit["record"]["source"], hit["record"]["id"]
    if hit["source"] != "files" and hit.get("id"):
        return hit["source"], hit["id"]
    return None


def passages(db, directory, hit, query, exact=False, limit=200):
    """The passages of one document as its source finds them, in the form `search.passages_of`
    gives: with words, the passages that match; without, the beginning of its first pages."""
    known = record_of(hit)
    if known is None:
        return []
    name, identifier = known
    program = registered(directory).get(name)
    if not program or not abilities(directory, name, program)["text"]:
        return []
    parts = query_terms(query)
    if parts:
        terms = [{"text": t, "literal": literal, "alternatives": [] if literal or exact else fuzzy.spellings(db, t)}
                 for t, literal in parts]
        found = question(directory, name, "search", query=query, terms=terms, exact=exact, where="text",
                         format=None, limit=limit, within=identifier, budget_ms=2000)["results"]
        return [{"part": item.get("page") or index, "page": item.get("page"), "page_label": item.get("page_label"),
                 "section": item.get("section"), "snippet": item.get("snippet") or "", "marks": item.get("marks") or []}
                for index, item in enumerate(found, 1)]
    told = question(directory, name, "pages", id=identifier, first=1, count=min(limit, 40))
    return [{"part": page["page"], "page": page["page"], "page_label": page.get("label"), "section": None,
             "snippet": " ".join(page["text"].split())[:240], "marks": []} for page in told["pages"]]


def page(directory, hit, part):
    """One page of a document as its source holds it, in the form `search.page_of` gives."""
    known = record_of(hit)
    if known is None or part < 1:
        return None
    told = question(directory, known[0], "pages", id=known[1], first=part, count=1)
    if not told["pages"]:
        return None
    found = told["pages"][0]
    return {"part": part, "page": found["page"], "page_label": found.get("label"), "section": None,
            "text": found["text"], "parts": told.get("total") or part}
