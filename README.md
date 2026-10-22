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

This opens <http://127.0.0.1:8765/>. Then:

1. **Open** a PDF: click the drop area or drag a file onto it.
2. **Mark what to change**:
   - **Drag a box** on a page. In *Blackout* mode (key `B`) everything under
     it is removed and painted over; in *Replace text* mode (key `R`) you're
     asked for the new text.
   - **Black out matches**: type words to hide, one per line, and click
     *Find* - every occurrence in the document is marked. Tick *regex* to use
     patterns, e.g. `\b04\d{2} ?\d{3} ?\d{3}\b` for mobile numbers.
   - **Find & replace**: type the old and new text and click *Find*.
3. **Check** the marked regions. Hover one to see through it, click **×** to
   drop it, click a blackout to give it the current colour, double-click a
   replacement to edit it. **Undo** steps back one action; **Undo all**
   starts over.
4. **Apply & download** saves `NAME-redacted.pdf`. Your original file is not
   changed. The status line lists anything worth checking.

Pick the blackout colour from the palette (black by default). *Match case*
makes searches case-sensitive; *scrub metadata* (on by default) also removes
the document's metadata, attachments, JavaScript and thumbnails.

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
is checked; if any search term can still be found (in the text, bookmarks or
metadata) it prints a warning and exits with code 2.

## Good to know

- Search finds real text only. On a scan without an OCR text layer, draw a
  box over the words instead.
- Form fields, comments and links that overlap a marked region are removed
  whole; the result tells you which.
- Bookmark titles aren't changed; the command line warns if a term appears
  in one.
- If the document's font lacks a letter of your replacement text, a matching
  standard font is used and you're told.

How it works and how it was tested: [DESIGN.md](DESIGN.md).

## Tests

```sh
uv run python -m unittest discover -s tests
```
