# Copyright 2026 Eudaemonic Enterprises Inc.
"""Two machines share their journals of searches through a folder or through rclone."""
import contextlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shelffind import history, machine, sharing

from support import run_main

# A stand-in for rclone that keeps its remote in a folder and answers as rclone does.
RCLONE = """#!/usr/bin/env python3
import os, sys
verb, *rest = sys.argv[1:]
count = -1
if "--count" in rest:                       # rclone cat --count N prints the first N bytes
    at = rest.index("--count")
    count = int(rest[at + 1])
    del rest[at:at + 2]
place = [word for word in rest if not word.startswith("--")][0]
path = os.path.join(os.environ["PRETENDED_REMOTE"], place.split(":", 1)[1])
def absent(code=3):                         # rclone says that something is not there by its exit code
    sys.stderr.write("NOTICE: Failed with 2 errors: last error was: directory not found\\n")
    sys.exit(code)
if verb == "lsf":
    if not os.path.isdir(path):
        absent()
    print("\\n".join(sorted(n for n in os.listdir(path) if os.path.isfile(os.path.join(path, n)))))
elif verb == "lsjson":
    if not os.path.isdir(path):
        sys.stdout.write("[\\n")
        absent()
    import json
    names = sorted(n for n in os.listdir(path) if os.path.isfile(os.path.join(path, n)))
    print(json.dumps([{"Path": n, "Name": n, "Size": os.path.getsize(os.path.join(path, n)), "IsDir": False,
                       "ModTime": str(os.stat(os.path.join(path, n)).st_mtime_ns)} for n in names]))
elif verb == "cat":
    if not os.path.isfile(path):
        absent()
    with open(path, "rb") as source:
        sys.stdout.buffer.write(source.read(count))
elif verb == "rcat":
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = sys.stdin.buffer.read()
    with open(path, "wb") as target:
        if os.environ.get("PRETENDED_FULL"):        # the disk refuses the rest: half is written
            target.write(data[:len(data) // 2])
            sys.exit("ERROR : Failed to copy: write: no space left on device")
        target.write(data)
elif verb == "moveto":
    target = [word for word in rest if not word.startswith("--")][1]
    os.replace(path, os.path.join(os.environ["PRETENDED_REMOTE"], target.split(":", 1)[1]))
elif verb == "deletefile":
    if not os.path.isfile(path):
        absent(4)
    os.remove(path)
else:
    sys.exit("unknown command " + verb)
"""


