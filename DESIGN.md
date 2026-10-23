# pdf-redaction-tool: design notes

How the tool works, why it works that way, and how it was tested. Usage is
in the [README](README.md). The experiments below are dated and kept as they
happened, including approaches that were tried and dropped.

The test files are referred to by what they are, not by name: a 14-page
company guide (subset TrueType and CID fonts, kerned text,
filled form fields and a drawn signature), a 212-page scanned book with an
invisible OCR text layer, and 36 generated form PDFs.

## What gets removed

- **Text** whose glyphs are under a region, including invisible text (OCR
  layers of scanned pages) and spaces (their positions would reveal the
  number and length of redacted words).
- **Image pixels** under a blackout are blanked inside the image itself, so
  extracting the image doesn't recover them. Vector graphics fully inside a
  blackout are removed.
- **Annotations, form fields and links** overlapping any region are deleted
  whole (a field's value can't be partially redacted). Each is reported.
- With scrub on (default): document metadata, XMP metadata, JavaScript,
  attachments, embedded files and page thumbnails. Form fields, links,
  comments and OCR layers elsewhere in the document are kept.
- The saved file is rewritten without the old, now unreferenced, content.

## How replacement works

The old text is removed and the new text is written on the same baseline, at
the same size and colour, **with the document's own font** - the very font
object the original used, so it looks the same and copies/searches
correctly. Nothing is painted behind it, so the page's own background (a
coloured band, a table shading) stays exactly as it was. On a scanned page
with an OCR text layer, the image pixels of the old words are cleared too
and refilled with the colour of the paper around them.

In the editor, a replace region previews the new text on the colour sampled
from the page around it, so it looks like the result rather than a box.

- If the new text is longer, it uses the free space after it (up to the next
  word, another marked region or the page edge) at full size, and is only
  shrunk if even that isn't enough. The rest of the line does not move: PDF
  has no text flow, so pushing the following words along would break
  justified lines and column edges.
- Embedded fonts are usually subsets holding only the letters the document
  uses. If the original font can't draw a letter of the new text, the
  replacement is written in the closest standard font (Helvetica / Times /
  Courier family, bold/italic kept; a built-in CJK font for non-Latin text)
  and the result says so.

## Limitations

- Bookmarks, comments, links and metadata are cleaned for *searched* terms
  only; a hand-drawn box can't know what text a bookmark elsewhere repeats.
- A replacement can only use the original font if that font object belongs
  to the page itself (not only to an embedded form/XObject), and for CID
  fonts if it uses the common Identity-H encoding; otherwise a standard font
  is used (reported).
- Text that is part of an image (a scan without OCR) can only be removed by
  drawing a box over it: search finds only real text.
- If a glyph from another line overlaps a redacted glyph so closely that
  MuPDF can't remove one without the other (seen with a 26pt bracket next to
  normal text), both are removed and the result notes it.

## How it works

`src/pdf_redact/core.py` does all the work, on top of PyMuPDF's redaction
annotations. The design came out of the experiments below (2026-10-22). Each
one was a question PyMuPDF's documentation didn't answer, settled by
measuring.

### 1. PyMuPDF's own "search and redact" wipes neighbouring lines

The obvious approach, `page.search_for(term)` and then `add_redact_annot` +
`apply_redactions`, removed words from the lines above and below the target
at every line spacing tried (1.0x, 1.1x, 1.2x the font size). `search_for`
returns the font's full ascender/descender box (1.37x the font size for
Helvetica, 1.71x for Poppins in a real file), and that box reaches into the
neighbouring lines. `TOOLS.set_small_glyph_heights(True)` did not change
`search_for`'s boxes.

### 2. MuPDF's removal rule, measured

Sweeping 0.5pt bands and squares across a 20pt glyph showed that MuPDF
removes a glyph when the redaction rect intersects its font box inset by
about 10% on every side: bands at y ≤ 80.5 left it, bands from 81.0 removed
it, against a box edge of 78.5 and a predicted inset edge of 81.25. Touching
a box edge never removes the glyph; a 0.5pt square at its centre always does.

