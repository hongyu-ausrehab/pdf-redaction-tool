import contextlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pymupdf

from pdf_redact import cli, core, web
from test_core import make_doc


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.src = self.dir / "in.pdf"
        doc = make_doc()
        doc.set_metadata({"author": "John Smith"})
        doc.save(self.src)

    def text(self, path):
        return pymupdf.open(path)[0].get_text()

    def test_terms_regex_replace_and_box(self):
        dst = self.dir / "out.pdf"
        code, out, err = run_cli("redact", self.src, dst, "-t", "john smith", "-r", r"\d{3}-1234",
                                 "--replace", "555-9876", "000-0000", "-b", "1:30,250,100,260")
        self.assertEqual((code, err), (0, ""))
        text = self.text(dst)
        self.assertNotIn("Smith\n", text.split("Email")[0])
        self.assertNotIn("555-1234", text)
        self.assertIn("000-0000", text)
        self.assertFalse(pymupdf.open(dst).metadata.get("author"))

    def test_default_output_name_and_dry_run(self):
        code, out, _ = run_cli("redact", self.src, "-t", "Smith", "-n")
        self.assertEqual(code, 0)
        self.assertIn("2 region(s)", out)  # "Smith" and "smith" in the email
        self.assertFalse((self.dir / "in-redacted.pdf").exists())
        code, out, _ = run_cli("redact", self.src, "-t", "Smith")
        self.assertEqual(code, 0)
        self.assertNotIn("mith", self.text(self.dir / "in-redacted.pdf"))

    def test_ops_file_and_pages(self):
        ops = self.dir / "ops.json"
        ops.write_text(json.dumps([{"page": 0, "rect": [0, 40, 400, 64], "kind": "replace", "text": "Name: [removed]"}]))
        code, _, err = run_cli("redact", self.src, self.dir / "o.pdf", "--ops", ops, "--pages", "1")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Name: [removed]", self.text(self.dir / "o.pdf"))

    def test_leak_in_bookmark_gives_exit_code_2(self):
        doc = pymupdf.open(self.src)
        doc.set_toc([[1, "John Smith", 1]])
        doc.save(self.dir / "toc.pdf")
        code, _, err = run_cli("redact", self.dir / "toc.pdf", self.dir / "o.pdf", "-t", "John Smith")
        self.assertEqual(code, 2)
        self.assertIn("bookmark", err)

    def test_color(self):
        dst = self.dir / "o.pdf"
        code, _, err = run_cli("redact", self.src, dst, "-b", "1:30,40,200,64", "--color", "#1f4e9c")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(pymupdf.open(dst)[0].get_pixmap().pixel(100, 52)[:3], (0x1F, 0x4E, 0x9C))
        code, _, err = run_cli("redact", self.src, dst, "-t", "John", "--color", "blue")
        self.assertEqual(code, 1)
        self.assertIn("bad colour", err)

    def test_password_protected_input(self):
        locked = self.dir / "locked.pdf"
        make_doc().save(locked, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
        code, _, err = run_cli("redact", locked, self.dir / "o.pdf", "-t", "John")
        self.assertEqual(code, 1)
        self.assertIn("password", err)
        code, _, err = run_cli("redact", locked, self.dir / "o.pdf", "-t", "John", "--password", "pw")
        self.assertEqual((code, err), (0, ""))
        out = pymupdf.open(self.dir / "o.pdf")
        self.assertFalse(out.needs_pass)  # output is written unencrypted
        self.assertNotIn("John", out[0].get_text())

    def test_errors(self):
        (self.dir / "bad.pdf").write_bytes(b"not a pdf")
        for argv, msg in [(("redact", self.dir / "bad.pdf", "-t", "x"), "cannot open"),
                          (("redact", self.src, self.src, "-t", "x"), "must differ"),
                          (("redact", self.src, self.dir / "o.pdf", "-r", "("), "bad regex"),
                          (("redact", self.src, self.dir / "o.pdf", "--pages", "9", "-t", "x"), "out of range"),
                          (("redact", self.src, self.dir / "o.pdf", "-b", "1:1,2,3"), "")]:
            with self.subTest(argv=argv):
                if argv[-1] == "1:1,2,3":
                    with self.assertRaises(SystemExit):  # argparse rejects the box spec
                        run_cli(*argv)
                    continue
                code, _, err = run_cli(*argv)
                self.assertEqual(code, 1)
                self.assertIn(msg, err)


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, path, body=None):
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data)) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_full_flow(self):
        status, _, page = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"PDF Redact", page)

        doc = make_doc(rotation=90)
        status, _, body = self.request("/api/open", doc.tobytes())
        info = json.loads(body)
        self.assertEqual(info["pages"], [[300.0, 400.0]])  # displayed size of the rotated page

        status, headers, png = self.request(f"/api/page?id={info['id']}&n=0&scale=1")
        self.assertEqual((status, headers["Content-Type"], png[:4]), (200, "image/png", b"\x89PNG"))

        status, _, body = self.request("/api/find", {"id": info["id"], "terms": ["John Smith"]})
        hits = json.loads(body)["hits"]
        self.assertEqual(len(hits), 1)
        ops = [{"page": 0, "rect": hits[0]["rects"][0], "kind": "blackout"},
               {"page": 0, "rect": [20, 300, 200, 330], "kind": "replace", "text": "Reviewed"}]
        status, headers, pdf = self.request("/api/apply", {"id": info["id"], "ops": ops, "scrub": True})
        self.assertEqual((status, headers["Content-Type"]), (200, "application/pdf"))
        self.assertEqual(json.loads(headers["X-Redact-Leaks"]), [])
        out = core.open_pdf(pdf)
        self.assertNotIn("John Smith", out[0].get_text())
        self.assertIn("Reviewed", out[0].get_text())

    def test_errors_are_json(self):
        for path, body, code in [("/api/open", b"garbage", 400), ("/api/page?id=nope&n=0", None, 400),
                                 ("/api/find", {"id": "nope"}, 400), ("/nowhere", None, 404),
                                 ("/api/apply", b"{not json", 400)]:
            with self.subTest(path=path):
                status, _, body = self.request(path, body)
                self.assertEqual(status, code)
                self.assertIn("error", json.loads(body))

    def test_bad_page_and_regex(self):
        _, _, body = self.request("/api/open", make_doc().tobytes())
        doc_id = json.loads(body)["id"]
        self.assertEqual(self.request(f"/api/page?id={doc_id}&n=5")[0], 400)
        status, _, body = self.request("/api/find", {"id": doc_id, "regexes": ["("]})
        self.assertEqual(status, 400)
        self.assertIn("bad regex", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
