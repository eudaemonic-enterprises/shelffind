# Copyright 2026 Eudaemonic Enterprises Inc.
"""The searches made and the documents opened, one JSON object per line in the data folder."""
import fcntl
import json
import os
import time

NAME = "history.jsonl"
LIMIT = 1 << 20          # beyond this size the oldest half of the file is dropped


def record(directory, action, query, hit=None, page=None):
    """Remember one use of the panel. A failure to write is never an error for the user."""
    event = {"t": int(time.time()), "act": action, "q": query}
    if hit:
        event.update(path=hit.get("path"), source=hit.get("source"), id=hit.get("id"),
                     name=hit.get("name"), page=page)
    line = json.dumps(event, ensure_ascii=False) + "\n"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(directory / NAME, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as out:
            fcntl.flock(out, fcntl.LOCK_EX)
            out.write(line)
            out.flush()
            if os.fstat(out.fileno()).st_size > LIMIT:
                out.seek(0)
                kept = out.readlines()
                kept = kept[len(kept) // 2:]
                out.seek(0)
                out.truncate()
                out.writelines(kept)
    except OSError:
        pass


def events(directory, limit=400):
    """The latest events, newest first. Lines that cannot be read are skipped."""
    try:
        with (directory / NAME).open(encoding="utf-8") as source:
            lines = source.readlines()[-limit:]
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


def queries(directory, earlier=()):
    """Distinct searches, newest first, followed by those of the earlier history table."""
    seen, found = set(), []
    for query in [e["q"] for e in events(directory)] + list(earlier):
        if query.strip() and query not in seen:
            seen.add(query)
            found.append(query)
    return found