### 3. Design: decouple *what is removed* from *what is painted*

From (1) and (2), a rect that visually covers one line's descenders always
reaches the next line's inset box once spacing drops below about 1.16x the
font size, and that is common (Times single spacing is about 1.15x). So:

- A region selects the glyphs whose estimated ink centre lies inside it.
  Ink is taken as 0.8x the font size above the baseline and 0.22x below.
- **Pass 1** removes exactly those glyphs, using thin bands through their
  box centres (one band per run of glyphs on a line, since 2000 separate
  marks took 19s to apply). If a band would touch another glyph's inset box,
  it falls back to one mark per glyph. Each mark is placed on a grid inside
  the glyph's own box, at the spot clear of its neighbours. This was found
  necessary for a 43pt bracket overlapping a text line in a real book.
- **Pass 2** paints the region black and blanks image pixels, with text
  removal switched off (`PDF_REDACT_TEXT_NONE`).
- Afterwards every glyph that should have stayed is checked, by character
  and position within 0.15pt, against a fresh extraction. Anything missing or
  moved is reported as a note.

Search results are drawn as the union of the matched glyphs' ink boxes, so
what you see marked is what gets removed.

### 4. Why PyMuPDF 1.28

With PyMuPDF 2.0 (the latest release), removing glyphs from the middle of a
text string shifted the rest of the line left. Minimal hand-written content
streams pinned it down:

- in a `TJ` array, the advance of a removed glyph that ends a string element
  is lost (`[(abc)10(def)]TJ`, remove `c` → the rest moves 6.00pt, the width
  of `c`);
- in a `Tj`, removing a single glyph emits no repositioning at all;
- 300 random removals from a kerned line moved kept glyphs by up to 40.5pt.

A workaround that rewrote every text operator into one-glyph string
elements fixed it (0.000pt over 300 trials, pixel-identical rendering), but
it needed a content-stream tokenizer. Re-running the same experiments on
PyMuPDF **1.28.2** showed none of these shifts (0.000pt on the original
layout), with identical results on the real-PDF stress set. 1.28.2 also has no
dependencies, while 2.0 pulls in onnxruntime, numpy, networkx and two
further PyMuPDF packages for features this tool doesn't use. So the
dependency is pinned `>=1.28,<2` and the tokenizer was removed.
`tests/test_core.py::LineShiftTests` fails on 2.0 (verified), so a future
upgrade to an affected version can't go unnoticed. The post-removal check in
(3) would also report any shift at runtime.

### 5. Other things found on real files

- `Document.scrub()` crashed (`cannot find object in xref (82 0 R)`) on a
  real file with a dangling object reference, even untouched. After a
  garbage-collecting rewrite it works, so `to_bytes()` scrubs a rewritten copy.
- `scrub()`'s defaults also empty **every** form field, delete every link and
  every invisible text layer in the document. In a test file that wiped a
  filled-in name and date nowhere near the redaction. Those options are
  turned off, since what overlaps a region is already handled per region.
- MuPDF's redaction never touches form-field values; their text is in the
  field, not the page content. Overlapping fields are therefore deleted, for
  replacements too (otherwise the old value would sit, still extractable,
  under the new text). `Document.bake()` (flatten) was considered, but it is
  document-wide and did not render identically.
- `tobytes(garbage=4, clean=True)` took 14.5s on a 37MB, 212-page book.
  `garbage=1` (enough to drop unreferenced objects) without `clean` takes
  about 2–4s.
- Building per-glyph boxes eagerly made search over that book take 14.4s;
  computing them lazily brought it to 1.0s.

### 6. Second round (2026-10-22): colours, and replacing in the original font

Feedback: blackouts "too light, needed applying several times", and
replacements looked like a text box pasted over the original.

