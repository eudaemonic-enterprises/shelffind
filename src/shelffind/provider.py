# Copyright 2026 Eudaemonic Enterprises Inc.
"""A source for ShelfFind in a few lines. One file without dependencies, to import or to copy.

    from shelffind.provider import serve

    def search(request):
        return [{"id": "a1", "title": "Tide Tables", "match": "catalogue"}]

    serve(name="Example", search=search)

Each function receives the question, a dict, and returns its answer. `serve` reads the question
from standard input, answers `hello` by itself, serves version 1 as well, checks the answer and
turns an exception into a refusal. The exchange is described in docs/sources.md.
"""
import json
import re
import sys

VERSION = 2
OPS = ("search", "resolve", "identify", "pages", "act")
RESULTS = ("copy", "open", "show", "message")
CODES = ("unavailable", "invalid", "not_found", "failed")
LIMIT = 8 << 20                 # an answer is at most this many bytes
KEY = re.compile(r"(?:sha256:[0-9a-f]{64}|md5:[0-9a-f]{32})\Z")
URI = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*:")
WORDS = ("title", "name", "byline", "venue", "format", "match", "page_label", "section", "snippet",
         "availability", "basis")


class Refusal(Exception):
    """Raise it inside a source to refuse a question: Refusal("not_found", "No such record")."""

    def __init__(self, code, message, retry=False):
        super().__init__(message)
        self.code = code if code in CODES else "failed"
        self.retry = bool(retry)


class Fault(ValueError):
    """An answer that departs from the exchange."""


def check_record(record, what="result"):
    """Check one result of `search` or one record of `identify`; returns it."""
    if not isinstance(record, dict):
        raise Fault(f"A {what} must be an object")
    if not isinstance(record.get("id"), str) or not record["id"]:
        raise Fault(f"A {what} needs a string id")
    if what == "result" and not any(isinstance(record.get(k), str) and record[k] for k in ("title", "name")):
        raise Fault(f"The {what} {record['id']} needs a title or a name")
    for field in WORDS:
        if record.get(field) is not None and not isinstance(record[field], str):
            raise Fault(f"{field} of {record['id']} must be a string")
    for field in ("page", "size", "notes", "n"):
        value = record.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise Fault(f"{field} of {record['id']} must be a whole number")
    if record.get("year") is not None and (isinstance(record["year"], bool)
                                           or not isinstance(record["year"], (int, str))):
        raise Fault(f"year of {record['id']} must be a number or a string")
    for field in ("keys", "flags", "actions"):
        value = record.get(field, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise Fault(f"{field} of {record['id']} must be a list of strings")
    for key in record.get("keys", []):
        if not KEY.match(key):
            raise Fault(f"A key of {record['id']} is neither sha256: nor md5: with lower-case digits")
    path = record.get("path")
    if path is not None and not (isinstance(path, str) and path.startswith("/")):
        raise Fault(f"path of {record['id']} must be an absolute address")
    marks = record.get("marks", [])
    if not isinstance(marks, list) or not all(
            isinstance(m, list) and len(m) == 2 and all(isinstance(v, int) and not isinstance(v, bool) for v in m)
            and 0 <= m[0] <= m[1] for m in marks):
        raise Fault(f"marks of {record['id']} must be ranges [start, end]")
    return record


def check_place(answer):
    """Check an answer of `resolve`: a file to open, or an address that opens elsewhere."""
    if not isinstance(answer, dict):
        raise Fault("The answer of resolve must be an object")
    path, uri = answer.get("path"), answer.get("uri")
    if isinstance(path, str) and path.startswith("/"):
        return answer
    if isinstance(uri, str) and URI.match(uri):
        return answer
    raise Fault("resolve must give an absolute path or a uri")


def check_hello(answer):
    """Check an answer of `hello`; returns it with every field present."""
    if not isinstance(answer, dict) or answer.get("ok") is not True or answer.get("version") != VERSION:
        raise Fault("Not a source of version 2")
    ops = answer.get("ops")
    if not isinstance(ops, list) or "search" not in ops or not all(op in OPS for op in ops):
        raise Fault("ops must list search, and only known questions")
    actions = answer.get("actions", [])
    if not isinstance(actions, list):
        raise Fault("actions must be a list")
    for action in actions:
        if not isinstance(action, dict) or not all(isinstance(action.get(k), str) and action[k]
                                                   for k in ("name", "label")) or action.get("result") not in RESULTS:
            raise Fault("An action needs a name, a label and a result among " + ", ".join(RESULTS))
    name = answer.get("name")
    return {"ok": True, "version": VERSION, "name": name if isinstance(name, str) else None, "ops": list(ops),
            "text": answer.get("text") is True, "network": answer.get("network") is True, "actions": actions}


def check_answer(op, answer):
    """Check a version 2 answer to the question `op`; returns it. A refusal passes as it is."""
    if not isinstance(answer, dict) or not isinstance(answer.get("ok"), bool):
        raise Fault("An answer must be an object with ok")
    if not answer["ok"]:
        error = answer.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("message"), str):
            raise Fault("A refusal needs error.message")
        return answer
    if op == "hello":
        return check_hello(answer)
    if op == "search":
        if not isinstance(answer.get("results"), list):
            raise Fault("search must give results")
        for record in answer["results"]:
            check_record(record)
    elif op == "identify":
        if not isinstance(answer.get("records"), list):
            raise Fault("identify must give records")
        for record in answer["records"]:
            check_record(record, "record")
            if not isinstance(record.get("n"), int):
                raise Fault("A record of identify needs n, the number of the file it answers")
    elif op == "resolve":
        check_place(answer)
    elif op == "pages":
        pages = answer.get("pages")
        if not isinstance(pages, list) or not all(
                isinstance(p, dict) and isinstance(p.get("page"), int) and isinstance(p.get("text"), str)
                for p in pages):
            raise Fault("pages must give pages, each with page and text")
    elif op == "act":
        if answer.get("result") not in RESULTS:
            raise Fault("act must give a result among " + ", ".join(RESULTS))
        needed = "uri" if answer["result"] == "open" else "text"
        if not isinstance(answer.get(needed), str):
            raise Fault(f"The result {answer['result']} needs {needed}")
    return answer


