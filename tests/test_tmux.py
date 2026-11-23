# Copyright 2026 Eudaemonic Enterprises Inc.
"""The panel inside a real terminal program (tmux) driving a real shell (bash): the key binding
at the prompt, and changes of the window's size."""
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from support import Shelf

SOURCE = str(Path(__file__).resolve().parent.parent / "src")


@unittest.skipUnless(shutil.which("tmux") and shutil.which("bash"), "tmux and bash are required")
class Shell(Shelf):
    def setUp(self):
        super().setUp()
        self.doc = self.root / "OrchardCalendar.txt"
        self.doc.write_text("Brass compass below the deck.")
        for number in range(12):
            (self.root / f"HarbourTides{number:02}.txt").write_text("The tide turns at noon.")
        self.build()
        tools = self.base / "tools"
        tools.mkdir()
        command = tools / "shelffind"
        command.write_text(f'#!/bin/sh\nPYTHONPATH="{SOURCE}" exec "{sys.executable}" -m shelffind '
                           f'--data-dir "{self.data}" "$@"\n')
        command.chmod(0o700)
        self.server = f"shelffind-test-{os.getpid()}"
        environment = {"PATH": f"{tools}:{os.environ['PATH']}", "HOME": str(self.base), "PS1": "$ ",
                       "TERM": "xterm-256color", "LC_ALL": "C.UTF-8"}
        shell = "env -i " + " ".join(f"{name}='{value}'" for name, value in environment.items()) + \
            " bash --norc --noprofile"
        self.tmux("-f", "/dev/null", "new-session", "-d", "-x", "100", "-y", "24", shell)
        self.addCleanup(lambda: subprocess.run(["tmux", "-L", self.server, "kill-server"],
                                               capture_output=True, check=False))
        self.wait_for("$")

    def tmux(self, *arguments):
        return subprocess.run(["tmux", "-L", self.server, *arguments], capture_output=True, text=True,
                              check=False, timeout=20).stdout

    def screen(self):
        return [line.rstrip() for line in self.tmux("capture-pane", "-p").split("\n")]

    def wait_for(self, text, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(text in line for line in self.screen()):
                return
            time.sleep(0.05)
        raise AssertionError(f"{text!r} never appeared:\n" + "\n".join(self.screen()))

    def wait_gone(self, text, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(text in line for line in self.screen()):
                return
            time.sleep(0.05)
        raise AssertionError(f"{text!r} stayed on screen:\n" + "\n".join(self.screen()))

    def keys(self, *names, literal=None):
        if literal is not None:
            self.tmux("send-keys", "-l", literal)
        if names:
            self.tmux("send-keys", *names)

    def test_the_key_at_the_prompt_puts_the_quoted_address_on_the_command_line(self):
        self.keys("Enter", literal='eval "$(shelffind init bash)"')
        self.keys(literal="ls -1 ")
        self.keys("C-t")
        self.wait_for("[ ALL ]")
        self.keys(literal="orchadr")
        self.wait_for("▌ txt   OrchardCalendar")
        self.keys("Tab")
        self.wait_gone("[ ALL ]")
        self.wait_for(f"$ ls -1 '{self.doc}'")
        self.keys("Enter")
        self.wait_for("$ ls -1")
        time.sleep(0.4)
        lines = self.screen()
        answers = [line for line in lines if str(self.doc) in line and not line.startswith("$")]
        self.assertEqual(len(answers), 1, lines)                # ls found the file it was given
        self.assertFalse(any("ShelfFind" in line or "No such file" in line for line in lines))

    def test_the_panel_survives_a_narrower_and_a_wider_window_and_leaves_nothing(self):
        self.keys("Enter", literal="for n in 1 2 3; do echo earlier line $n; done")
        self.wait_for("earlier line 3")
        self.keys("Enter", literal="shelffind pick tide; echo ended with $?")
        self.wait_for("[ ALL ] tide")
        self.tmux("resize-window", "-x", "60", "-y", "24")
        time.sleep(0.6)
        self.wait_for("[ ALL ] tide")
        self.assertEqual(sum("ShelfFind" in line for line in self.screen()), 1)
        self.tmux("resize-window", "-x", "120", "-y", "18")
        time.sleep(0.6)
        self.wait_for("[ ALL ] tide")
        self.assertEqual(sum("[ ALL ]" in line for line in self.screen()), 1)
        self.keys("Escape")
        self.wait_for("ended with 1")
        lines = self.screen()
        self.assertFalse(any("ShelfFind" in line or "[ ALL ]" in line or "│" in line for line in lines))
        kept = self.tmux("capture-pane", "-p", "-S", "-200").split("\n")     # with what scrolled away
        self.assertEqual([line.rstrip() for line in kept if line.startswith("earlier line")],
                         ["earlier line 1", "earlier line 2", "earlier line 3"])


if __name__ == "__main__":
    unittest.main()