- **Blackout colour.** The saved PDF was already solid black (pixel-checked
  in the tests); the *editor preview* was drawn 72% opaque so the text under
  it stayed readable, and stacking boxes made it darker. Previews are now
  drawn exactly as saved, see-through only while hovered, with a palette.
- **Original font, first attempt: reload the embedded font program** and
  write with it by Unicode. A survey of real fonts showed why that fails:
  fonts without a Unicode cmap (MinionPro CFF in the guide, the handwriting
  fonts in the forms, and anything subset by PyMuPDF's own `subset_fonts()`)
  can't be looked up at all; and `Font.glyph_bbox()` reported every letter
  present for subsets whose outlines had been stripped - rendering showed
  one SourceSans3 subset could draw only `a r v M R W 0 1 2 6`.
- **Adopted: reuse the original font object.** The text is encoded through
  the font's `/ToUnicode` map (or WinAnsi/MacRoman for simple fonts without
  one), measured with the PDF's `/W` / `/Widths` (standard 14 metrics when a
  font has none), and drawn with raw operators referencing the page's own
  font resource. Nothing is re-embedded and the result extracts correctly.
  Availability needs three checks, each found necessary on a real case:
  the character is mapped; it has its own width (subsets list widths only
  for glyphs they contain); and it actually draws ink, checked by rendering
  the spot before and after - with the write undone if any glyph is blank.
  The ink check alone was fooled by DejaVu's visible `.notdef` box; the
  width check alone missed PyMuPDF subsets, which keep the full `/W` but
  strip outlines.
- `page.transformation_matrix` (PDF ↔ PyMuPDF space) gave wrong positions
  on rotated pages whose MediaBox isn't at the origin (off by exactly the
  offset); PyMuPDF measures from the CropBox's top-left, so the conversion
  is done explicitly from the CropBox and MediaBox (verified for 0/90/180/270°
  with an offset MediaBox and an inset CropBox).
- **Scans.** Rendering the results showed old and new words superimposed on
  the book although extraction said the old words were gone: it is a scan
  (one full-page image per page) with an invisible OCR text layer. A
  replacement whose old text isn't visible text now also clears the image
  pixels under it.
- Replacements keep their size when there is free space after them. The
  first version only avoided *kept* text, and the stress run promptly showed
  leaks: a replacement grew into a neighbouring region that was being
  blacked out and ended up, extractable, under the black box. Other regions
  are now obstacles too.

### 7. Third round (2026-10-22): no "text box" look; clickable drop zone

Feedback: a replacement looked like a coloured box laid over the text, and
the "choose or drop" area couldn't be clicked.

- The saved PDF never painted anything behind a replacement on normal pages
  (checked: a light-blue band behind a replaced phone number is untouched).
  The box came from (a) the editor preview, drawn on white, and (b) scans,
  where MuPDF refills cleared image pixels with pure white (255, 255, 255),
  which shows on tinted paper. Both now use the median colour of a thin ring
  just outside the region - sampled from the rendered page in the browser,
  and from the PDF before clearing for scans (tested on beige paper at 0°
  and 90°).
