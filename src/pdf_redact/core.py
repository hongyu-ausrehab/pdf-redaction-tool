"""Redaction operations on top of PyMuPDF.

All public rects are in *displayed* page coordinates: PDF points, origin at
the top-left of the page as a viewer shows it (i.e. after /Rotate). They are
converted to PyMuPDF's unrotated page space internally.

How a region is redacted (see README "How it works" for the measurements):
MuPDF removes every glyph whose font box, inset ~10% per side, intersects a
redaction rect. Font boxes are ~1.375x the font size tall, so a rect that
visually covers one line also removes glyphs from the next line whenever
line spacing is below ~1.16x - common in real documents. So the two jobs are
split: pass 1 removes exactly the chosen glyphs (those whose ink centre lies
in the region) using thin bands through their box centres; pass 2 paints the
region black and blanks image pixels without touching text.
"""

import contextlib
import math
import re
import secrets
from dataclasses import dataclass, field

import pymupdf


# Glyph ink extent relative to the baseline, as a fraction of font size.
ASCENT, DESCENT = 0.8, 0.22
# MuPDF's measured inset is 0.09-0.11 of the box per side; treat neighbours
# as slightly bigger than that when checking a removal band is safe.
MUPDF_INSET = 0.08
# Half-size of the removal mark around a glyph box centre, as a fraction of
# the box's smaller side - comfortably inside MuPDF's inset box.
MARK = 0.05

BLACK = (0, 0, 0)
BASE14 = {  # family -> (regular, italic, bold, bold-italic)
    "he": ("helv", "heit", "hebo", "hebi"),
    "ti": ("tiro", "tiit", "tibo", "tibi"),
    "co": ("cour", "coit", "cobo", "cobi"),
}


class RedactError(Exception):
    pass


def parse_color(value):
    """'#rrggbb' or (r, g, b) in 0..1 -> (r, g, b) in 0..1."""
    if isinstance(value, str):
        m = re.fullmatch(r"#?([0-9a-fA-F]{6})", value.strip())
        if not m:
            raise RedactError(f"bad colour {value!r} (expected #rrggbb)")
        return tuple(int(m.group(1)[i : i + 2], 16) / 255 for i in (0, 2, 4))
    rgb = tuple(float(v) for v in value)
    if len(rgb) != 3 or not all(0 <= v <= 1 for v in rgb):
        raise RedactError(f"bad colour {value!r}")
    return rgb


@dataclass
class Op:
    page: int  # 0-based
    rect: tuple  # (x0, y0, x1, y1), displayed coordinates
    kind: str = "blackout"  # "blackout" | "replace"
    text: str = ""  # replacement text for kind == "replace"
    color: tuple = BLACK  # fill of a blackout, (r, g, b) in 0..1

    @classmethod
    def from_dict(cls, d):
        op = cls(int(d["page"]), tuple(float(v) for v in d["rect"]), d.get("kind", "blackout"),
                 str(d.get("text", "")), parse_color(d.get("color", BLACK)))
        if op.kind not in ("blackout", "replace") or len(op.rect) != 4 or not all(map(math.isfinite, op.rect)):
            raise RedactError(f"bad op: {d!r}")
        return op


@dataclass
class Hit:
    page: int
    text: str
    rects: list = field(default_factory=list)  # one per line the match spans, displayed coordinates


class Char:
    """One extracted glyph. Boxes are built on first use: a big document has
    hundreds of thousands of chars and search needs boxes for very few."""

    __slots__ = ("line", "pos", "c", "span", "_raw", "_bbox", "_ink")

    def __init__(self, line, pos, raw, span):
        self.line = line  # line number on the page
        self.pos = pos  # index in the page's char sequence
        self.c, self.span, self._raw = raw["c"], span, raw
        self._bbox = self._ink = None

    @property
    def bbox(self):
        """Font box, as MuPDF sees it."""
        if self._bbox is None:
            self._bbox = pymupdf.Rect(self._raw["bbox"])
        return self._bbox

    @property
    def ink(self):
        """Tight box around the likely ink."""
        if self._ink is None:
            self._ink = _ink_rect(self._raw, self.span)
        return self._ink

    @property
    def origin(self):
        """Baseline start."""
        return pymupdf.Point(self._raw["origin"])


def open_pdf(src, password=None):
    """Open a path or bytes as a PDF, raising RedactError for anything unusable."""
    try:
        doc = pymupdf.open(stream=src) if isinstance(src, (bytes, bytearray)) else pymupdf.open(src)
    except Exception as e:
        raise RedactError(f"cannot open PDF: {e}") from e
    if not doc.is_pdf:
        raise RedactError("not a PDF file")
    if doc.needs_pass and not doc.authenticate(password or ""):
        raise RedactError("PDF is password-protected (pass the correct password)")
    return doc


def pattern(terms=(), regexes=(), case=False):
    """Compile search terms (literal, whitespace-tolerant) and regexes into one pattern."""
    parts = [r"\s+".join(map(re.escape, t.split())) for t in terms if t.strip()]
    parts += [r for r in regexes if r]
    if not parts:
        return None
    try:
        return re.compile("|".join(f"(?:{p})" for p in parts), 0 if case else re.IGNORECASE)
    except re.error as e:
        raise RedactError(f"bad regex: {e}") from e


