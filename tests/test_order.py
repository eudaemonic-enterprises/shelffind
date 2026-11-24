# Copyright 2026 Eudaemonic Enterprises Inc.
"""The order in which the waiting documents are read."""
import unittest
from unittest.mock import patch

from shelffind import indexer

from support import Shelf


class Order(Shelf):
    def reads(self, **opts):
        """The files in the order in which a reading takes them up."""
        taken, real = [], indexer.update_text

        def reading(db, root, row, *args, **kwargs):
            taken.append(row["rel"])
            return real(db, root, row, *args, **kwargs)
        with patch("shelffind.indexer.update_text", reading):
            self.build(**opts)
        return taken

    def write(self):
        for name, size in (("middle.txt", 300), ("tiny.txt", 20), ("large.txt", 3000), ("small.txt", 90), ("big.txt", 900)):
            (self.root / name).write_text(("word%s " % name[:3]) * (size // 8))

    def test_the_smallest_documents_come_first(self):
        self.write()
        sizes = {p.name: p.stat().st_size for p in self.root.iterdir()}
        order = self.reads(order="smallest")
        self.assertEqual([sizes[n] for n in order], sorted(sizes.values()))

    def test_the_largest_documents_come_first_when_asked(self):
        self.write()
        sizes = {p.name: p.stat().st_size for p in self.root.iterdir()}
        order = self.reads(order="largest")
        self.assertEqual([sizes[n] for n in order], sorted(sizes.values(), reverse=True))

    def test_the_same_order_from_one_run_to_the_next_and_all_documents_are_read(self):
        self.write()
        first = self.reads(order="smallest", limit=2)
        self.assertEqual(len(first), 2)
        rest = self.reads(order="smallest")
        self.assertEqual(sorted(first + rest), sorted(p.name for p in self.root.iterdir()))
        self.assertEqual(self.db.execute("SELECT count(*) FROM docs WHERE state='indexed'").fetchone()[0], 5)

    def test_the_setting_and_the_flag(self):
        self.write()
        sizes = {p.name: p.stat().st_size for p in self.root.iterdir()}
        taken, real = [], indexer.update_text

        def reading(db, root, row, *args, **kwargs):
            taken.append(sizes[row["rel"]])
            return real(db, root, row, *args, **kwargs)
        with patch("shelffind.indexer.update_text", reading):
            code, _, _ = self.run_cli("index", "--order", "largest")
        self.assertEqual((code, taken), (0, sorted(sizes.values(), reverse=True)))

    def test_the_order_can_be_chosen_in_the_settings(self):
        self.write()
        (self.data / "config.toml").write_text('[index]\norder = "largest"\n')
        sizes = {p.name: p.stat().st_size for p in self.root.iterdir()}
        taken, real = [], indexer.update_text

        def reading(db, root, row, *args, **kwargs):
            taken.append(sizes[row["rel"]])
            return real(db, root, row, *args, **kwargs)
        with patch("shelffind.indexer.update_text", reading):
            self.assertEqual(self.run_cli("index")[0], 0)
        self.assertEqual(taken, sorted(sizes.values(), reverse=True))


if __name__ == "__main__":
    unittest.main()