- The drop zone was a plain `div` with no click handling; clicking it now
  opens the file chooser (tested via the browser's file-chooser event).
- While re-running the replacement check, making *every* standard-font
  fallback produce a note revealed that the previous round's figure was
  overstated: fallbacks without missing letters had been silent, and 34 of
  895 replacements had used a standard font. All were one handwriting font
  in the generated forms: MuPDF reports span font names cut to 31
  characters, subset tag included (`AAAAAA+AnnieUseYourTelescope-Regular`
  → `AnnieUseYourTelescope-Re`), so the name match failed. Matching the
  truncated BaseFont too fixed all 34 (reproduced in a unit test by
  renaming an embedded font). No note is given when the original is itself
  a standard 14 font, since the substitute is then the same font.

### 8. Fourth round (2026-10-23): is the underlying data really gone?

Question: after a blackout or replacement, is the data under it wiped from
the file, or only from the visible page? Earlier tests checked the visible
layers (extracted text, content streams, image pixels under a box).

**Method.** A trap PDF (`tests/test_forensic.py`) plants one token in 17
places: (1) visible text, (2) invisible OCR text, (3) image pixels with an
OCR layer 5pt off, (4) marked-content `/ActualText`, (5) a hidden
optional-content layer, (6) a form XObject, (7) `/PieceInfo`, (8) comments
on and away from the text, (9) a form field, (10) a link URI, (11) metadata,
(12) a bookmark, (13) an attachment, (14) JavaScript run on opening,
(15) an older revision kept by an incremental save, (16) structure-tree
`/Alt` (and later (17), a page thumbnail). `scripts/forensic_check.py` then searches every object and every
decompressed stream (plain, hex and UTF-16 forms), the raw bytes, text with
hidden layers off and on, bookmarks, metadata, attachments, fields,
annotations and links. On the input it finds the token in 33 places.

**Result before fixes: 13 places survived** a normal *Black out matches* +
apply + scrub. Gone already: (1) (2) (6) (9 - it overlapped a match) (11)
(13) (15). Survived, and how each was fixed:

- (5) hidden layer: PyMuPDF's extraction skips hidden layers, so search and
  the glyph selection never saw the text. MuPDF's redaction itself does
  remove hidden-layer glyphs in a rect (tested with the layer on and off).
  `set_layer()` was tried first to switch layers on and silently changed
  nothing; `set_layer_ui_config()` works. All layers are now on while
  finding and redacting, and restored afterwards (the output keeps the layer
  hidden). A replacement whose old text was only on a hidden layer is
  removed but not rewritten - writing it would make it visible.
- (4) `/ActualText`: MuPDF removed the glyphs but kept the marked-content
  tag, and extraction still reported its text. (16) `/Alt` in the structure
  tree and (7) `/PieceInfo` are untouched by redaction. On redacted pages
  these are now stripped (content streams, property lists, structure
  elements on that page). In the real 14-page guide 45 such entries were
  removed from its redacted pages in the stress run - the cost is lost alt
  text for screen readers on those pages. (An earlier note here called the
  guide a tagged PDF; checked later, it has no structure tree - the entries
  are marked-content alt text in its page content.)
- Follow-up: listing the guide's document-level data before and after a
  scrubbed save (InDesign creator, producer, dates, 2,483 chars of XMP, 14
  page thumbnails - all removed) showed one tie to the original left: the
  first half of the trailer `/ID`, a permanent identifier, was unchanged.
  Scrubbing now writes a fresh random ID.
- (14) JavaScript: `scrub(javascript=True)` left the catalog's
  `/OpenAction` script; JavaScript open actions and `/AA` are now removed.
- (8) comments away from the text, (10) the link, (12) the bookmark: a page
  region can't reach them. For *searched* terms, `scrub_matches()` now
  deletes comments, fields, links and attachments mentioning them and
  replaces them in bookmark titles and metadata. In the editor, each found
  region carries its search, so Undo drops this too.
- (3) the image: 62 of 834 dark pixels of the token survived, because the
  OCR box was offset from the word in the picture and the blackout followed
  the OCR box. Blackouts overlapping an image are now padded by 20% of the
  line height for pixel blanking.

  Follow-up the same day, measuring an image-only scan: that padding was
  applied to *every* blackout over an image and scaled with the box, so a
  box drawn by hand on a scan came out ~6pt larger per side (and a big box
  far more). It now applies only when the region holds OCR text, capped at
  4pt; a box with no text under it is painted exactly as drawn. The same
  measurement showed the scan's JPEG is stored again losslessly (PNG/Flate):
  outside the box the image data is bit-for-bit identical, but that page's
  image grew from 210 KB to 576 KB.

  A second follow-up: asked whether wiping depends on *scrub metadata*, the
  trap was re-run with scrub **off**. Two things survived: the JavaScript
  open action (holding the token) and a page **thumbnail** - added to the
  trap as place (17), a picture of the unredacted page that a text scan
  can't see. Neither should hang on a metadata option: thumbnails of
  redacted pages are now always removed, and JavaScript mentioning a
  searched term is always emptied. With scrub off the trap now also scans
  clean (`test_scrub_off_still_wipes_the_term`).

**Result after fixes: 0 places, 0 dark pixels**, for blackout and replace,
through the library, the CLI and the web API (all in the test suite). The
real-PDF stress and replacement runs were repeated without change.

## Testing

```sh
uv run python -m unittest discover -s tests            # 59 tests
uv run python -I scripts/forensic_check.py OUT.pdf TERM...  # is a term anywhere in the file?
uv run python -I scripts/stress_real_pdfs.py FILE.pdf...  # random redactions on real PDFs
uv run python -I scripts/check_replace.py FILE.pdf...     # replacements: font, extraction, overlap
uv run python -I scripts/check_scanned.py SCAN.pdf 1      # image pixels destroyed, not covered
```

`stress_real_pdfs.py` redacts random words on every page (two thirds
blackouts, one third replacements), then counts leaks (text left under a
blackout) and collateral (other glyphs missing or moved by more than
0.15pt). It prints counts only, never document text. Final results on
2026-10-22, PyMuPDF 1.28.2, seeds 1–5, 10 words per page (190 file runs):

| file | what it exercises | pages | glyphs | leaks | collateral |
|---|---|---|---|---|---|
| 14-page company guide | TrueType + Type0 (Identity-H) subset fonts, kerned `TJ` text, filled form fields, ink signature, dangling xref | 14 | 13,081 | 0 | 0 |
| 212-page scanned book | 37MB, scanned with an invisible OCR layer, oversized bracket glyphs | first 20 | 39,313 | 0 | 1 in 2 of 5 seeds¹ |
| 36 generated form PDFs | ReportLab templates, pypdf-merged instances | 3–6 each | 1.4k–3k each | 0 | 0 |

¹ the same glyph each time: the tall-glyph case in
[Limitations](#limitations), which the tool reports itself. These numbers
were re-run, unchanged, after the second round.

`check_replace.py` replaces random words with the word reversed (and, with
`--longer`, doubled), then checks the old word is gone, the new one
extracts, which font it used, and that it doesn't run into other text.
On the same 38 files (after the third round): of 895 replacements, 894 were
written with the document's own font object and 1 with standard
Helvetica-Bold replacing a non-reusable Helvetica-Bold (the same font); all
extract correctly, none left an original word behind, and none caused an
overlap - for both same-length and doubled text. (14 replacements overlap
another word, but each original word already did: form values typed over
labels.) The second round's "895 of 895" was an overcount, see section 7.

`check_scanned.py` on an image-only scan: every pixel of the embedded image
inside the box became one uniform value, while outside it 125 of 226,767
samples differed, all on the box boundary. The browser editor was tested
end to end with Playwright and headless Chromium (opening a file, drawing,
replace prompts, colour palette and recolouring, Undo per action (a Find batch, a recolour), Undo all and undoing it, Preview (result pages, no overlays, drawing disabled, Back and edit-to-leave), replace previews on a coloured background, opening a file by clicking the drop zone, search on normal and rotated pages, find & replace, ×,
undo, zoom, download, a bad upload, console errors), and the downloaded PDF
was checked for what was removed, replaced and kept.

## Layout

```
src/pdf_redact/core.py     redaction engine (find, apply, verify, save)
src/pdf_redact/cli.py      `pdf-redact redact` / `pdf-redact serve`
src/pdf_redact/web.py      stdlib HTTP server for the editor (127.0.0.1 only)
src/pdf_redact/index.html  the editor (vanilla JS, no build step)
tests/                     unittest suites (core; CLI + HTTP API)
scripts/                   verification tools for real PDFs
```
