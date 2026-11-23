# Copyright 2026 Eudaemonic Enterprises Inc.
"""Command-line interface to ShelfFind."""
import argparse
from contextlib import closing
import curses
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from . import desktop, indexer, sources, store
from .picker import picker


def parser():
    p = argparse.ArgumentParser(prog="shelffind", description="Find documents by name, folder, or passage.")
    p.add_argument("--data-dir", type=Path, default=store.state_directory(), help="Private index directory")
    cmds = p.add_subparsers(dest="command")
    rs = cmds.add_parser("roots", help="Choose document folders").add_subparsers(dest="action", required=True)
    rs.add_parser("add").add_argument("folders", nargs="+", type=Path)
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
    se = cmds.add_parser("search", help="Interactive fuzzy search or machine-readable results")
    se.add_argument("query", nargs="*")
    se.add_argument("--json", action="store_true")
    se.add_argument("--list", action="store_true", help="Print matches without opening the picker")
    se.add_argument("--exact", action="store_true", help="Whole words and phrases without fuzzy expansion")
    se.add_argument("--where", choices=["all","name","text"], default="all", help="Search names, passages, or both")
    se.add_argument("--source", help="Search only files or one external source")
    se.add_argument("--root", help="Filter by registered folder")
    se.add_argument("--format", dest="fmt", help="Filter by extension, e.g. djvu")
    se.add_argument("--limit", default=25, type=int)
    op = cmds.add_parser("open", help="Open with the default or a registered application")
    op.add_argument("--with", dest="app", metavar="DESKTOP-ID")
    apps = cmds.add_parser("apps", help="List applications for a file")
    reveal = cmds.add_parser("reveal", help="Select a file in Nautilus")
    for action in (op, apps, reveal):
        action.add_argument("path", help="File path, or a result ID with --source")
        action.add_argument("--source", help="Resolve a result ID from this source")
    return p


def coverage(db, errors):
    result = []
    for root in store.roots(db):
        counts = dict(db.execute("SELECT state,count(*) FROM files WHERE root=? GROUP BY state",
                                 (root["id"],)))
        row = dict(path=root["path"], available=store.available(root), scanned=root["scanned"],
                   problem=root["problem"], files=sum(counts.values()), states=counts)
        if errors:
            row["errors"] = [dict(r) for r in db.execute("SELECT rel,error FROM files WHERE root=? "
                                                        "AND error IS NOT NULL", (root["id"],))]
        result.append(row)
    return result


def execute(args, db, directory):
    if args.command == "roots":
        if args.action == "add":
            with store.index_lock(directory):
                for path in args.folders:
                    print(store.add_root(db, path, directory))
        else:
            for root in store.roots(db):
                print(root["path"])
    elif args.command == "sources":
        if args.action == "list":
            print("files")
            for name, program in sources.registered(directory).items():
                print(f"{name}  {program}")
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
                                        paths=files, progress=lambda s: print(s, file=sys.stderr, flush=True))
        print(f"{count:,} documents processed, {failures:,} problems", file=sys.stderr)
        return int(bool(failures))
    elif args.command == "status":
        rows = coverage(db, args.errors)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        for row in rows:
            states = ", ".join(f"{n:,} {k}" for k, n in row["states"].items())
            print(f"{row['path']}   {row['files']:,} files   {states}")
            if not row["available"] or row["problem"]:
                print("  " + (row["problem"] or "Folder unavailable"))
            for e in row.get("errors", []):
                print(f"  {e['rel']}: {e['error']}")
    elif args.command == "search":
        opts = dict(root=args.root, fmt=args.fmt, exact=args.exact, limit=args.limit, source=args.source, where=args.where)
        query = " ".join(args.query)
        if not args.json and not args.list and sys.stdin.isatty() and sys.stdout.isatty():
            curses.wrapper(picker, db, directory, query, opts)
        else:
            hits, problem = [], ""
            def collect(rows, message):
                nonlocal hits, problem
                hits, problem = rows, message
            sources.find(db, directory, query, opts, collect)
            if problem:
                print(problem, file=sys.stderr)
            if args.json:
                print(json.dumps(hits, ensure_ascii=False, indent=2))
            else:
                for hit in hits:
                    if hit["source"] == "files":
                        print(hit["path"])
                    else:
                        print(f"[{hit['source']}] {hit['name']}  ({hit['availability'] or hit['match']}, {hit['id']})")
                    if hit["snippet"]:
                        location = f"p. {hit['page']}  " if hit["page"] else ""
                        print("  " + location + hit["snippet"])
            return int(bool(problem) and not hits)
    return 0


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.command is None:
        args = p.parse_args(["--data-dir", str(args.data_dir), "search"])
    try:
        directory = args.data_dir.expanduser().resolve()
        if args.command in {"open", "apps", "reveal"}:
            target = sources.resolve(directory, args.source, args.path)
            if "uri" in target:
                if args.command != "open" or args.app:
                    raise ValueError("This result has a citation link. Use open to view it.")
                desktop.open_uri(target["uri"])
            elif args.command == "open":
                desktop.launch(target["path"], args.app)
            elif args.command == "apps":
                print("\n".join(desktop.applications(target["path"])))
            else:
                desktop.reveal(target["path"])
        else:
            with closing(store.connect(directory)) as db:
                return execute(args, db, directory)
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Completed documents and OCR pages have been saved.", file=sys.stderr)
        return 130
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError, curses.error) as exc:
        print(f"shelffind: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
