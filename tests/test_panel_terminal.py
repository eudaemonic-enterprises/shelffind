# Copyright 2026 Eudaemonic Enterprises Inc.
"""The panel run in a real pseudo-terminal, its output read by the strict small terminal.

Every sequence the panel sends must be one the strict terminal allows: in particular the
alternate screen is never requested, and nothing is ever written in the last column.
"""
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import unittest
from pathlib import Path

from support import Shelf
from vt import Screen

SOURCE = str(Path(__file__).resolve().parent.parent / "src")


# Run by the child before the program under test: the pseudo-terminal becomes its controlling
# terminal (so that /dev/tty is that terminal), and standard output may go to a file.
ATTACH = """
import fcntl, os, sys, termios
os.setsid()
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
target = os.environ.pop("SESSION_STDOUT", "")
if target:
    os.dup2(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 1)
os.execvp(sys.argv[1], sys.argv[1:])
"""


class Session:
    """A child process attached to a pseudo-terminal of a given size."""

    def __init__(self, argv, cols=80, rows=20, env=None, early=b"", stdout=None):
        self.screen = Screen(cols, rows)
        self.raw = b""
        self.fd, slave = pty.openpty()
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.before = termios.tcgetattr(self.fd)
        if early:
            os.write(self.fd, early)
        environment = {**os.environ, **(env or {}), "SESSION_STDOUT": stdout or ""}
        self.process = subprocess.Popen([sys.executable, "-c", ATTACH, *argv], stdin=slave, stdout=slave,
                                        stderr=slave, env=environment)
        self.pid = self.process.pid
        os.close(slave)

    def pump(self, timeout=0.05):
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return False
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            return False
        if not data:
            return False
        self.raw += data
        self.screen.feed(data.decode("utf-8", "replace"))
        return True

    def wait_for(self, text, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            if any(text in line for line in self.screen.text()):
                return
        raise AssertionError(f"{text!r} never appeared:\n" + "\n".join(self.screen.text()))

    def send(self, data, pause=0.05):
        os.write(self.fd, data.encode() if isinstance(data, str) else data)
        deadline = time.monotonic() + pause
        while time.monotonic() < deadline:
            self.pump(0.01)

    def resize(self, cols, rows):
        self.screen = Screen(cols, rows)           # the strict terminal does not rewrap: start afresh
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def finish(self, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            code = self.process.poll()
            if code is not None:
                while self.pump(0.05):
                    pass
                after = termios.tcgetattr(self.fd)
                os.close(self.fd)
                return (128 - code if code < 0 else code), after
        self.process.kill()
        self.process.wait()
        os.close(self.fd)
        raise AssertionError("the panel did not end:\n" + "\n".join(self.screen.text()))


class Panel(Shelf):
    def setUp(self):
        super().setUp()
        self.doc = self.root / "OrchardCalendar.txt"
        self.doc.write_text("Brass compass below the deck.")
        (self.root / "HarbourTides.txt").write_text("The tide turns at noon by the brass bell.")
        self.assertEqual(self.build(), (2, 0))
        self.out = self.base / "chosen.txt"

    def start(self, *arguments, **options):
        argv = [sys.executable, "-m", "shelffind", "--data-dir", str(self.data), *arguments]
        env = {"PYTHONPATH": SOURCE, "TERM": "xterm-256color", "LC_ALL": "C.UTF-8", "HOME": str(self.base)}
        session = Session(argv, env={**env, **options.pop("env", {})}, **options)
        self.addCleanup(lambda: session.process.kill() if session.process.poll() is None else None)
        return session

    def test_choose_a_document_and_print_its_address(self):
        session = self.start("pick", stdout=str(self.out))
        session.wait_for("[ ALL ]")
        session.send("orchadr")
        session.wait_for("▌ txt   OrchardCalendar")
        session.send("\r")
        code, after = session.finish()
        self.assertEqual(code, 0)
        self.assertEqual(self.out.read_text(), str(self.doc) + "\n")
        self.assertEqual(after, session.before)             # the terminal is as it was found
        self.assertEqual([line for line in session.screen.text() if line], [])
        self.assertTrue(session.screen.modes["cursor"])
        self.assertFalse(session.screen.modes["paste"])
        self.assertNotIn(b"\x1b[?1049", session.raw)
        self.assertNotIn(b"\x1b[6n", session.raw)

    def test_what_is_typed_before_the_panel_is_ready_is_kept(self):
        session = self.start("pick", stdout=str(self.out), early=b"harbour")
        session.wait_for("[ ALL ] harbour")
        session.wait_for("▌ txt   HarbourTides")
        session.send("\t")
        code, _ = session.finish()
        self.assertEqual((code, self.out.read_text()), (0, str(self.root / "HarbourTides.txt") + "\n"))

    def test_leaving_without_a_choice(self):
        session = self.start("pick", stdout=str(self.out))
        session.wait_for("[ ALL ]")
        session.send("\x1b", pause=0.3)
        code, after = session.finish()
        self.assertEqual((code, self.out.read_text(), after), (1, "", session.before))
        session = self.start("pick", "compass", stdout=str(self.out))
        session.wait_for("OrchardCalendar")
        session.send("\x03")
        self.assertEqual(session.finish()[0], 130)
        session = self.start()                              # the plain command: leaving is no failure
        session.wait_for("[ ALL ]")
        session.send("\x1b", pause=0.3)
        self.assertEqual(session.finish()[0], 0)

    def test_scope_passage_and_quoted_address(self):
        session = self.start("pick", "--quote", "brass", stdout=str(self.out), cols=100, rows=24)
        session.wait_for("1 of 2")
        session.send("\x12\x12")                            # Ctrl-R twice: passages only
        session.wait_for("[ TEXT ] brass")
        session.wait_for("│ Brass compass below the deck.")
        session.send("\x15tide")
        session.wait_for("The tide turns at noon")
        session.send("\x1b[A\x1b[B\t")
        code, _ = session.finish()
        self.assertEqual(code, 0)
        self.assertEqual(self.out.read_text(), f"'{self.root / 'HarbourTides.txt'}'\n")

    def test_a_new_size_redraws_the_panel_and_a_signal_restores_the_terminal(self):
        session = self.start("pick", "brass", stdout=str(self.out), cols=100, rows=24)
        session.wait_for("1 of 2")
        session.resize(60, 12)
        session.wait_for("1 of 2")
        self.assertTrue(any(line.startswith("ShelfFind") for line in session.screen.text()))
        os.kill(session.pid, signal.SIGTERM)
        code, after = session.finish()
        self.assertEqual((code, after), (128 + signal.SIGTERM, session.before))
        self.assertTrue(session.screen.modes["cursor"])

    def test_without_colour_and_with_plain_signs(self):
        session = self.start("pick", "compass", stdout=str(self.out),
                             env={"NO_COLOR": "1", "SHELFFIND_PANEL_GLYPHS": "ascii"})
        session.wait_for("> txt   OrchardCalendar")
        self.assertNotIn(b"\x1b[3", session.raw)            # no colour was sent
        self.assertNotIn("│".encode(), session.raw)
        session.send("\r")
        self.assertEqual(session.finish()[0], 0)

    def test_passages_a_page_read_the_actions_and_the_rank_keys(self):
        session = self.start("pick", "brass", stdout=str(self.out), cols=100, rows=24)
        session.wait_for("1 of 2")
        session.send("\x0f")                                # Ctrl-O: the passages of the document
        session.wait_for("[ PASSAGES ] brass")
        session.wait_for("▌ part 1")
        session.send("\x0f")                                # again: the page itself
        session.wait_for("[ PART 1 ] brass")
        session.wait_for("  Brass compass below the deck.")
        session.send("\x1b", pause=0.3)
        session.wait_for("[ PASSAGES ] brass")
        session.send("\x1b", pause=0.3)
        session.wait_for("[ ALL ] brass")
        session.send("\x18")                                # Ctrl-X: what can be done
        session.wait_for("[ ACTIONS ] OrchardCalendar")
        session.wait_for("▌ Open")
        session.send("\x1b", pause=0.3)
        session.wait_for("[ ALL ] brass")
        session.wait_for("2 txt   HarbourTides")
        session.send("\x1b2")                               # Alt-2: the second best, at once
        code, _ = session.finish()
        self.assertEqual((code, self.out.read_text()), (0, str(self.root / "HarbourTides.txt") + "\n"))


if __name__ == "__main__":
    unittest.main()
