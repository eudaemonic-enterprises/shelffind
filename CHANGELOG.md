<!-- Copyright 2026 Eudaemonic Enterprises Inc. -->

# Changes

ShelfFind follows semantic versioning. Before version 1.0 a minor version may change a command; the
JSON keys and the exit codes that other programs rely on are kept from one version to the next and
are covered by tests.

## 0.5.0

A reading waits for the folder, not for the computer, and the search finds more.

Reading. A folder on a network answers each request after a wait of about a second, and a long
reading spent most of its time waiting. The files of the documents that come next are now
copied, four at a time by default (`index.workers`, from 1 to 16), while a document is read; each
copy is checked as before, against the checksum of the storage and against the file as the
inventory saw it, and the index that results is the one of a reading that copies one file at a
time, which the tests verify on the same shelves and on random histories. The copies held ahead
are at most a quarter of the room that is free and never more files than the limit of the run
allows; a copy that could not be placed is made again, alone, once the others are given up, so that
a disk with little room does not stop a reading that one file at a time finishes. The copy of a
file is named after it with every character that a shell would take for an order put down, so
that a file name cannot run a command through a converter written with a shell. The pages of a
scanned document are recognised several at once and kept in the order of the pages, so that an
interruption keeps all those before it, and the DjVu pages that must be asked for one by one are
read at once. Programs are limited by `prlimit` where it exists, which lets several run at
once, and Tesseract is held to one thread each. During a long reading, the next exchange with
the shared place begins two minutes after the last one ended, or ten times as long after as it
took when that is longer, so that reading keeps most of the time; before, an exchange of more
than two minutes with a distant place was followed by another at once. A reading whose output is
a log writes one line a minute, with the time. Over a slow line a reading is limited by the bytes
it must take, and the smallest documents bring the most text for them: the order of reading is a
setting, `index.order`, and a flag, `index --order`: `random` (an order of each data folder's own,
as before), `smallest` or `largest` by the size of the file, which lets two machines read from the
two ends of a shelf.

A folder on a network whose program has died answers with the error "Transport endpoint is not
connected". Python 3.13 raised it from the test of whether a folder exists, which stopped
`status` and would have stopped a reading at the first drop. Such a folder is now unavailable
like any other: `status` says so, and the reading goes on with the other folders and ends with
its work kept.

Search. A query whose words are shared between the name of a document (its name, folder, title
or author) and its text finds the document: `whitham dispersion` where one word is in the name
and the other only in a page. The words are shared in every way that leaves each side
something, the ways that give the name more words first (beyond five words, only the ways that
give it one word, or all but one, and a query of more than eight words is not shared, so that a
keystroke stays quick); such a document is listed after those
found by their names and before those found by their text alone, at the page that holds the
words asked of the text. Among matches of the same kind, documents (the formats that are read, converters included) come
before the programs, pictures and data that some folders hold, and a program whose name is the
query is no longer put before the documents that have it in their names.

Formats. HTML, Word (`docx`), OpenDocument (`odt`), FB2, Typst and BibTeX documents are read.
A document without pages is cut into passages of about six thousand characters made of whole
paragraphs, each named after its section, and an archive that unpacks to more than 64 MB, or XML of more than 32 MB, is
refused. What a word processor's file does not show (deleted words, field codes, the second copy
of a text box) is not indexed, a footnote is a passage of its own, and an HTML page keeps its
title and its body whether or not its head was closed. Any other format is read through a program named in the settings, `[convert.FORMAT]
command = ["program", "{path}"]`, which prints the text. Files already listed in a format that
is read since wait for their text from the next inventory on, and the documents of a format that is
no longer read, a converter removed from the settings, go back to their names.

Machines. The line of `status` on the machines also says when this data folder last exchanged
texts, apart from the journals, and a reading that begins again remembers how long the last
exchange took, so that it does not begin with another at once. The settings can name the machines by label, `[machines] LABEL = "office"`, so that
one shared file names them all although they call themselves by one hostname; the line of
`status` and the command `machine` show that name.

## 0.4.0

The text that one machine reads travels to the others. With `share.texts`, each data folder puts
the pages it read in the shared place of the journals, in parcels of at most 16 MB that unpack to
at most 256 MB, are never changed once written and never take a number twice, listed with their
checksums under a name of the data folder's own; a parcel is written under a passing name, then
given its own. Another machine takes a parcel only when its checksum and its number of documents
agree, so a parcel cut or changed on the way is taken another time. The pages are given only by
a run of the index, once its files are listed and its sources asked, to the document known by a
measure by the checksums of their bytes, when it waits for its text and no source holds its
text; readings of the same pages add up page by page, and nothing recognised is replaced. A
complete run takes what the others read before it compares the files, reads in an order of the
data folder's own, leaves unread a copied file whose bytes another machine read, which takes
those pages at the end of the pass once the sources were asked, and exchanges every two minutes
while it reads, so that several machines can share one long reading. Pages read by version 0.2
are not given to other machines. `shelffind sync` carries parcels both ways, with exit code 1
when the shared place fails. What a data folder knows of the parcels is kept in
`exchange.sqlite3`, bound to one shared place, checked at each exchange against the shared place,
and may be deleted while nothing runs. Each parcel put is read back once, then known by its size
and date in the shared place; one found changed there is removed and its documents given again.
A parcel of its own named and not there yet stays named for seven days, the greatest number seen
is kept, a list or a card that cannot be fetched stops the exchange instead of being written
anew, a list of a newer version is left to it, and every writing looks first at the mark of the
shared place. A document takes pages only when its files still have the size and the date that
the inventory saw, and the sources that answer for texts are looked at anew after each round of
questions. A machine whose journals are not shared writes no card in the shared place.

