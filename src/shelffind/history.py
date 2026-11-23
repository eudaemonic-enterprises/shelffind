# Copyright 2026 Eudaemonic Enterprises Inc.
"""The searches made and the documents opened, one JSON object per line in the data folder.

A line is added at the end of the file; whenever the file must be written anew (its oldest half
dropped, earlier searches put in front), the new file is written beside it and takes its place
in one step, so that a reader always finds a whole journal and an interruption loses nothing.
"""
import contextlib
import fcntl
import json
import os
import tempfile
import time

NAME = "history.jsonl"
LIMIT = 1 << 20          # beyond this size the oldest half of the file is dropped


@contextlib.contextmanager
def writing(directory):
    """One writer at a time. The lock is a file of its own, since the journal may be replaced."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "history.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def lines_of(directory):
    """The lines of the journal; bytes that are not text are read as marks of doubt, so that one
    damaged line never hides the others."""
    try:
        with (directory / NAME).open(encoding="utf-8", errors="replace") as source:
            return source.readlines()
    except FileNotFoundError:
        return []


def rewrite(directory, lines):
    """Put a new journal in the place of the present one, in one step. The caller is the one
    writer, so a file left beside the journal by a writer that was killed can be removed."""
    for left in directory.glob(NAME + ".*"):
        left.unlink(missing_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, prefix=NAME + ".", delete=False) as out:
        try:
            os.fchmod(out.fileno(), 0o600)
            out.writelines(line if line.endswith("\n") else line + "\n" for line in lines)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            os.unlink(out.name)
            raise
    try:
        os.replace(out.name, directory / NAME)
    except BaseException:
        os.unlink(out.name)
        raise


def record(directory, action, query, hit=None, page=None, when=None):
    """Remember one use of the panel. A failure to write is never an error for the user."""
    event = {"t": int(time.time()) if when is None else int(when), "act": action, "q": query}
    if hit:
        event.update(path=hit.get("path"), source=hit.get("source"), id=hit.get("id"),
                     name=hit.get("name"), page=page)
    line = json.dumps(event, ensure_ascii=False) + "\n"
    try:
        with writing(directory):
            fd = os.open(directory / NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
                large = os.fstat(fd).st_size > LIMIT
            finally:
                os.close(fd)
            if large:
                kept = lines_of(directory)
                rewrite(directory, kept[len(kept) // 2:])
    except OSError:
        pass


def absorb(directory, asked):
    """Put the searches of an earlier journal, a list of (time, query), in front of this one.
    Those it holds already are left out, so the same list may be given twice. Returns False
    when the journal could not be written."""
    if not asked:
        return True
    try:
        with writing(directory):
            kept = lines_of(directory)
            held = set()
            for line in kept:
                try:
                    event = json.loads(line)
                    held.add((event.get("t"), event.get("q")))
                except (ValueError, AttributeError):
                    continue
            added = [json.dumps({"t": int(when), "act": "search", "q": query}, ensure_ascii=False) + "\n"
                     for when, query in asked if (int(when), query) not in held]
            if added:
                rewrite(directory, added + kept)
        return True
    except OSError:
        return False


def events(directory, limit=400):
    """The latest events, newest first. Lines that cannot be read are skipped."""
    if limit <= 0:
        return []
    try:
        lines = lines_of(directory)[-limit:]
    except OSError:
        return []
    found = []
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get("q"), str):
            found.append(event)
    return found


def queries(directory):
    """Distinct searches, newest first."""
    seen, found = set(), []
    for query in (event["q"] for event in events(directory)):
        if query.strip() and query not in seen:
            seen.add(query)
            found.append(query)
    return found


def documents(directory, limit=30):
    """The addresses of the documents opened lately, newest first, each once."""
    seen, found = set(), []
    for event in events(directory):
        if len(found) >= limit:
            break
        path = event.get("path")
        if path and event.get("act") in ("open", "launch", "address") and path not in seen:
            seen.add(path)
            found.append(path)
    return found
