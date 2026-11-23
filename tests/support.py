# Copyright 2026 Eudaemonic Enterprises Inc.
"""A small shelf in a temporary folder, shared by the tests."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from shelffind import indexer, store
from shelffind.cli import main


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

    def fake_tool(self, name, script):
        """Put a shell script called `name` in a folder and return that folder, for PATH."""
        folder = self.base / "bin"
        folder.mkdir(exist_ok=True)
        tool = folder / name
        tool.write_text("#!/bin/sh\n" + script)
        tool.chmod(0o700)
        return folder
