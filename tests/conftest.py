from __future__ import annotations

from pathlib import Path

import fitz


def make_embedded_type0_pdf(
    path: Path,
    text: str = "AB",
    *,
    basefont_override: str | None = "AAAAAA+DplrwySourceHanSansCN-Regular-Identity-H",
    encoding: str = "/Identity-H",
    tounicode_mode: str = "keep",  # "keep" | "strip" | "null" | "empty" | "custom"
    custom_tounicode: bytes | None = None,
    rotate_deg: int = 0,
    fontsize: float = 14.0,
) -> tuple[Path, list[int]]:
    """Create a real self-contained embedded Type0 PDF using PyMuPDF's built-in font buffer.

    Returns `(pdf_path, raw_cids_used)` where `raw_cids_used` are the actual CIDs emitted
    when `/ToUnicode` is absent.
    """
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    f = fitz.Font("helv")
    page.insert_font(fontname="F0", fontbuffer=f.buffer)
    if rotate_deg:
        page.insert_text((120, 220), text, fontname="F0", fontsize=fontsize, rotate=rotate_deg)
    else:
        page.insert_text((40, 120), text, fontname="F0", fontsize=fontsize)

    tmp_bytes = doc.tobytes()
    doc.close()

    doc2 = fitz.open(stream=tmp_bytes, filetype="pdf")
    fonts = doc2[0].get_fonts(full=True)
    font_xref = fonts[0][0]

    # Discover exact raw CIDs before modifying ToUnicode
    probe = fitz.open(stream=tmp_bytes, filetype="pdf")
    probe.xref_set_key(font_xref, "ToUnicode", "null")
    raw = probe[0].get_text("rawdict")
    raw_cids = [
        ord(ch["c"])
        for b in raw.get("blocks", [])
        if b.get("type") == 0
        for ln in b.get("lines", [])
        for sp in ln.get("spans", [])
        for ch in sp.get("chars", [])
        if not ch.get("synthetic") and ch.get("c")
    ]
    probe.close()

    if basefont_override:
        doc2.xref_set_key(font_xref, "BaseFont", f"/{basefont_override}")
    if encoding != "/Identity-H":
        doc2.xref_set_key(font_xref, "Encoding", encoding)

    if tounicode_mode == "strip":
        # Rebuild object without ToUnicode key at all
        obj = doc2.xref_object(font_xref)
        lines = [ln for ln in obj.splitlines() if "/ToUnicode" not in ln]
        doc2.update_object(font_xref, "\n".join(lines))
    elif tounicode_mode == "null":
        doc2.xref_set_key(font_xref, "ToUnicode", "null")
    elif tounicode_mode == "empty":
        sx = doc2.get_new_xref()
        doc2.update_object(sx, "<<>>")
        doc2.update_stream(sx, b"")
        doc2.xref_set_key(font_xref, "ToUnicode", f"{sx} 0 R")
    elif tounicode_mode == "custom":
        assert custom_tounicode is not None
        sx = doc2.get_new_xref()
        doc2.update_object(sx, "<<>>")
        doc2.update_stream(sx, custom_tounicode)
        doc2.xref_set_key(font_xref, "ToUnicode", f"{sx} 0 R")

    doc2.save(path, deflate=True, garbage=3)
    doc2.close()
    return path, raw_cids
