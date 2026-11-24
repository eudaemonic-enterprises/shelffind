# Copyright 2026 Eudaemonic Enterprises Inc.
"""Command-line interface to ShelfFind."""
import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import termios
import time
from contextlib import closing
from pathlib import Path

from . import (__version__, config, curate, desktop, doctor, helptext, indexer, machine, migrate, parcels, sharing, shell,
               sources, store)
from .picker import panel
from .text import json_text, safe
from .tui import report, style

COMMANDS = ["search", "pick", "open", "apps", "reveal", "roots", "sources", "index", "status",
            "config", "doctor", "upgrade", "report", "sync", "machine", "cheatsheet", "init", "completion"]


def search_options(command):
    command.add_argument("query", nargs="*")
    command.add_argument("--exact", action="store_true", help="Whole words and phrases without fuzzy expansion")
    command.add_argument("--where", choices=["all","name","text"], help="Search names, passages, or both")
    command.add_argument("--source", help="Search only files or one external source")
    command.add_argument("--root", help="Filter by registered folder")
    command.add_argument("--format", dest="fmt", help="Filter by extension, e.g. djvu")
    command.add_argument("--limit", type=int, help="Number of results (25 in lists, the setting in the panel)")


def parser():
    p = argparse.ArgumentParser(prog="shelffind", description="Find documents by name, folder, or passage.",
                                epilog=helptext.EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"ShelfFind {__version__}")
    p.add_argument("--data-dir", type=Path, help="Private index directory; only its own config.toml is then read")
    cmds = p.add_subparsers(dest="command")
    rs = cmds.add_parser("roots", help="Choose document folders").add_subparsers(dest="action", required=True)
    rs.add_parser("add").add_argument("folders", nargs="+", type=Path)
    rs.add_parser("remove", help="Forget a folder and everything indexed from it").add_argument("folder")
    rs.add_parser("list")
    external = cmds.add_parser("sources", help="Configure optional search providers").add_subparsers(dest="action", required=True)
    provider = external.add_parser("add", help="Register a trusted local executable")
    provider.add_argument("name")
    provider.add_argument("program", type=Path)
    external.add_parser("remove").add_argument("name")
    external.add_parser("list")
    trial = external.add_parser("check", help="Ask a source every question and report what it answers")
    trial.add_argument("name")
    trial.add_argument("query", nargs="*", help="Words the source surely knows (default: the)")
    ix = cmds.add_parser("index", help="Refresh names and continue reading document text")
    mode = ix.add_mutually_exclusive_group()
    mode.add_argument("--names-only", action="store_true",
                      help="Update the list of files and ask the sources and the storage about them; open no file")
    mode.add_argument("--ocr", action="store_true", help="Recognize pages missing embedded text")
    ix.add_argument("--lang", default="eng", help="Installed Tesseract languages, e.g. eng+fra")
    ix.add_argument("--retry", action="store_true", help="Include previous extraction or OCR failures")
    ix.add_argument("--limit", type=int, help="Maximum number of documents to read this pass")
    ix.add_argument("--root", help="Index one registered folder")
    ix.add_argument("--file", type=Path, action="append", default=[], help="Read text from selected files only (repeatable)")
    ix.add_argument("--relink", action="store_true", help="Ask the sources about every file again")
    st = cmds.add_parser("status", help="Index coverage and pending work")
    st.add_argument("--json", action="store_true")
    st.add_argument("--errors", action="store_true", help="List failed files and reasons")
    se = cmds.add_parser("search", help="The search panel, or results as text or JSON")
    search_options(se)
    se.add_argument("--json", action="store_true")
    se.add_argument("--list", action="store_true", help="Print matches without opening the panel")
    pk = cmds.add_parser("pick", help="The search panel; print the address of the chosen document")
    search_options(pk)
    pk.add_argument("--enter", choices=["print", "open"], default="print",
                    help="What Enter does: print the address (default) or open the document")
    pk.add_argument("--quote", action="store_true", help="Quote the address for a shell")
    pk.add_argument("--widget", action="store_true", help="Run from a key binding of the shell")
    op = cmds.add_parser("open", help="Open with the default or a registered application")
    op.add_argument("--with", dest="app", metavar="DESKTOP-ID")
    op.add_argument("--page", type=int, help="Open at this page where the viewer can")
    apps = cmds.add_parser("apps", help="List applications for a file")
    reveal = cmds.add_parser("reveal", help="Show a file in the file manager")
    for action in (op, apps, reveal):
        action.add_argument("path", help="File path, or a result ID with --source")
        action.add_argument("--source", help="Resolve a result ID from this source")
    cf = cmds.add_parser("config", help="The settings and where each value comes from")
    cf.add_argument("action", choices=["show", "path", "check"], nargs="?", default="show")
    cf.add_argument("--json", action="store_true")
    dr = cmds.add_parser("doctor", help="Check the installation; nothing is changed")
    dr.add_argument("--json", action="store_true")
    up = cmds.add_parser("upgrade", help="Convert an index of the earlier format; the old one is kept")
    up.add_argument("--discard-old", action="store_true", help="Delete the earlier index once converted")
    rp = cmds.add_parser("report", help="Lists for keeping the folders in order")
    rp.add_argument("what", choices=["copies", "unlinked", "names"],
                    help="copies: documents kept in several places; unlinked: files no source knows; "
                         "names: files whose name does not show the work a source says they are")
    rp.add_argument("--root", help="One registered folder")
    rp.add_argument("--source", help="For unlinked: the files this source does not know")
    rp.add_argument("--json", action="store_true")
    sy = cmds.add_parser("sync", help="Exchange the journal of searches, and the texts read, with your other machines")
    sy.add_argument("--quiet", action="store_true", help="Say nothing unless something fails")
    mc = cmds.add_parser("machine", help="The name of this machine among those that share")
    mc.add_argument("--name", help="Give this machine a name, such as home or office")
    cmds.add_parser("cheatsheet", help="A one-screen reference")
    it = cmds.add_parser("init", help="Print a key binding for the shell: eval \"$(shelffind init bash)\"")
    it.add_argument("shell", choices=["bash"])
    it.add_argument("--key", default="ctrl-t", help="The key that opens the panel (default ctrl-t)")
    cmds.add_parser("completion", help="Print completion for the shell").add_argument("shell", choices=["bash"])
    return p


