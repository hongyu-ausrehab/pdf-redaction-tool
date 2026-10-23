"""Forensic test: a term hidden in many layers of a PDF must be gone from the
whole file after redaction - not only from the visible page (DESIGN.md s.8)."""

import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pymupdf

from pdf_redact import core, web
from test_cli_web import run_cli

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("forensic_check", ROOT / "scripts" / "forensic_check.py")
forensic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(forensic)

S = "Zebra-4471"


def trap_pdf(path):
    """A PDF with S in 17 places (numbered as in DESIGN.md section 8)."""
    doc = pymupdf.open()
    page = doc.new_page(width=500, height=700)
    page.insert_text((40, 60), f"1 visible text: Account {S}", fontsize=11)
    page.insert_text((40, 90), f"2 invisible OCR-style text {S}", fontsize=11, render_mode=3)
    src = pymupdf.open()  # 3: S as image pixels, with an OCR layer that is 5pt off, as real OCR can be
    sp = src.new_page(width=200, height=30)
    sp.insert_text((5, 20), S, fontsize=14)
    page.insert_image(pymupdf.Rect(40, 100, 240, 130), pixmap=sp.get_pixmap(dpi=144))
    page.insert_text((40, 120), S, fontsize=14, render_mode=3)
    page.insert_text((0, -50), "x", fontname="helv")  # 4: /ActualText in marked content
    x = doc.get_new_xref()
    doc.update_object(x, "<<>>")
    doc.update_stream(x, f"/Span <</ActualText ({S})>> BDC BT /helv 11 Tf 1 0 0 1 40 550 Tm "
                         f"(4 actualtext: {S}) Tj ET EMC".encode())
    page.wrap_contents()
    doc.xref_set_key(page.xref, "Contents", "[" + " ".join(f"{c} 0 R" for c in page.get_contents() + [x]) + "]")
    page.insert_text((40, 180), f"5 hidden layer {S}", fontsize=11, oc=doc.add_ocg("Hidden", on=False))
    xo = pymupdf.open()  # 6: inside a form XObject
    xo.new_page(width=300, height=30).insert_text((5, 20), f"6 xobject {S}", fontsize=11)
    page.show_pdf_page(pymupdf.Rect(40, 195, 340, 225), xo, 0)
    doc.xref_set_key(page.xref, "PieceInfo", f"<</Illustrator <</Private ({S})>>>>")  # 7
    page.add_text_annot((60, 55), f"8a comment by the text {S}")  # 8
    page.add_text_annot((400, 600), f"8b comment elsewhere {S}")
    w = pymupdf.Widget()  # 9: a form field elsewhere
    w.field_type, w.field_name, w.rect, w.field_value = pymupdf.PDF_WIDGET_TYPE_TEXT, "acct", pymupdf.Rect(40, 600, 240, 620), S
    page.add_widget(w)
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(300, 640, 400, 660), "uri": f"https://x.test/?a={S}"})
    doc.set_metadata({"subject": f"about {S}"})  # 11
    doc.set_toc([[1, f"Account {S}", 1]])  # 12
    doc.embfile_add("notes.txt", f"secret {S}".encode())  # 13
    cat = doc.pdf_catalog()
    doc.xref_set_key(cat, "OpenAction", f"<</S/JavaScript/JS (app.alert('{S}'))>>")  # 14
    elem = doc.get_new_xref()  # 16: structure-tree alternative text
    doc.update_object(elem, f"<</S/Figure/P {cat} 0 R/Pg {page.xref} 0 R/Alt ({S})>>")
    doc.xref_set_key(cat, "StructTreeRoot", f"<</Type/StructTreeRoot/K {elem} 0 R>>")
    thumb = page.get_pixmap(dpi=36)  # 17: a page thumbnail - a picture of the unredacted page
    x = doc.get_new_xref()
    doc.update_object(x, f"<</Type/XObject/Subtype/Image/Width {thumb.width}/Height {thumb.height}"
                         "/ColorSpace/DeviceRGB/BitsPerComponent 8>>")
    doc.update_stream(x, thumb.samples)
    doc.xref_set_key(page.xref, "Thumb", f"{x} 0 R")
    doc.save(path)
    for keywords in (f"kw {S}", "none"):  # 15: an older revision, kept by incremental saving
        d = pymupdf.open(path)
        d.set_metadata({**d.metadata, "keywords": keywords})
        d.saveIncr()


class ForensicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.src = cls.dir / "trap.pdf"
        trap_pdf(cls.src)

    def assert_clean(self, path):
        self.assertEqual(forensic.scan(str(path), [S]), [])
        doc = pymupdf.open(path)
        pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.Pixmap(doc, doc[0].get_images(full=True)[0][0]))
        self.assertEqual(sum(v < 128 for v in pix.samples), 0)  # the picture of S is gone too
        self.assertEqual([v["on"] for v in doc.get_ocgs().values()], [False])  # layer still hidden
        self.assertEqual(doc.xref_get_key(doc[0].xref, "Thumb")[0], "null")  # no picture of the old page

    def test_scrub_off_still_wipes_the_term(self):
        # scrub only cleans document-wide extras; what was under / about the redaction goes regardless
        doc = core.open_pdf(str(self.src))
        pat = core.pattern([S])
        core.apply(doc, core.hits_to_ops(core.find(doc, pat)))
        core.scrub_matches(doc, pat)
        out = self.dir / "scrub-off.pdf"
        out.write_bytes(core.to_bytes(doc, scrub=False))
        self.assert_clean(out)

    def test_trap_file_hides_the_term_everywhere(self):
        self.assertGreaterEqual(len(forensic.scan(str(self.src), [S])), 25)

    def test_library_blackout_and_replace(self):
        for mode, new in (("blackout", None), ("replace", "REPLACED")):
            with self.subTest(mode=mode):
                doc = core.open_pdf(str(self.src))
                pat = core.pattern([S])
                core.apply(doc, core.hits_to_ops(core.find(doc, pat), mode, new or ""))
                core.scrub_matches(doc, pat, new)
                out = self.dir / f"lib-{mode}.pdf"
                out.write_bytes(core.to_bytes(doc))
                self.assert_clean(out)
                if new:  # the hidden layer must not have become visible text
                    self.assertNotIn("hidden layer", pymupdf.open(out)[0].get_text())

    def test_cli(self):
        out = self.dir / "cli.pdf"
        code, _, err = run_cli("redact", self.src, out, "-t", S)
        self.assertEqual((code, err), (0, ""))
        self.assert_clean(out)

    def test_web_apply(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            post = lambda path, data: urllib.request.urlopen(urllib.request.Request(base + path, data=data)).read()
            doc_id = json.loads(post("/api/open", self.src.read_bytes()))["id"]
            hits = json.loads(post("/api/find", json.dumps({"id": doc_id, "terms": [S]}).encode()))["hits"]
            query = {"terms": [S], "regexes": [], "case": False, "replace": None}  # as the editor sends it
            ops = [{"page": h["page"], "rect": r, "kind": "blackout", "query": query} for h in hits for r in h["rects"]]
            out = self.dir / "web.pdf"
            out.write_bytes(post("/api/apply", json.dumps({"id": doc_id, "ops": ops, "scrub": True}).encode()))
            self.assert_clean(out)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