Only a reading that this version made from the bytes it measured, or took whole, is given:
pages that versions 0.2 and 0.3 read stay on their machine, whenever they were read. A file
changed in any way since a run began, or made a link, takes no pages in that run and is read
there; a copy no longer there holds nothing back. A parcel of its own found changed is read
twice before it is doubted, and one missing or changed stays named for seven days and fifty
exchanges while its documents are given again; nothing is deleted. Through rclone, only its
exit codes 3 and 4 mean that something is not there, and a list of files that cannot be read
stops the exchange. The comparison of readings rests on a mixing that tells apart readings that
differ only by which pages are recognised, and each document takes first the reading that
leaves the fewest pages to recognise. Parcels found whole are kept for the later rounds of a run.
A file must still be the very file that the inventory of the run listed, whatever was done to its
folder, a parcel that waits is read again from time to time and before it is named no more, and
a reading found too large or refused is weighed again in each run. A file refused at placement is
measured anew, whatever stands at its address later, and a parcel left to a newer version follows
its list when the list describes the bytes there.

When a file read by version 0.2 joins a document read since, the two readings are joined page by
page when they have the same pages, and otherwise the one that recognised more pages is kept: a
page recognised by the earlier version is no longer lost to a shorter reading. In an index
converted from version 0.2, the first run of this version marks the readings that version 0.3
tied to bytes, which nothing tells apart from a reading of its own: they stay on their machine
until they are read again.

A file that a pass of a run copied without reading it may be read by a later pass of the same
run, and is taken at most twice, so that a run always ends.

## 0.3.0

The folders and the sources in one list, with the text attached to documents.

The text that ShelfFind reads now belongs to a document, not to an address. A renamed or moved
file keeps its text, and a document kept in several places is read once and shown on one row
that names its copies. A document is known by the checksums of its whole content alone, never by
a likeness of name, size and date. Where a file belongs follows from what is measured of it
and from what the sources say of it, whatever the order in which this came to be known. A
measured file belongs to the document known by its checksums, which the storage of a folder
gives without any reading when the folder has an rclone twin and which one full reading gives
otherwise; a file that was never measured is placed by the word of a source that knows the work
by its checksum. A measure outranks a word: a statement that a measure refutes is dropped,
whichever file was measured, and `shelffind index` proves what the sources said of each file,
by the checksum of its storage or by one reading; a statement without checksum only names a work. What a source says is kept with the very file it was said of.

A document is read from a copy of its file made on this machine in one pass, during which the
checksums are computed: the pages kept are those of the very bytes the checksums describe, and
only a file measured to hold these bytes completes them. The copies are made in a folder that
each run makes for itself in the data folder of ShelfFind, which the index names and which is
removed after use; nothing is removed that the index does not name. A copy that contradicts the
checksum of the storage is made a second time. A failed reading is the fault of the document that the
file holds, and a file that cannot be copied leaves the document to its other files. An
inventory alone opens no file. A document whose last file has gone keeps its text for a month,
with what the sources said of its bytes as far as a measure had proved it; anything else that
was said of a file goes with it.

Sources speak a second, richer version of their exchange, described in `docs/sources.md`, with
a helper of one file for writing one and `shelffind sources check NAME` for trying one. A
source says what it can do, gives titles, authors, checksums, warnings, notes and actions, and
tells which files of the folders it knows. A document that the folders and a source both hold
then appears once, on the row of the file, under its true title, and is found by the words of
that title. When the source holds the text of a file, ShelfFind does not read the file itself.
The panel names such rows by their work, shows the page as it is printed, lists the actions of
the source under Ctrl-X and the passages the source finds under Ctrl-O, and opens the copy on
a disk of this machine in preference to one that comes through a network. A source of the
first version keeps working unchanged. `contrib/zotero.py` is a complete source for a local
Zotero library.

Search stays fast on a large index. Measured on a million pages, every search answers within
two tenths of a second: passages are ranked exactly while at most twenty thousand pages match
and through pages taken at regular intervals beyond, the excerpt is cut around the place where
most words of the query meet, and the matches by name are shown before the passages are
searched. In the text only the last word of a query is completed, from four letters. Spelling
suggestions give the usual word first. Documents opened often rise among matches of their kind.
In the panel, F7 turns the fuzzy matching off and on, and `search.exact` makes exact search what
the panel opens on. A key that the settings give to an action is no longer disputed by the
default key of another action.

Several machines can share the journal of searches and of opened documents, through a folder
kept in step or a remote of rclone (`[share]`, `shelffind sync`, `shelffind machine`); nothing is
shared unless asked for. The settings can name the folders (`[roots.NAME]`), with their rclone
twin, whose checksums recognise a moved file without reading it. The list of files is refreshed
beside the panel when it is old. `shelffind report copies`, `unlinked` and `names` print lists
for keeping the folders in order.

The index has a new form, kept in a new file. An index of version 0.2 is converted by copy the
first time the panel or `shelffind index` runs, and by `shelffind upgrade`: every file comes
over as a document of its own with the pages read from it. Those pages stay with their file
alone until the first `shelffind index` that follows reads the file in full once, which ties
them to its bytes; copies are joined from then on. The earlier index
is taken exclusively while it is copied, which is refused for as long as another program has
it open, marked as converted before the new one takes its name, and deleted only by
`shelffind upgrade --discard-old`, as long as it is exactly what the new index was made
from. `status` reports from an earlier index without converting it, and a script that meets a
large earlier index receives exit code 75. The JSON of `search` gains `copies`, `title`,
`byline`, `year` and, for a file a source knows, `record`, `flags`, `notes` and `fast`; the keys
of version 0.2 are kept. The full-screen screen of version 0.1, kept for one version, is removed.

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
