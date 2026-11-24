# Copyright 2026 Eudaemonic Enterprises Inc.
"""Read document text locally. The original document is always opened read-only."""
import gzip
import os
import posixpath
import re
import resource
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree
from zipfile import ZipFile

SUPPORTED = {"pdf", "djvu", "ps", "ps.gz", "epub", "txt", "md", "rst", "tex", "typ", "bib",
             "html", "htm", "docx", "odt", "fb2"}
PLAIN = {"txt", "md", "rst", "tex", "typ", "bib"}     # read as they are, up to MAX_TEXT
CHUNK = 6000            # characters of a passage of a document that has no pages
# Formats read by a program of the user's choice (see `declare`): format -> its command, in which
# {path} stands for the document and which prints the text.
CONVERTERS = {}


def threads(wanted):
    """How many programs may run at once: as many as wanted where `prlimit` limits them, and
    one where a hook in each new process would do it, which Python does not allow among threads."""
    return wanted if shutil.which("prlimit") else 1


def declare(converters):
    """Take the [convert.FORMAT] tables of the settings: command = ["program", "{path}"]."""
    CONVERTERS.clear()
    for name, table in converters.items():
        CONVERTERS[name.lower().lstrip(".")] = list(table["command"])


def formats():
    """Every format that is read: those that ShelfFind reads itself and those of a converter."""
    return SUPPORTED | CONVERTERS.keys()
MAX_TEXT = 64 * 1024 * 1024
PAGES = 4               # pages of one document read at the same time when a program must be asked for each


def file_format(path):
    if path.name.lower().endswith(".ps.gz"):
        return "ps.gz"
    suffix = path.suffix.lower().lstrip(".")
    return "djvu" if suffix == "djv" else suffix


def tool_limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, (512 << 20, 512 << 20))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run_tool(argv, work):
    """Run a program on a document and return what it printed. Its output file and its dumps of
    memory are limited: by `prlimit` where it exists, which leaves the program free to run several
    at once, and otherwise by a hook that runs in the new process."""
    found = shutil.which(argv[0])
    if found is None:
        raise ValueError(f"Required program is missing: {argv[0]}")
    argv = [os.path.abspath(found), *argv[1:]] if os.sep in argv[0] else argv    # the program runs in another folder
    limit = shutil.which("prlimit")
    command = [limit, f"--fsize={512 << 20}", "--core=0", "--", *argv] if limit else argv
    # Tesseract works with as many threads as it finds processors; the pages are read in parallel here.
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"} if argv[0] == "tesseract" else None
    with tempfile.TemporaryFile(dir=work) as out, tempfile.TemporaryFile(dir=work) as err:
        try:
            result = subprocess.run(command, cwd=work, stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env,
                                    timeout=240, preexec_fn=None if limit else tool_limits, check=False)
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


BLOCKS = frozenset("p div br hr li ul ol dl dt dd h1 h2 h3 h4 h5 h6 tr th td table thead tbody tfoot caption blockquote "
                   "pre section article header footer nav aside main figure figcaption address form fieldset details "
                   "summary".split())     # where a line ends: the words on either side are not one word
HIDDEN = frozenset({"script", "style", "template", "noscript"})     # what is not text of the page
IN_HEAD = frozenset({"title", "meta", "link", "base", "script", "style", "noscript", "template"})


class ChapterText(HTMLParser):
    """The text of an HTML page or of a chapter of an EPUB, in lines: its scripts, styles, templates
    and metadata are left out, and its title is kept. The head ends at its end tag, or where a tag
    that cannot be in a head begins, since the end tag may be omitted."""

    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0
        self.head = False
        self.title = False

    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self.head = True
        elif self.head and tag not in IN_HEAD:
            self.head = False
        if tag == "title":
            self.title = True
        if tag in HIDDEN:
            self.hidden += 1
        if tag in BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "head":
            self.head = False
        if tag == "title":
            self.title = False
            self.parts.append("\n")
        if tag in HIDDEN:
            self.hidden = max(0, self.hidden - 1)
        if tag in BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden and (not self.head or self.title):
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


def pieces(paragraphs, label, numbered=True):
    """Passages of about CHUNK characters made of whole paragraphs, for a document without pages;
    the section of each is `label` and its number, or with `numbered` false the label alone for
    the first and the label with the number for the others."""
    block, size, number = [], 0, 0

    def named(n):
        return f"{label} {n}" if numbered else (label if n == 1 else f"{label} ({n})")
    for paragraph in paragraphs:
        block.append(paragraph)
        size += len(paragraph) + 1
        if size >= CHUNK:
            number += 1
            yield Passage(None, named(number), "\n".join(block))
            block, size = [], 0
    if block or not number:
        yield Passage(None, named(number + 1), "\n".join(block))


def member(archive, name, budget):
    """A file of a zip archive, given its size to a budget of what may be unpacked in all."""
    entry = archive.getinfo(name)
    budget[0] += entry.file_size
    if budget[0] > MAX_TEXT:
        raise ValueError("The text of the document exceeds 64 MiB")
    return archive.read(entry)


BLANKS = frozenset({"s", "tab", "br", "line-break"})             # what a word processor writes for a blank
UNSEEN = frozenset({"delText", "instrText", "Fallback", "note-body", "note-citation"})    # deleted words, field codes,
                                                                    # the copy of a text box, a note, which is its own passage
MAX_XML = 32 << 20      # bytes of XML of a word processor's file or of FB2 that are read: a tree takes ten times that


