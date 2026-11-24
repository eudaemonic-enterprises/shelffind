# Copyright 2026 Eudaemonic Enterprises Inc.
"""What several machines share: the journal of the searches made and of the documents opened,
and the text that each one read (see `parcels`).

Each machine writes its own files in the shared place and never touches those of another, so
no two machines write the same file. A machine lists what it has put there in one small file,
with a checksum for each journal; another machine takes a journal only when its checksum
agrees. The shared place is a folder kept in step by any tool, or a remote reached with rclone.

    .shelffind-shared               {"format": 1}
    machines/LABEL.json             the name of the machine and the list of its journals
    history/LABEL/YYYY-MM.jsonl     its journal of one month, one event per line
    texts/WRITER.json               the list of the parcels of one data folder (see `parcels`)
    texts/WRITER/NNNNNN.jsonl.gz    a parcel of the text that data folder read

The exchanges of one data folder take turns (`alone`), and a machine rewrites its card keeping
what it does not change. A machine whose journals are not shared writes no card. A shared
place that fails while a card or a journal is read stops the exchange; a card that cannot be
read once fetched is counted as damaged, or written anew when it is this machine's.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import history, machine, store

FORMAT = 1
MONTH = re.compile(r"\d{4}-\d{2}\Z")
MONTHS = 3              # journals of this many months are taken from the other machines


ABSENT = (3, 4)         # the exit codes by which rclone says that a folder or a file is not there; any other is a failure


class Oversized(ValueError):
    """A file of the shared place larger than it should be: refused before it is read."""


class Folder:
    """A shared place that is a folder of this machine."""

    def __init__(self, path):
        self.path = Path(path)
        self.identity = f"folder:{self.path.expanduser().absolute()}"

    def names(self, folder):
        try:
            return sorted(entry.name for entry in (self.path / folder).iterdir() if entry.is_file())
        except FileNotFoundError:
            return []

    def stamps(self, folder):
        """The files of a folder of the shared place, each with what changes when it is
        written: its size and the time of its last change."""
        found = {}
        for name in self.names(folder):
            try:
                info = (self.path / folder / name).stat()
            except FileNotFoundError:
                continue
            found[name] = f"{info.st_size}:{info.st_mtime_ns}"
        return found

    def get(self, name, limit=None):
        """The bytes of a file of the shared place, None when there is none; with `limit`, a
        file larger than that is refused before it is read."""
        try:
            with (self.path / name).open("rb") as held:
                if limit is not None and os.fstat(held.fileno()).st_size > limit:
                    raise Oversized(f"{name} is larger than it should be")
                return held.read()
        except FileNotFoundError:
            return None

    def delete(self, name):
        (self.path / name).unlink(missing_ok=True)

    def put(self, name, data):
        """Write a file of the shared place under a passing name, NAME.arriving.PID, then move it
        to its own, so that no one meets it half written; what a writing cut short leaves under
        that name is its writer's alone to remove."""
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        passing = target.with_name(f"{target.name}.arriving.{os.getpid()}")
        try:
            with passing.open("wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(passing, target)
        except BaseException:
            passing.unlink(missing_ok=True)
            raise


class Rclone:
    """A shared place reached with rclone, such as `cloud:ShelfFind`."""

    def __init__(self, remote, program="rclone"):
        self.remote, self.program = remote.rstrip("/"), program
        self.identity = f"rclone:{self.remote}"

    def run(self, *words, data=None, seconds=900):
        try:
            done = subprocess.run([self.program, *words], input=data, capture_output=True, timeout=seconds)
        except (OSError, subprocess.TimeoutExpired) as fault:
            raise ValueError(f"rclone could not be run: {fault}") from None
        return done.returncode, done.stdout, done.stderr.decode("utf-8", "replace").strip()

    @staticmethod
    def fault(err):
        """What rclone says went wrong: its last line, which follows its notices."""
        lines = [line for line in err.splitlines() if line.strip() and " NOTICE: " not in line]
        return f"rclone: {(lines[-1] if lines else 'it stopped without saying why')[:300]}"

    def names(self, folder):
        code, out, err = self.run("lsf", "--files-only", f"{self.remote}/{folder}", seconds=300)
        if code in ABSENT:
            return []
        if code:
            raise ValueError(self.fault(err))
        return sorted(line for line in out.decode("utf-8", "replace").splitlines() if line)

    def stamps(self, folder):
        """The files of a folder of the shared place, each with what changes when it is
        written: its size and the time of its last change, as the remote tells them."""
        code, out, err = self.run("lsjson", "--files-only", f"{self.remote}/{folder}", seconds=300)
        if code in ABSENT:
            return {}
        if code:
            raise ValueError(self.fault(err))
        try:
            listed = json.loads(out)
        except (ValueError, RecursionError):
            listed = None
        # A list that is not one, or names a file without its size or its date, says nothing of what is
        # there: it stops the exchange, so that nothing is taken for withdrawn.
        if not isinstance(listed, list) or not all(isinstance(item, dict) and isinstance(item.get("Name"), str)
                                                   and type(item.get("Size")) is int and isinstance(item.get("ModTime"), str)
                                                   for item in listed):
            raise ValueError("rclone gave a list of files that cannot be read")
        return {item["Name"]: f"{item['Size']}:{item['ModTime']}" for item in listed if not item.get("IsDir")}

    def get(self, name, limit=None):
        """The bytes of a file of the shared place, None when there is none; with `limit`, no
        more than one byte beyond it is read, so that a file larger than that is refused."""
        bound = ["--count", str(limit + 1)] if limit is not None else []
        code, out, err = self.run("cat", *bound, f"{self.remote}/{name}", seconds=600)
        if code in ABSENT:
            return None
        if code:
            raise ValueError(self.fault(err))
        if limit is not None and len(out) > limit:
            raise Oversized(f"{name} is larger than it should be")
        return out

    def delete(self, name):
        code, _, err = self.run("deletefile", f"{self.remote}/{name}", seconds=120)
        if code and code not in ABSENT:
            raise ValueError(self.fault(err))

    def put(self, name, data):
        """Write a file of the shared place under a passing name, then move it to its own:
        rclone writes a file in place, and a writing cut short would leave the file empty, or
        remove the one it replaces. A reader never meets a file half written, and a writing
        that fails leaves the former file as it was."""
        passing = f"{name}.arriving.{os.getpid()}"
        code, _, err = self.run("rcat", f"{self.remote}/{passing}", data=data)
        if code:
            self.run("deletefile", f"{self.remote}/{passing}", seconds=120)
            raise ValueError(self.fault(err))
        code, _, err = self.run("moveto", f"{self.remote}/{passing}", f"{self.remote}/{name}")
        if code:
            raise ValueError(self.fault(err))


@contextlib.contextmanager
def alone(directory, wait=0.0):
    """Hold the exchange of the data folder `directory`: yields True, or False when another
    process of this folder is exchanging and goes on doing so for `wait` seconds."""
    deadline = time.monotonic() + wait
    Path(directory).mkdir(mode=0o700, parents=True, exist_ok=True)
    with (Path(directory) / "sync.lock").open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.2)
        yield True


