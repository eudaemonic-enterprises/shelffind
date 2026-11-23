# Copyright 2026 Eudaemonic Enterprises Inc.
"""The terminal and the loop that feeds keys, answers of workers and signals to a program."""
import os
import queue
import select
import signal
import termios
import time
from dataclasses import dataclass

from .keys import Decoder, Key
from .region import Region


@dataclass(frozen=True)
class Resize:
    cols: int
    rows: int


@dataclass(frozen=True)
class Tick:
    """A timer set with `App.after` has run out."""
    name: str


@dataclass(frozen=True)
class Stop:
    """The terminal went away, or the process was asked to end."""
    signal: int = 0


class Terminal:
    """The controlling terminal, opened on its own so that standard output stays free."""

    def __init__(self, path="/dev/tty", paste=True):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        self.saved = termios.tcgetattr(self.fd)
        self.paste = paste
        self.raw = False

    def size(self):
        try:
            size = os.get_terminal_size(self.fd)
            return size.columns, size.lines
        except OSError:
            return 80, 24

    def write(self, text):
        data = text.encode("utf-8", "replace")
        while data:
            data = data[os.write(self.fd, data):]

    def enter(self):
        """Keys arrive one by one and unechoed; Ctrl-C and Ctrl-Z arrive as keys. What was typed
        before this moment is kept, and the output settings are left as they are."""
        mode = termios.tcgetattr(self.fd)
        mode[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON)
        mode[2] |= termios.CS8
        mode[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
        mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
        termios.tcsetattr(self.fd, termios.TCSADRAIN, mode)
        self.raw = True
        if self.paste:
            self.write("\x1b[?2004h")

    def leave(self):
        if self.raw:
            self.raw = False
            try:
                if self.paste:
                    self.write("\x1b[?2004l")
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            except (OSError, termios.error):
                pass            # the terminal is gone; nothing is left to restore

    def close(self):
        self.leave()
        os.close(self.fd)


class App:
    """Runs `handle(event)` for each event and paints `draw(cols, rows)` after each batch.

    `handle` returns True to end the loop. Events are keys and pastes, `Resize`, `Tick`, `Stop`,
    and whatever another thread passes to `post`.
    """

    SIGNALS = (signal.SIGWINCH, signal.SIGTERM, signal.SIGHUP)

    def __init__(self, terminal, height, escape_delay=None, suspendable=True):
        self.terminal = terminal
        self.region = Region(terminal.write, terminal.size, height)
        self.decoder = Decoder()
        self.posted = queue.SimpleQueue()
        self.timers = {}
        self.suspendable = suspendable
        if escape_delay is None:        # a remote line may deliver a sequence in two parts
            escape_delay = 0.1 if os.environ.get("SSH_CONNECTION") else 0.03
        self.escape_delay = escape_delay
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)
        self.stopped_by = 0

    def post(self, event):
        """Hand an event to the loop from any thread."""
        self.posted.put(event)
        try:
            os.write(self.wake_w, b"\0")
        except OSError:
            pass                # the pipe is full: the loop is about to wake anyway

    def after(self, name, seconds):
        """Deliver Tick(name) once, `seconds` from now; a second call moves the deadline."""
        self.timers[name] = time.monotonic() + seconds

    def cancel(self, name):
        self.timers.pop(name, None)

    def _timeout(self):
        waits = [self.escape_delay] if self.decoder.waiting() else []
        if self.timers:
            waits.append(max(0.0, min(self.timers.values()) - time.monotonic()))
        return min(waits) if waits else None

    def _gather(self):
        events = []
        try:
            ready, _, _ = select.select([self.terminal.fd, self.wake_r], [], [], self._timeout())
        except InterruptedError:
            ready = []
        if self.terminal.fd in ready:
            try:
                data = os.read(self.terminal.fd, 4096)
            except OSError:
                data = b""
            events.extend(self.decoder.feed(data) if data else [Stop()])
        elif not ready and self.decoder.waiting():
            events.extend(self.decoder.flush())
        if self.wake_r in ready:
            try:
                marks = os.read(self.wake_r, 4096)
            except OSError:
                marks = b""
            for number in sorted(set(marks) - {0}):
                if number == signal.SIGWINCH:
                    events.append(Resize(*self.terminal.size()))
                else:
                    events.append(Stop(number))
        while True:
            try:
                events.append(self.posted.get_nowait())
            except queue.Empty:
                break
        now = time.monotonic()
        for name in [n for n, due in self.timers.items() if due <= now]:
            del self.timers[name]
            events.append(Tick(name))
        return events

    def suspend(self):
        """Ctrl-Z: give the terminal back, stop, and take the band again on return."""
        self.region.close()
        self.terminal.leave()
        signal.raise_signal(signal.SIGTSTP)
        self.terminal.enter()
        self.region.open()

    def run(self, handle, draw):
        previous = {number: signal.signal(number, lambda *_: None) for number in self.SIGNALS}
        old_wakeup = signal.set_wakeup_fd(self.wake_w, warn_on_full_buffer=False)
        self.terminal.enter()
        try:
            self.region.open()
            self.region.paint(draw(self.region.cols, self.region.height))
            done = False
            while not done:
                for event in self._gather():
                    if isinstance(event, Resize):
                        self.region.resized()
                    elif isinstance(event, Stop):
                        self.stopped_by = event.signal or signal.SIGHUP
                    elif self.suspendable and event == Key("ctrl-z"):
                        self.suspend()
                        continue
                    if handle(event) or isinstance(event, Stop):
                        done = True
                        break
                if not done:
                    self.region.paint(draw(self.region.cols, self.region.height))
        finally:
            try:
                self.region.close()
            except OSError:
                pass
            self.terminal.leave()
            signal.set_wakeup_fd(old_wakeup)
            for number, handler in previous.items():
                signal.signal(number, handler)
            os.close(self.wake_r)
            os.close(self.wake_w)
