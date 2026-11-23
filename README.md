<!-- Copyright 2026 Eudaemonic Enterprises Inc. -->

# ShelfFind

Find a document by its name, a few remembered letters, its folder, or a passage. Search works while the index is building. Press Enter to open the selected file, F2 to choose a registered application, or F3 to select it in Nautilus.

## Install and search

On Linux, use Python 3.11 or newer with SQLite FTS5 support. From the repository run `uv tool install .` or `pipx install .`. ShelfFind has no Python package dependencies. Register the folders you want on this computer.

```sh
shelffind roots add ~/Documents ~/Downloads
shelffind index
shelffind
```

The first pass lists every regular file. The second reads supported documents one at a time. To start with filenames, use `shelffind index --names-only`. Run `shelffind index` again after adding or moving files. Press Ctrl-C to interrupt a build. A later run continues with pending and changed files. A disconnected registered mount retains its last complete inventory. Folder symlinks and overlapping roots are excluded from traversal.

```sh
shelffind search --list orchadr
shelffind search --json --format pdf '"brass compass"'
shelffind search --root Documents --list garden
shelffind search --where text --list "brass compass"
shelffind status --errors
shelffind index --limit 100
shelffind index --retry
shelffind index --ocr --lang eng
```

Fuzzy matching is built in. It handles prefixes, abbreviations, omitted letters, and adjacent transpositions. Exact name and path matches appear first. Passage results include a snippet and the file page when available. Enclose a phrase in double quotes for an exact phrase. Use `--exact` for whole words without spelling expansion. In the picker, F4 changes folder, F5 changes format, F6 changes source, and Ctrl-P recalls a previous successful search. Piped output is plain text. `--json` adds machine-readable fields.

## Reading and opening files

| Format | Local programs used |
|---|---|
| PDF | `pdftotext` from Poppler |
| DjVu | `djvutxt` and `djvused` from DjVuLibre |
| PostScript and gzipped PostScript | Ghostscript and Poppler |
| EPUB | Python's standard library, following the EPUB reading order |
| UTF-8 plain text, Markdown, reStructuredText and TeX | Python's standard library |
| OCR for images inside PDF, DjVu and PostScript documents | Tesseract with the selected languages, plus `pdftoppm` or `ddjvu` |

Files with other extensions are searchable by name and folder. Pages without embedded alphanumeric text are marked for OCR. The optional OCR pass saves each completed page. Failed reads appear under `status --errors`. Enable `--retry` after installing a missing program or repairing a file. `index --file PATH` limits content extraction to one file and can be repeated.

```sh
shelffind open ~/Documents/atlas.pdf
shelffind apps ~/Documents/atlas.pdf
shelffind open --with org.gnome.Evince.desktop ~/Documents/atlas.pdf
shelffind reveal ~/Documents/atlas.pdf
```

Desktop actions use GIO, `gtk-launch` and Nautilus in the logged-in graphical session. `apps` lists the actual desktop IDs registered for that file's MIME type.

## Optional search sources

A local installation can add a catalogue through a trusted executable. With no providers configured, all searches use the selected folders. The private index, provider settings, and recalled queries are under `$XDG_DATA_HOME/shelffind`, normally `~/.local/share/shelffind`. Use `--data-dir DIRECTORY` before the command for a separate collection.

```sh
shelffind sources add catalogue /absolute/path/to/catalogue-provider
shelffind search --source catalogue --json orchard
shelffind open --source catalogue RECORD-ID
shelffind sources remove catalogue
```

A provider reads one JSON object from standard input and writes one JSON response to standard output. ShelfFind runs only executables the user registers. It sends the query to each selected provider. The folder picker shows local matches as soon as they are ready. Providers may implement their own storage, authentication, and search.

- A search request carries `version` (currently `1`), `op: "search"`, `query`, `limit`, `format` (or null), `exact`, `where` and `terms`. Each term has `text`, `literal` and alternative spellings in `alternatives`.
- Return an array of results with string `id` and `name`. Optional fields are `byline`, `format`, `availability`, `match`, `snippet`, `page` and `section`. A folder filter selects local files. Use `--source files` to search the local collection alone.
- An open, open-with or reveal action sends `{"version":1,"op":"resolve","id":"RECORD-ID"}`. Return `{"path":"/absolute/path/to/document"}` for an available file or `{"uri":"https://example.org/record"}` for a catalogue record. Retrieval can take place at selection time. A provider error is shown alongside any successful results.

## Tests

Run `PYTHONPATH=src python3 -m unittest discover -s tests -v`. Format integration tests use the available document tools and synthetic files. The OCR test checks recovery from an interruption between pages.

Copyright 2026 Eudaemonic Enterprises Inc. Released under the Apache License, Version 2.0.
