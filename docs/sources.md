<!-- Copyright 2026 Eudaemonic Enterprises Inc. -->

# Sources

A source is a program that answers for a collection ShelfFind does not index itself: a reference manager, a library of full texts, a catalogue. ShelfFind asks it in a small exchange of JSON, shows its answers in the same list as the files of the folders, and recognises a document that both hold, so that it appears once. This page is the reference of that exchange. It is stable: a later ShelfFind keeps understanding a source written for this page.

A source is registered by the user and by nobody else, either with `shelffind sources add NAME /path/to/program` or in the settings:

```toml
[sources.catalogue]
command = ["catalogue-provider", "--fast"]
```

`shelffind sources check NAME` asks a source every question of this page and reports what it answers and where an answer departs from it.

## The exchange

ShelfFind starts the program, writes one JSON object to its standard input, closes it, and reads one JSON value from its standard output. Each question is a new process. Text is UTF-8. An answer is at most 8 MiB. The program ends with status 0 when it answers; any other status is a failure, and the first lines of its standard error are shown to the user.

A question that is no longer needed (the user typed another letter) is abandoned by killing the program with its whole process group, so a source keeps no state that a kill would damage. The time allowed is 5 seconds for `hello`, 45 for `search`, 60 for `pages`, 120 for `identify`, and 180 for `resolve` and `act`.

Every question carries `version` and `op`. Version 2 answers are objects with `"ok": true`, or a refusal:

```json
{"ok": false, "error": {"code": "unavailable", "message": "The library index is being rebuilt", "retry": true}}
```

The codes are `unavailable` (not now), `invalid` (the question is not understood), `not_found` (no such record) and `failed` (anything else). The message is shown to the user.

## Version 1

A source of version 1 knows two questions and answers them without `ok`. It keeps working unchanged, and ShelfFind sends it nothing else. A source written for version 2 with the helper below answers version 1 as well: `name` then carries the title of the work, which is what a caller of that version shows.

```json
{"version": 1, "op": "search", "query": "orchid", "terms": [{"text": "orchid", "literal": false, "alternatives": ["orchis"]}],
 "exact": false, "limit": 25, "format": null, "where": "all"}
```

The answer is an array of results, each with a string `id` and a string `name`, and optionally `byline`, `format`, `match`, `page`, `section`, `snippet` and `availability`.

```json
{"version": 1, "op": "resolve", "id": "RECORD-ID"}
```

The answer is `{"path": "/absolute/path/to/document"}` for a file that can be opened, or `{"uri": "https://example.org/record"}` for a record.

## hello

ShelfFind learns what a source can do with one question, and remembers the answer until the program file changes.

```json
{"version": 2, "op": "hello", "client": "shelffind/0.3.0"}
```

```json
{"ok": true, "version": 2, "name": "Catalogue", "ops": ["search", "resolve", "identify", "pages", "act"],
 "text": true, "network": false,
 "actions": [{"name": "cite", "label": "Copy the citation key", "result": "copy"}]}
```

A program that fails this question, or answers anything else, is taken for a source of version 1. The fields are these.

| Field | Meaning |
|---|---|
| `ops` | The questions the source answers, among `search`, `resolve`, `identify`, `pages` and `act`. Only `search` is required. |
| `name` | A name for people. The registered name stays the one used in commands. |
| `text` | True when the source holds the full text of its documents and `search` finds passages in it. |
| `network` | True when `search`, `identify` or `pages` need the network. Such a source is asked only when the user asks for it, never at each letter typed. `resolve` and `act`, which the user starts on one result, may use the network whatever this says. |
| `actions` | What can be done with a result besides opening it: `name`, a `label` for the menu, and the kind of `result` (see `act`). |

## search

```json
{"version": 2, "op": "search", "query": "whitham dispersion",
 "terms": [{"text": "whitham", "literal": false, "alternatives": ["witham"]},
           {"text": "dispersion", "literal": false, "alternatives": []}],
 "exact": false, "where": "all", "format": null, "limit": 25, "within": null, "budget_ms": 800}
```