def answer(request, *, name=None, search, resolve=None, identify=None, pages=None, act=None, actions=(),
           text=False, network=False):
    """The answer to one question, as (value, failure): `failure` is a message when a question
    of version 1 cannot be answered, since that version has no way to refuse."""
    handlers = {"search": search, "resolve": resolve, "identify": identify, "pages": pages, "act": act}
    old = isinstance(request, dict) and request.get("version") == 1
    try:
        if not isinstance(request, dict) or request.get("version") not in (1, VERSION):
            raise Refusal("invalid", "Unsupported version of the exchange")
        op = request.get("op")
        if op == "hello" and not old:
            return check_hello({"ok": True, "version": VERSION, "name": name, "text": text, "network": network,
                                "ops": [o for o in OPS if handlers[o]], "actions": list(actions)}), None
        if op not in handlers or handlers[op] is None or (old and op not in ("search", "resolve")):
            raise Refusal("invalid", f"Unknown question: {op}")
        if op == "search":
            request = {"within": None, "budget_ms": None, "where": "all", "format": None, "exact": False,
                       "limit": 25, **request}
        found = handlers[op](request)
        if op == "search":
            found = found if isinstance(found, dict) else {"results": found}
            found = {"partial": False, **found}
        elif op == "identify":
            found = found if isinstance(found, dict) else {"records": found}
        if not isinstance(found, dict):
            raise Fault(f"The answer of {op} must be an object")
        found = check_answer(op, {"ok": True, **found})
        if not old:
            return found, None
        if op == "resolve":
            return {key: found[key] for key in ("path", "uri") if key in found}, None
        return [{**row, "name": row.get("name") or row["title"]} for row in found["results"]], None
    except Refusal as refusal:
        error = {"code": refusal.code, "message": str(refusal), "retry": refusal.retry}
    except Fault as fault:
        error = {"code": "failed", "message": f"The source gave a faulty answer: {fault}", "retry": False}
    except Exception as fault:                      # a source must answer, whatever happens inside it
        error = {"code": "failed", "message": f"{type(fault).__name__}: {fault}", "retry": False}
    return (None, error["message"]) if old else ({"ok": False, "error": error}, None)


def serve(**source):
    """Answer the question waiting on standard input, on standard output."""
    try:
        request = json.load(sys.stdin)
    except ValueError:
        request = None
    value, failure = answer(request, **source)
    if failure is not None:
        sys.exit(failure)
    text = json.dumps(value, ensure_ascii=False)
    if len(text.encode("utf-8", "replace")) > LIMIT:
        text = json.dumps({"ok": False, "error": {"code": "failed", "message": "The answer is too large",
                                                  "retry": False}})
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
