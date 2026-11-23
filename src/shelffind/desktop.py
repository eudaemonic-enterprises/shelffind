# Copyright 2026 Eudaemonic Enterprises Inc.
"""Desktop integration. Pass URIs as separate arguments to the desktop programs."""
import base64
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

# How the known viewers are told to open at a page, by a part of their desktop name.
PAGE_OPTIONS = (
    (("evince", "papers", "atril", "xreader"), ["--page-index={page}"]),
    (("okular",), ["-p", "{page}"]),
    (("zathura",), ["-P", "{page}"]),
    (("sioyek",), ["--page", "{page}"]),
    (("djview",), ["-page={page}"]),
)
FIND_OPTIONS = {"evince": ["--find={phrase}"], "okular": ["--find", "{phrase}"],
                "zathura": ["-f", "{phrase}"]}
BROWSERS = ("chrome", "chromium", "firefox", "brave", "vivaldi", "epiphany", "librewolf", "opera", "edge")


def uri(path):
    path = Path(path).expanduser().absolute()
    if not path.is_file():
        raise ValueError(f"File unavailable: {path}")
    return path.as_uri()


def output(*argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"{argv[0]} exited with {result.returncode}")
    return result.stdout


def content_type(path):
    info = output("gio", "info", "-a", "standard::content-type", uri(path))
    mime = next((line.split(": ", 1)[1].strip() for line in info.splitlines()
                 if line.strip().startswith("standard::content-type: ")), None)
    if not mime:
        raise ValueError("The desktop could not determine the file type")
    return mime


def applications(path):
    listed = output("gio", "mime", content_type(path))
    return list(dict.fromkeys(line.strip() for line in listed.splitlines()
                              if line.startswith((" ", "\t")) and line.strip().endswith(".desktop")))


def default_handler(path):
    """The desktop name of the program that opens this kind of file by default, or None."""
    listed = output("gio", "mime", content_type(path)).splitlines()
    found = re.search(r":\s*(\S+\.desktop)\s*$", listed[0]) if listed else None
    return found.group(1) if found else None


def desktop_exec(name):
    """The command of a desktop entry, without the places where the desktop puts the file."""
    home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    shared = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    for base in [home, *shared.split(":")]:
        entry = Path(base) / "applications" / name
        try:
            lines = entry.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if line.startswith("Exec="):
                try:
                    words = shlex.split(line[5:])
                except ValueError:
                    return None
                return [word for word in words if not re.fullmatch(r"%[a-zA-Z]", word)] or None
    return None


def viewer_argv(path, page=None, phrase=None, handler=None, custom=None):
    """The command that opens `path` at `page`, with `phrase` handed to viewers that can look
    for it. Returns None when the ordinary opener of the desktop should be used instead.

    `custom` is a command from the settings, with {path}, {uri}, {page} and {phrase}; an
    argument whose value is unknown is left out, together with the bare option before it.
    """
    values = {"path": str(path), "uri": uri(path), "page": page, "phrase": phrase}
    if custom:
        argv = []
        for word in custom:
            needed = re.findall(r"\{(\w+)\}", word)
            if any(values.get(name) is None for name in needed):
                if argv and argv[-1].startswith("-") and "=" not in argv[-1] and len(argv) > 1:
                    argv.pop()
                continue
            argv.append(re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]), word))
        return argv or None
    if not page or not handler:
        return None
    lowered = handler.lower()
    if any(name in lowered for name in BROWSERS):
        return ["gtk-launch", handler, f"{values['uri']}#page={page}"]
    for names, option in PAGE_OPTIONS:
        family = next((name for name in names if name in lowered), None)
        if family:
            command = desktop_exec(handler)
            if not command:
                return None
            argv = command + [word.format(page=page) for word in option]
            if phrase and family in FIND_OPTIONS:
                argv += [word.format(phrase=phrase) for word in FIND_OPTIONS[family]]
            return argv + [str(path)]
    return None


def _run_launcher(argv):
    # A viewer can keep the launcher's streams open after the launcher exits.
    # A regular file lets us report startup errors without waiting for pipe EOF.
    with tempfile.TemporaryFile() as errors:
        result = subprocess.run(argv, check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=errors, start_new_session=True, timeout=20)
        if result.returncode:
            detail = os.pread(errors.fileno(), 2048, 0).decode(errors="replace").strip()
            message = f"{argv[0]} exited with status {result.returncode}"
            raise ValueError(f"{message}: {detail}" if detail else message)


def _start_viewer(argv):
    """Start a viewer that stays open. A failure within the first moments is reported."""
    with tempfile.TemporaryFile() as errors:
        try:
            viewer = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=errors, start_new_session=True)
        except FileNotFoundError:
            raise ValueError(f"Program not found: {argv[0]}") from None
        try:
            code = viewer.wait(timeout=0.6)
        except subprocess.TimeoutExpired:
            # The viewer stays open: it is collected when it ends, however late that is.
            threading.Thread(target=viewer.wait, daemon=True).start()
            return
        if code:
            detail = os.pread(errors.fileno(), 2048, 0).decode(errors="replace").strip()
            message = f"{argv[0]} exited with status {code}"
            raise ValueError(f"{message}: {detail}" if detail else message)


def launch(path, app=None, page=None, phrase=None, custom=None):
    """Open a document: with a chosen application, with a command from the settings, at a page
    when the default viewer is known to accept one, else through the desktop's own opener."""
    target = uri(path)
    if app:
        if app not in applications(path):
            raise ValueError(f"Application is not registered for this file type: {app}")
        argv = viewer_argv(path, page, phrase, handler=app) if page else None
    elif custom:
        argv = viewer_argv(path, page, phrase, custom=custom)
    elif page:
        try:
            argv = viewer_argv(path, page, phrase, handler=default_handler(path))
        except (OSError, ValueError, subprocess.SubprocessError):
            argv = None
    else:
        argv = None
    if argv and argv[0] != "gtk-launch":
        _start_viewer(argv)
    else:
        _run_launcher(argv or (["gtk-launch", app, target] if app else ["gio", "open", target]))


def reveal(path):
    """Show the file selected in the file manager, whichever it is."""
    target = uri(path)
    if shutil.which("gdbus"):
        shown = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
             "--object-path", "/org/freedesktop/FileManager1",
             "--method", "org.freedesktop.FileManager1.ShowItems", f"['{target}']", ""],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False)
        if shown.returncode == 0:
            return
    instance = subprocess.Popen(["nautilus", "--select", target], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        code = instance.wait(timeout=0.7)
    except subprocess.TimeoutExpired:
        return
    if code:
        raise ValueError(f"Nautilus exited with status {code}")


def open_uri(target):
    _run_launcher(["gio", "open", target])


def copy(text, write=None):
    """Put text on the clipboard: through the terminal (which also works over SSH) and through
    the desktop's own tool when there is one. Returns the means that were used."""
    used = []
    if write:
        write("\x1b]52;c;" + base64.b64encode(text.encode()).decode("ascii") + "\x07")
        used.append("terminal")
    for argv in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(argv[0]):
            try:
                done = subprocess.run(argv, input=text.encode(), stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, timeout=5, check=False)
            except (OSError, subprocess.SubprocessError):
                continue
            if done.returncode == 0:
                used.append(argv[0])
                break
    return used
