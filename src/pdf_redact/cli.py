"""Command line: `pdf-redact redact ...` and `pdf-redact serve`."""

import argparse
import json
import sys
from pathlib import Path

from . import core


def _box(spec, kind):
    """PAGE:x0,y0,x1,y1[:TEXT] -> Op (page is 1-based on the command line)."""
    try:
        page, coords, *text = spec.split(":", 2)
        rect = tuple(float(v) for v in coords.split(","))
        return core.Op.from_dict({"page": int(page) - 1, "rect": rect, "kind": kind, "text": text[0] if text else ""})
    except (ValueError, core.RedactError):
        raise argparse.ArgumentTypeError(f"expected PAGE:x0,y0,x1,y1{'[:TEXT]' if kind == 'replace' else ''}, got {spec!r}")


def redact(args):
    doc = core.open_pdf(args.input, args.password)
    pages = None
    if args.pages:
        pages = sorted({int(p) - 1 for p in args.pages.split(",")})
        if any(not 0 <= p < doc.page_count for p in pages):
            raise core.RedactError(f"--pages out of range (document has {doc.page_count})")

    fill = core.parse_color(args.color)
    ops = list(args.box) + list(args.replace_box)
    pat = core.pattern(args.term, args.regex, args.case_sensitive)
    if pat:
        ops += core.hits_to_ops(core.find(doc, pat, pages))
    for op in ops:
        op.color = fill
    if args.ops:  # these carry their own colour, if any
        ops += [core.Op.from_dict(d) for d in json.loads(Path(args.ops).read_text())]
    replace_olds = []
    for old, new in args.replace:
        rpat = core.pattern([old], case=args.case_sensitive)
        if rpat is None:
            continue
        ops += core.hits_to_ops(core.find(doc, rpat, pages), "replace", new)
        if old.lower() not in new.lower():  # otherwise the new text would "leak" the old
            replace_olds.append(old)

    for op in ops:
        label = f" -> {op.text!r}" if op.kind == "replace" else ""
        print(f"page {op.page + 1}: {op.kind} {tuple(round(v, 1) for v in op.rect)}{label}")
    print(f"{len(ops)} region(s)")
    if args.dry_run:
        return 0
    if not ops and not args.scrub:
        print("nothing to do", file=sys.stderr)
        return 1

    for note in core.apply(doc, ops):
        print(f"note: {note}")
    data = core.to_bytes(doc, scrub=args.scrub)
    Path(args.output).write_bytes(data)
    print(f"wrote {args.output}")

    out = core.open_pdf(data)
    leaks = core.verify(out, ops, core.pattern(args.term + replace_olds, args.regex, args.case_sensitive))
    for leak in leaks:
        print(f"WARNING: {leak}", file=sys.stderr)
    return 2 if leaks else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pdf-redact", description="Black out or replace text in PDFs.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("redact", help="redact a PDF from the command line",
                       description="Coordinates are PDF points from the top-left of the page as displayed.")
    r.add_argument("input")
    r.add_argument("output", nargs="?", help="output path (default: INPUT-redacted.pdf)")
    r.add_argument("-t", "--term", action="append", default=[], help="text to black out (repeatable)")
    r.add_argument("-r", "--regex", action="append", default=[], help="regex to black out (repeatable)")
    r.add_argument("--replace", nargs=2, action="append", default=[], metavar=("OLD", "NEW"),
                   help="replace text OLD with NEW (repeatable)")
    r.add_argument("-b", "--box", action="append", default=[], type=lambda s: _box(s, "blackout"),
                   metavar="PAGE:x0,y0,x1,y1", help="black out a rectangle (repeatable)")
    r.add_argument("--replace-box", action="append", default=[], type=lambda s: _box(s, "replace"),
                   metavar="PAGE:x0,y0,x1,y1:TEXT", help="clear a rectangle and write TEXT in it (repeatable)")
    r.add_argument("--color", default="#000000", metavar="#RRGGBB", help="blackout fill colour (default black)")
    r.add_argument("--ops", help="JSON file: list of {page (0-based), rect, kind, text, color}")
    r.add_argument("--pages", help="limit term/regex/replace search to these pages, e.g. 1,3")
    r.add_argument("--case-sensitive", action="store_true")
    r.add_argument("--no-scrub", dest="scrub", action="store_false",
                   help="keep metadata, XMP, JavaScript, attachments and thumbnails")
    r.add_argument("--password")
    r.add_argument("-n", "--dry-run", action="store_true", help="only list what would be redacted")

    s = sub.add_parser("serve", help="start the browser editor")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "serve":
            from . import web
            return web.serve(args.port, open_browser=not args.no_browser)
        if args.output is None:
            p = Path(args.input)
            args.output = str(p.with_name(p.stem + "-redacted.pdf"))
        if Path(args.output).resolve() == Path(args.input).resolve():
            raise core.RedactError("output must differ from input")
        return redact(args)
    except core.RedactError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