def _ink_rect(ch, span):
    """Tight box around a glyph's likely ink, in unrotated page space."""
    s = span["size"]
    o, d = pymupdf.Point(ch["origin"]), pymupdf.Point(span["dir"])
    n = pymupdf.Point(d.y, -d.x)  # "up" for this text direction (y grows downwards)
    bb = pymupdf.Rect(ch["bbox"])
    along = [(c - o).x * d.x + (c - o).y * d.y for c in (bb.tl, bb.tr, bb.bl, bb.br)]
    pts = [o + d * t + n * u for t in (min(along), max(along)) for u in (-DESCENT * s, ASCENT * s)]
    r = pymupdf.Rect(pts[0], pts[0])
    for p in pts[1:]:
        r |= p
    return r


def page_chars(page):
    """Page text as one string, plus a parallel list of Char (None for line breaks)."""
    text, chars = [], []
    raw = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    line_no = 0
    for block in raw["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                span["dir"] = line["dir"]
                for ch in span["chars"]:
                    chars.append(Char(line_no, len(chars), ch, span))
                    text.append(ch["c"])
            text.append("\n")
            chars.append(None)
            line_no += 1
    return "".join(text), chars


def _centre(r):
    return pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)


def chars_in(chars, rect):
    """Chars whose ink centre lies in rect (unrotated space). Spaces count too:
    their positions would reveal the number and length of redacted words."""
    return [c for c in chars if c and rect.contains(_centre(c.ink))]


def _mark(c, avoid=()):
    """A tiny rect inside c's box that MuPDF matches to c: at the box centre,
    or else at the nearest grid point clear of the `avoid` zones (e.g. a tall
    bracket glyph overlapping the line)."""
    b, m = c.bbox, MARK * min(c.bbox.width, c.bbox.height)
    # 0.2 from the edges stays inside MuPDF's inset box (inset <= 0.11)
    grid = [pymupdf.Point(b.x0 + b.width * fx, b.y0 + b.height * fy)
            for fx in (0.2, 0.35, 0.5, 0.65, 0.8) for fy in (0.2, 0.35, 0.5, 0.65, 0.8)]
    centre = _centre(b)
    grid.sort(key=lambda p: abs(p - centre))
    marks = [pymupdf.Rect(p.x - m, p.y - m, p.x + m, p.y + m) for p in grid]
    return next((r for r in marks if not any(r.intersects(a) for a in avoid)), marks[0])


def _danger(c):
    """The part of a glyph's box that MuPDF reacts to (slightly enlarged for safety)."""
    dx, dy = MUPDF_INSET * c.bbox.width, MUPDF_INSET * c.bbox.height
    return pymupdf.Rect(c.bbox.x0 + dx, c.bbox.y0 + dy, c.bbox.x1 - dx, c.bbox.y1 - dy)


def _removal_rects(chars, selected):
    """Rects that make MuPDF remove exactly `selected`: one band per run of
    consecutive chars on a line, falling back to per-char marks where a band
    would graze a glyph that should stay."""
    chosen = {c.pos for c in selected}
    others = [_danger(c) for c in chars if c and c.pos not in chosen and not c.c.isspace()]
    runs = []
    for c in sorted(selected, key=lambda c: c.pos):
        if runs and runs[-1][-1].line == c.line and runs[-1][-1].pos + 1 == c.pos:
            runs[-1].append(c)
        else:
            runs.append([c])
    rects = []
    for run in runs:
        band = _mark(run[0])
        for c in run[1:]:
            band |= _mark(c)
        near = [o for o in others if o.intersects(band)]
        if near:
            area = pymupdf.Rect(run[0].bbox)
            for c in run[1:]:
                area |= c.bbox
            near = [o for o in others if o.intersects(area)]
            rects += [_mark(c, near) for c in run]
        else:
            rects.append(band)
    return rects


def _disturbed(kept, after, tol=0.15):
    """Chars of `kept` with no identical char within tol points in `after` -
    a check that removal touched only what it should."""
    where = {}  # (char, rounded y) -> [top-left points]
    for c in after:
        if c:
            where.setdefault((c.c, round(c.bbox.y0)), []).append(c.bbox.tl)
    near = lambda c: (p for dy in (-1, 0, 1) for p in where.get((c.c, round(c.bbox.y0) + dy), ()))
    return [c for c in kept if not any(abs(p.x - c.bbox.x0) <= tol and abs(p.y - c.bbox.y0) <= tol
                                       for p in near(c))]


def _find(doc, pat, pages=None):
    """All matches of a compiled pattern, as Hits with one displayed rect per line."""
    hits = []
    for pno in pages if pages is not None else range(doc.page_count):
        page = doc[pno]
        text, chars = page_chars(page)
        for m in pat.finditer(text):
            lines = {}
            for c in chars[m.start() : m.end()]:
                if c and not c.c.isspace():
                    lines[c.line] = lines[c.line] | c.ink if c.line in lines else pymupdf.Rect(c.ink)
            if lines:
                hits.append(Hit(pno, m.group(0), [tuple(r * page.rotation_matrix) for r in lines.values()]))
    return hits


def hits_to_ops(hits, kind="blackout", text=""):
    ops = []
    for h in hits:
        for i, r in enumerate(h.rects):
            # A multi-line replacement goes on the first line; later lines are just cleared.
            ops.append(Op(h.page, r, kind, text if i == 0 else ""))
    return ops


def _font_key(name):
    """Comparable font name: the same font appears as 'ABCDEF+DejaVuSans' in a
    span, 'DejaVu Sans Book' as BaseFont - both give 'dejavusans'."""
    if len(name) > 7 and name[6] == "+" and name[:6].isupper():
        name = name[7:]  # subset tag
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    return re.sub(r"(book|regular|normal|roman|psmt|mt)$", "", key) or key