`terms` gives the words of the query as ShelfFind understood them: folded to lower case without accents, a quoted phrase as one `literal` term, and for each word the spellings ShelfFind would also accept. Every term must be met; a term that is not literal may also be met by one of its alternatives. In the description of a record every such term may be the beginning of a longer word; in the text ShelfFind itself completes only the last term, and only from four letters, since completing short common beginnings over a large text is slow, and a source does well to do the same. `where` is `name` for the description of a record (title, authors, year), `text` for passages, and `all` for both. `format` keeps the records whose file has that extension. `budget_ms` is the time after which a partial answer is worth more than a complete one; it is a wish, not a limit.

`within` is null, or the `id` of one record: the answer then lists the passages of that record alone, in the order of its pages.

`partial` is true when the results were not chosen among all that match: the words were so common that only a sample of the matches was ranked, or the time ran out. An answer cut at `limit` is not partial.

```json
{"ok": true, "partial": false, "results": [
  {"id": "QX7R2M4K/9f2c", "title": "Linear and Nonlinear Waves", "byline": "G. B. Whitham", "year": 1974,
   "format": "djvu", "match": "passage", "page": 380, "page_label": "363",
   "snippet": "the dispersion relation gives the group velocity", "marks": [[4, 23]],
   "keys": ["sha256:9f2c…", "md5:41d8…"], "path": "/home/reader/library/9f/2c.djvu", "fetch": true,
   "flags": ["unverified"], "notes": 2, "actions": ["cite"]}]}
```

| Field | Meaning |
|---|---|
| `id` | Required. A string that names the result to the source in later questions. It is never shown. |
| `title` | Required, unless `name` is given. The title of the work. |
| `name` | The name of the file the record holds, if it has one. |
| `byline`, `year`, `venue` | Authors as they should be shown, without the year, which is given apart; the year; journal or publisher. |
| `format`, `size` | Extension and size in bytes of the file the record holds. |
| `match` | What matched, in a word or two: it is shown on the row. A value ending in `passage` marks a match in the text. |
| `page`, `page_label`, `section` | The page of the passage counted from 1 in the file, which the viewer opens; the number printed on that page; a chapter or part. |
| `snippet`, `marks` | The passage on one line, and the ranges `[start, end)` of the matched words in it, counted in characters (Unicode code points) of the snippet as it is given. |
| `keys` | Checksums of the file this result stands for, as `sha256:` or `md5:` followed by lower-case hexadecimal digits, one of each kind at most: two of one kind are taken for none. ShelfFind recognises by them a file of the folders, and shows the result with no file of the folders when what is known here says that it describes other bytes: a file of another `size`, when the result gives one, or one known by another checksum of a kind the result gives. |
| `path` | The absolute address of a copy on this machine that can be opened at once. |
| `fetch` | True when `resolve` can obtain the file, even slowly. |
| `flags` | Short warnings shown on the row, such as `retracted` or `unverified`. |
| `notes` | The number of notes the source holds on the work. |
| `availability` | A few words on what can be had, shown when there is no file to open. |
| `actions` | The names of the declared actions that apply to this result. |

## resolve

```json
{"version": 2, "op": "resolve", "id": "QX7R2M4K/9f2c"}
```

The answer is `{"ok": true, "path": "/absolute/path"}` for a file, which the source may fetch first, or `{"ok": true, "uri": "zotero://select/library/items/QX7R2M4K"}` for a record that opens elsewhere.

## identify

ShelfFind asks a source which of the files of the folders it knows, at most 500 in one question. Each file comes with its address, its size, its date in seconds, and the checksums that ShelfFind measured of that very file or received from its storage; the list is empty for most files, which were never read in full.

```json
{"version": 2, "op": "identify", "files": [
  {"n": 0, "path": "/home/reader/Books/Whitham-LinearAndNonlinearWaves-Wiley-1974.djvu", "size": 10270451,
   "mtime": 1700000000, "keys": ["md5:41d8…"]}]}
```

```json
{"ok": true, "records": [
  {"n": 0, "id": "QX7R2M4K/9f2c", "basis": "md5", "keys": ["sha256:9f2c…", "md5:41d8…"],
   "title": "Linear and Nonlinear Waves", "byline": "G. B. Whitham", "year": 1974,
   "flags": [], "text": true, "path": "/home/reader/library/9f/2c.djvu"}]}
```

