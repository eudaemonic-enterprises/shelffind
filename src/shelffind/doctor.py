# Copyright 2026 Eudaemonic Enterprises Inc.
"""`shelffind doctor`: the installation checked line by line. It changes nothing."""
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import __version__, config, desktop, sources, store
from .tui import style

READERS = [
    ("pdftotext", "PDF and PostScript text", "install Poppler (package poppler-utils)"),
    ("djvutxt", "DjVu text", "install DjVuLibre (package djvulibre-bin)"),
    ("djvused", "DjVu page count", "install DjVuLibre (package djvulibre-bin)"),
    ("gs", "PostScript", "install Ghostscript"),
]
OPTIONAL = [
    ("tesseract", "recognition of scanned pages", "install tesseract-ocr to use `index --ocr`"),
    ("pdftoppm", "page images for recognition", "install Poppler (package poppler-utils)"),
    ("ddjvu", "DjVu page images for recognition", "install DjVuLibre (package djvulibre-bin)"),
    ("gio", "opening documents", "install the GLib tools (package libglib2.0-bin)"),
    ("gtk-launch", "opening with a chosen application", "install the GTK tools (package libgtk-3-bin)"),
]


def first_line(*argv):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = (done.stdout or done.stderr).strip().splitlines()
    return lines[0].strip() if lines else ""