def _standard_font(name, flags, text):
    """Closest built-in font, judged by name first (MuPDF's style flags are
    guesses - it flags DejaVu Sans as serif), then by flags."""
    key = name.lower()
    if re.search(r"mono|courier|consol|code", key):
        family = "co"
    elif "sans" in key or re.search(r"arial|helvet|verdana|calibri|segoe|roboto|inter\b", key):
        family = "he"
    elif re.search(r"times|serif|roman|georgia|garamond|minion|cambria|palatino|book ?antiqua", key):
        family = "ti"
    else:
        family = "co" if flags & pymupdf.TEXT_FONT_MONOSPACED else "ti" if flags & pymupdf.TEXT_FONT_SERIFED else "he"
    bold = bool(flags & pymupdf.TEXT_FONT_BOLD or re.search(r"bold|black|heavy|semibold|demi", key))
    italic = bool(flags & pymupdf.TEXT_FONT_ITALIC or re.search(r"italic|oblique", key))
    if any(ord(c) > 255 for c in text):
        return "china-s"  # base-14 fonts are Latin-1 only
    return BASE14[family][italic + 2 * bold]


# --- writing in the document's own font -------------------------------------
# A replacement is drawn with the very font object the original text used:
# the text is encoded through the font's /ToUnicode map (or its standard
# encoding) and placed with raw PDF operators. Embedded fonts are usually
# subsets whose glyph tables can't be trusted (a cmap entry or ToUnicode code
# may point at a stripped outline), so every glyph written is checked for ink
# on the rendered page, and the write is undone if any is blank.

_HEX = re.compile(rb"<([0-9A-Fa-f]*)>")


def _hexbytes(digits):
    """PDF hex string digits -> bytes (an odd final digit means a trailing 0)."""
    return bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode())


def _utf16(hexdigits):
    return _hexbytes(hexdigits).decode("utf-16-be", errors="ignore")


def _parse_tounicode(data):
    """ToUnicode CMap stream -> {code bytes: text}."""
    out = {}
    for block in re.finditer(rb"beginbfchar(.*?)endbfchar", data, re.S):
        h = _HEX.findall(block.group(1))
        for src, dst in zip(h[::2], h[1::2]):
            out[_hexbytes(src)] = _utf16(dst)
    for block in re.finditer(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", block.group(1)):
            n, lo, hi = len(lo) // 2, int(lo, 16), min(int(hi, 16), int(lo, 16) + 65535)
            if dst.startswith(b"["):
                for i, d in enumerate(_HEX.findall(dst)):
                    out[(lo + i).to_bytes(n, "big")] = _utf16(d)
            elif len(dst) >= 6:
                base = _hexbytes(dst[1:-1])
                last = int.from_bytes(base[-2:], "big")
                for i in range(hi - lo + 1):
                    out[(lo + i).to_bytes(n, "big")] = (base[:-2] + (last + i).to_bytes(2, "big")).decode(
                        "utf-16-be", errors="ignore")
    return out


def _numbers(text):
    return [float(v) for v in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)", text)]


def _object_text(doc, xref, key):
    """Text of a dictionary value, following an indirect reference."""
    kind, value = doc.xref_get_key(xref, key)
    if kind == "xref":
        return doc.xref_object(int(value.split()[0]))
    return value if kind != "null" else ""


def _font_encoding(doc, xref):
    """({char: code bytes}, {code: width in 1/1000 em}, default width) for a
    font object, or None if text can't be encoded for it."""
    subtype = doc.xref_get_key(xref, "Subtype")[1]
    kind, ref = doc.xref_get_key(xref, "ToUnicode")
    to_unicode = _parse_tounicode(doc.xref_stream(int(ref.split()[0])) or b"") if kind == "xref" else {}
    widths = {}
    if subtype == "/Type0":
        if doc.xref_get_key(xref, "Encoding")[1] != "/Identity-H" or not to_unicode:
            return None  # other CMaps / vertical writing: codes aren't CIDs
        desc = re.search(r"(\d+)\s+0\s+R", _object_text(doc, xref, "DescendantFonts"))
        if not desc:
            return None
        desc = int(desc.group(1))
        tokens = re.findall(r"\[|\]|[-+]?(?:\d+\.?\d*|\.\d+)", _object_text(doc, desc, "W"))[1:-1]  # drop outer [ ]
        i = 0
        while i < len(tokens):  # c [w1 w2 ...]  or  c_first c_last w
            if i + 1 < len(tokens) and tokens[i + 1] == "[":
                c, i = int(float(tokens[i])), i + 2
                while i < len(tokens) and tokens[i] != "]":
                    widths[c], c, i = float(tokens[i]), c + 1, i + 1
                i += 1
            elif i + 2 < len(tokens):
                for c in range(int(float(tokens[i])), int(float(tokens[i + 1])) + 1):
                    widths[c] = float(tokens[i + 2])
                i += 3
            else:
                break
        default = (_numbers(_object_text(doc, desc, "DW")) or [1000])[0]
    elif subtype in ("/TrueType", "/Type1", "/MMType1"):
        if not to_unicode:  # common for standard 14 fonts and simple encodings
            codec = {"/WinAnsiEncoding": "cp1252", "/MacRomanEncoding": "mac_roman"}.get(
                doc.xref_get_key(xref, "Encoding")[1])
            if not codec:
                return None
            to_unicode = {bytes([i]): bytes([i]).decode(codec, errors="ignore") for i in range(32, 256)}
        first = int((_numbers(_object_text(doc, xref, "FirstChar")) or [0])[0])
        widths = {first + i: w for i, w in enumerate(_numbers(_object_text(doc, xref, "Widths")))}
        desc = re.search(r"(\d+)\s+0\s+R", doc.xref_get_key(xref, "FontDescriptor")[1])
        default = ((_numbers(_object_text(doc, int(desc.group(1)), "MissingWidth")) if desc else []) or [0])[0]
        if not widths:  # a standard 14 font, not embedded: use its built-in metrics
            std = _standard_font(doc.xref_get_key(xref, "BaseFont")[1].lstrip("/"), 0, "")
            widths = {int.from_bytes(code, "big"): pymupdf.get_text_length(s, fontname=std, fontsize=1000)
                      for code, s in to_unicode.items() if len(s) == 1}
    else:
        return None  # Type 3 fonts have their own glyph space
    codes = {}
    for code, s in to_unicode.items():
        if len(s) == 1:
            codes.setdefault(s, code)
    return codes, widths, default


def _pdf_point(page, p):
    """Unrotated PyMuPDF page point -> PDF user space. PyMuPDF measures from
    the CropBox's top-left; page.transformation_matrix is wrong for rotated
    pages whose boxes aren't at the origin."""
    return pymupdf.Point(p.x + page.cropbox.x0, page.mediabox.y1 - page.cropbox.y0 - p.y)


def _write_with_font(page, ref, codes, size, color, o, d):
    """Append raw operators drawing `codes` with font resource `ref`; returns
    the previous /Contents value, to undo the write."""
    doc = page.parent
    old = doc.xref_get_key(page.xref, "Contents")[1]
    a, b = _pdf_point(page, o), _pdf_point(page, o + d)
    dx, dy = b.x - a.x, b.y - a.y  # text direction in PDF space (y up)
    tm = " ".join(f"{v:.4f}" for v in (size * dx, size * dy, -size * dy, size * dx, a.x, a.y))
    rgb = " ".join(f"{v:.4f}" for v in color)
    stream = (f"q BT /{ref} 1 Tf 0 Tc 0 Tw 100 Tz 0 Ts 0 Tr {rgb} rg {tm} Tm "
              f"<{b''.join(codes).hex()}> Tj ET Q").encode()
    page.wrap_contents()
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, stream)
    refs = " ".join(f"{c} 0 R" for c in page.get_contents() + [xref])
    doc.xref_set_key(page.xref, "Contents", f"[{refs}]")
    return old


