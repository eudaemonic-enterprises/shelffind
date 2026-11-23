# Copyright 2026 Eudaemonic Enterprises Inc.
"""Optional search providers registered explicitly by the local user."""
import fcntl
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from itertools import zip_longest
from pathlib import Path

from . import fuzzy
from .search import query_terms, search


def registered(directory):
    config = directory / "sources.json"
    data = json.loads(config.read_text()) if config.exists() else {}
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                             for k, v in data.items()):
        raise ValueError("Invalid sources.json: expected source names and executable paths")
    return data


def configure(directory, name, program):
    if not re.fullmatch("[a-z][a-z0-9_-]{0,39}", name) or name == "files":
        raise ValueError("Use a short source name, beginning with a letter; files is reserved")
    with (directory / "sources.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        sources = registered(directory)
        if program is None:
            if name not in sources:
                raise ValueError(f"Unknown source: {name}")
            del sources[name]
        else:
            path = program.expanduser().resolve(strict=True)
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError(f"Choose an executable provider: {path}")
            sources[name] = str(path)
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as out:
            json.dump(sources, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(out.name, directory / "sources.json")


class Cancelled(Exception):
    pass


def call(program, request, cancelled=lambda: False):
    deadline = time.monotonic() + (180 if request["op"] == "resolve" else 45)
    payload = json.dumps({"version":1, **request}).encode()
    with subprocess.Popen([program], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True) as child:
        try:
            while time.monotonic() < deadline:
                if cancelled():
                    raise Cancelled()
                try:
                    out, err = child.communicate(payload, timeout=0.1)
                    if child.returncode:
                        raise ValueError(err.decode("utf-8", "replace").strip()[:600] or "Provider failed")
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


def combine(groups, limit):
    return [row for batch in zip_longest(*groups) for row in batch if row is not None][:limit]


def find(db, directory, query, options, publish, cancelled=lambda: False):
    opts = dict(options)
    source = opts.pop("source", None)
    groups, errors = [], []
    if source in (None, "files"):
        local = search(db, query, **opts)
        for row in local:
            row["source"] = "files"
        groups.append(local)
        publish(local, "")
    if source == "files" or opts.get("root"):
        if source not in (None, "files"):
            raise ValueError("A folder filter selects the files source")
        return
    try:
        programs = registered(directory)
    except (OSError, ValueError) as exc:
        publish(combine(groups, opts["limit"]), f"Source configuration: {exc}")
        return
    if source:
        if source not in programs:
            raise ValueError(f"Unknown source: {source}")
        programs = {source:programs[source]}
    if not query.strip() or not programs:
        if not groups:
            publish([], "")
        return
    terms = [{"text":t, "literal":literal, "alternatives":[] if literal or opts["exact"] else fuzzy.spellings(db,t)}
             for t,literal in query_terms(query)]
    request = {"op": "search", "query": query, "terms": terms, "exact": opts["exact"],
                   "limit": opts["limit"], "format": opts.get("fmt"), "where": opts.get("where", "all")}
    for name, program in programs.items():
        try:
            data = call(program, request, cancelled)
            if not isinstance(data, list):
                raise TypeError("Expected an array of results")
            rows = []
            for record in data[:opts["limit"]]:
                if not isinstance(record, dict) or not all(isinstance(record.get(k),str)
                                                           and record[k] for k in ("id","name")):
                    raise ValueError("Each result needs a string id and name")
                rows.append({"source": name, "id": record["id"], "name": record["name"], "path": None, "root": None,
                                 "format": record.get("format"), "match": record.get("match") or "catalogue",
                                 "page": record.get("page"), "section": record.get("section"),
                                 "snippet": record.get("snippet"), "byline": record.get("byline"), "availability": record.get("availability")})
            groups.append(rows)
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
        publish(combine(groups, opts["limit"]), " | ".join(errors))


def resolve(directory, source, identifier):
    if source in (None, "files"):
        return {"path":identifier}
    program = registered(directory).get(source)
    if not program:
        raise ValueError(f"Unknown source: {source}")
    result = call(program, {"op":"resolve", "id":identifier})
    if isinstance(result, dict):
        path = result.get("path")
        if isinstance(path,str) and Path(path).is_absolute() and Path(path).is_file():
            return {"path":path}
        uri = result.get("uri")
        if isinstance(uri,str) and re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", uri):
            return {"uri":uri}
    raise ValueError("Provider returned no available file or citation URI")