def listing(hits, terminal):
    """Lines of `search --list`. Text taken from names and documents is cleaned before it is
    shown; a piped file path stays exact, so that another program can use it."""
    for hit in hits:
        if hit["source"] == "files":
            yield safe(hit["path"]) if terminal else hit["path"]
        else:
            yield safe(f"[{hit['source']}] {hit['name']}  ({hit['availability'] or hit['match']})")
            if hit.get("byline"):
                yield "  " + safe(hit["byline"])
        if hit["snippet"]:
            location = f"p. {hit['page']}  " if hit["page"] else ""
            yield "  " + location + safe(hit["snippet"])


def coverage(db, errors):
    """One row per folder: its files counted by the state of their documents."""
    result = []
    for root in store.roots(db):
        counts = dict(db.execute("SELECT d.state,count(*) FROM files f JOIN docs d ON d.id=f.doc "
                                 "WHERE f.root=? GROUP BY d.state", (root["id"],)))
        row = {"path": root["path"], "name": root["name"], "available": store.available(root),
               "scanned": root["scanned"], "problem": root["problem"], "files": sum(counts.values()),
               "states": counts}
        if errors:
            row["errors"] = [dict(r) for r in db.execute(
                "SELECT f.rel,d.error FROM files f JOIN docs d ON d.id=f.doc WHERE f.root=? "
                "AND d.error IS NOT NULL", (root["id"],))]
        result.append(row)
    return result