class Machines(unittest.TestCase):
    """Two data folders and two home folders on one real machine, told apart as two machines."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.shared = self.base / "shared"
        self.homes = {name: self.base / name for name in ("home", "office")}
        self.data = {name: self.base / f"{name}-state" for name in self.homes}
        for name, home in self.homes.items():
            home.mkdir()
            (self.base / f"{name}-id").write_text(f"{name * 8}\n")

    @contextlib.contextmanager
    def at(self, name):
        """Run what follows as on the machine `name`."""
        with patch.dict(os.environ, {"HOME": str(self.homes[name])}), \
                patch.object(machine, "SOURCES", (str(self.base / f"{name}-id"),)):
            yield self.data[name]

    def opened(self, name, document, query, when):
        with self.at(name) as data:
            history.record(data, "open", query, {"path": str(self.homes[name] / document), "source": "files",
                                                 "name": Path(document).name}, when=when)

    def exchange(self, name, place=None, **more):
        with self.at(name) as data:
            return sharing.sync(data, place or sharing.Folder(self.shared), name=name, **more)


class Journals(Machines):
    def test_what_one_machine_did_is_known_on_the_other_under_its_own_addresses(self):
        now = 1_795_000_000
        self.opened("home", "Books/Tides.pdf", "tide tables", now - 300)
        self.opened("office", "Books/Winds.pdf", "wind roses", now - 200)
        self.opened("home", "Books/Capes.pdf", "southern capes", now - 100)
        self.assertEqual(self.exchange("home", now=now), {"sent": 1, "received": 0, "damaged": 0, "machines": []})
        self.assertEqual(self.exchange("office", now=now),
                         {"sent": 1, "received": 1, "damaged": 0, "machines": ["home"]})
        self.assertEqual(self.exchange("home", now=now)["received"], 1)
        with self.at("office") as data:
            self.assertEqual(history.queries(data), ["southern capes", "wind roses", "tide tables"])
            self.assertEqual(history.documents(data), [str(self.homes["office"] / name) for name in
                                                       ("Books/Capes.pdf", "Books/Winds.pdf", "Books/Tides.pdf")])
            self.assertEqual(sorted(card["name"] for card in sharing.last(data)["machines"].values()), ["home"])
        with self.at("home") as data:
            self.assertEqual(history.documents(data)[1], str(self.homes["home"] / "Books/Winds.pdf"))
        # Nothing new: nothing travels. One more search: one journal travels.
        self.assertEqual((self.exchange("home", now=now)["sent"], self.exchange("office", now=now)["received"]), (0, 0))
        self.opened("home", "Books/Tides.pdf", "spring tide", now - 50)
        self.assertEqual((self.exchange("home", now=now)["sent"], self.exchange("office", now=now)["received"]), (1, 1))
        with self.at("office") as data:
            self.assertEqual(history.queries(data)[0], "spring tide")
        # In the shared place each machine has its own files, and no address of a home folder.
        files = sorted(str(path.relative_to(self.shared)) for path in self.shared.rglob("*") if path.is_file())
        self.assertEqual(len(files), 5)
        self.assertEqual(files[0], ".shelffind-shared")
        together = "".join(path.read_text() for path in self.shared.rglob("*.jsonl"))
        self.assertNotIn(str(self.base), together)
        self.assertIn('"~/Books/Tides.pdf"', together)

    def test_a_journal_damaged_on_the_way_is_not_taken(self):
        now = 1_795_000_000
        self.opened("home", "Books/Tides.pdf", "tide tables", now - 300)
        self.exchange("home", now=now)
        journal = next(self.shared.rglob("*.jsonl"))
        journal.write_bytes(journal.read_bytes()[:-7])              # cut before its end
        self.assertEqual(self.exchange("office", now=now), {"sent": 0, "received": 0, "damaged": 1, "machines": ["home"]})
        with self.at("office") as data:
            self.assertEqual(history.queries(data), [])
        self.opened("home", "Books/Tides.pdf", "spring tide", now - 100)       # the month grows: it is given again
        self.exchange("home", now=now)
        self.assertEqual(self.exchange("office", now=now)["received"], 1)

    def test_what_does_not_belong_in_the_shared_place_is_ignored(self):
        now = 1_795_000_000
        self.opened("home", "Books/Tides.pdf", "tide tables", now - 300)
        self.exchange("home", now=now)
        cards = self.shared / "machines"
        (cards / "not-a-label.json").write_text('{"journals": {}}')
        (cards / "0123456789abcdef.json").write_text("{ not json")
        (cards / "fedcba9876543210.json").write_text(json.dumps(
            {"name": "x" * 200, "journals": {"../../escape": {"sha256": "00"}, "2026-11": "nonsense"}}))
        done = self.exchange("office", now=now)
        self.assertEqual((done["received"], done["damaged"]), (1, 1))
        self.assertEqual(sorted(len(name) for name in done["machines"]), [4, 40])
        self.assertFalse((self.base / "escape").exists())
        (self.shared / ".shelffind-shared").write_text('{"format": 2}')
        with self.assertRaises(ValueError) as refused:
            self.exchange("office", now=now)
        self.assertIn("newer ShelfFind", str(refused.exception))

    def test_a_month_already_shared_is_never_replaced_by_less(self):
        now = 1_795_000_000
        for number in range(6):
            self.opened("home", "Books/Tides.pdf", f"query {number}", now - 600 + number)
        self.exchange("home", now=now)
        before = next(self.shared.rglob("*.jsonl")).read_bytes()
        with self.at("home") as data:                              # the journal of the machine dropped its oldest lines
            history.rewrite(data, history.lines_of(data)[4:])
        self.assertEqual(self.exchange("home", now=now)["sent"], 0)
        self.assertEqual(next(self.shared.rglob("*.jsonl")).read_bytes(), before)

    def test_the_same_exchange_through_rclone(self):
        program = self.base / "rclone"
        program.write_text(RCLONE)
        program.chmod(0o700)
        now = 1_795_000_000
        self.opened("home", "Books/Tides.pdf", "tide tables", now - 300)
        with patch.dict(os.environ, {"PRETENDED_REMOTE": str(self.base / "remote")}):
            place = sharing.Rclone("cloud:Shared/", program=str(program))
            self.assertEqual((place.names("machines"), place.get("machines/none.json")), ([], None))
            self.assertEqual(self.exchange("home", place, now=now)["sent"], 1)
            self.assertEqual(self.exchange("office", place, now=now)["received"], 1)
        with self.at("office") as data:
            self.assertEqual(history.queries(data), ["tide tables"])
        self.assertTrue((self.base / "remote" / "Shared" / ".shelffind-shared").is_file())
        with self.assertRaises(ValueError):
            sharing.Rclone("cloud:Shared", program=str(self.base / "no-such-program")).names("machines")

    def test_what_rclone_says_went_wrong_is_its_last_line_and_not_a_notice(self):
        notice = "2026/11/24 09:42:05 NOTICE: Config file \"rclone.conf\" not found - using defaults"
        self.assertEqual(sharing.Rclone.fault(notice), "rclone: it stopped without saying why")
        self.assertEqual(sharing.Rclone.fault(notice + "\n2026/11/24 09:42:06 ERROR : write: no space left on device\n"),
                         "rclone: 2026/11/24 09:42:06 ERROR : write: no space left on device")

    @unittest.skipUnless(shutil.which("rclone"), "rclone is not installed")
    def test_the_real_rclone_on_a_folder_of_this_machine(self):
        place = sharing.Rclone(str(self.base / "by-rclone"))
        self.assertEqual((place.names("machines"), place.get("machines/none.json")), ([], None))
        place.put("machines/a.json", b"{}\n")
        self.assertEqual((place.names("machines"), place.get("machines/a.json")), (["a.json"], b"{}\n"))


class Identity(Machines):
    def test_a_machine_keeps_its_label_and_a_copied_folder_gets_its_own(self):
        with self.at("home") as data:
            first = machine.identity(data, "home")
            self.assertEqual(machine.identity(data), first)
            self.assertRegex(first["id"], r"^[0-9a-f]{16}$")
            self.assertNotIn("homehome", (data / machine.NAME).read_text())    # the identifier never leaves the system
            self.assertEqual(machine.identity(data, "  winter   study ")["name"], "winter study")
        shutil.copytree(self.data["home"], self.data["office"])
        with self.at("office") as data:
            other = machine.identity(data)
            self.assertNotEqual(other["id"], first["id"])
            self.assertEqual(other["name"], "winter study")
        with patch.object(machine, "SOURCES", ()):                  # a system without identifier
            lone = machine.identity(self.base / "lone")
            self.assertEqual(machine.identity(self.base / "lone"), lone)

    def test_the_commands_exchange_when_the_settings_ask_for_it(self):
        with self.at("home") as data:
            self.assertEqual(run_main(data, "sync")[0], 1)          # nothing is shared unless asked for
            data.mkdir(exist_ok=True)
            (data / "config.toml").write_text(f'[share]\nhistory = true\nfolder = "{self.shared}"\n')
            self.assertEqual(run_main(data, "machine", "--name", "home")[1].split()[0], "home")
        self.opened("home", "Books/Tides.pdf", "tide tables", 1_795_000_000)
        with self.at("home") as data:
            code, out, _ = run_main(data, "sync")
            self.assertEqual((code, out.strip()), (0, "1 sent, 0 received (no other machine yet)"))
            self.assertEqual(run_main(data, "sync", "--quiet")[1], "")
            shown = json.loads(run_main(data, "config", "show", "--json")[1])
            self.assertEqual(shown["share.folder"]["value"], str(self.shared))
            rows = json.loads(run_main(data, "doctor", "--json")[1])
            self.assertTrue(any(row["label"] == "shared place" and "1 machine listed" in row["detail"] for row in rows))

    def test_status_names_the_machines_that_share(self):
        import re
        from shelffind.tui import report, style

        class Plain:
            """The text of what a theme would colour."""
            theme = style.theme("plain", {}, {}, terminal=False)

            def render(self, pieces, base=None):
                return re.sub(r"\x1b\[[0-9;]*m", "", self.theme.render(pieces, base))

        plain = Plain()
        last = {"synced": 1000, "machines": {"a" * 16: {"name": "office", "updated": 400},
                                              "b" * 16: {"name": "laptop", "updated": 1000 - 3 * 86400}}}
        self.assertEqual(list(report.machines({"id": "c" * 16, "name": "home"}, last, plain, now=4600)),
                         ["Machines  home (here) · laptop, shared 3 days ago · office, shared 1 h ago"
                          " · last exchange 1 h ago"])
        self.assertIn("no exchange yet", next(report.machines({"id": "c" * 16, "name": "home"}, {}, plain)))
        # The texts have their own time, when they were exchanged by this data folder.
        self.assertEqual(list(report.machines({"id": "c" * 16, "name": "home"}, last, plain, now=4600, texts=4000)),
                         ["Machines  home (here) · laptop, shared 3 days ago · office, shared 1 h ago"
                          " · last exchange 1 h ago, texts 10 min ago"])
        # Names that the settings choose, by label, win over the names that machines give themselves.
        named = {"a" * 16: "work", "c" * 16: "desk"}
        self.assertEqual(list(report.machines({"id": "c" * 16, "name": "spy"}, last, plain, now=4600, names=named)),
                         ["Machines  desk (here) · laptop, shared 3 days ago · work, shared 1 h ago"
                          " · last exchange 1 h ago"])
        self.assertEqual([report.ago(s) for s in (5, 59, 60, 3599, 3600, 86400 * 2)],
                         ["5 s", "59 s", "1 min", "59 min", "1 h", "2 days"])


if __name__ == "__main__":
    unittest.main()
