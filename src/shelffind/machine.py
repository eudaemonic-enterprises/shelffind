# Copyright 2026 Eudaemonic Enterprises Inc.
"""Which machine this is, for what several machines share.

A machine is known by a label of sixteen hexadecimal digits drawn from its system identifier
with a key of ShelfFind's own, so that the identifier itself never leaves the machine, and by
a name for people. Both are kept in the data folder.
"""
import hashlib
import hmac
import json
import os
import re
import socket
import tempfile
from pathlib import Path

NAME = "machine.json"
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


def write(directory, told):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, prefix=NAME + ".", delete=False) as out:
        json.dump(told, out, indent=2)
    os.replace(out.name, directory / NAME)


def identity(directory, name=None):
    """The label and the name of this machine, and the name under which this data folder
    writes the texts it shares, as {"id", "name", "writer"}. They are made at the first call
    and kept; a data folder copied from another machine gets a label of its own. The writer is
    the data folder's own, so that two data folders of one machine never write the same file
    of the shared place: a folder copied, even on the same machine, gets a writer of its own,
    since the folder that holds it is another one. `name` gives the machine a new name."""
    directory = Path(directory)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)     # the folder that holds the writer is this one
    except OSError:
        pass
    try:
        told = json.loads((directory / NAME).read_text())
        told = told if isinstance(told, dict) else {}
    except (OSError, ValueError):
        told = {}
    system = system_label()
    label, known = told.get("id"), told.get("system")
    fresh = not isinstance(label, str) or not LABEL.match(label) or known != (system is not None) \
        or (system is not None and label != system)
    if fresh:
        label = system or os.urandom(8).hex()
    try:
        info = directory.stat()
        here = f"{info.st_dev}:{info.st_ino}"
    except OSError:
        here = ""
    writer = told.get("writer")
    if fresh or not isinstance(writer, str) or not LABEL.match(writer) or told.get("folder") != here:
        writer = os.urandom(8).hex()
    shown = name or told.get("name") or socket.gethostname().split(".")[0] or "machine"
    wanted = {"id": label, "name": " ".join(str(shown).split())[:40], "system": system is not None,
              "writer": writer, "folder": here}
    if wanted != told:
        try:
            write(directory, wanted)
        except OSError:
            pass
    return {"id": wanted["id"], "name": wanted["name"], "writer": wanted["writer"]}