def coverage_earlier(directory, errors):
    """The same rows read from an index of the earlier format, which `status` never converts."""
    old = sqlite3.connect((directory / store.EARLIER).as_uri() + "?mode=ro", uri=True, timeout=10)
    old.row_factory = sqlite3.Row
    try:
        result = []
        for root in old.execute("SELECT * FROM roots ORDER BY id").fetchall():
            counts = dict(old.execute("SELECT state,count(*) FROM files WHERE root=? GROUP BY state", (root["id"],)))
            place = {"path": root["path"], "mount": root["mount"]}
            row = {"path": root["path"], "name": Path(root["path"]).name, "available": store.available(place),
                   "scanned": root["scanned"], "problem": root["problem"], "files": sum(counts.values()),
                   "states": counts}
            if errors:
                row["errors"] = [dict(r) for r in old.execute(
                    "SELECT rel,error FROM files WHERE root=? AND error IS NOT NULL", (root["id"],))]
            result.append(row)
        return result
    finally:
        old.close()


def status(args, rows, directory, settings):
    if args.json:
        print(json_text(rows))
    elif sys.stdout.isatty():
        extra = [name for name in sources.registered(directory)]
        theme = look(settings, sys.stdout)
        for line in report.status(rows, theme, __version__, extra,
                                  unicode_signs(settings, sys.stdout), shutil.get_terminal_size().columns):
            print(line)
        if sharing.carrier(settings) is not None:
            for line in report.machines(machine.identity(directory), sharing.last(directory), theme,
                                        unicode_signs(settings, sys.stdout)):
                print(line)
    else:
        for row in rows:
            states = ", ".join(f"{n:,} {k}" for k, n in row["states"].items())
            print(safe(f"{row['path']}   {row['files']:,} files   {states}"))
            if not row["available"] or row["problem"]:
                print("  " + safe(row["problem"] or "Folder unavailable"))
            for e in row.get("errors", []):
                print("  " + safe(f"{e['rel']}: {e['error']}"))
    return 0


def find(args, db, directory, settings):
    """`search` and `pick`: the panel at a terminal, a list or JSON otherwise."""
    query = " ".join(args.query)
    picking = args.command == "pick"
    wanted = picking or not (args.json or args.list)
    if wanted and (picking or (sys.stdin.isatty() and sys.stdout.isatty())):
        opts = {"root": args.root, "fmt": args.fmt, "exact": args.exact or settings["search.exact"],
                "source": args.source, "limit": args.limit or settings["search.limit"],
                "where": args.where or settings["search.scope"]}
        refresh_later(args, db, settings)
        if picking:
            code, text, _ = panel(db, directory, query, opts, settings, enter=args.enter,
                                  quote=args.quote, widget=args.widget)
        else:
            code, text, _ = panel(db, directory, query, opts, settings)
            code = code if code >= 128 else 0       # leaving the panel is not a failure
        if text:
            print(text)
        exchange_later(args, directory, settings)
        return code
    opts = {"root": args.root, "fmt": args.fmt, "exact": args.exact, "limit": args.limit or 25,
            "source": args.source, "where": args.where or "all"}
    hits, problem = [], ""
    def collect(rows, message):
        nonlocal hits, problem
        hits, problem = rows, message
    sources.find(db, directory, query, opts, collect)
    if problem:
        print(safe(problem), file=sys.stderr)
    if args.json:
        # The number of a document inside the index means nothing outside it.
        print(json_text([{key: value for key, value in hit.items() if key != "doc"} for hit in hits]))
    elif sys.stdout.isatty():
        for line in report.results(hits, look(settings, sys.stdout), home=os.path.expanduser("~")):
            print(line)
    else:
        for line in listing(hits, False):
            print(line)
    return int(bool(problem) and not hits)


EXCHANGE = 600          # seconds between two exchanges started by the panel