def checks(directory, explicit):
    """Yield (status, label, detail, remedy); status is ok, note, warn or fail."""
    yield "ok", "shelffind", f"{__version__}, Python {sys.version.split()[0]}", ""
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        yield "ok", "sqlite", f"{sqlite3.sqlite_version} with full-text search", ""
    except sqlite3.Error:
        yield "fail", "sqlite", f"{sqlite3.sqlite_version} without FTS5", \
            "use a Python whose SQLite includes FTS5"
    finally:
        probe.close()

    try:
        settings = config.load(directory, explicit)
        where = settings.file if settings.file.is_file() else f"{settings.file} (absent: defaults apply)"
        yield "ok", "settings", str(where), ""
        settings.keymap()
        for warning in settings.warnings:
            yield "warn", "settings", warning, "correct the settings file, then run `shelffind config check`"
    except ValueError as error:
        settings = None
        yield "fail", "settings", str(error), "correct the settings file"

    index = directory / store.INDEX
    try:
        earlier = store.earlier_format(directory)
    except ValueError as fault:
        earlier = None
        yield "fail", "earlier index", str(fault), "make the file readable; nothing is converted or replaced meanwhile"
    if earlier is None:
        pass
    elif not index.is_file() and earlier == 1:
        yield "warn", "index", f"{directory / store.EARLIER} has the earlier format", \
            "run `shelffind upgrade`; the earlier index is kept beside the new one"
    elif not index.is_file():
        yield "note", "index", f"none yet in {directory}", "run `shelffind roots add FOLDER` and `shelffind index`"
    else:
        try:
            db = sqlite3.connect(index.as_uri() + "?mode=ro", uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            try:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                files = db.execute("SELECT count(*) FROM files").fetchone()[0]
                documents = db.execute("SELECT count(DISTINCT doc) FROM files").fetchone()[0]
                waiting = db.execute("SELECT count(DISTINCT f.doc) FROM files f JOIN docs d ON d.id=f.doc "
                                     "WHERE d.state='pending'").fetchone()[0]
                yield "ok", "index", (f"{index} (format {version}): {files:,} files, {documents:,} documents, "
                                      f"{waiting:,} waiting to be read"), ""
                for row in db.execute("SELECT * FROM roots ORDER BY id").fetchall():
                    root = store.root_row(row)
                    if store.available(root):
                        yield "ok", "folder", f"{root['name']}: {root['path']}", ""
                    else:
                        yield "warn", "folder", f"{root['name']}: {root['path']} is not reachable now", \
                            "mount it; its last inventory stays searchable"
            finally:
                db.close()
            if earlier == 1:
                yield "warn", "earlier index", f"{directory / store.EARLIER} was not converted into the index in use", \
                    "`shelffind upgrade` completes a conversion that was cut short; otherwise move that file away"
            elif earlier:
                yield "note", "earlier index", f"{directory / store.EARLIER} is kept", \
                    "`shelffind upgrade --discard-old` deletes it once the new index has served you well"
        except sqlite3.Error as error:
            yield "fail", "index", f"{index}: {error}", "run `shelffind status` for details"

    for program, purpose, remedy in READERS:
        found = shutil.which(program)
        yield ("ok" if found else "warn"), program, (found or f"missing: no {purpose}"), ("" if found else remedy)
    for program, purpose, remedy in OPTIONAL:
        found = shutil.which(program)
        yield ("ok" if found else "note"), program, (found or f"absent: no {purpose}"), ("" if found else remedy)
    if shutil.which("tesseract"):
        try:
            listed = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True,
                                    timeout=10, check=False).stdout.split("\n")[1:]
            yield "ok", "languages", " ".join(sorted(x for x in listed if x and x != "osd")), ""
        except (OSError, subprocess.SubprocessError):
            pass

    if shutil.which("gio"):
        for mime, name in (("application/pdf", "pdf viewer"), ("image/vnd.djvu", "djvu viewer")):
            try:
                listed = desktop.output("gio", "mime", mime).splitlines()
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
            handler = listed[0].rsplit(":", 1)[-1].strip() if listed and ".desktop" in listed[0] else ""
            if not handler:
                yield "note", name, "no default application", "choose one in the desktop settings"
                continue
            lowered = handler.lower()
            known = any(n in lowered for n in desktop.BROWSERS) or \
                any(n in lowered for names, _ in desktop.PAGE_OPTIONS for n in names)
            yield ("ok" if known else "note"), name, \
                f"{handler}: {'opens at the page of a passage' if known else 'opens at the first page'}", \
                ("" if known else "name a viewer under [open] in the settings to open at the page")
    clip = next((tool for tool in ("wl-copy", "xclip") if shutil.which(tool)), None)
    yield ("ok" if clip else "note"), "clipboard", (clip or "through the terminal only"), ""

    try:
        for name, command in sources.registered(directory).items():
            program = command[0]
            found = shutil.which(program) or (program if os.access(program, os.X_OK) else None)
            yield ("ok" if found else "fail"), f"source {name}", \
                (" ".join(command) if found else f"{program} cannot be run"), \
                ("" if found else f"correct it or run `shelffind sources remove {name}`")
    except (OSError, ValueError) as error:
        yield "fail", "sources", str(error), "correct sources.json in the data folder"

    colours = style.depth(os.environ, sys.stdout.isatty())
    words = {0: "no colour", 16: "16 colours", 256: "256 colours", 1 << 24: "full colour"}[colours]
    encoding = (sys.stdout.encoding or "").lower()
    yield ("ok" if "utf" in encoding else "note"), "terminal", \
        f"{os.environ.get('TERM', 'unknown')}, {words}, {encoding or 'unknown encoding'}", \
        ("" if "utf" in encoding else "a UTF-8 locale shows accented names correctly")
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        os.close(fd)
        yield "ok", "panel", "a terminal is at hand for the search panel", ""
    except OSError:
        yield "note", "panel", "no terminal here: use `search --list` or `--json`", ""


def run(directory, explicit, as_json=False):
    rows = [dict(zip(("status", "label", "detail", "remedy"), row)) for row in checks(directory, explicit)]
    failures = sum(row["status"] == "fail" for row in rows)
    if as_json:
        from .text import json_text
        print(json_text(rows))
        return int(bool(failures))
    from .text import safe
    for row in rows:
        mark = {"ok": "[ ok ]", "note": "[note]", "warn": "[warn]", "fail": "[fail]"}[row["status"]]
        print(f"{mark} {row['label']:<14} {safe(row['detail'])}")
        if row["remedy"]:
            print(f"       {'':<14} -> {row['remedy']}")
    if failures:
        print(f"{failures} problem(s) found. Correct the [fail] lines above, then run doctor again.")
    else:
        print("Everything needed to search is in place.")
    return int(bool(failures))
