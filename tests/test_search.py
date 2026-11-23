# Copyright 2026 Eudaemonic Enterprises Inc.
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from shelffind import desktop, indexer, sources, store
from shelffind.cli import main
from shelffind.search import search


class Collection(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "Reading Room"
        self.root.mkdir()
        self.data = self.base / "state"
        self.db = store.connect(self.data)
        self.addCleanup(self.db.close)
        store.add_root(self.db, self.root, self.data)

    def build(self, **opts):
        return indexer.build(self.db, self.data, store.roots(self.db), progress=lambda _:None, **opts)

    def test_fuzzy_filename_content_and_filters(self):
        path = self.root / "OrchardCalendar.txt"
        path.write_text("A telescope reveals the ultraviolet horizon.")
        (self.root / "OrchardCalendar.bin").write_bytes(b"data")
        self.assertEqual(self.build(), (1,0))
        for q in ("orchadr", "orclnd", "calendr", "ultravoilet"):
            self.assertEqual(search(self.db, q, fmt="txt")[0]["name"], path.name, q)
        self.assertEqual(search(self.db, '"ultraviolet horizon"')[0]["match"], "passage")
        self.assertFalse(search(self.db, '"ultravoilet horizon"'))
        self.assertFalse(search(self.db, "ultravoilet", exact=True))
        self.assertEqual(self.build(), (0,0))
        path.write_text("Midnight chimneys")
        self.build()
        self.assertFalse(search(self.db, "ultraviolet"))
        self.assertEqual(len(search(self.db, "chimneys", root=self.root.name)), 1)

    def test_disconnect_and_interrupted_inventory(self):
        path = self.root / "portrait.txt"
        path.write_text("vermillion")
        self.build()
        with patch("shelffind.store.inventory", side_effect=OSError("stale mount")):
            self.assertEqual(self.build(), (0,1))
        hidden = self.root.rename(self.base / "offline")
        self.assertEqual(self.build(), (0,1))
        self.assertEqual(search(self.db,"vermillion")[0]["path"], str(path))
        hidden.rename(self.root)
        with patch("shelffind.indexer.store.available", return_value=False):
            self.assertEqual(self.build(), (0,1))
        path.unlink()
        self.build()
        self.assertFalse(search(self.db,"vermillion"))

    def test_resume_document_queue_and_missing_program(self):
        for n in range(3):
            (self.root / f"record{n}.txt").write_text(f"item {n}")
        self.assertEqual(self.build(limit=1), (1,0))
        self.assertEqual(self.build(), (2,0))
        (self.root / "bad.pdf").write_bytes(b"not a PDF")
        self.assertEqual(self.build(), (1,1))
        self.assertEqual(self.build(), (0,0))
        self.assertEqual(self.build(retry=True), (1,1))
        self.assertTrue(search(self.db, "bad.pdf"))

    def test_epub_spine_and_plain_json_cli(self):
        path = self.root / "tour.epub"
        with ZipFile(path, "w") as z:
            z.writestr("META-INF/container.xml", '<container><rootfile full-path="O/book.opf"/></container>')
            z.writestr("O/book.opf", '<package><manifest><item id="one" href="part.xhtml" '
                       'media-type="application/xhtml+xml"/></manifest><spine><itemref idref="one"/></spine></package>')
            z.writestr("O/part.xhtml", "<html><body><p>Clockwork orchids</p></body></html>")
        self.assertEqual(self.build(), (1,0))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["--data-dir",str(self.data),"search","--json","orchids"]),0)
        result = json.loads(out.getvalue())[0]
        self.assertEqual((result["page"],result["section"],result["source"]), (None,"O/part.xhtml","files"))

    @unittest.skipUnless(all(shutil.which(p) for p in ["gs","pdftotext","djvused","djvm","cjb2"]),
                         "Poppler, Ghostscript and DjVuLibre required")
    def test_real_pdf_djvu_and_postscript_pages(self):
        ps = self.root / "sample.ps"
        ps.write_text("%!PS-Adobe-3.0\n/Helvetica findfont 20 scalefont setfont\n"
                      "40 700 moveto (Solar winds) show showpage\n40 700 moveto (Brass compass) show showpage\n")
        pdf = self.root / "photo.pdf"
        subprocess.run(["gs","-q","-dSAFER","-dBATCH","-dNOPAUSE","-sDEVICE=pdfwrite",
                        f"-sOutputFile={pdf}","-f",str(ps)],check=True)
        import gzip
        (self.root / "copy.ps.gz").write_bytes(gzip.compress(ps.read_bytes()))
        pbm = self.base / "blank.pbm"
        pbm.write_bytes(b"P4\n80 60\n" + bytes(600))
        pages = []
        for n, body in enumerate(["Scarlet garden","Midnight willow"],1):
            djvu = self.base / f"part{n}.djvu"
            subprocess.run(["cjb2",str(pbm),str(djvu)],check=True)
            text = self.base / "text.dsed"
            text.write_text(f'(page 0 0 80 60 "{body}")')
            subprocess.run(["djvused",str(djvu),"-e",f"select 1; set-txt {text}; save"],check=True)
            pages.append(str(djvu))
        subprocess.run(["djvm","-c",str(self.root/"diagram.djvu"),*pages],check=True)
        self.assertEqual(self.build(),(4,0))
        for fmt in ("pdf","ps","ps.gz"):
            self.assertEqual(search(self.db,'"brass compass"',fmt=fmt)[0]["page"],2)
        self.assertEqual(search(self.db,"willow",fmt="djvu")[0]["page"],2)

    @unittest.skipUnless(all(shutil.which(p) for p in ("gs","pdftoppm","pdftotext","tesseract")),
                         "Poppler, Ghostscript and Tesseract required")
    def test_resumable_ocr(self):
        ps = self.base / "outlined.ps"
        ps.write_text("%!PS\n/Helvetica findfont 40 scalefont setfont\n"
                      "40 600 moveto (Sapphire meadow) true charpath fill showpage\n" * 2)
        pdf = self.root / "scan.pdf"
        subprocess.run(["gs","-q","-dSAFER","-dBATCH","-dNOPAUSE","-sDEVICE=pdfwrite",
                        f"-sOutputFile={pdf}",str(ps)],check=True)
        self.assertEqual(self.build(),(1,0))
        self.assertEqual(self.db.execute("SELECT state FROM files").fetchone()[0], "needs-ocr")
        from shelffind.extract import recognize
        def interrupt(path,fmt,work,number,lang):
            if number == 2:
                raise KeyboardInterrupt()
            return recognize(path,fmt,work,number,lang)
        with patch("shelffind.extract.recognize",side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.build(ocr=True)
        self.assertEqual(self.db.execute("SELECT sum(needs_ocr) FROM pages").fetchone()[0],1)
        self.assertEqual(self.build(ocr=True),(1,0))
        self.assertTrue(search(self.db, '"sapphire meadow"'))
        self.assertEqual(self.build(ocr=True),(0,0))

    def test_desktop_arguments(self):
        doc = self.root / "a 'quote' & a $ sign.pdf"
        doc.touch()
        with patch("shelffind.desktop.subprocess.run") as run:
            run.return_value.returncode = 0
            desktop.launch(doc)
            self.assertEqual(run.call_args.args[0], ["gio","open",doc.as_uri()])
            with patch("shelffind.desktop.applications", return_value=["reader.desktop"]):
                desktop.launch(doc,"reader.desktop")
                self.assertEqual(run.call_args.args[0], ["gtk-launch","reader.desktop",doc.as_uri()])
                with self.assertRaises(ValueError):
                    desktop.launch(doc,"unknown.desktop")
            desktop.open_uri("https://example.org/record")
            self.assertEqual(run.call_args.args[0], ["gio","open","https://example.org/record"])
        with patch("shelffind.desktop.subprocess.Popen") as popen:
            popen.return_value.wait.return_value = 0
            desktop.reveal(doc)
            self.assertEqual(popen.call_args.args[0], ["nautilus","--select",doc.as_uri()])

    def test_viewer_output_stays_out_of_terminal_and_launcher_errors_are_reported(self):
        doc = self.root / "article.pdf"
        doc.touch()
        launcher = self.base / "gio"
        launcher.write_text(
            "#!/bin/sh\n"
            'if [ "$TEST_LAUNCH_FAILURE" = 1 ]; then\n'
            "    echo 'No application accepted this file' >&2\n"
            "    exit 7\n"
            "fi\n"
            "echo 'Opening in existing session'\n"
            "(sleep 0.1; echo 'late viewer diagnostic' >&2) &\n"
        )
        launcher.chmod(0o700)
        env = {**os.environ, "PATH": str(self.base) + os.pathsep + os.environ["PATH"],
               "TEST_LAUNCH_FAILURE": "0"}
        argv = [sys.executable, "-m", "shelffind.cli", "--data-dir", str(self.data), "open", str(doc)]
        opened = subprocess.run(argv, env=env, check=False, capture_output=True, text=True, timeout=5)
        self.assertEqual((opened.returncode, opened.stdout, opened.stderr), (0, "", ""))

        env["TEST_LAUNCH_FAILURE"] = "1"
        failed = subprocess.run(argv, env=env, check=False, capture_output=True, text=True, timeout=5)
        self.assertEqual(failed.returncode, 1)
        self.assertIn("No application accepted this file", failed.stderr)
        self.assertIn("status 7", failed.stderr)

    def test_version_without_index_and_invalid_optional_configuration(self):
        fresh = self.base / "unopened"
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit_status:
            main(["--data-dir", str(fresh), "--version"])
        self.assertEqual(exit_status.exception.code, 0)
        self.assertIn("ShelfFind", output.getvalue())
        self.assertFalse(fresh.exists())
        (self.root / "compass.txt").write_text("direction")
        self.build()
        (self.data / "sources.json").write_text("{")
        observed = []
        opts = {"fmt": None, "root": None, "limit": 5, "exact": False}
        for source in ("files", None):
            sources.find(self.db, self.data, "compass", {**opts, "source": source},
                         lambda rows, message: observed.append((rows, message)))
            self.assertEqual(observed[-1][0][0]["name"], "compass.txt")
            self.assertEqual(bool(observed[-1][1]), source is None)

    def test_provider_federation_failure_and_resolution(self):
        doc = self.root / "orchid.txt"
        doc.write_text("local document")
        self.build()
        tool = self.base / "catalogue"
        tool.write_text("#!/usr/bin/env python3\nimport json,sys\nr=json.load(sys.stdin)\n"
                        "assert r['version']==1\nprint(json.dumps([{'id':'abc','name':'Other edition'}] "
                        "if r['op']=='search' else {'path':" + repr(str(doc)) + "}))\n")
        tool.chmod(0o700)
        sources.configure(self.data,"catalogue",tool)
        opts = {"source": None, "fmt": None, "root": None, "limit": 5, "exact": False}
        batches = []
        sources.find(self.db,self.data,"orchid",opts,lambda rows,msg:batches.append((rows,msg)))
        self.assertEqual([r["source"] for r in batches[-1][0]],["files","catalogue"])
        self.assertEqual(sources.resolve(self.data,"catalogue","abc")["path"],str(doc))
        tool.unlink()
        sources.find(self.db,self.data,"orchid",opts,lambda rows,msg:batches.append((rows,msg)))
        self.assertEqual(batches[-1][0][0]["source"],"files")
        self.assertIn("catalogue",batches[-1][1])
        sources.configure(self.data,"catalogue",None)
        self.assertFalse(sources.registered(self.data))
