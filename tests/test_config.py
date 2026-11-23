# Copyright 2026 Eudaemonic Enterprises Inc.
"""Settings: the layers, the origin of each value, and what a faulty file does."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shelffind import config, sources, store
from shelffind.search import search

from support import Shelf


class Layers(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.environ = {"HOME": str(self.home), "LIBRARY": "/srv/library"}
        self.file = self.home / ".config/shelffind/config.toml"
        self.file.parent.mkdir(parents=True)

    def load(self, text=None, **extra):
        if text is not None:
            self.file.write_text(text)
        return config.load(environ={**self.environ, **extra.pop("environ", {})}, **extra)

    def test_defaults_then_file_then_environment_then_flags(self):
        settings = self.load()
        self.assertEqual((settings["panel.height"], settings.origins["panel.height"]), (40, "default"))
        settings = self.load('[panel]\nheight = "50%"\nprompt = "top"\n[keys]\nscope = "ctrl-s f7"\n')
        self.assertEqual(settings["panel.height"], "50%")
        self.assertEqual(settings.origins["panel.prompt"], str(self.file))
        self.assertEqual(settings.height(40), 20)
        self.assertEqual(settings.height(8), 4)
        self.assertEqual(settings.keymap()["f7"], "scope")      # a key chosen by the reader outranks a default
        self.assertNotIn("exact", settings.keymap().values())
        self.assertEqual(settings.warnings, [])
        self.assertNotIn("ctrl-r", settings.keymap())
        self.assertFalse(settings["search.exact"])
        self.assertEqual(config.defaults().keymap()["f7"], "exact")
        both = self.load('[keys]\nscope = "f9"\nexact = "f9"\n')
        both.keymap()
        self.assertTrue(any("f9" in warning for warning in both.warnings))
        settings = self.load('[panel]\nheight = "50%"\nprompt = "top"\n[keys]\nscope = "ctrl-s f7"\n')
        settings = self.load(environ={"SHELFFIND_PANEL_HEIGHT": "12", "SHELFFIND_PANEL_HELP": "false"},
                             flags={"panel.prompt": "bottom", "panel.preview": None})
        self.assertEqual((settings["panel.height"], settings["panel.help"]), (12, False))
        self.assertEqual(settings.origins["panel.height"], "environment SHELFFIND_PANEL_HEIGHT")
        self.assertEqual((settings["panel.prompt"], settings.origins["panel.prompt"]),
                         ("bottom", "command line"))
        self.assertEqual(settings.height(10), 9)

    def test_an_included_file_ranks_below_the_file_that_names_it(self):
        private = self.home / "private/shelffind.toml"
        private.parent.mkdir()
        private.write_text('[panel]\npreview = 6\ntheme = "mine"\n[theme.mine]\nmatch = "red"\n')
        settings = self.load('include = ["~/private/shelffind.toml", "~/absent.toml"]\n[panel]\npreview = 2\n')
        self.assertEqual((settings["panel.preview"], settings["panel.theme"]), (2, "mine"))
        self.assertEqual(settings.origins["panel.theme"], str(private))
        self.assertEqual(settings.tables["theme"]["mine"], {"match": "red"})
        self.assertEqual(len(settings.warnings), 1)
        self.assertIn("absent.toml", settings.warnings[0])
        private.write_text('include = ["../.config/shelffind/config.toml"]\n')
        with self.assertRaises(config.ConfigError):
            self.load()

    def test_unknown_settings_warn_and_wrong_values_stop(self):
        settings = self.load('colour = true\n[panel]\nhieght = 3\n')
        self.assertEqual(len(settings.warnings), 2)
        for text in ('[panel]\nheight = "tall"\n', '[panel]\nprompt = "left"\n', '[panel]\nhelp = 3\n',
                     '[panel]\npreview = true\n', 'panel = 3\n[open\n', '[keys]\nopen = []\n',
                     'include = 3\n', '[theme]\nmine = 3\n', '[open]\npdf = "zathura"\n',
                     '[open]\npdf = []\n', '[sources]\ncatalogue = "program"\n'):
            with self.assertRaises(ValueError, msg=text):
                self.load(text)
        twice = self.load('[keys]\nopen = "enter"\naddress = "enter"\n')
        twice.keymap()
        self.assertTrue(any("enter" in w for w in twice.warnings))

    def test_addresses_are_written_from_the_home_folder(self):
        self.assertEqual(config.expand("~/Books", self.environ), f"{self.home}/Books")
        self.assertEqual(config.expand("$LIBRARY/a/${LIBRARY}", self.environ), "/srv/library/a//srv/library")
        self.assertEqual(config.expand("/plain/~/x", self.environ), "/plain/~/x")
        with self.assertRaises(config.ConfigError):
            config.expand("$NOWHERE/x", self.environ)
        self.assertEqual(config.portable(f"{self.home}/Books/a.pdf", self.environ), "~/Books/a.pdf")
        self.assertEqual(config.portable(str(self.home), self.environ), "~")
        self.assertEqual(config.portable(f"{self.home}less/x", self.environ), f"{self.home}less/x")
        other = {"HOME": "/home/someone"}
        self.assertEqual(config.expand(config.portable(f"{self.home}/Books", self.environ), other),
                         "/home/someone/Books")

    def test_the_report_names_the_origin_of_every_value(self):
        settings = self.load('[panel]\nheight = 20\n[open]\npdf = ["zathura", "-P", "{page}", "{path}"]\n')
        lines = config.report(settings)
        self.assertTrue(any(line.startswith("panel.height = 20") and line.endswith(str(self.file))
                            for line in lines))
        self.assertTrue(any(line.startswith('panel.prompt = "bottom"') and line.endswith("default")
                            for line in lines))
        self.assertTrue(any(line.startswith('open.pdf = ["zathura", "-P", "{page}", "{path}"]')
                            for line in lines))


class Commands(Shelf):
    def test_an_explicit_data_folder_reads_only_its_own_file(self):
        (self.data / "config.toml").write_text('[panel]\nheight = 7\n[sources.catalogue]\n'
                                               'command = ["/bin/echo", "[]"]\n')
        code, out, _ = self.run_cli("config", "show", "--json")
        shown = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(shown["panel.height"], {"value": 7, "origin": str(self.data / "config.toml")})
        self.assertEqual(shown["sources.catalogue"]["value"], {"command": ["/bin/echo", "[]"]})
        self.assertEqual(self.run_cli("config", "path")[1].strip(), str(self.data / "config.toml"))
        code, out, _ = self.run_cli("sources", "list")
        self.assertIn("catalogue  /bin/echo []", out)
        self.assertEqual(sources.registered(self.data)["catalogue"], ["/bin/echo", "[]"])
        sources.declare({})

    def test_folders_named_in_the_settings_are_registered_and_followed(self):
        self.addCleanup(store.declare, {})
        annex, moved = self.base / "Annex", self.base / "Moved"
        annex.mkdir()
        (annex / "lantern.txt").write_text("Copper lantern on the quay.")
        settings = self.data / "config.toml"
        settings.write_text(f'[roots.Lamps]\npath = "{annex}"\nremote = "cloud:Lamps"\n'
                            f'[roots.Absent]\npath = "{self.base}/nowhere"\n'
                            f'[roots.Room]\npath = "{self.root}"\n')
        code, _, err = self.run_cli("index")
        self.assertEqual(code, 0, err)
        self.assertIn("The folder Absent of the settings cannot be reached now", err)
        found = {root["name"]: root for root in store.roots(self.db)}
        self.assertEqual(sorted(found), ["Lamps", "Room"])          # the folder known as Reading Room took its name
        self.assertEqual((found["Lamps"]["path"], found["Lamps"]["remote"]), (str(annex), "cloud:Lamps"))
        self.assertIsNone(found["Room"]["remote"])
        self.assertEqual(search(self.db, "lantern")[0]["path"], str(annex / "lantern.txt"))
        # The folder moves, the settings follow, and its document is not read again.
        annex.rename(moved)
        settings.write_text(f'[roots.Lamps]\npath = "{moved}"\n')
        with patch("shelffind.extract.passages", side_effect=AssertionError("read again")):
            self.assertEqual(self.run_cli("index")[0], 0)
        self.assertEqual(search(self.db, "copper lantern")[0]["path"], str(moved / "lantern.txt"))
        for text in ('[roots.Lamps]\nremote = "cloud:x"\n', '[roots.Lamps]\npath = 3\n',
                     '[roots.Lamps]\npath = "/a"\ncolour = "red"\n', '[roots]\nLamps = "/a"\n'):
            settings.write_text(text)
            self.assertEqual(self.run_cli("config", "check")[0], 1, text)

    def test_an_old_list_of_files_is_refreshed_beside_the_panel(self):
        from argparse import Namespace
        from shelffind import cli
        settings, args = config.defaults(), Namespace(data_dir=self.data)
        (self.root / "lantern.txt").write_text("Copper lantern.")
        with patch("shelffind.cli.apart") as started:
            cli.refresh_later(args, self.db, settings)              # never listed yet
            self.assertEqual(started.call_args.args[1:], ("index", "--names-only"))
            self.build()
            started.reset_mock()
            cli.refresh_later(args, self.db, settings)              # listed a moment ago
            started.assert_not_called()
            self.db.execute("UPDATE roots SET scanned='2020-01-01 00:00:00'")
            cli.refresh_later(args, self.db, settings)
            self.assertEqual(started.call_count, 1)
            settings.set("index.refresh", 0, "test")
            cli.refresh_later(args, self.db, settings)
            self.assertEqual(started.call_count, 1)                 # the setting 0 leaves the list alone
        settings = config.load(self.data, True, environ={"SHELFFIND_INDEX_REFRESH": "5"})
        self.assertEqual(settings["index.refresh"], 5)

    def test_check_reports_faults_and_other_commands_go_on_with_the_defaults(self):
        self.assertEqual(self.run_cli("config", "check")[0], 0)
        (self.data / "config.toml").write_text("[panel]\nhieght = 3\n")
        code, out, err = self.run_cli("config", "check")
        self.assertEqual(code, 1)
        self.assertIn("unknown setting panel.hieght", err)
        (self.data / "config.toml").write_text("[panel\n")
        code, _, err = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("the defaults are used", err)
        self.assertEqual(self.run_cli("config", "show")[0], 1)
        self.assertEqual(self.run_cli("pick", "anything")[0], 1)

    def test_doctor_cheatsheet_and_shell_snippets_print(self):
        code, out, _ = self.run_cli("doctor", "--json")
        rows = json.loads(out)
        self.assertIn(code, (0, 1))
        self.assertTrue(all(set(row) == {"status", "label", "detail", "remedy"} for row in rows))
        self.assertEqual(rows[0]["label"], "shelffind")
        self.assertTrue(any(row["label"] == "folder" and row["detail"].endswith(str(self.root)) for row in rows))
        code, out, _ = self.run_cli("doctor")
        self.assertTrue(out.startswith("[ ok ] shelffind"))
        code, out, _ = self.run_cli("cheatsheet")
        self.assertEqual(code, 0)
        self.assertIn("shelffind search --json WORDS", out)
        self.assertLessEqual(len(out.splitlines()), 40)
        code, out, _ = self.run_cli("init", "bash")
        self.assertIn(r'''bind -x '"\C-t": __shelffind_panel''', out)
        self.assertIn("shelffind pick --widget --enter open --quote", out)
        self.assertIn(r'"\ef"', self.run_cli("init", "bash", "--key", "alt-f")[1])
        self.assertEqual(self.run_cli("init", "bash", "--key", "ctrl-q")[0], 1)
        code, out, _ = self.run_cli("completion", "bash")
        self.assertIn("complete -o filenames -F _shelffind shelffind", out)
        for command in ("search", "pick", "doctor", "config"):
            self.assertIn(command, out)


if __name__ == "__main__":
    unittest.main()
