"""Local browser editor: a stdlib HTTP server around core.

Uploaded PDFs are held in memory only, keyed by a random id; nothing is
written to disk. The server binds to 127.0.0.1 only.
"""

import json
import secrets
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pymupdf

from . import core

INDEX = Path(__file__).with_name("index.html")
MAX_UPLOAD = 200 * 1024 * 1024
MAX_DOCS = 10

_docs = OrderedDict()  # id -> (PDF bytes, opened Document for rendering / search)
# PyMuPDF is not thread-safe: all of its work happens under this lock.
_lock = threading.Lock()


def _get(doc_id):
    if doc_id not in _docs:
        raise core.RedactError("unknown document (server restarted?) - open the PDF again")
    return _docs[doc_id]


def index(q, body):
    return 200, INDEX.read_bytes(), "text/html; charset=utf-8"


def open_doc(q, body):
    doc = core.open_pdf(body)  # password-protected PDFs are rejected here
    doc_id = secrets.token_hex(8)
    _docs[doc_id] = (body, doc)
    while len(_docs) > MAX_DOCS:
        _docs.popitem(last=False)
    return 200, {"id": doc_id, "pages": [[p.rect.width, p.rect.height] for p in doc]}


def page(q, body):
    _, doc = _get(q["id"][0])
    n = int(q["n"][0])
    scale = min(max(float(q.get("scale", ["1.5"])[0]), 0.1), 6)
    if not 0 <= n < doc.page_count:
        raise core.RedactError("page out of range")
    return 200, doc[n].get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png"), "image/png"


def find(q, body):
    req = json.loads(body)
    _, doc = _get(req["id"])
    pat = core.pattern(req.get("terms", []), req.get("regexes", []), bool(req.get("case")))
    hits = core.find(doc, pat) if pat else []
    return 200, {"hits": [{"page": h.page, "text": h.text, "rects": h.rects} for h in hits]}


def _redacted(req):
    """(PDF bytes, notes, leaks) for a request's ops - shared by apply and preview."""
    doc = core.open_pdf(_get(req["id"])[0])  # a fresh copy: the cached one stays the original
    ops = [core.Op.from_dict(d) for d in req.get("ops", [])]
    notes = core.apply(doc, ops)
    # regions found by a search carry it: remove those terms from comments,
    # links, bookmarks and metadata too
    queries = {json.dumps(d["query"], sort_keys=True): d["query"] for d in req.get("ops", []) if d.get("query")}
    for q in queries.values():
        pat = core.pattern(q.get("terms", []), q.get("regexes", []), bool(q.get("case")))
        if pat:
            notes += core.scrub_matches(doc, pat, q.get("replace"))
    data = core.to_bytes(doc, scrub=bool(req.get("scrub", True)))
    return data, notes, core.verify(core.open_pdf(data), ops)


def apply(q, body):
    data, notes, leaks = _redacted(json.loads(body))
    return 200, data, "application/pdf", [("X-Redact-Leaks", json.dumps(leaks, ensure_ascii=True)),
                                          ("X-Redact-Notes", json.dumps(notes, ensure_ascii=True))]


def preview(q, body):
    """Like apply, but keep the result for /api/page instead of downloading it.
    One preview per document: each new one replaces the last."""
    req = json.loads(body)
    data, notes, leaks = _redacted(req)
    preview_id = req["id"] + "-preview"
    _docs[preview_id] = (data, core.open_pdf(data))
    return 200, {"id": preview_id, "notes": notes, "leaks": leaks}


ROUTES = {("GET", "/"): index, ("GET", "/api/page"): page,
          ("POST", "/api/open"): open_doc, ("POST", "/api/find"): find, ("POST", "/api/apply"): apply,
          ("POST", "/api/preview"): preview}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, ctype="application/json", headers=()):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        url = urlparse(self.path)
        route = ROUTES.get((method, url.path))
        if route is None:
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD:
                raise core.RedactError("file too large")
            body = self.rfile.read(n)  # read before taking the lock
            with _lock:
                result = route(parse_qs(url.query), body)
        except (core.RedactError, ValueError, KeyError, TypeError) as e:
            result = 400, {"error": str(e) or e.__class__.__name__}
        except Exception as e:  # keep the server alive; report to the page
            result = 500, {"error": f"{e.__class__.__name__}: {e}"}
        self._send(*result)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def serve(port=8765):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"PDF redact editor: open http://127.0.0.1:{server.server_port}/ in your browser  (Ctrl+C to stop)",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
