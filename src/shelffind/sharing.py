# Copyright 2026 Eudaemonic Enterprises Inc.
"""What several machines share: the journal of the searches made and of the documents opened,
and the text that each one read (see `parcels`).

Each machine writes its own files in the shared place and never touches those of another, so
no two machines write the same file. A machine lists what it has put there in one small file,
with a checksum for each journal; another machine takes a journal only when its checksum
agrees. The shared place is a folder kept in step by any tool, or a remote reached with rclone.

    .shelffind-shared               {"format": 1}
    machines/LABEL.json             the name of the machine, the list of its journals and of its parcels
    history/LABEL/YYYY-MM.jsonl     its journal of one month, one event per line
    texts/LABEL/NNNNNN.jsonl.gz     a parcel of the text it read

The exchanges of one data folder take turns (`alone`), and a machine rewrites its card keeping
what it does not change.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from . import history, machine, store

FORMAT = 1
MONTH = re.compile(r"\d{4}-\d{2}\Z")
MONTHS = 3              # journals of this many months are taken from the other machines


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

    def get(self, name, limit=None):
        """The bytes of a file of the shared place, None when there is none; with `limit`, a
        file larger than that is refused before it is read."""
        try:
            with (self.path / name).open("rb") as held:
                if limit is not None and os.fstat(held.fileno()).st_size > limit:
                    raise ValueError(f"{name} is larger than it should be")
                return held.read()
        except FileNotFoundError:
            return None

    def put(self, name, data):
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".arriving.", delete=False) as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(out.name, target)


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
        lines = [line for line in err.splitlines() if line.strip()]
        return f"rclone: {(lines[-1] if lines else 'no answer')[:300]}"

    def names(self, folder):
        code, out, err = self.run("lsf", "--files-only", f"{self.remote}/{folder}", seconds=300)
        if code:
            if "not found" in err:
                return []
            raise ValueError(self.fault(err))
        return sorted(line for line in out.decode("utf-8", "replace").splitlines() if line)

    def get(self, name, limit=None):
        """The bytes of a file of the shared place, None when there is none; with `limit`, no
        more than one byte beyond it is read, so that a file larger than that is refused."""
        bound = ["--count", str(limit + 1)] if limit is not None else []
        code, out, err = self.run("cat", *bound, f"{self.remote}/{name}", seconds=600)
        if code:
            if "not found" in err:
                return None
            raise ValueError(self.fault(err))
        if limit is not None and len(out) > limit:
            raise ValueError(f"{name} is larger than it should be")
        return out

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
    try:
        listed = json.loads(place.get(f"machines/{me['id']}.json") or b"{}")
    except ValueError:
        pass
    listed = listed if isinstance(listed, dict) else {}
    given = listed.get("journals") if isinstance(listed.get("journals"), dict) else {}
    sent = 0
    for month, (lines, body) in sorted(outgoing(directory, me["id"]).items() if journals else ()):
        known = given.get(month) if isinstance(given.get(month), dict) else {}
        checksum = hashlib.sha256(body).hexdigest()
        # A month already given is given again only when it has grown: the journal of this
        # machine drops its oldest lines when it is full, and what was shared stays shared.
        if known.get("sha256") == checksum or known.get("lines", 0) > lines:
            continue
        place.put(f"history/{me['id']}/{month}.jsonl", body)
        given[month] = {"lines": lines, "sha256": checksum}
        sent += 1
    if sent or listed.get("name") != me["name"]:
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
        try:
            card = json.loads(place.get(f"machines/{entry}") or b"")
            theirs = card.get("journals") or {}
            others[label] = {"name": " ".join(str(card.get("name") or label).split())[:40],
                             "updated": int(card.get("updated") or 0)}
        except (ValueError, KeyError, TypeError, AttributeError):
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
