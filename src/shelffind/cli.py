# Copyright 2026 Eudaemonic Enterprises Inc.
"""Command-line interface to ShelfFind."""
import argparse
import curses
import os
import sqlite3
import subprocess
import sys
import termios
from contextlib import closing
from pathlib import Path

from . import __version__, config, desktop, doctor, helptext, indexer, shell, sources, store
from .picker import panel, picker
from .text import json_text, safe
from .tui import style

COMMANDS = ["search", "pick", "open", "apps", "reveal", "roots", "sources", "index", "status",
            "config", "doctor", "cheatsheet", "init", "completion"]


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
    ix = cmds.add_parser("index", help="Refresh names and continue reading document text")
    mode = ix.add_mutually_exclusive_group()
    mode.add_argument("--names-only", action="store_true", help="Only update the file inventory")
    mode.add_argument("--ocr", action="store_true", help="Recognize pages missing embedded text")
    ix.add_argument("--lang", default="eng", help="Installed Tesseract languages, e.g. eng+fra")
    ix.add_argument("--retry", action="store_true", help="Include previous extraction or OCR failures")
    ix.add_argument("--limit", type=int, help="Maximum number of documents to read this pass")
    ix.add_argument("--root", help="Index one registered folder")
    ix.add_argument("--file", type=Path, action="append", default=[], help="Read text from selected files only (repeatable)")
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
    result = []
    for root in store.roots(db):
        counts = dict(db.execute("SELECT state,count(*) FROM files WHERE root=? GROUP BY state",
                                 (root["id"],)))
        row = {"path": root["path"], "available": store.available(root), "scanned": root["scanned"],
                   "problem": root["problem"], "files": sum(counts.values()), "states": counts}
        if errors:
            row["errors"] = [dict(r) for r in db.execute("SELECT rel,error FROM files WHERE root=? "
                                                        "AND error IS NOT NULL", (root["id"],))]
        result.append(row)
    return result


def find(args, db, directory, settings):
    """`search` and `pick`: the panel at a terminal, a list or JSON otherwise."""
    query = " ".join(args.query)
    picking = args.command == "pick"
    wanted = picking or not (args.json or args.list)
    if wanted and (picking or (sys.stdin.isatty() and sys.stdout.isatty())):
        opts = {"root": args.root, "fmt": args.fmt, "exact": args.exact, "source": args.source,
                "limit": args.limit or settings["search.limit"],
                "where": args.where or settings["search.scope"]}
        if not picking and os.environ.get("SHELFFIND_UI") == "curses":
            try:
                curses.wrapper(picker, db, directory, query, opts)
            except KeyboardInterrupt:
                return 130
            return 0
        if picking:
            code, text, _ = panel(db, directory, query, opts, settings, enter=args.enter,
                                  quote=args.quote, widget=args.widget)
        else:
            code, text, _ = panel(db, directory, query, opts, settings)
            code = code if code >= 128 else 0       # leaving the panel is not a failure
        if text:
            print(text)
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
        print(json_text(hits))
    else:
        for line in listing(hits, sys.stdout.isatty()):
            print(line)
    return int(bool(problem) and not hits)


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
        else:
            sources.configure(directory, args.name, args.program if args.action == "add" else None)
    elif args.command == "index":
        roots = store.roots(db, args.root)
        if not roots:
            raise ValueError("Register a document folder with shelffind roots add FOLDER")
        if args.limit is not None and args.limit < 0:
            raise ValueError("Choose a nonnegative limit")
        files = [p.expanduser().resolve(strict=True) for p in args.file]
        for path in files:
            if not path.is_file() or not any(path.is_relative_to(r["path"]) for r in roots):
                raise ValueError(f"File outside the selected roots: {path}")
        count, failures = indexer.build(db, directory, roots, names_only=args.names_only, ocr=args.ocr,
                                        languages=args.lang, retry=args.retry, limit=args.limit,
                                        paths=files, progress=lambda s: print(safe(s), file=sys.stderr, flush=True))
        print(f"{count:,} documents processed, {failures:,} problems", file=sys.stderr)
        return int(bool(failures))
    elif args.command == "status":
        rows = coverage(db, args.errors)
        if args.json:
            print(json_text(rows))
            return 0
        for row in rows:
            states = ", ".join(f"{n:,} {k}" for k, n in row["states"].items())
            print(safe(f"{row['path']}   {row['files']:,} files   {states}"))
            if not row["available"] or row["problem"]:
                print("  " + safe(row["problem"] or "Folder unavailable"))
            for e in row.get("errors", []):
                print("  " + safe(f"{e['rel']}: {e['error']}"))
    elif args.command in ("search", "pick"):
        return find(args, db, directory, settings)
    return 0


def configuration(args, directory, explicit):
    """`config show`, `config path`, `config check`."""
    if args.action == "path":
        path = (directory if explicit else config.config_home()) / "config.toml"
        print(path)
        return 0
    settings = config.load(directory, explicit)
    sources.declare(settings.tables["sources"])
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
                    print("\n".join(desktop.applications(target["path"])))
                else:
                    desktop.reveal(target["path"])
            else:
                with closing(store.connect(directory)) as db:
                    return execute(args, db, directory, settings)
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Completed documents and OCR pages have been saved.", file=sys.stderr)
        return 130
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError, curses.error,
            termios.error) as exc:
        print(f"shelffind: {safe(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
