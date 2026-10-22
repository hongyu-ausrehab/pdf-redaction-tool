"""Check replacements on real PDFs: random words are replaced by the word
reversed (same letters, so the original font should usually have them; --longer
doubles it, to exercise fitting into the free space).
Counts how often the original font was used, whether the new text extracts,
and whether it overlaps a neighbouring word. Prints counts only.

usage: python scripts/check_replace.py [--words N] [--pages N] [--seed N] [--png DIR] PDF...
"""

import argparse
import random
from pathlib import Path

import pymupdf

from pdf_redact import core


def overlapping(r, o):
    """More than a sliver of o overlaps r."""
    return (r & o).width > 1 and (r & o).height > 0.5 * r.height


def run(path, n_words, max_pages, rng, png_dir, verbose=False, longer=False):
    doc = core.open_pdf(path)
    pages = range(min(doc.page_count, max_pages))
    ops, targets = [], []
    for pno in pages:
        page = doc[pno]
        words = [w for w in page.get_text("words") if len(w[4]) >= 4 and w[4].isalpha()]
        for w in rng.sample(words, min(n_words, len(words))):
            # find the word as a hit so the region is its ink box, like the UI does
            hits = [h for h in core.find(doc, core.pattern([w[4]], case=True), [pno])
                    if pymupdf.Rect(h.rects[0]).intersects(pymupdf.Rect(w[:4]) * page.rotation_matrix)]
            if hits and len(hits[0].rects) == 1:
                new = w[4][::-1] * (2 if longer else 1)
                ops.append(core.Op(pno, hits[0].rects[0], "replace", new))
                r = pymupdf.Rect(w[:4])
                # did the original word already overlap another word (e.g. a form value typed over a label)?
                pre = any(o is not w and overlapping(r, pymupdf.Rect(o[:4])) for o in page.get_text("words"))
                targets.append((pno, new, w[4], r, pre))
    notes = core.apply(doc, ops)
    out = core.open_pdf(core.to_bytes(doc))
    fallback = sum("standard font" in n for n in notes)
    if verbose:
        for n in notes:
            print("   note:", n)
    found = overlaps = old_left = 0
    for pno, new, old, old_rect, pre in targets:
        words = out[pno].get_text("words")
        mine = [w for w in words if w[4] == new]
        found += bool(mine)
        # the original word must be gone from where it was
        left = any(w[4] == old and pymupdf.Rect(w[:4]).intersects(old_rect)
                   and min((pymupdf.Rect(w[:4]) & old_rect).width, (pymupdf.Rect(w[:4]) & old_rect).height) > 1
                   for w in words)
        old_left += left
        if left and verbose:
            print(f"   p{pno + 1}: original {old!r} at {[round(v, 1) for v in old_rect]} still present")
        for m in mine[:1]:
            r = pymupdf.Rect(m[:4])
            others = [pymupdf.Rect(w[:4]) for w in words if w is not m and w[4] != new]
            # overlap = more than a sliver of another word's box
            overlaps += not pre and any(overlapping(r, o) for o in others)  # only overlaps we caused
    other_notes = [n for n in notes if "standard font" not in n]
    print(f"{Path(path).name[:40]:40s} replaced={len(ops):4d} original-font={len(ops) - fallback:4d} "
          f"fallback={fallback:3d} extracts={found:4d} old-left={old_left:2d} overlaps={overlaps:2d} "
          f"other-notes={len(other_notes)}")
    if png_dir:
        for pno in sorted({t[0] for t in targets})[:2]:
            out[pno].get_pixmap(dpi=110).save(Path(png_dir) / f"{Path(path).stem[:20]}-p{pno + 1}.png")
    return fallback, len(ops) - found, overlaps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--words", type=int, default=5)
    ap.add_argument("--pages", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--png", help="save rendered pages here for a visual check")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--longer", action="store_true", help="replace with the reversed word twice (tests fitting)")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    for p in args.pdfs:
        run(p, args.words, args.pages, rng, args.png, args.verbose, args.longer)


if __name__ == "__main__":
    main()
