<!-- Copyright 2026 Eudaemonic Enterprises Inc. -->

# Changes

ShelfFind follows semantic versioning. Before version 1.0 a minor version may change a command; the
JSON keys and the exit codes that other programs rely on are kept from one version to the next and
are covered by tests.

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
