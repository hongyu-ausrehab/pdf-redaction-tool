import os
import unittest

import pymupdf

from pdf_redact import core

DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def make_doc(lines=("Name: John Smith", "Phone 555-1234 or 555-9876", "Email john.smith@example.com"),
             rotation=0, size=11, pitch=1.2, color=(0, 0, 0), upright=False):
    """upright=True: text reads normally as displayed on a rotated page (like a
    landscape scan); otherwise the text is rotated along with the page."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    if upright:
        page.set_rotation(rotation)
    for i, text in enumerate(lines):
        point = pymupdf.Point(40, 60 + i * size * pitch)
        if upright:
            point *= page.derotation_matrix
        page.insert_text(point, text, fontsize=size, color=color, rotate=rotation if upright else 0)
    page.set_rotation(rotation)
    return doc


def displayed_words(page):
    """Words in reading order as displayed: by displayed row, then x."""
    words = [(pymupdf.Rect(w[:4]) * page.rotation_matrix, w[4]) for w in page.get_text("words")]
    words.sort(key=lambda w: (round(w[0].y1 / 5), w[0].x0))
    return " ".join(t for _, t in words)


def roundtrip(doc, scrub=True):
    return core.open_pdf(core.to_bytes(doc, scrub))


def all_stream_bytes(doc):
    """Every decompressed stream in the file, to catch text kept in unused objects."""
    out = b""
    for xref in range(1, doc.xref_length()):
        if doc.xref_is_stream(xref):
            out += doc.xref_stream(xref) or b""
    return out


def redact_terms(doc, *terms, regexes=(), scrub=True):
    pat = core.pattern(terms, regexes)
    ops = core.hits_to_ops(core.find(doc, pat))
    core.apply(doc, ops)
    out = roundtrip(doc, scrub)
    return out, ops, core.verify(out, ops, pat)


class FindTests(unittest.TestCase):
    def test_case_insensitive_and_case_sensitive(self):
        doc = make_doc()
        self.assertEqual(len(core.find(doc, core.pattern(["john smith"]))), 1)  # not "john.smith"
        self.assertEqual(len(core.find(doc, core.pattern(["john smith"], case=True))), 0)

    def test_term_spanning_line_break_gives_rect_per_line(self):
        doc = make_doc(lines=("contact John", "Smith today"))
        hits = core.find(doc, core.pattern(["John Smith"]))
        self.assertEqual(len(hits), 1)
        self.assertEqual(len(hits[0].rects), 2)

    def test_regex(self):
        hits = core.find(make_doc(), core.pattern(regexes=[r"\d{3}-\d{4}"]))
        self.assertEqual([h.text for h in hits], ["555-1234", "555-9876"])

    def test_bad_regex(self):
        with self.assertRaises(core.RedactError):
            core.pattern(regexes=["("])

    def test_empty_pattern(self):
        self.assertIsNone(core.pattern(["  "], [""]))


class BlackoutTests(unittest.TestCase):
    def test_removes_text_from_file_not_just_covers_it(self):
        out, _, leaks = redact_terms(make_doc(), "John Smith")
        self.assertEqual(leaks, [])
        text = out[0].get_text()
        self.assertNotIn("Smith", text)
        self.assertIn("Phone 555-1234", text)
        raw = all_stream_bytes(out)
        self.assertNotIn(b"Smith", raw)
        self.assertNotIn(b"Smith".hex().encode(), raw.lower())

    def test_adjacent_lines_survive(self):
        # PyMuPDF's own search_for() rect wipes neighbouring lines at these spacings.
        for pitch in (0.9, 1.0, 1.15, 1.2, 1.5):
            with self.subTest(pitch=pitch):
                doc = make_doc(lines=("line0 alpha gyp", "line1 alpha gyp", "line2 alpha gyp"), pitch=pitch)
                out, _, leaks = redact_terms(doc, "line1 alpha")
                self.assertEqual(leaks, [])
                lines = out[0].get_text().splitlines()
                self.assertIn("line0 alpha gyp", lines)
                self.assertIn("line2 alpha gyp", lines)
                self.assertNotIn("line1", out[0].get_text())

    def test_manual_box_over_tight_lines_keeps_neighbours(self):
        doc = make_doc(lines=("line0 alpha gyp", "line1 alpha gyp", "line2 alpha gyp"), pitch=1.0)
        # box drawn generously around the whole of line1, touching its neighbours' glyph boxes
        ops = [core.Op(0, (30, 60 + 11 - 9, 300, 60 + 11 + 2.5))]
        core.apply(doc, ops)
        out = roundtrip(doc)
        self.assertEqual(out[0].get_text().splitlines(), ["line0 alpha gyp", "line2 alpha gyp"])

    def test_full_page_box_is_fast(self):
        import time
        doc = make_doc(lines=["lorem ipsum dolor sit amet consectetur adipiscing"] * 50, size=5)
        t = time.time()
        core.apply(doc, [core.Op(0, (0, 0, 400, 300))])
        self.assertLess(time.time() - t, 2)
        self.assertEqual(roundtrip(doc)[0].get_text().strip(), "")

    def test_tall_neighbour_glyph_survives(self):
        # a 40pt bracket whose glyph box covers the start of the word next to it
        doc = pymupdf.open()
        page = doc.new_page(width=300, height=200)
        page.insert_text((20, 80), "(", fontsize=40)
        page.insert_text((30, 60), "John Smith", fontsize=10)
        out, _, leaks = redact_terms(doc, "John")
        self.assertEqual(leaks, [])
        self.assertEqual(out[0].get_text().split(), ["(", "Smith"])

    def test_unavoidable_collateral_is_reported(self):
        # word drawn right inside a huge glyph's box: MuPDF can't remove one without the other
        doc = pymupdf.open()
        page = doc.new_page(width=300, height=200)
        page.insert_text((20, 80), "(", fontsize=40)
        page.insert_text((24, 60), "Jo", fontsize=6)
        notes = core.apply(doc, core.hits_to_ops(core.find(doc, core.pattern(["Jo"]))))
        out = roundtrip(doc)
        self.assertNotIn("Jo", out[0].get_text())
        self.assertEqual(notes, ["page 1: 1 character(s) next to a redacted region were also removed or moved: '('"])

    def test_adjacent_words_survive(self):
        out, _, _ = redact_terms(make_doc(lines=("abcSECRETxyz",)), "SECRET")
        self.assertEqual(out[0].get_text().replace("\n", "").replace(" ", ""), "abcxyz")

    def test_punctuation_inside_match_is_removed(self):
        out, _, leaks = redact_terms(make_doc(), "john.smith@example.com")
        self.assertEqual(leaks, [])
        self.assertNotIn("@", out[0].get_text())
        self.assertNotIn(".", out[0].get_text().split("Email")[1])

    def test_box_is_painted_black(self):
        doc = make_doc()
        hits = core.find(doc, core.pattern(["Smith"]))
        core.apply(doc, core.hits_to_ops(hits))
        pix = roundtrip(doc)[0].get_pixmap(clip=hits[0].rects[0])
        dark = sum(pix.samples[i] < 40 for i in range(0, len(pix.samples), pix.n))
        self.assertGreater(dark / (pix.width * pix.height), 0.9)

    def test_blackout_colours(self):
        doc = make_doc()
        core.apply(doc, [core.Op.from_dict({"page": 0, "rect": [0, 40, 200, 64], "color": "#c81e1e"}),
                         core.Op(0, (0, 100, 200, 130), color=(1, 1, 1))])
        pix = roundtrip(doc)[0].get_pixmap()
        self.assertEqual(pix.pixel(100, 52)[:3], (200, 30, 30))
        self.assertEqual(pix.pixel(100, 115)[:3], (255, 255, 255))
        self.assertEqual(core.Op.from_dict({"page": 0, "rect": [0, 0, 1, 1]}).color, (0, 0, 0))
        for bad in ("red", "#12345", [2, 0, 0]):
            with self.assertRaises(core.RedactError):
                core.parse_color(bad)

    def test_manual_box_removes_text_and_image_pixels(self):
        doc = make_doc()
        page = doc[0]
        red = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 50, 50), False)
        red.set_rect(red.irect, (255, 0, 0))
        page.insert_image(pymupdf.Rect(200, 150, 300, 250), pixmap=red)
        ops = [core.Op(0, (0, 40, 400, 64)), core.Op(0, (200, 150, 250, 250))]
        core.apply(doc, ops)
        out = roundtrip(doc)
        self.assertEqual(core.verify(out, ops), [])
        self.assertNotIn("John", out[0].get_text())
        self.assertIn("Phone", out[0].get_text())
        pix = out[0].get_pixmap()
        self.assertEqual(pix.pixel(225, 200)[:3], (0, 0, 0))  # covered half: blacked
        self.assertEqual(pix.pixel(275, 200)[:3], (255, 0, 0))  # other half: intact

    def test_hidden_ocr_text_on_scanned_page(self):
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=300)
        page.insert_text((40, 60), "Patient Jane Doe", fontsize=11, render_mode=3)  # invisible, like OCR
        out, _, leaks = redact_terms(doc, "Jane Doe")
        self.assertEqual(leaks, [])
        self.assertNotIn("Jane", out[0].get_text())

    def test_rotated_pages(self):
        for rot, upright in [(r, u) for r in (90, 180, 270) for u in (False, True)]:
            with self.subTest(rotation=rot, upright=upright):
                doc = make_doc(rotation=rot, upright=upright)
                hits = core.find(doc, core.pattern(["Smith"]))
                # displayed coords must lie inside the displayed (rotated) page
                for h in hits:
                    for r in h.rects:
                        self.assertTrue(doc[0].rect.contains(pymupdf.Rect(r)))
                out, _, leaks = redact_terms(doc, "Smith")
                self.assertEqual(leaks, [])
                self.assertNotIn("Smith", out[0].get_text())
                self.assertIn("Phone", out[0].get_text())

    def test_overlapping_annotations_are_removed(self):
        doc = make_doc()
        page = doc[0]
        page.add_text_annot((60, 55), "comment mentioning John Smith")
        page.add_text_annot((300, 250), "unrelated")
        page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(40, 45, 200, 65), "uri": "mailto:john"})
        out, _, _ = redact_terms(doc, "John Smith", scrub=False)
        self.assertEqual([a.info["content"] for a in out[0].annots()], ["unrelated"])
        self.assertEqual(out[0].get_links(), [])

    def test_scrub_removes_metadata(self):
        doc = make_doc()
        doc.set_metadata({"author": "John Smith", "title": "Secret"})
        doc.set_toc([[1, "About John Smith", 1]])
        out, _, leaks = redact_terms(doc, "John Smith")
        self.assertFalse(out.metadata.get("author"))
        self.assertEqual([lk for lk in leaks if "metadata" in lk], [])
        # bookmarks are not scrubbed, but verify() flags them
        self.assertTrue(any("bookmark" in lk for lk in leaks))

    def test_scrub_keeps_unrelated_fields_links_and_ocr_text(self):
        doc = make_doc()
        page = doc[0]
        for name, rect, value in [("name", (40, 45, 200, 64), "John Smith"), ("other", (40, 200, 200, 220), "keep me")]:
            w = pymupdf.Widget()
            w.field_type, w.field_name, w.rect, w.field_value = pymupdf.PDF_WIDGET_TYPE_TEXT, name, pymupdf.Rect(rect), value
            page.add_widget(w)
        page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(300, 250, 380, 270), "uri": "https://example.com"})
        page.insert_text((40, 280), "ocr layer text", fontsize=9, render_mode=3)
        notes = core.apply(doc, core.hits_to_ops(core.find(doc, core.pattern(["John Smith"]))))
        out = roundtrip(doc)
        self.assertEqual(notes, ["page 1: removed form field 'name' (overlaps a redacted region)"])
        self.assertEqual([(w.field_name, w.field_value) for w in out[0].widgets()], [("other", "keep me")])
        self.assertEqual(len(out[0].get_links()), 1)
        self.assertIn("ocr layer text", out[0].get_text())

    def test_pending_redaction_marks_are_applied_in_full(self):
        doc = make_doc()
        page = doc[0]
        red = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20), False)
        red.set_rect(red.irect, (255, 0, 0))
        page.insert_image(pymupdf.Rect(200, 150, 300, 250), pixmap=red)
        page.add_redact_annot(pymupdf.Rect(200, 150, 300, 250))  # marked in the file, never applied
        notes = core.apply(doc, core.hits_to_ops(core.find(doc, core.pattern(["John"]))))
        out = roundtrip(doc, scrub=False)
        self.assertIn("page 1: applied redaction marks already present in the file", notes)
        self.assertNotEqual(out[0].get_pixmap().pixel(250, 200)[:3], (255, 0, 0))  # image pixels gone

    def test_verify_detects_unapplied_text(self):
        doc = make_doc()
        ops = core.hits_to_ops(core.find(doc, core.pattern(["Smith"])))
        self.assertTrue(core.verify(doc, ops))


class ReplaceTests(unittest.TestCase):
    def _replace(self, doc, old, new):
        hits = core.find(doc, core.pattern([old]))
        core.apply(doc, core.hits_to_ops(hits, "replace", new))
        return roundtrip(doc), hits

    def _spans(self, page, needle):
        return [s for b in page.get_text("dict")["blocks"] for line in b.get("lines", [])
                for s in line["spans"] if needle in s["text"]]

    def test_replace_keeps_position_size_colour(self):
        doc = make_doc(color=(1, 0, 0))
        before = self._spans(doc[0], "Phone")[0]
        out, _ = self._replace(doc, "555-1234", "000-0000")
        text = out[0].get_text()
        self.assertNotIn("555-1234", text)
        self.assertIn("000-0000", text)
        self.assertIn("555-9876", text)
        new = self._spans(out[0], "000-0000")[0]
        self.assertAlmostEqual(new["size"], before["size"], delta=0.5)
        self.assertAlmostEqual(new["origin"][1], before["origin"][1], delta=0.5)
        self.assertEqual(new["color"], 0xFF0000)

    def test_longer_replacement_keeps_size_when_line_has_room(self):
        out, _ = self._replace(make_doc(), "Smith", "Smithersonington-Jones")  # last word on the line
        self.assertEqual(self._spans(out[0], "Smithersonington")[0]["size"], 11)

    def test_longer_replacement_shrinks_only_to_clear_next_word(self):
        doc = make_doc()
        smith_x0 = [w for w in doc[0].get_text("words") if w[4] == "Smith"][0][0]
        out, _ = self._replace(doc, "John", "Jonathan-Alexander")
        span = self._spans(out[0], "Jonathan")[0]
        self.assertLess(span["size"], 11)
        self.assertLess(span["bbox"][2], smith_x0)  # stops before the next word
        self.assertGreater(span["bbox"][2], smith_x0 - 4)  # ...but uses the space up to it

    @unittest.skipUnless(os.path.exists(DEJAVU), "DejaVuSans not installed")
    def test_replacement_uses_embedded_font(self):
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=100)
        page.insert_font(fontname="dj", fontfile=DEJAVU)
        page.insert_text((20, 50), "Account holder Jane Citizen", fontname="dj", fontsize=11, color=(0, 0, 1))
        out, _ = self._replace(doc, "Jane", "June")
        span = self._spans(out[0], "June")[0]
        self.assertEqual((span["font"], span["size"], span["color"]), ("DejaVuSans", 11, 0x0000FF))
        self.assertIn("Account holder June Citizen", displayed_words(out[0]))

    @unittest.skipUnless(os.path.exists(DEJAVU), "DejaVuSans not installed")
    def test_font_with_long_name_is_reused(self):
        # MuPDF cuts span font names to 31 chars (subset tag included) - seen with
        # 'AAAAAA+AnnieUseYourTelescope-Regular' in real forms
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=100)
        page.insert_font(fontname="dj", fontfile=DEJAVU)
        page.insert_text((20, 50), "Account holder Jane Citizen", fontname="dj", fontsize=11)
        long = "/AAAAAA+DejaVuSansWithAVeryLongName-Regular"
        xref = page.get_fonts(full=True)[0][0]
        doc.xref_set_key(xref, "BaseFont", long)
        doc.xref_set_key(int(doc.xref_get_key(xref, "DescendantFonts")[1].strip("[]").split()[0]), "BaseFont", long)
        doc = core.open_pdf(doc.tobytes())
        notes = core.apply(doc, core.hits_to_ops(core.find(doc, core.pattern(["Jane"])), "replace", "June"))
        self.assertEqual(notes, [])
        self.assertEqual(self._spans(roundtrip(doc)[0], "June")[0]["font"], "DejaVuSansWithAVeryLongN")

    @unittest.skipUnless(os.path.exists(DEJAVU), "DejaVuSans not installed")
    def test_subset_font_missing_glyphs_falls_back_with_note(self):
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=100)
        page.insert_font(fontname="dj", fontfile=DEJAVU)
        page.insert_text((20, 50), "Account holder Jane Citizen", fontname="dj", fontsize=11)
        doc = core.open_pdf(doc.tobytes())
        doc.subset_fonts()  # keep only the glyphs in use: no 'q', 'w', 'x' ...
        doc = core.open_pdf(doc.tobytes(garbage=3))
        notes = core.apply(doc, core.hits_to_ops(core.find(doc, core.pattern(["Jane"])), "replace", "Quwxa"))
        out = roundtrip(doc)
        self.assertEqual(self._spans(out[0], "Quwxa")[0]["font"], "Helvetica")
        self.assertEqual(len(notes), 1)
        self.assertIn("standard font", notes[0])
        # letters the subset does have are still written in it
        doc2 = pymupdf.open(stream=out.tobytes())
        notes = core.apply(doc2, core.hits_to_ops(core.find(doc2, core.pattern(["Citizen"])), "replace", "Count"))
        self.assertEqual((notes, core._font_key(self._spans(roundtrip(doc2)[0], "Count")[0]["font"])), ([], "dejavusans"))

    def test_replacement_preserves_background(self):
        doc = make_doc()
        doc[0].draw_rect(pymupdf.Rect(30, 45, 300, 70), fill=(1, 1, 0), overlay=False)  # yellow highlight
        out, hits = self._replace(doc, "John", "Paul")
        x0, y0, x1, y1 = hits[0].rects[0]
        pix = out[0].get_pixmap(clip=pymupdf.Rect(x0, y0, x1, y0 + 1))
        self.assertEqual(pix.pixel(0, 0)[:3], (255, 255, 0))

    def _dir_of(self, page, needle):
        return [ln["dir"] for b in page.get_text("dict")["blocks"] for ln in b["lines"]
                if needle in "".join(s["text"] for s in ln["spans"])][0]

    def test_replace_on_rotated_pages_is_upright(self):
        for rot in (0, 90, 180, 270):
            with self.subTest(rotation=rot):
                out, _ = self._replace(make_doc(rotation=rot, upright=True), "John", "Paul")
                page = out[0]
                self.assertIn("Name: Paul Smith", displayed_words(page))
                m = page.rotation_matrix
                shown = pymupdf.Point(self._dir_of(page, "Paul")) * m - pymupdf.Point(0, 0) * m
                self.assertAlmostEqual(shown.x, 1, places=3)

    def test_replace_follows_sideways_text(self):
        for rot in (90, 270):
            with self.subTest(rotation=rot):
                doc = make_doc(rotation=rot)
                before = self._dir_of(doc[0], "Smith")
                out, _ = self._replace(doc, "John", "Paul")
                self.assertEqual(self._dir_of(out[0], "Paul"), before)

    def test_replace_box_in_empty_area(self):
        for rot in (0, 90):
            with self.subTest(rotation=rot):
                doc = make_doc(rotation=rot)
                core.apply(doc, [core.Op(0, (40, 200, 200, 220), "replace", "Approved")])
                out = roundtrip(doc)
                self.assertEqual(out[0].get_text(clip=pymupdf.Rect(40, 200, 200, 220) * out[0].derotation_matrix).strip(),
                                 "Approved")

    def test_replace_on_ocr_scan_clears_image_pixels(self):
        paper = (240, 228, 200)  # beige, so a white patch would show
        for rot in (0, 90):
            with self.subTest(rotation=rot):
                # a "scan": the text as an image on tinted paper, plus an invisible OCR layer
                src = pymupdf.open()
                sp = src.new_page(width=300, height=100)
                sp.draw_rect(sp.rect, color=None, fill=[v / 255 for v in paper])
                sp.insert_text((20, 50), "Patient Jane Doe", fontsize=14)
                doc = pymupdf.open()
                page = doc.new_page(width=300, height=100)
                page.insert_image(page.rect, pixmap=sp.get_pixmap(dpi=144))
                page.insert_text((20, 50), "Patient Jane Doe", fontsize=14, render_mode=3)
                page.set_rotation(rot)
                out, hits = self._replace(doc, "Jane", "Mary")
                r = pymupdf.Rect(hits[0].rects[0])
                self.assertIn("Mary", out[0].get_text())
                # with the text layer removed: nothing of "Jane" is left, and the spot is paper-coloured
                bare = pymupdf.open(stream=out.tobytes())
                bare[0].add_redact_annot(bare[0].rect * bare[0].derotation_matrix)
                bare[0].apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                         graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
                spot = bare[0].get_pixmap(clip=r)
                colours = {spot.pixel(x, y)[:3] for x in range(spot.width) for y in range(spot.height)}
                self.assertTrue(all(max(abs(a - b) for a, b in zip(c, paper)) <= 3 for c in colours), colours)
                # ...while the rest of the scan is intact
                self.assertGreater(sum(v < 128 for v in bare[0].get_pixmap(colorspace=pymupdf.csGRAY).samples), 100)

    def test_non_latin_replacement(self):
        out, _ = self._replace(make_doc(), "John", "约翰")
        self.assertIn("约翰", out[0].get_text())

    def test_bold_style_kept(self):
        doc = pymupdf.open()
        doc.new_page().insert_text((40, 60), "Total: 1000", fontname="hebo", fontsize=12)
        out, _ = self._replace(doc, "1000", "2000")
        span = self._spans(out[0], "2000")[0]
        self.assertTrue(span["flags"] & pymupdf.TEXT_FONT_BOLD)

    def test_blackout_beats_overlapping_replace(self):
        doc = make_doc()
        r = core.find(doc, core.pattern(["John"]))[0].rects[0]
        core.apply(doc, [core.Op(0, r, "replace", "Paul"), core.Op(0, r)])
        self.assertNotIn("Paul", roundtrip(doc)[0].get_text())


def stream_doc(content):
    """One page drawing `content` (raw operators) with Helvetica as /helv."""
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=100)
    page.insert_text((0, -50), "x", fontname="helv")  # registers /helv
    doc.update_stream(page.get_contents()[0], content)
    return doc


class LineShiftTests(unittest.TestCase):
    """MuPDF regression guard: PyMuPDF 2.0 shifted the rest of the line after
    removing glyphs mid-string (README "How it works"); the pinned 1.28 does not."""

    def assert_no_shift(self, doc, term):
        keep = lambda page: [(c.c, round(c.bbox.x0, 2)) for c in core.page_chars(page)[1] if c and not c.c.isspace()]
        target = {(c.c, round(c.bbox.x0, 2)) for h in core.find(doc, core.pattern([term]))
                  for c in core.chars_in(core.page_chars(doc[h.page])[1], pymupdf.Rect(h.rects[0]))}
        before = [k for k in keep(doc[0]) if k not in target]
        core.apply(doc, core.hits_to_ops(core.find(doc, core.pattern([term]))))
        out = roundtrip(doc)
        self.assertNotIn(term, out[0].get_text())
        self.assertEqual(keep(out[0]), before)

    def test_mid_line_redaction_does_not_shift_rest_of_line(self):
        # Each of these shifted the rest of the line with MuPDF's own redaction.
        for show, term in [(b"[(Pay J)10(ohn now ok)]TJ", "John"), (b"(ab cd ef)Tj", "d"),
                           (b"[(ab cd ef)]TJ", "cd"), (b"[(ab)5(cd)5(ef)]TJ", "bcd")]:
            with self.subTest(show=show):
                self.assert_no_shift(stream_doc(b"BT /helv 12 Tf 1 0 0 1 20 50 Tm 0.3 Tc 1 Tw " + show + b" ET"), term)

    @unittest.skipUnless(os.path.exists(DEJAVU), "DejaVuSans not installed")
    def test_embedded_two_byte_font(self):
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=100)
        page.insert_font(fontname="dj", fontfile=DEJAVU)  # embedded as Type0 / Identity-H
        page.insert_text((20, 50), "Account holder Jane Citizen, BSB 062-000", fontname="dj", fontsize=11)
        self.assertEqual(page.get_fonts()[0][2], "Type0")
        self.assert_no_shift(doc, "Jane")


class ErrorTests(unittest.TestCase):
    def test_not_a_pdf(self):
        with self.assertRaises(core.RedactError):
            core.open_pdf(b"hello world")

    def test_password(self):
        data = make_doc().tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
        with self.assertRaises(core.RedactError):
            core.open_pdf(data)
        self.assertEqual(core.open_pdf(data, "pw").page_count, 1)

    def test_page_out_of_range(self):
        with self.assertRaises(core.RedactError):
            core.apply(make_doc(), [core.Op(3, (0, 0, 10, 10))])

    def test_bad_op_dict(self):
        for bad in ({"page": 0, "rect": [1, 2, 3]}, {"page": 0, "rect": [0, 0, 1, 1], "kind": "erase"}, {"rect": [0] * 4}):
            with self.assertRaises((core.RedactError, KeyError)):
                core.Op.from_dict(bad)

    def test_inverted_and_empty_rects(self):
        doc = make_doc()
        core.apply(doc, [core.Op(0, (400, 64, 0, 40)), core.Op(0, (10, 10, 10, 10))])
        self.assertNotIn("John", roundtrip(doc)[0].get_text())


if __name__ == "__main__":
    unittest.main()