def _strip(page, o, d, size, t0, t1):
    """Displayed rect around text drawn from t0 to t1 along d from o."""
    n = pymupdf.Point(d.y, -d.x)
    pts = [o + d * t + n * u for t in (t0, t1) for u in (-0.3 * size, size)]
    r = pymupdf.Rect(pts[0], pts[0])
    for p in pts[1:]:
        r |= p
    return r * page.rotation_matrix


def _render(page, clip, zoom):
    return page.get_pixmap(clip=clip, matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY)


def _blank_glyphs(page, before, clip, zoom, o, d, size, text, advances):
    """Chars of text whose cell shows no new ink compared to `before`, the
    rendering of `clip` at `zoom` taken before the text was written."""
    after = _render(page, clip, zoom)
    if (after.width, after.height) != (before.width, before.height):
        return [c for c in text if not c.isspace()]  # can't compare: treat as failed
    sx, sy, blank, t = before.width / clip.width, before.height / clip.height, [], 0
    b, a = before.samples, after.samples  # each .samples access copies the pixmap
    for ch, adv in zip(text, advances):
        if not ch.isspace():
            cell, changed = _strip(page, o, d, size, t, t + adv), 0
            for y in range(max(0, int((cell.y0 - clip.y0) * sy)), min(before.height, int((cell.y1 - clip.y0) * sy))):
                row = y * before.stride
                for x in range(max(0, int((cell.x0 - clip.x0) * sx)), min(before.width, int((cell.x1 - clip.x0) * sx))):
                    changed += abs(b[row + x] - a[row + x]) > 40
            if changed < 3:
                blank.append(ch)
        t += adv
    return blank


def _write_original_font(page, font_name, text, size, color, o, d, room, encodings):
    """Write text with the page's own font object(s) named font_name. Returns
    (size used, None) on success, else (None, chars the font couldn't draw or
    None if no such font object is usable)."""
    missing, key = None, _font_key(font_name)
    for xref, _ext, _type, basefont, ref, _enc, referencer in page.get_fonts(full=True):
        # Only fonts in the page's own resources can be referenced. MuPDF reports
        # span font names cut to 31 chars, subset tag included - hence two keys.
        if referencer != 0 or key not in (_font_key(basefont), _font_key(basefont[:31])):
            continue
        if xref not in encodings:  # parsing a big ToUnicode map is slow: once per font
            encodings[xref] = _font_encoding(page.parent, xref)
        enc = encodings[xref]
        if enc is None:
            continue
        codes, widths, default = enc
        # A glyph must be mapped AND have its own width: subsets list widths
        # only for glyphs they contain, and a missing glyph can still render
        # ink (many fonts draw .notdef as a box), fooling the ink check below.
        lack = [c for c in text if c not in codes
                or (not c.isspace() and widths.get(int.from_bytes(codes[c], "big"), 0) <= 0)]
        if not lack:
            per_em = [widths.get(int.from_bytes(codes[c], "big"), default) / 1000 for c in text]
            s = size * min(1, room / (sum(per_em) * size)) if sum(per_em) * size > room * 1.01 > 0 else size
            advances = [w * s for w in per_em]
            clip = _strip(page, o, d, s, 0, sum(advances))
            zoom = max(2, 40 / max(s, 1))
            before = _render(page, clip, zoom)
            old = _write_with_font(page, ref, [codes[c] for c in text], s, color, o, d)
            lack = _blank_glyphs(page, before, clip, zoom, o, d, s, text, advances)
            if not lack:
                return s, None
            page.parent.xref_set_key(page.xref, "Contents", old)  # undo
        missing = lack if missing is None or len(lack) < len(missing) else missing
    return None, missing


