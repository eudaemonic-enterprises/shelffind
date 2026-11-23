# Copyright 2026 Eudaemonic Enterprises Inc.
"""A small shelf in a temporary folder, shared by the tests."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shelffind import indexer, store
from shelffind.cli import main

# A stand-in for rclone: it answers the two questions ShelfFind puts to the storage of a folder
# (sizes and dates, then checksums) from a folder of this machine, and notes each question.
RCLONE = """#!/usr/bin/env python3
import datetime, hashlib, json, os, sys
args = sys.argv[1:]
twin = args[-1] if os.path.isdir(args[-1]) else os.environ["SHELFFIND_TEST_TWIN"]    # a folder stands for itself
with open(args[args.index("--files-from-raw") + 1], encoding="utf-8") as listing:
    names = [line.rstrip("\\n") for line in listing if line.rstrip("\\n")]
with open(os.environ["SHELFFIND_TEST_TWIN_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps([args[0], args[-1], names]) + "\\n")
if os.environ.get("SHELFFIND_TEST_TWIN_SILENT"):
    sys.exit(3)
there = [name for name in names if os.path.isfile(os.path.join(twin, name))]
if args[0] == "lsjson":
    zone = datetime.timezone(datetime.timedelta(hours=4))       # written in another zone than this machine's
    print(json.dumps([{"Path": name, "Size": os.path.getsize(os.path.join(twin, name)), "IsDir": False,
                       "ModTime": datetime.datetime.fromtimestamp(int(os.path.getmtime(os.path.join(twin, name))),
                                                                  zone).isoformat()} for name in there]))
elif args[0] == "hashsum" and args[1] == "md5":
    for name in there:
        with open(os.path.join(twin, name), "rb") as held:
            print(hashlib.md5(held.read()).hexdigest() + "  " + name)
else:
    sys.exit(2)
"""


def run_main(data, *argv):
    """Run the command line in this process on the data folder `data`: exit code, standard
    output, standard error."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(["--data-dir", str(data), *argv])
        except SystemExit as stop:
            code = stop.code
    return code, out.getvalue(), err.getvalue()


class Shelf(unittest.TestCase):
    """One registered folder, `Reading Room`, with its own private index."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "Reading Room"
        self.root.mkdir()
        self.data = self.base / "state"
        self.db = store.connect(self.data)
        self.addCleanup(self.db.close)
        store.add_root(self.db, self.root, self.data)

    def build(self, **opts):
        return indexer.build(self.db, self.data, store.roots(self.db), progress=lambda _: None, **opts)

    def run_cli(self, *argv):
        """Run the command line in this process: exit code, standard output, standard error."""
        return run_main(self.data, *argv)

    def twin(self, folder=None):
        """Give the folder a storage that tells checksums: a stand-in for rclone answers from
        `folder`, which is the folder itself unless another is named, and may then lag behind
        it. Returns the list of the questions put to it, read again at each call."""
        tools = self.base / "bin"
        tools.mkdir(exist_ok=True)
        (tools / "rclone").write_text(RCLONE)
        (tools / "rclone").chmod(0o700)
        log = self.base / "rclone.log"
        setting = patch.dict(os.environ, {"PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
                                          "SHELFFIND_TEST_TWIN": str(folder or self.root),
                                          "SHELFFIND_TEST_TWIN_LOG": str(log)})
        setting.start()
        self.addCleanup(setting.stop)
        store.declare({"Reading Room": {"path": str(self.root), "remote": "twin:Room"}})
        self.addCleanup(store.declare, {})

        def questions():
            import json
            return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return questions

    def fake_tool(self, name, script):
        """Put a shell script called `name` in a folder and return that folder, for PATH."""
        folder = self.base / "bin"
        folder.mkdir(exist_ok=True)
        tool = folder / name
        tool.write_text("#!/bin/sh\n" + script)
        tool.chmod(0o700)
        return folder