A file the source does not know is left out of the answer. `basis` says how the file was recognised: `sha256` or `md5` when a checksum agreed, or words of the source's own, such as `path and size`, when it relied on something weaker. `text` is true when the source holds the text of that file, in which case ShelfFind does not read the file itself. A source that needs the network is never sent this question.

The `keys` of an answer are the checksums of the bytes of the file asked about, as the source knows them. ShelfFind takes them at the source's word for a file it has not measured: such a file is placed with the document that is measured to have these checksums, or with the other files of its size and format for which a source gives the same ones, and it then shows the text of that document. A source should therefore return `keys` only when it has reason to hold that the file of the folders is byte for byte the file it knows, and leave them out when it recognises the work alone; it gives one checksum of each kind at most, and two of one kind are taken for none. When the question carries the checksums of a file, they were measured: a source that knows the work by a checksum should answer from them, and not from the address.

The word of a source never outranks a measure. An answer is not kept when the checksums it gives contradict those measured of the file, or are those of a document of another size, or of two documents, or of a document that the file is measured not to be; an answer kept earlier is dropped the moment a measure refutes it in one of these ways, whichever file was measured, and the source is then asked about the file again. `shelffind index` proves every answer in this way, by the checksum that the storage of the folder gives or by one reading of the file; an inventory alone (`--names-only`) proves nothing. The proof needs a checksum of a kind the answer gives: an answer that gives a SHA-256 alone for a file whose storage tells an MD5 costs one reading of the file, so a source does well to give both. An answer that gives two checksums is proved by the one that is measured, and the other is then taken on the word of the source until the file is read in full; when two answers kept with one document give two different checksums of one kind, one of them is false, and `shelffind index` reads a file of the document in full to tell which. An answer without any checksum gives the file a title and nothing more: it cannot be proved, and `text` is then of no effect, the file being read by ShelfFind like any other. Two sources that give contradicting checksums for one file place it nowhere until they agree or a measure decides. The pages that an earlier version read from a file are never given to another file on the word of a source. What a source says is kept with the file it was said of, and is replaced by what the source says when it is asked about that file again, were it nothing. What it said of a file outlives the file only as far as a measure proved it: an answer that gives a checksum measured of the document stays with those bytes when their file is moved or removed, until the source is asked about the file at its new address, and any other answer goes with the file.

## pages

```json
{"version": 2, "op": "pages", "id": "QX7R2M4K/9f2c", "first": 380, "count": 3}
```

```json
{"ok": true, "total": 636, "pages": [{"page": 380, "label": "363", "text": "…"}]}
```

Pages are counted from 1 in the order of the file. ShelfFind uses this question to show a page inside the panel. A range past the end, or a `count` of zero, is answered by an empty list with the `total`. A record whose text is not ready yet is refused with `unavailable` and `"retry": true`, and a record without text with `not_found`.

## act

```json
{"version": 2, "op": "act", "id": "QX7R2M4K/9f2c", "action": "cite"}
```

The answer names what ShelfFind does next: `{"ok": true, "result": "copy", "text": "whitham1974"}` puts the text on the clipboard, `{"result": "open", "uri": "…"}` opens an address, `{"result": "show", "title": "Notes", "text": "…"}` shows a text inside the panel, and `{"result": "message", "text": "The full text is requested"}` shows one line.

## One document, one row

ShelfFind merges the answers of the folders and of every source into one list. Two results are the same document when the address of one is a copy of the other, when they share a checksum, or when an earlier `identify` linked the file to the record. The merged row keeps the file of the folders, which opens fastest, takes the title, the authors, the flags and the actions of the record, and shows the passage of whichever found one.

## A source in a few lines

`shelffind/provider.py` is one file without dependencies, to be imported or copied beside a source. It reads the question, answers `hello`, serves version 1 as well, checks the answers and turns an exception into a refusal.

```python
#!/usr/bin/env python3
from shelffind.provider import serve

RECORDS = {"a1": {"title": "Tide Tables of the Northern Harbours", "byline": "Admiralty", "year": 1936}}

def search(request):
    words = [term["text"] for term in request["terms"]]
    return [{"id": key, **record, "match": "catalogue"} for key, record in RECORDS.items()
            if all(word in record["title"].lower() for word in words)]

def resolve(request):
    return {"uri": "https://example.org/records/" + request["id"]}

serve(name="Example", search=search, resolve=resolve)
```