def local(element):
    return element.tag.rpartition("}")[2]


def text_of(element, names, top=True):
    """The text of a paragraph, in reading order. What is not seen on the page (deleted words, field
    codes), the second copy of a text box, and the paragraphs and notes within it, which are passages of
    their own, give a blank; a space, a tab or a line break of a word processor's file is a blank."""
    name = local(element)
    if not top and (name in names or name in UNSEEN):
        yield " "
        return
    if name in BLANKS:
        yield " "
    elif element.text:
        yield element.text
    for child in element:
        yield from text_of(child, names, False)
        if child.tail:
            yield child.tail


def paragraphs_of(root, names):
    """The paragraphs of a word processor's file, each once, in the order of the file."""
    if local(root) == "Fallback":
        return
    if local(root) in names:
        text = " ".join("".join(text_of(root, names)).split())
        if text:
            yield text
    for child in root:
        yield from paragraphs_of(child, names)


def html_text(data):
    """The text of an HTML page, from bytes of an encoding that it names, or UTF-8, or Latin-1."""
    head = data[:2048].decode("ascii", "replace").lower()
    charset = re.search(r"charset\s*=\s*[\"']?([a-z0-9_-]+)", head)
    for name in ([charset.group(1)] if charset else []) + ["utf-8-sig"]:
        try:
            text = data.decode(name)
            break
        except (LookupError, UnicodeDecodeError):
            continue
    else:
        text = data.decode("latin-1")
    parser = ChapterText()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)


def xml_member(archive, name, budget):
    entry = archive.getinfo(name)
    if entry.file_size > MAX_XML:
        raise ValueError("The XML of the document exceeds 32 MiB")
    return member(archive, name, budget)


def office_pages(path, fmt):
    with ZipFile(path) as archive:
        budget = [0]
        if fmt == "docx":
            root = ElementTree.fromstring(xml_member(archive, "word/document.xml", budget))
            names = {"p"}
        else:
            root = ElementTree.fromstring(xml_member(archive, "content.xml", budget))
            names = {"p", "h"}
        yield from pieces(paragraphs_of(root, names), "part")


def fb2_pages(path):
    if path.stat().st_size > MAX_XML:
        raise ValueError("The XML of the document exceeds 32 MiB")
    root = ElementTree.fromstring(path.read_bytes())
    for body in root.iter():
        if local(body) != "body":
            continue
        # What stands in the body outside its sections (a title, an epigraph) comes first.
        front = [child for child in body if local(child) != "section"]
        lines = [text for child in front for text in paragraphs_of(child, {"p", "v", "text-author"})]
        if lines:
            yield from pieces(lines, "front", numbered=False)
        for number, section in enumerate(body.findall("{*}section") or [], 1):
            title = next((" ".join(paragraphs_of(t, {"p", "v"})) for t in section.iter() if local(t) == "title"), "")
            yield from pieces(paragraphs_of(section, {"p", "v", "text-author"}), title or f"section {number}", numbered=False)


def converted(path, fmt, work):
    command = [word.replace("{path}", str(path)) for word in CONVERTERS[fmt]]
    return pieces((line.strip() for line in run_tool(command, work).splitlines() if line.strip()), "part")


def passages(path, fmt, work):
    if fmt in CONVERTERS and fmt not in SUPPORTED:
        yield from converted(path, fmt, work)
    elif fmt == "epub":
        yield from epub_pages(path)
    elif fmt in {"docx", "odt"}:
        yield from office_pages(path, fmt)
    elif fmt == "fb2":
        yield from fb2_pages(path)
    elif fmt in {"html", "htm"}:
        if path.stat().st_size > MAX_TEXT:
            raise ValueError("HTML exceeds 64 MiB")
        text = html_text(path.read_bytes())
        yield from pieces((line.strip() for line in text.splitlines() if line.strip()), "part")
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
                # The text of the whole document does not divide into its pages: each is asked for,
                # a few at once, and they come back in the order of the pages.
                with ThreadPoolExecutor(threads(PAGES)) as pool:
                    pages = list(pool.map(lambda n: run_tool(["djvutxt", f"--page={n}", str(path)], work).strip("\f"),
                                          range(1, count + 1)))
        for number, body in enumerate(pages, 1):
            yield Passage(number, None, body, not any(c.isalnum() for c in body))
    elif fmt in PLAIN:
        with path.open("rb") as source:
            data = source.read(MAX_TEXT + 1)
        if len(data) > MAX_TEXT:
            raise ValueError("Plain text exceeds 64 MiB")
        yield Passage(None, None, data.decode("utf-8-sig", "replace"))
    else:
        raise ValueError(f"No program reads the format {fmt}: name one under [convert.{fmt}]")


def recognize(path, fmt, work, number, languages):
    """The text of one page, recognised from its image. Each page has an image of its own in
    `work`, so that several pages can be recognised at once."""
    if fmt == "djvu":
        image = work / f"page{number}.pgm"
        run_tool(["ddjvu", f"-page={number}", "-size=2200x3200", "-format=pgm",
                  str(path), str(image)], work)
    else:
        image = work / f"page{number}.png"
        run_tool(["pdftoppm", "-f", str(number), "-l", str(number), "-scale-to", "2600",
                  "-png", "-singlefile", str(path), str(work / f"page{number}")], work)
    try:
        return run_tool(["tesseract", str(image), "stdout", "-l", languages], work)
    finally:
        image.unlink(missing_ok=True)