def check_format(place):
    """Make sure the shared place is one that this version may write in: it bears the mark of
    its format, which is written when there is none, and which a newer ShelfFind may have
    raised, in which case ValueError says so and nothing is written."""
    told = place.get(".shelffind-shared", 4096)
    if told is None:
        place.put(".shelffind-shared", json.dumps({"format": FORMAT}).encode() + b"\n")
        return
    try:
        made = json.loads(told).get("format", 0)
        newer = type(made) is not int or made > FORMAT
    except (ValueError, AttributeError, RecursionError):
        newer = True
    if newer:
        raise ValueError("The shared place was written by a newer ShelfFind: update this one first")


def put_card(place, label, card):
    """Write the card of the machine `label` in the shared place."""
    place.put(f"machines/{label}.json", json.dumps(card, indent=2).encode() + b"\n")


def carrier(settings):
    """The shared place the settings name, or None when nothing is shared."""
    if not (settings["share.history"] or settings["share.texts"]):
        return None
    if settings["share.folder"]:
        return Folder(os.path.expanduser(settings["share.folder"]))
    if settings["share.remote"]:
        return Rclone(settings["share.remote"])
    return None


def month_of(seconds):
    return time.strftime("%Y-%m", time.gmtime(seconds))


def recent(now=None):
    """The names of the last months, the present one first."""
    year, month = time.gmtime(now)[:2]
    found = []
    for _ in range(MONTHS):
        found.append(f"{year:04d}-{month:02d}")
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return found


