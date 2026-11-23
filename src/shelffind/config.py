# Copyright 2026 Eudaemonic Enterprises Inc.
"""Settings, read in layers; each value remembers where it came from.

The layers, weakest first: the defaults, the files named by `include`, the settings file itself,
the environment variables `SHELFFIND_*`, and the flags of the command line. The settings file is
`$XDG_CONFIG_HOME/shelffind/config.toml`; when `--data-dir D` is given, only `D/config.toml` is
read, so that a separate collection never picks up personal settings. The file is never written
by the program.
"""
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# The keys of the panel. An action may have several keys, separated by spaces.
KEYS = {
    "open": "enter",
    "address": "tab",
    "quit": "esc ctrl-c ctrl-g",
    "up": "up ctrl-k",
    "down": "down ctrl-j",
    "page-up": "pgup",
    "page-down": "pgdn",
    "scope": "ctrl-r",
    "passages": "ctrl-o",
    "actions": "ctrl-x",
    "earlier-search": "ctrl-p",
    "later-search": "ctrl-n",
    "copy": "ctrl-y",
    "open-with": "f2",
    "reveal": "f3",
    "folder": "f4",
    "format": "f5",
    "source": "f6",
    "help": "f1",
}

# key: (kind, default, description). A kind is a type, a tuple of allowed words, or "size".
SETTINGS = {
    "panel.height": ("size", 40, 'Lines the panel may take: a number, or a share of the window such as "50%".'),
    "panel.prompt": (("bottom", "top"), "bottom", "Where the prompt sits; the best match is listed next to it."),
    "panel.preview": (int, 4, "Lines of preview under the prompt; 0 hides it."),
    "panel.help": (bool, True, "Show the line of key hints."),
    "panel.theme": (str, "terminal", "A built-in theme (terminal, plain) or the name of a [theme.NAME] table."),
    "panel.glyphs": (("auto", "unicode", "ascii"), "auto", "Signs used to draw the panel."),
    "panel.after_open": (("close", "stay"), "close", "What the panel does once a document is opened."),
    "search.limit": (int, 50, "Results listed in the panel."),
    "search.scope": (("all", "name", "text"), "all", "What the panel searches when it opens."),
    "history.record": (bool, True, "Remember the searches made and the documents opened."),
    **{f"keys.{action}": ("keys", default, f"Key or keys for the action {action}.")
       for action, default in KEYS.items()},
}
TABLES = {
    "open": "Viewer per format: \"default\", or a command with {path}, {page} and {phrase}.",
    "theme": "Colours by role, one table per theme.",
    "sources": "Outside sources: [sources.NAME] command = [\"program\", \"argument\"].",
    "roots": "Document folders: [roots.NAME] path = \"~/Books\", and remote = \"cloud:Books\" for its rclone twin.",
}
VARIABLE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")


class ConfigError(ValueError):
    pass


def expand(text, environ=None):
    """Replace `~`, `$NAME` and `${NAME}` in an address. An unknown variable is an error."""
    environ = os.environ if environ is None else environ

    def value(match):
        name = match.group(1) or match.group(2)
        if name not in environ:
            raise ConfigError(f"Unknown variable ${name} in {text}")
        return environ[name]

    text = VARIABLE.sub(value, str(text))
    if text == "~" or text.startswith("~/"):
        text = environ.get("HOME", str(Path.home())) + text[1:]
    return text


def portable(path, environ=None):
    """Write an address from the home folder when it lies there, so that it means the same on
    another machine."""
    environ = os.environ if environ is None else environ
    home = environ.get("HOME", str(Path.home())).rstrip("/")
    text = str(path)
    if home and (text == home or text.startswith(home + "/")):
        return "~" + text[len(home):]
    return text


def config_home(environ=None):
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CONFIG_HOME") or os.path.join(environ.get("HOME", str(Path.home())), ".config")
    return Path(base) / "shelffind"


def convert(key, value, kind, origin):
    """Check one value against its kind; return it in its final form."""
    problem = ConfigError(f"{origin}: {key} = {value!r} is not valid")
    if kind is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "yes", "no"):
            return value.lower() in ("true", "1", "yes")
        raise problem
    if kind is int:
        if isinstance(value, bool):
            raise problem
        if isinstance(value, int) or (isinstance(value, str) and value.lstrip("-").isdigit()):
            return int(value)
        raise problem
    if kind is str:
        if isinstance(value, str):
            return value
        raise problem
    if kind == "size":
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        if isinstance(value, str) and re.fullmatch(r"[1-9]\d*%?", value):
            return value if value.endswith("%") else int(value)
        raise problem
    if kind == "keys":
        words = value.split() if isinstance(value, str) else value
        if isinstance(words, list) and words and all(isinstance(w, str) and w for w in words):
            return " ".join(w.lower() for w in words)
        raise problem
    if isinstance(kind, tuple):
        if value in kind:
            return value
        raise ConfigError(f"{origin}: {key} = {value!r}; choose one of {', '.join(kind)}")
    raise problem


