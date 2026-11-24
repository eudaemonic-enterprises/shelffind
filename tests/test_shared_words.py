# Copyright 2026 Eudaemonic Enterprises Inc.
"""A query whose words are shared between the name of a document and its text."""
import json
import re
import unittest

from shelffind import search

from support import Shelf


class Shared(Shelf):
    def setUp(self):
        super().setUp()
        (self.root / "Whitham-LinearAndNonlinearWaves.txt").write_text(
            "A first page about rivers.\fThe dispersion relation gives the group velocity of waves.")
        (self.root / "Whitham-Notes.txt").write_text("Nothing here but harbours and tides.")
        (self.root / "Dispersion.txt").write_text("Only a name that holds the other word.")
        (self.root / "Other.txt").write_text("The dispersion relation of an unrelated medium.")
        (self.root / "Together.txt").write_text("Whitham wrote on dispersion in this very sentence.")
        self.assertEqual(self.build(), (5, 0))

    def names(self, query, **opts):
        return [(row["name"], row["match"]) for row in search.search(self.db, query, **opts)]

    def test_one_word_in_the_name_and_the_other_in_the_text(self):
        found = self.names("whitham dispersion")
        self.assertIn(("Whitham-LinearAndNonlinearWaves.txt", "name and passage"), found)
        self.assertNotIn("Whitham-Notes.txt", [name for name, _ in found])     # its text lacks the word
        self.assertNotIn("Other.txt", [name for name, _ in found])             # its name lacks the other
        self.assertNotIn("Dispersion.txt", [name for name, _ in found])

    def test_such_a_document_comes_before_a_document_found_by_its_text_alone(self):
        found = self.names("whitham dispersion")
        order = [name for name, _ in found]
        self.assertLess(order.index("Whitham-LinearAndNonlinearWaves.txt"), order.index("Together.txt"))
        self.assertEqual(dict(found)["Together.txt"], "passage")

    def test_the_passage_shown_is_the_one_that_holds_the_words_of_the_text(self):
        row = next(r for r in search.search(self.db, "whitham dispersion") if r["name"].startswith("Whitham-Linear"))
        self.assertIn("dispersion relation", row["snippet"])
        self.assertTrue(row["marks"])
        shown = row["snippet"][row["marks"][0][0]:row["marks"][0][1]]
        self.assertEqual(shown.lower(), "dispersion")

    def test_three_words_shared_in_either_way(self):
        found = self.names("whitham dispersion relation")
        self.assertIn(("Whitham-LinearAndNonlinearWaves.txt", "name and passage"), found)
        found = self.names("linear whitham velocity")
        self.assertIn(("Whitham-LinearAndNonlinearWaves.txt", "name and passage"), found)

    def test_the_filters_and_the_scope_still_hold(self):
        self.assertFalse([n for n, _ in self.names("whitham dispersion", without=("linear",))
                          if n.startswith("Whitham-Linear")])
        self.assertFalse(self.names("whitham dispersion", fmt="pdf"))
        self.assertFalse([m for _, m in self.names("whitham dispersion", where="name") if "passage" in m])
        self.assertIn("name and passage", [m for _, m in self.names("whitham dispersion", where="all")])

    def test_a_misspelt_word_is_helped_in_the_same_way(self):
        found = dict(self.names("whitam dispersion"))
        self.assertIn(found.get("Whitham-LinearAndNonlinearWaves.txt"), ("fuzzy name and passage", "name and passage"))

    def test_exact_search_keeps_the_words_as_typed(self):
        self.assertFalse(self.names("whitam dispersion", exact=True))
        self.assertIn("Whitham-LinearAndNonlinearWaves.txt", [n for n, _ in self.names("whitham dispersion", exact=True)])

    def test_the_command_line_gives_it_too(self):
        code, out, _ = self.run_cli("search", "--json", "--source", "files", "whitham dispersion")
        self.assertEqual(code, 0)
        rows = {row["name"]: row for row in json.loads(out)}
        self.assertEqual(rows["Whitham-LinearAndNonlinearWaves.txt"]["match"], "name and passage")

    def test_the_look_into_the_text_of_the_documents_that_the_names_give_is_bounded(self):
        for n in range(search.SHARED * 3):
            (self.root / f"Alpha{n:04d}.txt").write_text(f"beta number {n} " * 5)
        self.build()
        probes = []
        self.db.set_trace_callback(lambda sql: probes.append(sql) if re.search(r"AND rowid BETWEEN \d+ AND \d+", sql) else None)
        self.assertEqual([r for r in search.search(self.db, "alpha gamma", where="all")], [])
        self.assertEqual(len(probes), search.SHARED)
        del probes[:]
        found = search.search(self.db, "alpha beta", where="all", limit=5)
        self.assertEqual([r["match"] for r in found], ["name and passage"] * 5)
        self.assertEqual(len(probes), 5)                     # it stops once the list is full
        self.db.set_trace_callback(None)

    def test_pictures_without_text_do_not_use_up_the_looks_into_the_text(self):
        for n in range(250):
            (self.root / f"waves-{n:03d}.png").write_bytes(b"\x89PNG" + n.to_bytes(2, "big"))
        (self.root / "Waves-Chapter-Of-Introduction-To-Shallow-Water-Theory.txt").write_text("These notes treat the shallow water theory.")
        self.build()
        for query in ("waves notes", "notes waves"):
            found = [(r["name"], r["match"]) for r in search.search(self.db, query, where="all")]
            self.assertIn(("Waves-Chapter-Of-Introduction-To-Shallow-Water-Theory.txt", "name and passage"), found, query)

    def test_a_word_that_is_in_many_names_does_not_take_the_looks_of_the_word_that_is_in_one(self):
        for n in range(400):
            (self.root / f"waves-{n:03d}.txt").write_text(f"Chapter number {n} on tides and harbours.")
        (self.root / "Whitham-Notes.txt").write_text("Notes on waves of every kind.")
        self.build()
        for query in ("waves whitham", "whitham waves"):
            found = [(r["name"], r["match"]) for r in search.search(self.db, query, where="all")]
            self.assertIn(("Whitham-Notes.txt", "name and passage"), found, query)

    def test_how_the_words_are_shared(self):
        self.assertEqual(search.shares(1), [])
        self.assertEqual(search.shares(2), [((0,), (1,)), ((1,), (0,))])
        three = search.shares(3)
        self.assertEqual(three[:3], [((0, 1), (2,)), ((0, 2), (1,)), ((1, 2), (0,))])
        self.assertEqual(len(three), 6)
        for chosen, rest in search.shares(8):
            self.assertTrue(chosen and rest and not set(chosen) & set(rest) and len(chosen) + len(rest) == 8)
        self.assertEqual(len(search.shares(8)), 16)


class Documents(Shelf):
    def test_documents_come_before_programs_and_pictures_of_the_same_name(self):
        for name in ("burgers.m", "burgers.eps", "Burgers.txt", "BurgersEquation.txt", "notes on burgers.md", "burgers.py"):
            (self.root / name).write_text("a stand-in for the file " + name)
        self.build()
        names = [row["name"] for row in search.search(self.db, "burgers", where="name")]
        first_other = min(names.index(n) for n in ("burgers.m", "burgers.eps", "burgers.py"))
        for document in ("Burgers.txt", "BurgersEquation.txt", "notes on burgers.md"):
            self.assertLess(names.index(document), first_other, names)

    def test_a_format_asked_for_is_not_put_behind(self):
        for name in ("burgers.m", "burgers.py", "Burgers.txt"):
            (self.root / name).write_text("a stand-in")
        self.build()
        self.assertEqual([r["name"] for r in search.search(self.db, "burgers", fmt="m")], ["burgers.m"])


if __name__ == "__main__":
    unittest.main()
