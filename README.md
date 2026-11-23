<!-- Copyright 2026 Eudaemonic Enterprises Inc. -->

# ShelfFind

Find a document by its name, a few remembered letters, its folder, or a passage, without leaving the terminal. ShelfFind keeps a private index of the folders you choose, searches names and full text as you type, and opens the chosen document at the page of the passage. Search works while the index is building.

![The search panel under the prompt](docs/panel.svg)

## Install and search

On Linux, use Python 3.11 or newer with SQLite FTS5 support. From the repository run `uv tool install .` or `pipx install .`. ShelfFind has no Python package dependencies. Register the folders you want on this computer.

```sh
shelffind roots add ~/Documents ~/Downloads
shelffind index
shelffind
```

The first pass lists every regular file. The second reads supported documents one at a time. To start with filenames, use `shelffind index --names-only`. Run `shelffind index` again after adding or moving files. `shelffind roots remove FOLDER` forgets a folder and what was indexed from it, and leaves the documents where they are. Press Ctrl-C to interrupt a build. A later run continues with pending and changed files. A disconnected registered mount retains its last complete inventory. Folder symlinks and overlapping roots are excluded from traversal.

## The search panel

`shelffind` opens a band of lines under the prompt, on the ordinary screen: what the terminal showed before stays above it, and the band disappears when you leave. Each result carries its format, its name with the letters of the query lit, its folders, and the page of the passage; under the prompt come the passage in its context, the address of the document and a line of facts.

| Key | Action |
|---|---|
| Enter | Open the document, at the page of the passage where the viewer accepts one |
| Tab | Leave with the address of the document: printed, or put on the command line by the key binding |
| Ctrl-R | Search everything, names only, or passages only |
| Ctrl-O | List the passages of the chosen document in the order of its pages; again, read the page in the panel |
| Ctrl-X | List what can be done with the chosen result |
| Up, Down, PgUp, PgDn | Move in the list; the best match is next to the prompt |
| Alt-1 to Alt-9 | Open one of the nine best results at once; each row shows its rank |
| Ctrl-P, Ctrl-N | Earlier and later searches |
| F2, F3, Ctrl-Y | Open with another application, show in the file manager, copy the address |
| F4, F5, F6 | One folder, one format, one source, or all |
| F1, Esc | The keys, leave |

The query is edited as a line: the arrows, Home and End move the cursor, Ctrl-W removes a word, Ctrl-U what precedes the cursor, and pasted text is taken as typed. Fuzzy matching is built in. It handles prefixes, abbreviations, omitted letters, and adjacent transpositions. Exact name and path matches appear first, and a third of the list is kept for passages when names would fill it alone. Enclose a phrase in double quotes for an exact phrase.

Filters can be typed among the words of a query: `ext:djvu` keeps one format, `in:Books` a registered folder by its name or any part of a path, `src:catalogue` one source, and `-draft` drops the files whose name or path contains the word. So `tide ext:pdf in:charts -draft` reads as it is written. While a page is read in the panel, the arrows scroll it, PgUp and PgDn turn the pages, and Enter opens the viewer at that page.

A key at the prompt opens the panel from bash. Add `eval "$(shelffind init bash)"` to `~/.bashrc`; Ctrl-T then opens the panel, Enter opens the chosen document and Tab puts its quoted address on the command line. `shelffind init bash --key alt-f` chooses another key, and `shelffind completion bash` prints completion of the commands. In a script, `shelffind pick` prints the address of the chosen document and exits with 1 when nothing was chosen.

## Lists and JSON

```sh
shelffind search --list orchadr
shelffind search --json --format pdf '"brass compass"'
shelffind search --root Documents --list garden
shelffind search --where text --list '"brass compass"'
shelffind status --errors
shelffind index --limit 100
shelffind index --retry
shelffind index --ocr --lang eng+fra
```

With `--list` or `--json`, or when the output is not a terminal, no panel opens. Passage results include a snippet and the file page when available. Use `--exact` for whole words without spelling expansion. The filters of the panel work here too; quote a query that contains `-word`, as in `shelffind search --list 'tide -draft'`. At a terminal `search --list` colours its results and makes each address a link, `status` prints one row per folder with a bar for the text at hand, and `index` follows the documents on a single line. Piped output is plain text. `--json` adds machine-readable fields. Names and passages are untrusted text: control characters are removed from what a terminal shows and escaped in JSON. `shelffind cheatsheet` prints a one-screen reference, and the end of `shelffind --help` lists the exit codes and the keys of the JSON for programs that call ShelfFind.

