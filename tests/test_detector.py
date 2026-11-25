from __future__ import annotations

from pathlib import Path

from conftest import make_embedded_type0_pdf

from pdf_cmap_fix.cmap import generate_tounicode_cmap
from pdf_cmap_fix.detector import inspect_pdf


def test_detects_missing_tounicode_identity_h_and_v(tmp_path: Path):
    pdf_h, _ = make_embedded_type0_pdf(tmp_path / "h.pdf", text="AB", encoding="/Identity-H", tounicode_mode="strip")
    rep_h = inspect_pdf(pdf_h)
    assert rep_h.needs_fix is True
    assert len(rep_h.problem_fonts) == 1
    assert rep_h.problem_fonts[0].reason == "missing"
    assert rep_h.problem_fonts[0].simplified_name == "SourceHanSansCN"

    pdf_v, _ = make_embedded_type0_pdf(tmp_path / "v.pdf", text="A", encoding="/Identity-V", tounicode_mode="strip")
    rep_v = inspect_pdf(pdf_v)
    assert rep_v.needs_fix is True
    assert rep_v.problem_fonts[0].encoding == "Identity-V"


def test_null_or_empty_tounicode_is_flagged_as_needing_fix(tmp_path: Path):
    pdf_null, _ = make_embedded_type0_pdf(tmp_path / "null.pdf", text="A", tounicode_mode="null")
    rep_null = inspect_pdf(pdf_null)
    assert rep_null.needs_fix is True
    assert rep_null.problem_fonts[0].reason == "missing"

    pdf_empty, _ = make_embedded_type0_pdf(tmp_path / "empty.pdf", text="A", tounicode_mode="empty")
    rep_empty = inspect_pdf(pdf_empty)
    assert rep_empty.needs_fix is True
    assert rep_empty.problem_fonts[0].reason == "invalid"


def test_healthy_pdf_with_complete_tounicode_returns_false(tmp_path: Path):
    probe_pdf, cids = make_embedded_type0_pdf(tmp_path / "probe.pdf", text="AB", tounicode_mode="strip")
    assert len(cids) == 2
    cmap = generate_tounicode_cmap({cids[0]: "A", cids[1]: "B"}).encode("ascii")
    pdf, _ = make_embedded_type0_pdf(tmp_path / "ok.pdf", text="AB", tounicode_mode="custom", custom_tounicode=cmap)
    rep = inspect_pdf(pdf)
    assert rep.needs_fix is False
    assert rep.problem_fonts == []


def test_partial_tounicode_missing_used_cid_still_flags_needs_fix(tmp_path: Path):
    _, cids = make_embedded_type0_pdf(tmp_path / "probe.pdf", text="AB", tounicode_mode="strip")
    assert len(cids) == 2
    # Only map the first CID, leave the second CID unmapped!
    cmap = generate_tounicode_cmap({cids[0]: "A"}).encode("ascii")
    pdf, _ = make_embedded_type0_pdf(
        tmp_path / "partial.pdf", text="AB", tounicode_mode="custom", custom_tounicode=cmap
    )
    rep = inspect_pdf(pdf)
    assert rep.needs_fix is True
    assert len(rep.problem_fonts) == 1
    assert rep.problem_fonts[0].reason == "incomplete"
    assert cids[1] in rep.problem_fonts[0].unmapped_cids


def test_strict_cmap_flags_legacy_bfrange_violation(tmp_path: Path):
    _, cids = make_embedded_type0_pdf(tmp_path / "probe.pdf", text="AB", tounicode_mode="strip")
    bad_cmap = f"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (CNS1) /Supplement 7 >> def
/CMapName /SourceHanSansCN-ToUnicode def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
2 beginbfchar
<{cids[0]:04X}> <0041>
<{cids[1]:04X}> <0042>
endbfchar
1 beginbfrange
<4BFF> <4C00> <635E>
endbfrange
endcmap
/SourceHanSansCN-ToUnicode currentdict /CMap defineresource pop
end
end
""".encode("ascii")
    pdf, _ = make_embedded_type0_pdf(
        tmp_path / "legacy_bad.pdf", text="AB", tounicode_mode="custom", custom_tounicode=bad_cmap
    )
    rep_default = inspect_pdf(pdf, strict_cmap=False)
    assert len(rep_default.cmap_warnings) > 0
    rep_strict = inspect_pdf(pdf, strict_cmap=True)
    assert rep_strict.needs_fix is True
    assert rep_strict.problem_fonts[0].reason == "invalid_cmap"


def test_ambiguous_same_name_fonts_still_detect_incomplete_tounicode(tmp_path: Path):
    # Two Type0 fonts on the same page with identical BaseFont name; one has incomplete ToUnicode
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    f = fitz.Font("helv")
    page.insert_font(fontname="F0", fontbuffer=f.buffer)
    page.insert_text((40, 120), "AB", fontname="F0", fontsize=14)
    raw = doc.tobytes()
    doc.close()

    d2 = fitz.open(stream=raw, filetype="pdf")
    fx1 = d2[0].get_fonts(full=True)[0][0]
    # Find raw CIDs
    probe = fitz.open(stream=raw, filetype="pdf")
    probe.xref_set_key(fx1, "ToUnicode", "null")
    cids = [ord(c["c"]) for c in probe[0].get_text("rawdict")["blocks"][0]["lines"][0]["spans"][0]["chars"] if not c.get("synthetic")]
    probe.close()

    # Give fx1 only partial ToUnicode (missing cids[1])
    partial_cmap = generate_tounicode_cmap({cids[0]: "A"}).encode("ascii")
    sx1 = d2.get_new_xref()
    d2.update_object(sx1, "<<>>")
    d2.update_stream(sx1, partial_cmap)
    d2.xref_set_key(fx1, "BaseFont", "/AAAAAA+DplrwySourceHanSansCN-Regular-Identity-H")
    d2.xref_set_key(fx1, "ToUnicode", f"{sx1} 0 R")

    # Add a second Type0 font on the same page with the SAME BaseFont and complete ToUnicode
    full_cmap = generate_tounicode_cmap({cids[0]: "A", cids[1]: "B"}).encode("ascii")
    sx2 = d2.get_new_xref()
    d2.update_object(sx2, "<<>>")
    d2.update_stream(sx2, full_cmap)
    fx2 = d2.get_new_xref()
    obj1 = d2.xref_object(fx1)
    d2.update_object(fx2, obj1)
    d2.xref_set_key(fx2, "ToUnicode", f"{sx2} 0 R")
    # Register /F1 in page Resources/Font (resolve indirect /Resources dict if needed)
    import re
    res_k, res_v = d2.xref_get_key(d2[0].xref, "Resources")
    if res_k == "xref":
        res_xref = int(re.search(r"(\d+)", res_v).group(1))
        d2.xref_set_key(res_xref, "Font/F1", f"{fx2} 0 R")
    else:
        d2.xref_set_key(d2[0].xref, "Resources/Font/F1", f"{fx2} 0 R")

    pdf = tmp_path / "ambig.pdf"
    d2.save(pdf)
    d2.close()

    rep = inspect_pdf(pdf)
    assert rep.needs_fix is True
    assert any(p.xref == fx1 and p.reason == "incomplete" for p in rep.problem_fonts)
