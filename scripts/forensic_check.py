"""Forensic check of a redacted PDF: is a term still anywhere in the file?

Looks well beyond the visible page: every object and every decompressed
stream (in plain, hex and UTF-16 encodings), the raw file bytes, extracted
text with hidden layers both off and on, bookmarks, metadata, attachments,
form fields and annotations. Prints where each term was found.

usage: python scripts/forensic_check.py OUT.pdf TERM [TERM...]
"""

import sys

import pymupdf


def encodings(term):
    """Byte forms a term can take inside a PDF file."""
    t = term.encode("latin-1", errors="ignore")
    u16 = term.encode("utf-16-be")
    forms = {"plain": t, "utf16": u16, "hex": t.hex().encode(), "HEX": t.hex().upper().encode(),
             "utf16-hex": u16.hex().encode(), "UTF16-HEX": u16.hex().upper().encode()}
    return {k: v for k, v in forms.items() if v}


def scan(path, terms):
    doc = pymupdf.open(path)
    found = []
    raw = open(path, "rb").read()
    lterms = [t.lower() for t in terms]
    for term in terms:
        for name, form in encodings(term).items():
            if form.lower() in raw.lower():
                found.append(f"raw file bytes ({name}): {term!r}")
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref, compressed=False).encode("latin-1", errors="ignore")
            data = doc.xref_stream(xref) if doc.xref_is_stream(xref) else b""
        except Exception:
            continue
        for where, blob in (("object", obj), ("stream", data or b"")):
            for term in terms:
                for name, form in encodings(term).items():
                    if form.lower() in blob.lower():
                        kind = doc.xref_get_key(xref, "Type")[1] or doc.xref_get_key(xref, "Subtype")[1]
                        found.append(f"xref {xref} {where} {kind} ({name}): {term!r}")
    # text as a viewer / extractor sees it, with hidden layers off (default) and all layers on
    for label, show_all in (("text", False), ("text with hidden layers on", True)):
        if show_all:
            for x in doc.get_ocgs():
                doc.set_layer(-1, on=list(doc.get_ocgs()))
        for page in doc:
            text = page.get_text().lower()
            for term, lt in zip(terms, lterms):
                if lt in text:
                    found.append(f"{label}, page {page.number + 1}: {term!r}")
    for item in doc.get_toc(simple=False):
        for term, lt in zip(terms, lterms):
            if lt in item[1].lower():
                found.append(f"bookmark: {term!r}")
    for key, value in (doc.metadata or {}).items():
        for term, lt in zip(terms, lterms):
            if value and lt in value.lower():
                found.append(f"metadata {key}: {term!r}")
    for i in range(doc.embfile_count()):
        content = doc.embfile_get(i).lower()
        for term in terms:
            if term.lower().encode() in content:
                found.append(f"attachment {i}: {term!r}")
    for page in doc:
        for a in page.annots():
            for term, lt in zip(terms, lterms):
                if lt in (a.info.get("content", "") + a.info.get("title", "")).lower():
                    found.append(f"annotation page {page.number + 1}: {term!r}")
        for w in page.widgets():
            for term, lt in zip(terms, lterms):
                if lt in str(w.field_value).lower():
                    found.append(f"form field page {page.number + 1}: {term!r}")
        for link in page.get_links():
            for term, lt in zip(terms, lterms):
                if lt in str(link.get("uri", "")).lower():
                    found.append(f"link page {page.number + 1}: {term!r}")
    return sorted(set(found))


if __name__ == "__main__":
    hits = scan(sys.argv[1], sys.argv[2:])
    for h in hits:
        print("FOUND", h)
    print(f"{len(hits)} place(s) still contain a term")
    sys.exit(1 if hits else 0)