def _along(p, o, d):
    return (p - o).x * d.x + (p - o).y * d.y


def _room(page, o, d, size, end, obstacles):
    """How far text starting at o may run along d: up to the next obstacle on
    this line (keeping the original gap, at most a word space), else to the
    page edge. `end` is where the removed text ended; obstacles are rects (other
    text, and regions being blacked out or replaced) the text must not enter."""
    n = pymupdf.Point(d.y, -d.x)
    nxt = None
    for box in obstacles:
        corners = (box.tl, box.tr, box.bl, box.br)
        ups = [_along(p, o, n) for p in corners]
        if max(ups) < 0.1 * size or min(ups) > 0.5 * size:
            continue  # not on this line (checked around x-height, clear of neighbouring lines)
        ts = [_along(p, o, d) for p in corners]
        if (min(ts) + max(ts)) / 2 > end:
            nxt = min(ts) if nxt is None else min(nxt, min(ts))
    if nxt is None:
        edge = page.rect * page.derotation_matrix
        return max(_along(p, o, d) for p in (edge.tl, edge.tr, edge.bl, edge.br)) - 0.5 * size
    return nxt - min(max(nxt - end, 0), 0.25 * size)


def _insert_replacement(page, rect, text, removed, obstacles, encodings):
    """Write text in place of `removed` (the chars cleared from rect, unrotated
    space): same baseline, colour, size and - when it has the glyphs - the
    same font object. For an empty area, the text is sized to the box.
    Returns a note if a substitute font had to be used."""
    visible = sorted((c for c in removed if not c.c.isspace()), key=lambda c: c.pos)
    note, font_name = None, ""
    if visible:
        first, span = visible[0], visible[0].span
        size, color, flags, font_name = span["size"], pymupdf.sRGB_to_pdf(span["color"]), span["flags"], span["font"]
        d, o = pymupdf.Point(span["dir"]), first.origin
        end = max(_along(p, o, d) for c in visible for p in (c.ink.tl, c.ink.tr, c.ink.bl, c.ink.br))
        room = max(end, _room(page, o, d, size, end, obstacles))
        used, missing = _write_original_font(page, font_name, text, size, color, o, d, room, encodings)
        if used:
            return None
        why = (f"has no {''.join(dict.fromkeys(missing))!r}" if missing
               else "can't be reused (not in the page's own resources, or an unsupported encoding)")
        if not re.fullmatch(r"(helvetica|times|courier)(bold)?(italic|oblique)?", _font_key(font_name)):
            # (for a standard 14 font the substitute is the same font: nothing to report)
            note = f"page {page.number + 1}: {text!r} written in a standard font - the document's {font_name} {why}"
    else:  # empty area: upright as displayed, vertically centred, fitted to the box
        disp = rect * page.rotation_matrix
        size, color, flags = disp.height * 0.7, BLACK, 0
        o = pymupdf.Point(disp.x0 + size * 0.05, (disp.y0 + disp.y1) / 2 + size * 0.35) * page.derotation_matrix
        d = pymupdf.Point(1, 0) * page.derotation_matrix - pymupdf.Point(0, 0) * page.derotation_matrix
        room = max(_along(p, o, d) for p in (rect.tl, rect.tr, rect.bl, rect.br))
    fontname = _standard_font(font_name, flags, text)
    width = pymupdf.get_text_length(text, fontname=fontname, fontsize=size)
    # Shrink only if the text would otherwise run into what follows it.
    if width > room * 1.01 and room > 0:
        size *= room / width
    rotate = round(math.degrees(math.atan2(-d.y, d.x)) / 90) % 4 * 90
    page.insert_text(o, text, fontsize=size, fontname=fontname, color=color, rotate=rotate)
    return note