@dataclass
class Settings:
    values: dict = field(default_factory=dict)
    origins: dict = field(default_factory=dict)
    tables: dict = field(default_factory=lambda: {name: {} for name in TABLES})
    table_origins: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    file: Path | None = None

    def __getitem__(self, key):
        return self.values[key]

    def set(self, key, value, origin):
        kind = SETTINGS[key][0]
        self.values[key] = convert(key, value, kind, origin)
        self.origins[key] = origin

    def height(self, rows):
        """The height of the panel in a window of `rows` lines."""
        wanted = self.values["panel.height"]
        if isinstance(wanted, str):
            wanted = rows * int(wanted[:-1]) // 100
        return max(3, min(wanted, rows - 1))

    def keymap(self):
        """Key name -> action. A key given to two actions is reported once."""
        found = {}
        for action in KEYS:
            for key in self.values[f"keys.{action}"].split():
                if key in found and found[key] != action:
                    self.warnings.append(f"The key {key} is given to {found[key]} and to {action}")
                found[key] = action
        return found

    def apply(self, data, origin):
        """Lay one parsed file over what is already known."""
        for name, value in data.items():
            if name == "include":
                continue
            if name in TABLES:
                if not isinstance(value, dict):
                    raise ConfigError(f"{origin}: [{name}] must be a table")
                for inner, content in value.items():
                    words = isinstance(content, list) and content and all(isinstance(w, str) for w in content)
                    sound = {"theme": isinstance(content, dict) and all(isinstance(v, str) for v in content.values()),
                             "open": content == "default" or words,
                             "sources": isinstance(content, dict),
                             "roots": isinstance(content, dict) and isinstance(content.get("path"), str)
                             and content["path"] and all(isinstance(v, str) for v in content.values())
                             and set(content) <= {"path", "remote"}}[name]
                    if not sound:
                        raise ConfigError(f"{origin}: [{name}] {inner} = {content!r} is not valid")
                    self.tables[name][inner] = content
                    self.table_origins[f"{name}.{inner}"] = origin
            elif isinstance(value, dict):
                for inner, content in value.items():
                    key = f"{name}.{inner}"
                    if key in SETTINGS:
                        self.set(key, content, origin)
                    else:
                        self.warnings.append(f"{origin}: unknown setting {key}")
            else:
                self.warnings.append(f"{origin}: unknown setting {name}")


def read(path, settings, environ, seen):
    """Read one file after the files it includes, which therefore rank below it."""
    path = Path(path)
    if path in seen or len(seen) > 8:
        raise ConfigError(f"{path}: files include each other")
    try:
        with path.open("rb") as source:
            data = tomllib.load(source)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from None
    includes = data.get("include", [])
    if isinstance(includes, str):
        includes = [includes]
    if not isinstance(includes, list) or not all(isinstance(i, str) for i in includes):
        raise ConfigError(f"{path}: include must be a list of file names")
    for name in includes:
        target = Path(expand(name, environ))
        if not target.is_absolute():
            target = path.parent / target
        if target.is_file():
            read(target, settings, environ, seen | {path})
        else:
            settings.warnings.append(f"{path}: the included file {name} is absent")
    settings.apply(data, str(path))


def defaults():
    settings = Settings()
    for key, (_, default, _) in SETTINGS.items():
        settings.set(key, default, "default")
    return settings


def load(directory=None, explicit=False, environ=None, flags=None):
    """The settings in force. `directory` is the data folder; `explicit` tells that it was named
    on the command line, in which case only its own config.toml is read."""
    environ = os.environ if environ is None else environ
    settings = defaults()
    settings.file = (Path(directory) if explicit and directory else config_home(environ)) / "config.toml"
    if settings.file.is_file():
        read(settings.file, settings, environ, frozenset())
    for key in SETTINGS:
        name = "SHELFFIND_" + key.upper().replace(".", "_").replace("-", "_")
        if environ.get(name):
            settings.set(key, environ[name], f"environment {name}")
    for key, value in (flags or {}).items():
        if value is not None:
            settings.set(key, value, "command line")
    return settings


def report(settings):
    """Lines of `config show`: every setting, its value and where the value comes from."""
    rows = [(key, toml_value(settings.values[key]), settings.origins[key]) for key in SETTINGS]
    for name in TABLES:
        for inner, content in settings.tables[name].items():
            rows.append((f"{name}.{inner}", toml_value(content), settings.table_origins[f"{name}.{inner}"]))
    left = max(len(f"{key} = {value}") for key, value, _ in rows)
    return [f"{f'{key} = {value}':<{left}}   {origin}" for key, value, origin in rows]


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }"
    return str(value)