def apart(args, *command):
    """Start a ShelfFind command in a process of its own, which outlives this one, says nothing
    and delays nothing."""
    place = ["--data-dir", str(args.data_dir)] if args.data_dir else []
    try:
        subprocess.Popen([sys.executable, "-m", "shelffind", *place, *command], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def refresh_later(args, db, settings):
    """When the panel opens and the list of files is older than the settings allow: refresh it
    beside the panel. Files added, moved or renamed since are then found without a command."""
    minutes = settings["index.refresh"]
    if minutes <= 0:
        return
    oldest = db.execute("SELECT min(coalesce(strftime('%s',scanned),0)),count(*) FROM roots").fetchone()
    if oldest[1] and time.time() - int(oldest[0]) > 60 * minutes:
        apart(args, "index", "--names-only")


def exchange(args, directory, settings):
    """The command sync: the journals, then the texts, when the settings share them: parcels
    are taken and given, while their pages wait for the next run of the index, which gives
    them once its inventory is made. Exits with 1 when the shared place failed; a parcel that
    arrived damaged is said, and taken again at a later exchange."""
    place = sharing.carrier(settings)
    if place is None:
        raise ValueError("Nothing is shared: set share.history or share.texts, and share.folder or share.remote")
    failed = False
    with sharing.alone(directory) as free:
        if not free:
            if not args.quiet:
                print("Another exchange of this data folder is under way.")
            return 0
        done = sharing.sync(directory, place, journals=settings["share.history"])
        damaged, texts = done["damaged"], None
        if settings["share.texts"]:
            # The parcels are carried both ways; their pages are given by the next run of the
            # index, once its inventory is made, which the panel starts by itself.
            try:
                with closing(store.connect(directory, upgrade=False)) as db:
                    texts = parcels.Exchange(directory, db, place, lambda text: print(safe(text), file=sys.stderr))
                    failed = not texts.steps("receive", "send")
                    damaged += texts.counts["damaged"]
            except store.NeedsUpgrade:
                pass                            # the texts wait for the conversion of the index
    if damaged or failed or not args.quiet:
        others = ", ".join(done["machines"]) or "no other machine yet"
        told = [f"{done['sent']} sent, {done['received']} received ({others})"]
        if texts is not None:
            told.append(f"texts of {texts.counts['sent']:,} documents given")
        if damaged:
            told.append(f"{damaged} could not be taken and will be asked again")
        print("; ".join(told))
    return int(failed)


def exchange_later(args, directory, settings):
    """After the panel, when journals are shared and the last exchange is not recent: start
    one in a process of its own, which outlives this one and delays nothing."""
    if sharing.carrier(settings) is None or time.time() - sharing.last(directory).get("synced", 0) < EXCHANGE:
        return
    apart(args, "sync", "--quiet")


def look(settings, stream):
    """The colour theme for what is printed on `stream`: none unless it is a terminal."""
    return style.theme(settings["panel.theme"], settings.tables["theme"], os.environ, stream.isatty())


def unicode_signs(settings, stream):
    choice = settings["panel.glyphs"]
    return choice == "unicode" if choice != "auto" else "utf" in (stream.encoding or "").lower()


def execute(args, db, directory, settings):
    if args.command == "roots":
        if args.action == "add":
            with store.index_lock(directory):
                for path in args.folders:
                    print(store.add_root(db, path, directory))
        elif args.action == "remove":
            with store.index_lock(directory):
                print(store.remove_root(db, args.folder))
        else:
            for root in store.roots(db):
                print(root["path"])
    elif args.command == "sources":
        if args.action == "list":
            print("files")
            for name, command in sources.registered(directory).items():
                print(f"{name}  {' '.join(command)}")
        elif args.action == "check":
            faults = 0
            for verdict, what, detail in sources.check(directory, args.name, " ".join(args.query) or "the"):
                print(safe(f"[{verdict:^4}] {what:<9} {detail}"))
                faults += verdict == "fail"
            return int(bool(faults))
        else:
            sources.configure(directory, args.name, args.program if args.action == "add" else None)
    elif args.command == "index":
        if args.limit is not None and args.limit < 0:
            raise ValueError("Choose a nonnegative limit")
        files = [p.expanduser().resolve(strict=True) for p in args.file]

        def roots():
            """The folders to index, chosen once the build holds its lock: no other command can
            then change them under this one."""
            store.adopt_declared(db, directory, say)    # the folders the settings name
            chosen = store.roots(db, args.root)
            if not chosen:
                raise ValueError("Register a document folder with shelffind roots add FOLDER")
            for path in files:
                if not path.is_file() or not any(path.is_relative_to(r["path"]) for r in chosen):
                    raise ValueError(f"File outside the selected roots: {path}")
            return chosen
        live = None
        if sys.stderr.isatty():         # at a terminal: one line follows the documents being read
            def write(text):
                sys.stderr.write(text)
                sys.stderr.flush()
            live = report.Progress(write, lambda: shutil.get_terminal_size(), look(settings, sys.stderr))
        said = live.message if live else lambda s: print(safe(s), file=sys.stderr, flush=True)
        place = sharing.carrier(settings) if settings["share.texts"] else None
        try:
            count, failures = indexer.build(
                db, directory, roots, names_only=args.names_only, ocr=args.ocr, languages=args.lang,
                retry=args.retry, limit=args.limit, paths=files, relink=args.relink, progress=said,
                on_document=live.document if live else None,
                exchange=parcels.Exchange(directory, db, place, said) if place is not None else None)
        finally:
            if live:
                live.clear()
        print(f"{count:,} documents processed, {failures:,} problems", file=sys.stderr)
        return int(bool(failures))
    elif args.command == "status":
        return status(args, coverage(db, args.errors), directory, settings)
    elif args.command == "report":
        rows = {"copies": lambda: curate.copies(db, args.root),
                "unlinked": lambda: curate.unlinked(db, args.root, args.source),
                "names": lambda: curate.names(db, args.root)}[args.what]()
        if args.json:
            print(json_text(rows))
        else:
            for row in rows:
                if args.what == "copies":
                    print(safe(f"{row['size']:>12,}  {row['title'] or Path(row['paths'][0]).name}"))
                    for path in row["paths"]:
                        print("              " + safe(path))
                elif args.what == "unlinked":
                    print(safe(row["path"]))
                else:
                    work = " ".join(str(part) for part in (row["byline"], f"({row['year']})" if row["year"] else "",
                                                           row["title"]) if part)
                    print(safe(f"{row['path']}\n    lacks {', '.join(row['lacks'])}: {work}"))
            print(f"{len(rows):,} " + {"copies": "documents kept in several places", "unlinked": "files no source knows",
                                      "names": "names that do not show their work"}[args.what], file=sys.stderr)
    elif args.command in ("search", "pick"):
        return find(args, db, directory, settings)
    return 0


SMALL_INDEX = 64 << 20          # an earlier index below this size is converted without being asked


def say(text):
    print(safe(text), file=sys.stderr, flush=True)


def opened(args, directory):
    """The index for a command. An index of the earlier format is converted first by the commands
    that change it or work on it at length (the panel, `index`, `roots add`, `roots remove`), and
    by any other command when it is small. `status` never converts: it reports from the old
    index. A large old index is otherwise left alone, and the command says what to do."""
    try:
        return store.connect(directory, upgrade=False)
    except store.NeedsUpgrade:
        panel = args.command == "pick" or (args.command == "search" and not (args.json or args.list)
                                           and sys.stdin.isatty() and sys.stdout.isatty())
        changing = args.command == "index" or (args.command == "roots" and args.action != "list")
        small = (directory / store.EARLIER).stat().st_size < SMALL_INDEX
        if args.command == "status" or not (panel or changing or small):
            raise
        say("Converting the index to its new form; the earlier one is kept.")
        migrate.upgrade(directory, say)
        return store.connect(directory, upgrade=False)


def configuration(args, directory, explicit):
    """`config show`, `config path`, `config check`."""
    if args.action == "path":
        path = (directory if explicit else config.config_home()) / "config.toml"
        print(path)
        return 0
    settings = config.load(directory, explicit)
    sources.declare(settings.tables["sources"])
    store.declare(settings.tables["roots"])
    settings.keymap()
    for name, roles in settings.tables["theme"].items():
        for role, spec in roles.items():
            try:
                style.parse(spec)
            except ValueError as fault:
                settings.warnings.append(f"[theme.{name}] {role}: {fault}")
    style.theme(settings["panel.theme"], settings.tables["theme"], {})
    if args.action == "check":
        for warning in settings.warnings:
            print(safe(warning), file=sys.stderr)
        print("The settings have faults." if settings.warnings else "The settings are sound.")
        return int(bool(settings.warnings))
    if args.json:
        rows = {key: {"value": settings.values[key], "origin": settings.origins[key]}
                for key in config.SETTINGS}
        for name in config.TABLES:
            for inner, content in settings.tables[name].items():
                rows[f"{name}.{inner}"] = {"value": content,
                                           "origin": settings.table_origins[f"{name}.{inner}"]}
        print(json_text(rows))
    else:
        for line in config.report(settings):
            print(safe(line))
        for warning in settings.warnings:
            print(safe(warning), file=sys.stderr)
    return 0


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.command is None:
        args = p.parse_args([*(["--data-dir", str(args.data_dir)] if args.data_dir else []), "search"])
    try:
        explicit = args.data_dir is not None
        directory = (args.data_dir or store.state_directory()).expanduser().resolve()
        if args.command == "cheatsheet":
            print(helptext.CHEATSHEET, end="")
        elif args.command == "init":
            print(shell.init(args.shell, args.key), end="")
        elif args.command == "completion":
            print(shell.completion(args.shell, COMMANDS), end="")
        elif args.command == "config":
            return configuration(args, directory, explicit)
        elif args.command == "doctor":
            return doctor.run(directory, explicit, args.json)
        else:
            try:
                settings = config.load(directory, explicit)
                sources.declare(settings.tables["sources"])
                store.declare(settings.tables["roots"])
            except ValueError as fault:
                if args.command in ("search", "pick"):
                    raise
                print(f"shelffind: {safe(fault)}; the defaults are used", file=sys.stderr)
                settings = config.defaults()
            if args.command in {"open", "apps", "reveal"}:
                target = sources.resolve(directory, args.source, args.path)
                if "uri" in target:
                    if args.command != "open" or args.app:
                        raise ValueError("This result has a citation link. Use open to view it.")
                    desktop.open_uri(target["uri"])
                elif args.command == "open":
                    custom = settings.tables["open"].get(Path(target["path"]).suffix.lstrip(".").lower())
                    desktop.launch(target["path"], args.app, page=args.page,
                                   custom=custom if isinstance(custom, list) else None)
                elif args.command == "apps":
                    # The identifier, which `open --with` takes, then the name people know.
                    for name, shown in desktop.choices(target["path"]):
                        print(safe(f"{name}  {shown}"))
                else:
                    desktop.reveal(target["path"])
            elif args.command == "machine":
                told = machine.identity(directory, args.name)
                print(f"{told['name']}  ({told['id']})")
            elif args.command == "sync":
                return exchange(args, directory, settings)
            elif args.command == "upgrade":
                if args.discard_old:
                    freed = migrate.discard_old(directory)
                    print(f"The earlier index is deleted: {freed / 1e6:,.1f} MB freed.")
                else:
                    present = (directory / store.INDEX).exists()
                    done = migrate.upgrade(directory, say)      # also completes one that was cut short
                    if present or done is None:
                        print("Nothing to convert.")
            else:
                try:
                    db = opened(args, directory)
                except store.NeedsUpgrade as waiting:
                    if args.command == "status":        # a report never converts: it reads the old index
                        try:
                            rows = coverage_earlier(directory, args.errors)
                        except sqlite3.Error:
                            # The old index went at this very moment: its successor answers.
                            with closing(opened(args, directory)) as db:
                                return execute(args, db, directory, settings)
                        return status(args, rows, directory, settings)
                    print(f"shelffind: {waiting}", file=sys.stderr)
                    return 75
                with closing(db):
                    return execute(args, db, directory, settings)
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Completed documents and OCR pages have been saved.", file=sys.stderr)
        return 130
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError,
            termios.error) as exc:
        print(f"shelffind: {safe(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
