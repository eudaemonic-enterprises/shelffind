# Copyright 2026 Eudaemonic Enterprises Inc.
"""Read document text locally. The original document is always opened read-only."""
import gzip
import posixpath
import resource
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree
from zipfile import ZipFile

SUPPORTED = {"pdf", "djvu", "ps", "ps.gz", "epub", "txt", "md", "rst", "tex"}
PLAIN = {"txt", "md", "rst", "tex"}     # read as they are, up to MAX_TEXT
MAX_TEXT = 64 * 1024 * 1024


def file_format(path):
    if path.name.lower().endswith(".ps.gz"):
        return "ps.gz"
    suffix = path.suffix.lower().lstrip(".")
    return "djvu" if suffix == "djv" else suffix


def tool_limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, (512 << 20, 512 << 20))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run_tool(argv, work):
    with tempfile.TemporaryFile(dir=work) as out, tempfile.TemporaryFile(dir=work) as err:
        try:
            result = subprocess.run(argv, cwd=work, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                    timeout=240, preexec_fn=tool_limits, check=False)
        except FileNotFoundError:
            raise ValueError(f"Required program is missing: {argv[0]}") from None
        if result.returncode:
            err.seek(0)
            detail = err.read(900).decode("utf-8", "replace").strip()
            raise ValueError(f"{argv[0]} exited with {result.returncode}: {detail}")
        if out.tell() > MAX_TEXT:
            raise ValueError(f"Text output from {argv[0]} exceeds 64 MiB")
        out.seek(0)
        return out.read().decode("utf-8", "replace")


@dataclass
class Passage:
    page: int | None
    section: str | None
    text: str
    needs_ocr: bool = False


class ChapterText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li", "td"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def epub_pages(path):
    total = 0
    with ZipFile(path) as archive:
        def read(name):
            nonlocal total
            entry = archive.getinfo(name)
            total += entry.file_size
            if total > MAX_TEXT:
                raise ValueError("EPUB text exceeds 64 MiB")
            return archive.read(entry)
        container = ElementTree.fromstring(read("META-INF/container.xml"))
        package = container.find(".//{*}rootfile")
        if package is None:
            raise ValueError("EPUB package is missing")
        opf = package.attrib["full-path"]
        book = ElementTree.fromstring(read(opf))
        items = {item.attrib["id"]: item for item in book.findall(".//{*}manifest/{*}item")}
        for ref in book.findall(".//{*}spine/{*}itemref"):
            item = items[ref.attrib["idref"]]
            if item.get("media-type") not in {"application/xhtml+xml", "text/html"}:
                continue
            name = posixpath.normpath(posixpath.join(posixpath.dirname(opf),
                                                     unquote(item.attrib["href"].split("#")[0])))
            parser = ChapterText()
            parser.feed(read(name).decode("utf-8-sig", "replace"))
            parser.close()
            yield Passage(None, name, "".join(parser.parts))


def in_several_files(path):
    """True for a DjVu document whose pages are kept in other files beside the one named."""
    with path.open("rb") as source:
        head = source.read(25)
    return head[:8] == b"AT&TFORM" and head[12:20] == b"DJVMDIRM" and len(head) == 25 and not head[24] & 0x80


@contextmanager
def document(path, fmt, hold=None):
    """A document ready to be read, as (file, format, folder for what the reading writes).
    That folder is made in `hold`, or among the temporary files of the system, and is removed
    afterwards; that it cannot be removed is not a fault of the document."""
    if fmt == "djvu" and in_several_files(path):
        # Its text is read from a copy of the one file, which the others would not follow.
        raise ValueError("A DjVu document kept in several files is not read: make one file of it with `djvmcvt -b`")
    with tempfile.TemporaryDirectory(prefix="shelffind-read-", dir=hold, ignore_cleanup_errors=True) as tmp:
        work = Path(tmp)
        if fmt in {"ps", "ps.gz"}:
            if fmt == "ps.gz":
                unpacked = work / "input.ps"
                with gzip.open(path, "rb") as src, unpacked.open("wb") as dest:
                    while data := src.read(1024 * 1024):
                        if dest.tell() + len(data) > 512 << 20:
                            raise ValueError("Uncompressed PostScript exceeds 512 MiB")
                        dest.write(data)
                path = unpacked
            pdf = work / "converted.pdf"
            run_tool(["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
                      f"-sOutputFile={pdf}", "-f", str(path)], work)
            path, fmt = pdf, "pdf"
        yield path, fmt, work


def passages(path, fmt, work):
    if fmt == "epub":
        yield from epub_pages(path)
    elif fmt in {"pdf", "djvu"}:
        if fmt == "pdf":
            raw = run_tool(["pdftotext", "-enc", "UTF-8", str(path), "-"], work)
        else:
            raw = run_tool(["djvutxt", str(path)], work)
        pages = raw.split("\f")
        if pages and not pages[-1].strip():
            pages.pop()
        if fmt == "djvu":
            count = int(run_tool(["djvused", str(path), "-e", "n"], work).strip())
            if count != len(pages):
                pages = [run_tool(["djvutxt", f"--page={n}", str(path)], work).strip("\f")
                         for n in range(1, count + 1)]
        for number, body in enumerate(pages, 1):
            yield Passage(number, None, body, not any(c.isalnum() for c in body))
    else:
        with path.open("rb") as source:
            data = source.read(MAX_TEXT + 1)
        if len(data) > MAX_TEXT:
            raise ValueError("Plain text exceeds 64 MiB")
        yield Passage(None, None, data.decode("utf-8-sig", "replace"))


def recognize(path, fmt, work, number, languages):
    if fmt == "djvu":
        image = work / "page.pgm"
        run_tool(["ddjvu", f"-page={number}", "-size=2200x3200", "-format=pgm",
                  str(path), str(image)], work)
    else:
        image = work / "page.png"
        run_tool(["pdftoppm", "-f", str(number), "-l", str(number), "-scale-to", "2600",
                  "-png", "-singlefile", str(path), str(work / "page")], work)
    return run_tool(["tesseract", str(image), "stdout", "-l", languages], work)
