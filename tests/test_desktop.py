# Copyright 2026 Eudaemonic Enterprises Inc.
"""Opening at a page: the command given to each family of viewers, with false desktop entries."""
import base64
import unittest
from unittest.mock import patch

from shelffind import desktop

from support import Shelf

ENTRIES = {
    "org.gnome.Evince.desktop": "evince %U",
    "org.gnome.Papers.desktop": "papers %U",
    "org.kde.okular.desktop": "okular %U",
    "org.pwmt.zathura.desktop": "zathura %U",
    "sioyek.desktop": "/opt/sioyek/sioyek %f",
    "djvulibre-djview4.desktop": "djview4 %f",
    "boxed.viewer.Evince.desktop": 'flatpak run --command=evince "org.gnome.Evince" @@u %U @@',
    "plain.reader.desktop": "reader %f",
}


class Viewers(Shelf):
    def setUp(self):
        super().setUp()
        self.doc = self.root / "a 'quoted' & odd name.pdf"
        self.doc.write_bytes(b"%PDF-1.4\n")
        entries = self.base / "share/applications"
        entries.mkdir(parents=True)
        for name, command in ENTRIES.items():
            (entries / name).write_text(f"[Desktop Entry]\nName=Viewer\nExec={command}\nType=Application\n")
        environment = patch.dict("os.environ", {"XDG_DATA_HOME": str(self.base / "share"),
                                                "XDG_DATA_DIRS": str(self.base / "nowhere")})
        environment.start()
        self.addCleanup(environment.stop)

    def command(self, handler, page=12, phrase=None):
        return desktop.viewer_argv(self.doc, page, phrase, handler=handler)

    def test_each_family_of_viewers_gets_its_own_page_option(self):
        path = str(self.doc)
        self.assertEqual(self.command("org.gnome.Evince.desktop"), ["evince", "--page-index=12", path])
        self.assertEqual(self.command("org.gnome.Evince.desktop", phrase="group velocity"),
                         ["evince", "--page-index=12", "--find=group velocity", path])
        self.assertEqual(self.command("org.gnome.Papers.desktop", phrase="wave"),
                         ["papers", "--page-index=12", path])
        self.assertEqual(self.command("org.kde.okular.desktop", phrase="wave"),
                         ["okular", "-p", "12", "--find", "wave", path])
        self.assertEqual(self.command("org.pwmt.zathura.desktop"), ["zathura", "-P", "12", path])
        self.assertEqual(self.command("sioyek.desktop"), ["/opt/sioyek/sioyek", "--page", "12", path])
        self.assertEqual(self.command("djvulibre-djview4.desktop"), ["djview4", "-page=12", path])
        self.assertEqual(self.command("boxed.viewer.Evince.desktop")[:4],
                         ["flatpak", "run", "--command=evince", "org.gnome.Evince"])

    def test_a_browser_receives_the_page_in_the_address(self):
        for handler in ("com.google.Chrome.desktop", "firefox_firefox.desktop", "org.chromium.Chromium.desktop"):
            self.assertEqual(self.command(handler), ["gtk-launch", handler, self.doc.as_uri() + "#page=12"])

    def test_without_a_page_or_a_known_viewer_the_desktop_opens_as_usual(self):
        self.assertIsNone(self.command("org.gnome.Evince.desktop", page=None))
        self.assertIsNone(self.command("plain.reader.desktop"))
        self.assertIsNone(self.command("org.gnome.Evince.desktop.absent"))
        self.assertIsNone(self.command(None))

    def test_a_command_from_the_settings_leaves_out_what_is_unknown(self):
        custom = ["zathura", "-P", "{page}", "-f", "{phrase}", "{path}"]
        path = str(self.doc)
        self.assertEqual(desktop.viewer_argv(self.doc, 7, "tide", custom=custom),
                         ["zathura", "-P", "7", "-f", "tide", path])
        self.assertEqual(desktop.viewer_argv(self.doc, 7, None, custom=custom), ["zathura", "-P", "7", path])
        self.assertEqual(desktop.viewer_argv(self.doc, None, None, custom=custom), ["zathura", path])
        self.assertEqual(desktop.viewer_argv(self.doc, 7, None, custom=["browser", "{uri}#page={page}"]),
                         ["browser", self.doc.as_uri() + "#page=7"])

    def test_launch_chooses_between_the_viewer_and_the_desktop(self):
        handler = patch("shelffind.desktop.default_handler", return_value="org.gnome.Evince.desktop")
        with handler, patch("shelffind.desktop._start_viewer") as viewer, \
                patch("shelffind.desktop._run_launcher") as launcher:
            desktop.launch(self.doc, page=5, phrase="tide")
            viewer.assert_called_once_with(["evince", "--page-index=5", "--find=tide", str(self.doc)])
            launcher.assert_not_called()
            desktop.launch(self.doc)
            launcher.assert_called_once_with(["gio", "open", self.doc.as_uri()])
            desktop.launch(self.doc, page=5, custom=["mupdf", "{path}", "{page}"])
            self.assertEqual(viewer.call_args.args[0], ["mupdf", str(self.doc), "5"])
        with patch("shelffind.desktop.default_handler", return_value="com.google.Chrome.desktop"), \
                patch("shelffind.desktop._start_viewer") as viewer, \
                patch("shelffind.desktop._run_launcher") as launcher:
            desktop.launch(self.doc, page=5)
            launcher.assert_called_once_with(["gtk-launch", "com.google.Chrome.desktop",
                                              self.doc.as_uri() + "#page=5"])
            viewer.assert_not_called()
        with patch("shelffind.desktop.default_handler", side_effect=ValueError("no gio")), \
                patch("shelffind.desktop._run_launcher") as launcher:
            desktop.launch(self.doc, page=5)
            launcher.assert_called_once_with(["gio", "open", self.doc.as_uri()])

    def test_the_default_application_is_read_from_the_desktop(self):
        answers = iter(["standard::content-type: application/pdf\n",
                        "Default application for 'application/pdf': org.gnome.Papers.desktop\n"
                        "Registered applications:\n\torg.gnome.Papers.desktop\n"])
        with patch("shelffind.desktop.output", side_effect=lambda *argv: next(answers)):
            self.assertEqual(desktop.default_handler(self.doc), "org.gnome.Papers.desktop")
        answers = iter(["standard::content-type: application/x-odd\n",
                        "No default applications for 'application/x-odd'\n"])
        with patch("shelffind.desktop.output", side_effect=lambda *argv: next(answers)):
            self.assertIsNone(desktop.default_handler(self.doc))

    def test_applications_are_offered_by_the_names_people_know(self):
        entries = self.base / "share/applications"
        for name, shown in {"org.gnome.Papers.desktop": "Document Viewer", "org.gnome.Evince.desktop": "Evince",
                            "com.google.Chrome.desktop": "Google Chrome", "google-chrome.desktop": "Google Chrome",
                            "com.github.xournalpp.xournalpp.desktop": "Xournal++",
                            "okularApplication_pdf.desktop": "Okular"}.items():
            (entries / name).write_text(f"[Desktop Entry]\nType=Application\nName={shown}\nName[fr]=Autre\nExec=x %U\n")
        self.assertEqual(desktop.label("org.gnome.Papers.desktop"), "Document Viewer (Papers)")
        self.assertEqual(desktop.label("com.github.xournalpp.xournalpp.desktop"), "Xournal++")
        self.assertEqual(desktop.label("okularApplication_pdf.desktop"), "Okular")
        self.assertEqual(desktop.label("unknown.to.Anyone.desktop"), "unknown.to.Anyone")
        listed = ["com.google.Chrome.desktop", "com.github.xournalpp.xournalpp.desktop", "google-chrome.desktop",
                  "okularApplication_pdf.desktop", "org.gnome.Evince.desktop", "org.gnome.Papers.desktop"]
        with patch("shelffind.desktop.applications", return_value=listed), \
                patch("shelffind.desktop.default_handler", return_value="org.gnome.Papers.desktop"):
            self.assertEqual(desktop.choices(self.doc), [
                ("org.gnome.Papers.desktop", "Document Viewer (Papers) (opens it by default)"),
                ("com.google.Chrome.desktop", "Google Chrome"),             # one line for the two entries of Chrome
                ("com.github.xournalpp.xournalpp.desktop", "Xournal++"),
                ("okularApplication_pdf.desktop", "Okular"),
                ("org.gnome.Evince.desktop", "Evince")])
            code, out, _ = self.run_cli("apps", str(self.doc))
            self.assertEqual(out.splitlines()[1], "com.google.Chrome.desktop  Google Chrome")

    def test_a_viewer_that_fails_at_once_is_reported_and_one_that_stays_is_left_alone(self):
        folder = self.fake_tool("failing-viewer", "echo 'cannot open display' >&2\nexit 3\n")
        self.fake_tool("patient-viewer", "sleep 5\n")
        with self.assertRaises(ValueError) as failure:
            desktop._start_viewer([str(folder / "failing-viewer"), "x"])
        self.assertIn("cannot open display", str(failure.exception))
        self.assertIn("status 3", str(failure.exception))
        desktop._start_viewer([str(folder / "patient-viewer")])
        with self.assertRaises(ValueError):
            desktop._start_viewer([str(folder / "absent-viewer")])

    def test_copying_goes_through_the_terminal_and_the_desktop_tool(self):
        written = []
        with patch("shelffind.desktop.shutil.which", return_value=None):
            self.assertEqual(desktop.copy("/a/b c.pdf", written.append), ["terminal"])
        self.assertEqual(written, ["\x1b]52;c;" + base64.b64encode(b"/a/b c.pdf").decode() + "\x07"])
        with patch("shelffind.desktop.shutil.which", side_effect=lambda name: "/usr/bin/" + name), \
                patch("shelffind.desktop.subprocess.run") as run:
            run.return_value.returncode = 0
            self.assertEqual(desktop.copy("text"), ["wl-copy"])
            self.assertEqual(run.call_args.kwargs["input"], b"text")


if __name__ == "__main__":
    unittest.main()