def outgoing(directory, label):
    """The journal of this machine month by month, as {month: (number of lines, bytes)}, with
    addresses written from the home folder so that they mean the same on another machine."""
    months = {}
    for line in history.lines_of(directory):
        try:
            event = json.loads(line)
            when = int(event["t"])
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(event.get("path"), str):
            event["path"] = store.from_home(event["path"])
        event["m"] = label
        months.setdefault(month_of(when), []).append(json.dumps(event, ensure_ascii=False) + "\n")
    return {month: (len(lines), "".join(lines).encode("utf-8")) for month, lines in months.items()}


def sync(directory, place, name=None, now=None, journals=True):
    """Put the journals of this machine in the shared place and take those of the others, or,
    without `journals`, only note which machines share. Returns what happened: {"sent",
    "received", "damaged", "machines"}. The caller holds the exchange (`alone`)."""
    me = machine.identity(directory, name)
    check_format(place)
    # What this machine gives.
    listed = {}
    if journals:
        told = place.get(f"machines/{me['id']}.json")      # a shared place that fails stops the exchange
        try:
            listed = json.loads(told or b"{}")
        except (ValueError, RecursionError):
            pass                                # spoilt: written anew
    listed = listed if isinstance(listed, dict) else {}
    given = listed.get("journals") if isinstance(listed.get("journals"), dict) else {}
    sent = 0
    for month, (lines, body) in sorted(outgoing(directory, me["id"]).items() if journals else ()):
        known = given.get(month) if isinstance(given.get(month), dict) else {}
        checksum = hashlib.sha256(body).hexdigest()
        # A month already given is given again only when it has grown: the journal of this
        # machine drops its oldest lines when it is full, and what was shared stays shared.
        if known.get("sha256") == checksum or (known.get("lines") if type(known.get("lines")) is int else 0) > lines:
            continue
        check_format(place)                     # a newer ShelfFind may have marked the shared place meanwhile
        place.put(f"history/{me['id']}/{month}.jsonl", body)
        given[month] = {"lines": lines, "sha256": checksum}
        sent += 1
    if journals and (sent or listed.get("name") != me["name"]):
        check_format(place)
        put_card(place, me["id"], {**listed, "id": me["id"], "name": me["name"],
                                   "updated": int(time.time() if now is None else now), "journals": given})
    # What the others give.
    received = damaged = 0
    others = {}
    peers = directory / "peers"
    for entry in place.names("machines"):
        label = entry.removesuffix(".json")
        if not machine.LABEL.match(label) or label == me["id"]:
            continue
        told = place.get(f"machines/{entry}")
        try:
            card = json.loads(told or b"")
            theirs = card.get("journals") or {}
            others[label] = {"name": " ".join(str(card.get("name") or label).split())[:40],
                             "updated": int(card.get("updated") or 0)}
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            damaged += 1
            continue
        for month in recent(now) if journals else ():
            wanted = theirs.get(month) if isinstance(theirs, dict) else None
            if not isinstance(wanted, dict) or not isinstance(wanted.get("sha256"), str):
                continue
            kept = peers / label / f"{month}.jsonl"
            try:
                if hashlib.sha256(kept.read_bytes()).hexdigest() == wanted["sha256"]:
                    continue
            except OSError:
                pass
            body = place.get(f"history/{label}/{month}.jsonl")
            if body is None or hashlib.sha256(body).hexdigest() != wanted["sha256"]:
                damaged += 1                    # cut or changed on the way: taken another time
                continue
            Folder(peers).put(f"{label}/{month}.jsonl", body)
            received += 1
    Folder(directory).put("share.json", json.dumps(
        {"synced": int(time.time() if now is None else now), "machines": others}, indent=2).encode() + b"\n")
    return {"sent": sent, "received": received, "damaged": damaged,
            "machines": sorted(other["name"] for other in others.values())}


def last(directory):
    """What the last exchange left: {"synced": time, "machines": {label: {"name", "updated"}}}."""
    try:
        told = json.loads((directory / "share.json").read_text())
        return told if isinstance(told, dict) else {}
    except (OSError, ValueError):
        return {}
