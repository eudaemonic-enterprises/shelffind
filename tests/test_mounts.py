# Copyright 2026 Eudaemonic Enterprises Inc.
"""A network folder whose program has died answers every question with an error: the folder is
then unavailable, and neither the reading nor the report of the state fails with it."""
import errno
import json
import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from shelffind import indexer, store
from shelffind.tui import report

from support import Shelf


class Mount:
    """The questions about a folder and what is under it, answered with ENOTCONN while `down`."""

    def __init__(self, folder):
        self.prefix = str(folder)
        self.down = False

    def guard(self, real):
        def asked(path, *args, **kwargs):
            where = path if isinstance(path, str) else (os.fspath(path) if not isinstance(path, int) else "")
            if self.down and isinstance(where, str) and where.startswith(self.prefix):
                raise OSError(errno.ENOTCONN, os.strerror(errno.ENOTCONN), where)
            return real(path, *args, **kwargs)
        return asked

    @contextmanager
    def watching(self):
        with patch("os.stat", self.guard(os.stat)), patch("os.lstat", self.guard(os.lstat)), \
                patch("os.scandir", self.guard(os.scandir)):
            yield self


class Dropped(Shelf):
    def test_a_folder_that_does_not_answer_is_unavailable(self):
        (self.root / "chart.txt").write_text("harbour lights")
        self.build()
        root = store.roots(self.db)[0]
        with Mount(self.root).watching() as mount:
            self.assertTrue(store.available(root))
            mount.down = True
            self.assertFalse(store.available(root))
            mount.down = False
            self.assertTrue(store.available(root))

    def test_the_inventory_and_the_state_survive_a_dropped_mount(self):
        (self.root / "chart.txt").write_text("harbour lights")
        self.build()
        with Mount(self.root).watching() as mount:
            mount.down = True
            self.assertEqual(self.build(), (0, 1))
            code, out, _ = self.run_cli("status", "--json")
            self.assertEqual(code, 0)
            self.assertEqual([row["available"] for row in json.loads(out)], [False])
            code, out, _ = self.run_cli("status")
            self.assertEqual(code, 0)
            self.assertIn("unavailable", out)
            mount.down = False
            self.assertEqual(self.build(), (0, 0))

    def test_a_reading_cut_by_a_dropped_mount_keeps_its_work_and_goes_on_later(self):
        for n in range(3):
            (self.root / f"note{n}.txt").write_text(f"lantern {n}")
        real = indexer.update_text
        calls = []

        with Mount(self.root).watching() as mount:
            def reading(*args, **kwargs):
                calls.append(1)
                if len(calls) == 2:
                    mount.down = True
                    raise OSError(errno.ENOTCONN, os.strerror(errno.ENOTCONN), str(self.root))
                return real(*args, **kwargs)
            said = []
            with patch("shelffind.indexer.update_text", reading):
                done, failures = indexer.build(self.db, self.data, store.roots(self.db), progress=said.append)
            self.assertEqual((done, failures), (1, 1))
            self.assertTrue(any("Disconnected" in line for line in said), said)
            kept = self.db.execute("SELECT count(*) FROM docs WHERE state='indexed'").fetchone()[0]
            self.assertEqual(kept, 1)
            mount.down = False
        self.assertEqual(self.build(), (2, 0))
        self.assertEqual(self.db.execute("SELECT count(*) FROM docs WHERE state='indexed'").fetchone()[0], 3)


class Log(Shelf):
    def test_a_log_gets_one_line_of_the_reading_each_period(self):
        lines, clock = [], iter([0, 10, 59, 61, 62, 130])
        pace = report.Pace(lines.append, 60, lambda: next(clock))
        for seen in range(6):
            pace.document(seen, 6, f"Books/volume{seen}.pdf")
        self.assertEqual(len(lines), 3)
        self.assertIn("reading 1 of 6", lines[0])
        self.assertIn("reading 4 of 6", lines[1])
        self.assertTrue(lines[1].endswith("Books/volume3.pdf"))
        self.assertIn("reading 6 of 6", lines[2])

    def test_index_writes_such_lines_to_a_log_that_is_not_a_terminal(self):
        for n in range(3):
            (self.root / f"note{n}.txt").write_text(f"lantern {n}")
        with patch("shelffind.cli.PACE", 0):
            code, _, err = self.run_cli("index")
        self.assertEqual(code, 0)
        self.assertEqual(sum("reading" in line for line in err.splitlines()), 3, err)
        self.assertIn("3 documents processed", err)


if __name__ == "__main__":
    unittest.main()