def _background(page, rect):
    """Median colour of a thin ring just outside rect (unrotated space), 0..1."""
    ring = (rect + (-3, -3, 3, 3)) * page.rotation_matrix
    pix = page.get_pixmap(clip=ring, matrix=pymupdf.Matrix(2, 2), colorspace=pymupdf.csRGB, alpha=False)
    w, h, s, n, stride = pix.width, pix.height, pix.samples, pix.n, pix.stride
    edge = [(x, y) for y in range(h) for x in range(w) if x < 3 or y < 3 or x >= w - 3 or y >= h - 3]
    if not edge:
        return (1, 1, 1)
    return tuple(sorted(s[y * stride + x * n + i] for x, y in edge)[len(edge) // 2] / 255 for i in range(3))


def _purge_overlapping(page, rects):
    """Delete annotations, form fields and links overlapping any rect. MuPDF's
    redaction leaves them alone, and their text or URI can hold what is being
    redacted. A field is removed whole: its value can't be partially redacted."""
    hit = lambda r: any(r.intersects(x) for x in rects)
    notes = []
    for annot in list(page.annots()):
        if annot.type[0] != pymupdf.PDF_ANNOT_REDACT and hit(annot.rect):
            notes.append(f"{annot.type[1]} annotation")
            page.delete_annot(annot)
    for widget in list(page.widgets()):
        if hit(widget.rect):
            notes.append(f"form field {widget.field_name!r}")
            page.delete_widget(widget)
    for link in page.get_links():
        if hit(pymupdf.Rect(link["from"])):
            notes.append("link")
            page.delete_link(link)
    return [f"page {page.number + 1}: removed {n} (overlaps a redacted region)" for n in notes]


def _apply(doc, ops, shown=None):
    """Apply ops to doc in place and return notes on what else was removed.
    Where a blackout overlaps a replacement, the blackout wins and the
    replacement text is not written."""
    by_page, notes, encodings = {}, [], {}  # encodings: font xref -> _font_encoding()
    for op in ops:
        if not 0 <= op.page < doc.page_count:
            raise RedactError(f"page {op.page + 1} out of range (document has {doc.page_count})")
        by_page.setdefault(op.page, []).append(op)
    for pno, page_ops in by_page.items():
        page = doc[pno]
        regions = [(op, pymupdf.Rect(op.rect).normalize() * page.derotation_matrix) for op in page_ops]
        regions = [(op, r) for op, r in regions if not r.is_empty]
        blackouts = [r for op, r in regions if op.kind == "blackout"]
        if any(a.type[0] == pymupdf.PDF_ANNOT_REDACT for a in page.annots()):
            # Redactions marked in the file but never applied: apply them as
            # marked (images included) before ours, which would apply them text-only.
            page.apply_redactions()
            notes.append(f"page {pno + 1}: applied redaction marks already present in the file")
        notes += _purge_overlapping(page, [r for _, r in regions])
        _, chars = page_chars(page)

        # Pass 1: remove the selected glyphs, nothing else.
        selected, writes = {}, []
        for op, r in regions:
            sel = chars_in(chars, r)
            selected.update((c.pos, c) for c in sel)
            if op.kind == "replace" and op.text and not any(r.intersects(b) for b in blackouts):
                visible = [c for c in sel if not c.c.isspace()]
                if shown is not None and pno in shown and visible and not any(_key(c) in shown[pno] for c in visible):
                    # text only on a hidden layer: writing it would make it appear
                    notes.append(f"page {pno + 1}: replaced text was on a hidden layer - removed, not rewritten")
                    continue
                writes.append((r, op.text, sel))
        kept = [c for c in chars if c and c.pos not in selected and not c.c.isspace()]
        removals = _removal_rects(chars, list(selected.values()))
        if removals:
            for r in removals:
                page.add_redact_annot(r, fill=False, cross_out=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
            disturbed = _disturbed(kept, page_chars(page)[1])
            if disturbed:
                notes.append(f"page {pno + 1}: {len(disturbed)} character(s) next to a redacted region were "
                             f"also removed or moved: {''.join(c.c for c in disturbed[:20])!r}")

        # A replacement whose old text isn't visible text - an OCR layer over a
        # scan, or a box drawn over a picture - must also clear the image
        # pixels that show the old words, or both would be visible.
        # MuPDF blanks cleared pixels to white, so the spot is then painted in
        # the colour around it to blend with the scan's paper.
        scans = []
        for r, _, removed in writes:
            if not any(not c.c.isspace() and c.span["alpha"] for c in removed):
                pad = 0.15 * min(r.height, r.width)
                spot = r + (-pad, -pad, pad, pad)
                scans.append((spot, _background(page, spot)))
                page.add_redact_annot(spot, fill=False, cross_out=False)
        if scans:
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_NONE)
            for spot, bg in scans:
                page.draw_rect(spot, color=None, fill=bg, overlay=True)  # unrotated coords, like insert_text

        for r, text, removed in writes:
            # a replacement may use free space after it, but not run into other
            # text or into any other region (it would end up under a blackout)
            own = {c.pos for c in removed}
            obstacles = ([c.bbox for c in chars if c and not c.c.isspace() and c.pos not in own]
                         + [other for _, other in regions if other is not r])
            note = _insert_replacement(page, r, text, removed, obstacles, encodings)
            if note:
                notes.append(note)

        # Pass 2: paint blackouts and blank whatever images / vector art they cover.
        # Where a blackout covers OCR text over an image, the area is padded a
        # little: OCR boxes don't align exactly with the words in the picture (a
        # trap file left part of the last digit). A box drawn over a picture
        # with no text in it is taken exactly as drawn.
        images = [r for im in page.get_images(full=True) for r in page.get_image_rects(im[0])]
        for op, r in regions:
            if op.kind == "blackout":
                if any(r.intersects(im) for im in images) and chars_in(chars, r):
                    pad = min(0.2 * min(r.height, r.width), 4)
                    r = r + (-pad, -pad, pad, pad)
                page.add_redact_annot(r, fill=op.color, cross_out=False)
        if blackouts:
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                  text=pymupdf.PDF_REDACT_TEXT_NONE)
    return notes


# --- copies of the text outside the visible page ------------------------------
# A forensic scan (scripts/forensic_check.py, DESIGN.md section 8) found text
# surviving a redaction in places the visible page doesn't show: hidden
# layers, marked-content /ActualText, structure-tree /Alt, /PieceInfo, and
# comments / links / bookmarks that mention a searched term.


def _key(c):
    """Identity of an extracted char across two extractions of the same page."""
    return c.c, round(c.bbox.x0, 1), round(c.bbox.y0, 1)


@contextlib.contextmanager
def all_layers_visible(doc):
    """Switch every optional-content layer on while text is found and removed
    (extraction skips hidden layers, so their text would survive), then
    restore the document's own settings. (set_layer() was tried first: it
    left the layers' state unchanged, so their text stayed invisible to
    extraction; the UI-config call is what MuPDF's renderer obeys.)"""
    configs = [c for c in doc.layer_ui_configs() if c["type"] in ("checkbox", "radiobox")]
    hidden = [c["number"] for c in configs if not c["on"]]
    for number in hidden:
        doc.set_layer_ui_config(number, 0)  # 0: on
    try:
        yield
    finally:
        for number in hidden:
            doc.set_layer_ui_config(number, 2)  # 2: off again


def find(doc, pat, pages=None):
    """All matches of a compiled pattern, including text on hidden layers, as
    Hits with one displayed rect per line."""
    with all_layers_visible(doc):
        return _find(doc, pat, pages)


def apply(doc, ops):
    """Apply ops to doc in place and return notes on what else was removed.
    Where a blackout overlaps a replacement, the blackout wins and the
    replacement text is not written. On every page with a region, copies of
    text kept outside the visible content are removed too."""
    shown = None  # page -> chars visible with the document's own layer settings
    if any(not c["on"] for c in doc.layer_ui_configs()):
        shown = {p: {_key(c) for c in page_chars(doc[p])[1] if c}
                 for p in {op.page for op in ops if op.kind == "replace" and 0 <= op.page < doc.page_count}}
    with all_layers_visible(doc):
        notes = _apply(doc, ops, shown)
    pages = sorted({op.page for op in ops})
    counts = [_strip_page_copies(doc, doc[pno]) for pno in pages]
    counts.append(_strip_struct_text(doc, {doc[pno].xref for pno in pages}) if pages else 0)
    if sum(counts):
        notes.append(f"removed {sum(counts)} hidden copies of text (accessibility tags such as ActualText/Alt, "
                     "application data) from the redacted pages")
    return notes


_MC_TEXT = re.compile(rb"/(?:ActualText|Alt|E)(?=[\s(<])\s*")
_INLINE_IMAGE = re.compile(rb"(?<![^\s])BI(?![^\s])")


def _strip_marked_text(src):
    """Remove /ActualText, /Alt and /E entries (alternative text for marked
    content) from a content stream. Returns (new stream, count)."""
    out, i, n = bytearray(), 0, 0
    while m := _MC_TEXT.search(src, i):
        j = k = m.end()
        if src[j : j + 1] == b"(":  # literal string: balanced parens, \-escapes
            depth = 0
            while k < len(src):
                c = src[k]
                if c == 0x5C:
                    k += 2
                    continue
                depth += (c == 0x28) - (c == 0x29)
                k += 1
                if depth == 0:
                    break
        elif src[j : j + 1] == b"<" and src[j + 1 : j + 2] != b"<":  # hex string
            k = src.find(b">", j) + 1 or len(src)
        else:  # a name, not a string value: leave it
            out += src[i:j]
            i = j
            continue
        out += src[i : m.start()]
        i, n = k, n + 1
    return bytes(out + src[i:]), n


def _strip_page_copies(doc, page):
    """Alternative-text entries in the page's content and property lists, its
    /PieceInfo (an editing application's private copy of the page) and its
    /Thumb (a thumbnail picture of the page as it was - kept with scrub off
    before, and invisible to a text scan)."""
    n = 0
    for xref in page.get_contents() + [x[0] for x in page.get_xobjects()]:
        src = doc.xref_stream(xref) or b""
        if _INLINE_IMAGE.search(src):
            continue  # inline image data can't be scanned safely
        new, k = _strip_marked_text(src)
        if k:
            doc.update_stream(xref, new)
            n += k
    for holder in [page.xref] + [x[0] for x in page.get_xobjects()]:
        props = _object_text(doc, holder, "Resources/Properties")
        for ref in re.findall(r"/[^\s/<>\[\]()]+\s+(\d+)\s+0\s+R", props):
            for key in ("ActualText", "Alt", "E"):
                if doc.xref_get_key(int(ref), key)[0] != "null":
                    doc.xref_set_key(int(ref), key, "null")
                    n += 1
        if doc.xref_get_key(holder, "PieceInfo")[0] != "null":
            doc.xref_set_key(holder, "PieceInfo", "null")
            n += 1
    if doc.xref_get_key(page.xref, "Thumb")[0] != "null":
        doc.xref_set_key(page.xref, "Thumb", "null")
        n += 1
    return n


def _strip_struct_text(doc, page_xrefs):
    """Alternative text in structure elements (tagged PDF) for the given pages,
    or for elements not tied to a page."""
    n = 0
    for x in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(x, "S")[0] != "name" or doc.xref_get_key(x, "P")[0] == "null":
                continue  # not a structure element
            kind, pg = doc.xref_get_key(x, "Pg")
            if kind == "xref" and int(pg.split()[0]) not in page_xrefs:
                continue
            for key in ("ActualText", "Alt", "E"):
                if doc.xref_get_key(x, key)[0] != "null":
                    doc.xref_set_key(x, key, "null")
                    n += 1
        except Exception:
            continue  # free or broken object
    return n


def scrub_matches(doc, pat, replacement=None):
    """Remove a searched term where page regions can't reach it: comments,
    form fields, links and attachments that mention it are deleted; bookmark
    titles and metadata get it replaced (by `replacement`, or "[redacted]")."""
    new = "[redacted]" if replacement is None else replacement
    sub = lambda s: pat.sub(lambda m: new, s)  # noqa: E731 (lambda: no backslash processing)
    notes = []
    for page in doc:
        found = []
        for annot in list(page.annots()):
            if annot.type[0] != pymupdf.PDF_ANNOT_REDACT and any(
                    pat.search(annot.info.get(k) or "") for k in ("content", "title", "subject")):
                found.append(f"{annot.type[1]} annotation")
                page.delete_annot(annot)
        for widget in list(page.widgets()):
            if pat.search(str(widget.field_value or "")) or pat.search(widget.field_name or ""):
                found.append(f"form field {widget.field_name!r}")
                page.delete_widget(widget)
        for link in page.get_links():
            if pat.search(link.get("uri") or "") or pat.search(link.get("file") or ""):
                found.append("link")
                page.delete_link(link)
        notes += [f"page {page.number + 1}: removed {f} mentioning a searched term" for f in found]
    toc = doc.get_toc(simple=False)
    if any(pat.search(item[1]) for item in toc):
        for item in toc:
            item[1] = sub(item[1])
        doc.set_toc(toc)
        notes.append("bookmark titles: searched terms replaced")
    meta = doc.metadata or {}
    if any(v and pat.search(v) for v in meta.values()):
        doc.set_metadata({k: sub(v) if v else v for k, v in meta.items() if k not in ("format", "encryption")})
        notes.append("document metadata: searched terms replaced")
    if pat.search(doc.get_xml_metadata() or ""):
        doc.del_xml_metadata()
        notes.append("XMP metadata removed: it mentioned a searched term")
    for name in [doc.embfile_info(i)["name"] for i in range(doc.embfile_count())]:
        if pat.search(name) or pat.search(doc.embfile_get(name).decode("latin-1", errors="ignore")):
            doc.embfile_del(name)
            notes.append(f"attachment {name!r} removed: it mentioned a searched term")
    js = 0  # JavaScript mentioning the term: emptied (with scrub off it would otherwise stay)
    for x in range(1, doc.xref_length()):
        for path in _JS_PATHS:
            try:
                kind, value = doc.xref_get_key(x, path)
                if kind == "null":
                    continue
                if kind == "xref":
                    value = (doc.xref_stream(int(value.split()[0])) or b"").decode("latin-1", errors="ignore")
                if pat.search(value):
                    doc.xref_set_key(x, path, "()")
                    js += 1
            except Exception:
                continue  # free or broken object
    if js:
        notes.append(f"{js} JavaScript action(s) mentioning a searched term emptied")
    return notes


# where JavaScript can sit: an action object, the document's open action, or
# the "additional actions" of a page, field or the document
_JS_PATHS = ["JS", "OpenAction/JS"] + [f"AA/{k}/JS" for k in (
    "O", "C", "WC", "WS", "DS", "WP", "DP", "K", "F", "V", "Fo", "Bl", "PO", "PC", "PV", "PI", "D", "E", "X", "U")]


def to_bytes(doc, scrub=True):
    """Serialise doc. garbage=1 drops the now-unreferenced original content
    streams from the file; scrub additionally removes metadata, XMP,
    JavaScript, attachments, embedded files and page thumbnails (which can
    show the unredacted page)."""
    data = doc.tobytes(garbage=1, deflate=True)
    if scrub:
        # Scrub a rewritten copy: scrub() fails on files with dangling xrefs,
        # which the garbage-collecting rewrite removes.
        clean = pymupdf.open(stream=data)
        # Not scrub()'s defaults: those also empty every form field, delete
        # every link and every invisible (OCR) text layer in the document.
        # Anything of that kind overlapping a redacted region is already gone.
        clean.scrub(hidden_text=False, remove_links=False, reset_fields=False, reset_responses=False)
        # scrub() leaves JavaScript run on opening the file and "additional
        # actions" (found by the forensic scan, DESIGN.md section 8)
        cat = clean.pdf_catalog()
        if "/JavaScript" in _object_text(clean, cat, "OpenAction"):
            clean.xref_set_key(cat, "OpenAction", "null")
        for holder in [cat] + [page.xref for page in clean]:
            if clean.xref_get_key(holder, "AA")[0] != "null":
                clean.xref_set_key(holder, "AA", "null")
        # a fresh file ID: the first half is a permanent identifier that otherwise
        # stays the same as the original's, tying the redacted copy to it
        clean.xref_set_key(-1, "ID", f"[<{secrets.token_hex(16)}><{secrets.token_hex(16)}>]")
        data = clean.tobytes(garbage=1, deflate=True)
    return data


def verify(doc, ops=(), pat=None):
    """Return human-readable leaks: text still under a blackout, or still matching pat."""
    leaks, chars = [], {}  # chars: page number -> extracted chars, once per page
    for op in ops:
        if op.kind == "blackout":
            page = doc[op.page]
            if op.page not in chars:
                chars[op.page] = page_chars(page)[1]
            r = pymupdf.Rect(op.rect).normalize() * page.derotation_matrix
            left = "".join(c.c for c in chars_in(chars[op.page], r))
            if left:
                leaks.append(f"page {op.page + 1}: text remains under blackout {op.rect}: {left!r}")
    if pat is not None:
        for h in find(doc, pat):
            leaks.append(f"page {h.page + 1}: still matches {h.text!r}")
        for _, title, pno, *_ in doc.get_toc(simple=False):
            if pat.search(title):
                leaks.append(f"bookmark (page {pno}) still matches: {title!r}")
        for key, value in (doc.metadata or {}).items():
            if value and pat.search(value):
                leaks.append(f"metadata {key} still matches: {value!r}")
    return leaks
