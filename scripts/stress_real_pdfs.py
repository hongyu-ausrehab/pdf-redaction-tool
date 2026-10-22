"""Stress test on real PDFs: redact random words per page, then measure leaks
and collateral damage (glyphs removed that were not selected).

Prints counts only, never document text, so it is safe to run on private files.

usage: python scripts/stress_real_pdfs.py [--words N] [--pages N] [--seed N] PDF...
"""

import argparse
import random
import time
from collections import Counter

import pymupdf

from pdf_redact import core


TOL = 0.15  # pt


def glyph_keys(chars):
    return Counter((c.c, round(c.bbox.x0, 1), round(c.bbox.y0, 1)) for c in chars if c and not c.c.isspace())


def run(path, n_words, max_pages, rng, verbose=False):
    doc = core.open_pdf(path)
    pages = list(range(min(doc.page_count, max_pages)))
    ops, expected_gone = [], {}
    for pno in pages:
        page = doc[pno]
        words = [w for w in page.get_text("words") if len(w[4]) >= 3]
        _, chars = core.page_chars(page)
        for w in rng.sample(words, min(n_words, len(words))):
            r = pymupdf.Rect(w[:4]) * page.rotation_matrix
            kind = rng.choice(["blackout", "blackout", "replace"])
            ops.append(core.Op(pno, tuple(r), kind, "X" * len(w[4]) if kind == "replace" else ""))
        expected_gone[pno] = set()
        regions = [pymupdf.Rect(op.rect) * page.derotation_matrix for op in ops if op.page == pno]
        for r in regions:
            sel = core.chars_in(chars, r)
            expected_gone[pno] |= {(c.c, round(c.bbox.x0, 1), round(c.bbox.y0, 1)) for c in sel}
        # fields / annotations overlapping a region are removed whole, by design
        boxes = [a.rect for a in page.annots()] + [w.rect for w in page.widgets()]
        for box in boxes:
            if any(box.intersects(r) for r in regions):
                expected_gone[pno] |= {(c.c, round(c.bbox.x0, 1), round(c.bbox.y0, 1))
                                       for c in core.chars_in(chars, box)}
    before = {pno: glyph_keys(core.page_chars(doc[pno])[1]) for pno in pages}

    t = time.time()
    notes = core.apply(doc, ops)
    if verbose:
        for n in notes:
            print("      note", n)
    out = core.open_pdf(core.to_bytes(doc, scrub=True))
    elapsed = time.time() - t

    leaks = core.verify(out, ops)
    collateral, per_page = 0, {}
    for pno in pages:
        after = glyph_keys(core.page_chars(out[pno])[1])
        by_char = {}
        for k in after:
            by_char.setdefault(k[0], []).append(k)
        for key, n in before[pno].items():
            if key in expected_gone[pno]:
                continue
            # same glyph within TOL counts as unmoved (MuPDF rewrites positions at limited precision)
            found = sum(after[k] for k in by_char.get(key[0], ())
                        if abs(k[1] - key[1]) <= TOL and abs(k[2] - key[2]) <= TOL)
            if found < n:
                per_page[pno] = per_page.get(pno, 0) + n - found
                if verbose:
                    near = sorted(by_char.get(key[0], ()), key=lambda k: abs(k[1] - key[1]) + abs(k[2] - key[2]))
                    print(f"      p{pno + 1} collateral {key} nearest-after {near[:1]}")
                    for op in ops:
                        r = pymupdf.Rect(op.rect)
                        if op.page == pno:
                            print(f"         nearby op {op.kind} {[round(v, 1) for v in op.rect]} {op.text!r}")
        collateral += per_page.get(pno, 0)
    if verbose:
        for lk in leaks:
            print("      leak", lk)
    leak_pages = Counter(int(lk.split()[1].rstrip(":")) for lk in leaks)
    for pno in pages:
        if per_page.get(pno) or leak_pages.get(pno + 1):
            kinds = Counter(op.kind for op in ops if op.page == pno)
            print(f"   page {pno + 1}: collateral={per_page.get(pno, 0)} leaks={leak_pages.get(pno + 1, 0)} ops={dict(kinds)}")
    total = sum(sum(c.values()) for c in before.values())
    print(f"{path.split('/')[-1][:40]:40s} pages={len(pages):3d} ops={len(ops):4d} glyphs={total:6d} "
          f"leaks={len(leaks):3d} collateral={collateral:4d} time={elapsed:.2f}s")
    return len(leaks), collateral


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--words", type=int, default=6)
    ap.add_argument("--pages", type=int, default=20)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    bad = 0
    for p in args.pdfs:
        try:
            leaks, collateral = run(p, args.words, args.pages, rng, args.verbose)
            bad += bool(leaks or collateral)
        except core.RedactError as e:
            print(f"{p}: skipped ({e})")
    print("FILES WITH PROBLEMS:", bad)


if __name__ == "__main__":
    main()