## Settings

Everything that may differ between two people is a setting. `shelffind config show` prints every setting with its value and where the value comes from, `shelffind config path` names the file, and `shelffind config check` reports its faults. The file is `~/.config/shelffind/config.toml`, read after the files it names in `include`, and never written by ShelfFind; environment variables such as `SHELFFIND_PANEL_HEIGHT` come next and win.

```toml
include = ["~/dotfiles/shelffind.toml"]     # optional: a file kept elsewhere, ranked below this one

[panel]
height  = 40            # lines at most, or a share of the window such as "50%"
prompt  = "bottom"      # or "top"
preview = 4             # lines under the prompt; 0 hides them
theme   = "terminal"    # the colours of your terminal; "plain"; or the name of a table below

[keys]
scope = "ctrl-r"        # one or several keys for each action; F1 in the panel lists them

[open]
pdf = ["zathura", "-P", "{page}", "{path}"]     # a viewer of your choice; {phrase} is also known

[theme.dusk]
match    = "bold #e0af68"
selected = "on #2f334d"
```

By default the panel uses the sixteen colours of the terminal, so it agrees with the scheme chosen there; `NO_COLOR` is honoured. `shelffind doctor` checks the installation line by line (the readers of each format, the viewers, the folders, the sources, the terminal) and says how to repair what is missing. It changes nothing.

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
shelffind open ~/Documents/atlas.pdf --page 12
shelffind apps ~/Documents/atlas.pdf
shelffind open --with org.gnome.Evince.desktop ~/Documents/atlas.pdf
shelffind reveal ~/Documents/atlas.pdf
```

Documents open through the desktop (GIO and `gtk-launch`). A page is passed to the viewers known to accept one: Evince, Papers, Atril, Xreader, Okular, Zathura, Sioyek, DjView and the web browsers; others open at the first page unless a command is given under `[open]`. `reveal` asks the file manager of the desktop, whichever it is. `apps` lists the actual desktop IDs registered for that file's MIME type.

## Optional search sources

A local installation can add a catalogue through a trusted executable. With no providers configured, all searches use the selected folders. The private index, provider settings, and the searches made are under `$XDG_DATA_HOME/shelffind`, normally `~/.local/share/shelffind`. Use `--data-dir DIRECTORY` before the command for a separate collection, which then reads only `DIRECTORY/config.toml`.

```sh
shelffind sources add catalogue /absolute/path/to/catalogue-provider
shelffind search --source catalogue --json orchard
shelffind open --source catalogue RECORD-ID
shelffind sources remove catalogue
```

A source may also be declared in the settings, with arguments: `[sources.catalogue]` and `command = ["catalogue-provider", "--fast"]`. A provider reads one JSON object from standard input and writes one JSON response to standard output. ShelfFind runs only executables the user registers. It sends the query to each selected provider. The panel shows local matches as soon as they are ready. Providers may implement their own storage, authentication, and search.

- A search request carries `version` (currently `1`), `op: "search"`, `query`, `limit`, `format` (or null), `exact`, `where` and `terms`. Each term has `text`, `literal` and alternative spellings in `alternatives`.
- Return an array of results with string `id` and `name`. Optional fields are `byline`, `format`, `availability`, `match`, `snippet`, `page` and `section`. A folder filter selects local files. Use `--source files` to search the local collection alone.
- An open, open-with or reveal action sends `{"version":1,"op":"resolve","id":"RECORD-ID"}`. Return `{"path":"/absolute/path/to/document"}` for an available file or `{"uri":"https://example.org/record"}` for a catalogue record. Retrieval can take place at selection time. A provider error is shown alongside any successful results.

## Tests

Run `PYTHONPATH=src python3 -m unittest discover -s tests -v`. Format integration tests use the available document tools and synthetic files. The OCR test checks recovery from an interruption between pages. The panel is run in a pseudo-terminal whose output is read by a strict small terminal that refuses any sequence outside an allowed list, and inside tmux with a real shell when tmux is installed. The earlier full-screen screen remains available for this version with `SHELFFIND_UI=curses`.

Copyright 2026 Eudaemonic Enterprises Inc. Released under the Apache License, Version 2.0.
