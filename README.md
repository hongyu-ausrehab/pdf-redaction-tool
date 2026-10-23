# pdf-redaction-tool

Black out or replace text in PDFs - in your browser or from the command line.

Redacted text is **removed from the file**, not just covered: it can't be
copied, searched or extracted afterwards. Replacements are written in place,
in the document's own font, size and colour. Everything runs locally; files
never leave your machine.

## Install

Needs Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/hongyu-ausrehab/pdf-redaction-tool.git
cd pdf-redaction-tool
uv sync
```

## Use it in the browser

```sh
uv run pdf-redact serve
```

Then open <http://127.0.0.1:8765/> in your browser (`--port` picks another port).

1. **Open** a PDF: click the drop area in the middle of the page or drag a
   file onto it, or use *Open PDF…*. To switch to another PDF later, use
   *Open PDF…* again or drop it anywhere on the window (you're asked first if
   you have changes you haven't downloaded).
2. **Mark what to change.** The top row of the toolbar has one block per
   action, Blackout and Replace text; the highlighted one is what dragging on
   a page does. The second row holds File, View and Edit & save.
   - **Blackout**: pick *draw boxes* (key `B`) and drag over anything to hide
     it. Choose the colour from the palette. Or type text to hide, one per
     line, and click *Find* to mark every occurrence - tick *regex* for
     patterns, e.g. `\b04\d{2} ?\d{3} ?\d{3}\b` for mobile numbers.
   - **Replace text**: pick *draw boxes* (key `R`) and drag over the text to
     replace - you're asked for the new text. Or type the old and new text
     and click *Find* to mark every occurrence.
   - Each search has its own *match case*.
3. **Check** the marked regions. Hover one to see through it, click **×** to
   drop it, click a blackout to give it the current colour, double-click a
   replacement to edit it. **Undo** steps back one action; **Undo all**
   starts over.
4. **Preview** shows the finished result right in the page view - the real
   redacted pages, nothing downloaded. Click *Back to editing* (or just make
   any change) to keep editing.
5. **Apply & download** saves `NAME-redacted.pdf`. Your original file is not
   changed. The status line lists anything worth checking.

The dino in the top-right corner is just for fun: click it (or click it three
times quickly). Left alone it paces and looks around, and after a minute of
no activity it falls asleep (any key or mouse movement wakes it). It wears
sunglasses while you preview, frets when something
needs your attention, and celebrates each download with a victory lap to the
left of the page (out from under the browser's download pop-up). It stays still if your
system is set to reduce motion.

*Scrub metadata* (in Edit & save, on by default) is an extra clean-up of the
whole document. It does **not** decide whether your redactions are wiped -
that happens either way (see [What gets wiped](#what-gets-wiped)). On top, it
removes: document info (author, title, subject, keywords, creating program,
dates), XMP metadata, all attachments, all JavaScript, all page thumbnails,
redaction marks never applied, and the file's internal ID (replaced, so the copy
can't be matched to the original by it). Form fields, links, comments and OCR text
elsewhere are kept. Turn it off only if you need to keep metadata or
attachments.

## Use it from the command line

```sh
# black out every occurrence of some text, and a regex
uv run pdf-redact redact in.pdf out.pdf -t "John Smith" -r '[\w.+-]+@[\w-]+\.[\w.]+'

# replace text
uv run pdf-redact redact in.pdf out.pdf --replace "John Smith" "Jane Citizen"

# black out a box on page 2 (PDF points from the top-left: x0,y0,x1,y1), in blue
uv run pdf-redact redact in.pdf out.pdf -b 2:72,100,300,140 --color "#1f4e9c"

# only list what would be changed
uv run pdf-redact redact in.pdf -t "John Smith" --dry-run
```

`uv run pdf-redact redact --help` lists all options. After saving, the output
is checked; if a search term can still be found anywhere it prints a warning
and exits with code 2.

## What gets wiped

Not just the visible page. The underlying data is removed from the file -
**always, whether *scrub metadata* is on or off**:

- the text itself, including invisible OCR layers and text on **hidden
  layers**, and the image pixels under a blackout (also on scans);
- hidden copies on the redacted pages: accessibility tags (ActualText /
  Alt), editing-application data (PieceInfo), and the page's thumbnail (a
  small picture of the page as it was);
- for terms you searched for (*Black out matches*, *Find & replace*): every
  comment, form field, link, attachment and JavaScript mentioning them is
  removed or emptied, and they are replaced in bookmark titles and document
  metadata;
- old versions of the file kept by earlier incremental saves.

*Scrub metadata* additionally clears document-wide extras that don't mention
a searched term (see above).

To check a result yourself - every object and stream in the file, not just
what's shown:

```sh
uv run python scripts/forensic_check.py out.pdf "John Smith"
```

## Good to know

- Search finds real text only. On a scan without an OCR text layer, draw a
  box over the words instead. Blacking out part of a scan stores that page's
  image again without loss (PNG instead of JPEG), so the file gets larger.
- Form fields, comments and links that overlap a marked region are removed
  whole; the result tells you which.
- A box you draw by hand removes what is under it; only *searched* terms are
  also cleaned from comments, bookmarks and metadata elsewhere.
- If the document's font lacks a letter of your replacement text, a matching
  standard font is used and you're told.

How it works and how it was tested: [DESIGN.md](DESIGN.md).

## Tests

```sh
uv run python -m unittest discover -s tests
```
