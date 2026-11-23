# Copyright 2026 Eudaemonic Enterprises Inc.
"""Desktop integration. Pass URIs as separate arguments to the desktop programs."""
import os
import subprocess
from pathlib import Path


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


def applications(path):
    info = output("gio", "info", "-a", "standard::content-type", uri(path))
    mime = next((line.split(": ", 1)[1].strip() for line in info.splitlines()
                 if line.strip().startswith("standard::content-type: ")), None)
    if not mime:
        raise ValueError("The desktop could not determine the file type")
    listed = output("gio", "mime", mime)
    return list(dict.fromkeys(line.strip() for line in listed.splitlines()
                              if line.startswith((" ", "\t")) and line.strip().endswith(".desktop")))


def launch(path, app=None):
    target = uri(path)
    if app:
        if app not in applications(path):
            raise ValueError(f"Application is not registered for this file type: {app}")
        argv = ["gtk-launch", app, target]
    else:
        argv = ["gio", "open", target]
    subprocess.run(argv, check=True, timeout=20)


def reveal(path):
    instance = subprocess.Popen(["nautilus", "--select", uri(path)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        code = instance.wait(timeout=0.7)
    except subprocess.TimeoutExpired:
        return
    if code:
        raise ValueError(f"Nautilus exited with status {code}")


def open_uri(target):
    subprocess.run(["gio", "open", target], check=True, timeout=20)
