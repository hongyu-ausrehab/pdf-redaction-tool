"""Check that blacking out part of a scanned (image-only) page destroys the
underlying image pixels, not just paints over them. Prints statistics only.

usage: python scripts/check_scanned.py PDF PAGE(1-based)
"""

import sys

import pymupdf

from pdf_redact import core


def page_image(doc, pno):
    """The largest image on the page as a Pixmap, plus where it is drawn."""
    page = doc[pno]
    best = max(page.get_images(full=True), key=lambda im: im[2] * im[3])
    rect = page.get_image_rects(best[0])[0]
    return pymupdf.Pixmap(doc, best[0]), rect


path, pno = sys.argv[1], int(sys.argv[2]) - 1
doc = core.open_pdf(path)
pix, where = page_image(doc, pno)
print(f"image {pix.width}x{pix.height} n={pix.n} drawn at {where}")
box = pymupdf.Rect(where.x0 + where.width * 0.3, where.y0 + where.height * 0.3,
                   where.x0 + where.width * 0.6, where.y0 + where.height * 0.4)
notes = core.apply(doc, [core.Op(pno, tuple(box * doc[pno].rotation_matrix))])
out = core.open_pdf(core.to_bytes(doc))
pix2, where2 = page_image(out, pno)
print("notes", notes, "| image drawn at", where2)

# compare pixels inside / outside the box in image space
sx, sy = pix.width / where.width, pix.height / where.height
ib = pymupdf.IRect(int((box.x0 - where.x0) * sx) + 2, int((box.y0 - where.y0) * sy) + 2,
                   int((box.x1 - where.x0) * sx) - 2, int((box.y1 - where.y0) * sy) - 2)


def stats(p, region, inside):
    same = changed = 0
    for y in range(0, p.height, 4):
        for x in range(0, p.width, 4):
            if region.contains(pymupdf.Point(x, y)) == inside:
                if pix.pixel(x, y) == p.pixel(x, y):
                    same += 1
                else:
                    changed += 1
    return same, changed


if (pix.width, pix.height) != (pix2.width, pix2.height):
    print("image dimensions changed:", (pix.width, pix.height), "->", (pix2.width, pix2.height))
else:
    s_in, c_in = stats(pix2, ib, True)
    s_out, c_out = stats(pix2, ib, False)
    print(f"inside box: {c_in} changed / {s_in} unchanged samples (want all changed or uniform)")
    print(f"outside box: {c_out} changed / {s_out} unchanged samples (want ~0 changed)")
    inside_vals = {pix2.pixel(x, y) for y in range(ib.y0, ib.y1, 4) for x in range(ib.x0, ib.x1, 4)}
    print("distinct pixel values left inside box:", len(inside_vals), sorted(inside_vals)[:3])
