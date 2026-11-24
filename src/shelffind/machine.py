# Copyright 2026 Eudaemonic Enterprises Inc.
"""Which machine this is, for what several machines share.

A machine is known by a label of sixteen hexadecimal digits drawn from its system identifier
with a key of ShelfFind's own, so that the identifier itself never leaves the machine, and by
a name for people. Both are kept in the data folder.
"""
import fcntl
import hashlib
import hmac
import json
import os
import re
import socket
import tempfile
from pathlib import Path

NAME = "machine.json"
WRITING = "writer.json"     # the name under which this data folder writes, apart from what 0.3 rewrites
LOCK = "machine.lock"
LABEL = re.compile(r"[0-9a-f]{16}\Z")
SOURCES = ("/etc/machine-id", "/var/lib/dbus/machine-id")


def system_label():
    """The label drawn from the identifier of the system, or None when the system has none."""
    for place in SOURCES:
        try:
            identifier = Path(place).read_text().strip()
        except OSError:
            continue
        if identifier:
            return hmac.new(identifier.encode(), b"shelffind machine label", hashlib.sha256).hexdigest()[:16]
    return None


def write(directory, told, name=NAME):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, prefix=name + ".", delete=False) as out:
        json.dump(told, out, indent=2)
    os.replace(out.name, directory / name)


def read(path):
    try:
        told = json.loads(path.read_text())
        return told if isinstance(told, dict) else {}
    except (OSError, ValueError):
        return {}


def identity(directory, name=None):
    """The label and the name of this machine, and the name under which this data folder
    writes the texts it shares, as {"id", "name", "writer"}. They are made at the first call
    and kept; a data folder copied from another machine gets a label of its own. The writer is
    the data folder's own, so that two data folders of one machine never write the same file
    of the shared place: a folder copied, even on the same machine, gets a writer of its own,
    since the folder that holds it is another one; a machine copied whole, with its system
    identifier and its disk, keeps it, and its copy must be given another by removing the
    file WRITING of its data folder. Two processes that start together agree on one writer.
    The writer is None when its file cannot be read or written at this moment: nothing is then
    written in the shared place under a name that may not be this folder's tomorrow. `name`
    gives the machine a new name."""
    directory = Path(directory)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)     # the folder that holds the writer is this one
        held = (directory / LOCK).open("a")
    except OSError:
        held = None
    try:
        if held is not None:
            fcntl.flock(held, fcntl.LOCK_EX)
        told = read(directory / NAME)
        system = system_label()
        label, known = told.get("id"), told.get("system")
        fresh = not isinstance(label, str) or not LABEL.match(label) or known != (system is not None) \
            or (system is not None and label != system)
        if fresh:
            label = system or os.urandom(8).hex()
        shown = name or told.get("name") or socket.gethostname().split(".")[0] or "machine"
        wanted = {"id": label, "name": " ".join(str(shown).split())[:40], "system": system is not None}
        if wanted != told:
            try:
                write(directory, wanted)
            except OSError:
                pass
        try:
            info = directory.stat()
            here = f"{info.st_dev}:{info.st_ino}"
        except OSError:
            here = ""
        try:
            mine = json.loads((directory / WRITING).read_text())
            mine = mine if isinstance(mine, dict) else {}
        except FileNotFoundError:
            mine = {}
        except ValueError:
            mine = {}                           # spoilt: made anew
        except OSError:
            mine = None                         # cannot be read at this moment
        writer = mine.get("writer") if mine is not None else None
        if mine is not None and (not isinstance(writer, str) or not LABEL.match(writer) or mine.get("machine") != label
                                 or mine.get("folder") != here):
            writer = os.urandom(8).hex()
            try:
                write(directory, {"writer": writer, "machine": label, "folder": here}, WRITING)
            except OSError:
                writer = None                   # a name that would not last is never written under
        return {"id": wanted["id"], "name": wanted["name"], "writer": writer}
    finally:
        if held is not None:
            held.close()
