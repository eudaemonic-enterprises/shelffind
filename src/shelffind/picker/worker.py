# Copyright 2026 Eudaemonic Enterprises Inc.
"""Searches run in a thread of their own; a newer search cancels the one under way."""
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass

from .. import sources, store


PAUSE = 0.12            # seconds without a new letter before the outside sources are asked


@dataclass(frozen=True)
class Reply:
    version: int
    rows: list
    message: str
    seconds: float


class Searches:
    """`submit` returns at once; `notify(Reply)` is called from the thread for each answer, the
    local one first and one more after each outside source."""

    def __init__(self, directory, notify):
        self.directory = directory
        self.notify = notify
        self.requests = queue.Queue()
        self.version = 0
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def submit(self, query, options):
        self.version += 1
        self.requests.put((self.version, query, dict(options), time.monotonic()))
        return self.version

    def run(self):
        db = store.connect(self.directory)
        try:
            while (request := self.requests.get()) is not None:
                version, query, options, started = request
                if version != self.version:
                    continue

                def cancelled(ticket=version):
                    return ticket != self.version

                def publish(rows, message, ticket=version, since=started):
                    self.notify(Reply(ticket, rows, message, time.monotonic() - since))

                db.set_progress_handler(cancelled, 2000)
                try:
                    # In the panel a source is asked only once the typing has paused.
                    sources.find(db, self.directory, query, {**options, "pause": PAUSE}, publish, cancelled)
                except sources.Cancelled:
                    continue
                except (sqlite3.Error, ValueError) as error:
                    if not cancelled():
                        self.notify(Reply(version, [], str(error), time.monotonic() - started))
        finally:
            db.close()

    def close(self):
        self.version += 1
        self.requests.put(None)
        self.thread.join(timeout=1)
