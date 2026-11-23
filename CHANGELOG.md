<!-- Copyright 2026 Eudaemonic Enterprises Inc. -->

# Changes

ShelfFind follows semantic versioning. Before version 1.0 a minor version may change a command; the
JSON keys and the exit codes that other programs rely on are kept from one version to the next and
are covered by tests.

## 0.2.2

The list of applications that F2 opens, and the `apps` command, now name each application as
people know it (Google Chrome, Okular, Evince), with the word of its identifier added when the
two have nothing in common, as in Document Viewer (Papers). The application that opens the
file by default comes first and is marked, and two entries of one application make one line.
The identifier is still what `open --with` takes, and `apps` prints it before the name.

## 0.2.1

The second half of the panel's comfort. Ctrl-O lists the passages of the chosen document in the
order of its pages; Enter opens the viewer at the chosen page, and Ctrl-O again shows the page
itself in the panel, where the arrows scroll it and PgUp and PgDn turn the pages. Ctrl-X lists
what can be done with a result. The nine best rows carry their rank, and Alt with that digit opens
one at once.

Filters can be typed among the words of a query, in the panel and on the command line alike:
`ext:djvu`, `in:Books` (a registered folder by its name, or any part of a path), `src:NAME`, and
`-word` to drop the files whose name or path contains the word. A quoted phrase is never a filter.

At a terminal, `status` prints one row per folder with a bar for the text at hand, `index` follows
the documents on one line rewritten in place, and `search --list` colours its results, lights the
words of the query and makes each address a link. A pipe receives the same plain forms as before.
The letters lit in a name written in CamelCase follow its words. The key binding at the prompt is
written for bash; zsh and fish follow.

## 0.2.0

The search no longer leaves the terminal. It opens as a band of lines under the prompt, on the
ordinary screen, and disappears on leaving; what the terminal showed before stays above it. Each
result carries its format, its name with the letters of the query lit, its folders and the page of
the passage, and the lines under the prompt show the passage in its context, the address and a
line of facts. Enter opens the document at the page of the passage where the default viewer
accepts one (Evince, Papers, Atril, Xreader, Okular, Zathura, Sioyek, DjView and the web
browsers), Tab leaves with its address, Ctrl-R switches between everything, names only and
passages only, and the query is edited as a line. When names would fill the list alone, a third of
it is kept for passages, which no key could reach before.

New commands: `pick` runs the panel and prints the chosen address for a script; `init bash` prints
a key binding that opens the panel at the prompt (Ctrl-T by default); `completion bash`; `config
show`, `config path` and `config check`; `doctor`, which checks the installation line by line and
changes nothing; `cheatsheet`; and `open --page N`. `reveal` now asks the file manager of the
desktop, whichever it is, before falling back to Nautilus.

Settings are read from `~/.config/shelffind/config.toml`, after the files it names in `include`
and before the `SHELFFIND_*` environment variables; each value remembers where it comes from, and
the file is never written by the program. They cover the height and the side of the panel, the
preview, the colour theme (by default the sixteen colours of the terminal; `NO_COLOR` is
honoured), the keys, the viewers per format and the outside sources, which may now be declared
with arguments. With `--data-dir DIRECTORY`, only `DIRECTORY/config.toml` is read.

For programs that call ShelfFind nothing is removed: the results of `search --json` gain the keys
`marks`, `size` and `mtime`, and the searches made and the documents opened are kept in the file
`history.jsonl` of the data folder. The index itself is unchanged. The earlier full-screen screen
remains available for this version with `SHELFFIND_UI=curses`.

## 0.1.1

A version of consolidation. Names and passages are treated as untrusted text: control characters
and marks that reverse the direction of writing are removed from everything a terminal shows
(`search --list`, `status`, progress and error messages) and written as `\uXXXX` escapes in JSON,
so that a parsing program reads the same data. A piped `search --list` still prints each file path
exactly.

`shelffind roots remove FOLDER` forgets a registered folder together with the names and the text
indexed from it; the documents themselves are not touched. In the search screen Esc now acts at
once, and Ctrl-C leaves quietly with exit code 130. `python -m shelffind` runs the program.

Tests now pin down what other programs use: `--data-dir`, `roots add`, `index`, the keys of
`search --json` and of `status --json`, the layout of `search --list`, and the exit codes 0, 1 and
130.

## 0.1.0

The first version: an inventory of registered folders, text read from PDF, DjVu, PostScript, EPUB
and plain-text documents, optional recognition of scanned pages, fuzzy search of names and
passages, optional outside sources, and opening through the desktop.
