from __future__ import annotations

import fitz
import pytest

from pdf_cmap_fix.fonts import (
    PageFontIndex,
    extract_basefont_from_xref_obj,
    get_descendant_font_info,
    load_six_char_fonts,
    parse_w_array,
    simplify_basefont,
)


def test_load_six_char_fonts_contains_expected_names():
    words = load_six_char_fonts()
    assert len(words) >= 600
    for expected in ("Source", "Myriad", "Roboto", "Songti", "Sarasa", "Smiley"):
        assert expected in words


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("JEMPKA+DplrwySourceHanSansCN-Light-Identity-H", "SourceHanSansCN"),
        ("KPBGOF+CtsnbwBywnmpSourceHanSansCN-Medium-Identity-H", "SourceHanSansCN"),
        ("WxxjmkSourceHanSerifCN-Heavy-Identity-H", "SourceHanSerifCN"),
        ("INKDDL+JdvtkjMyriadPro-Regular", "MyriadPro"),
        ("ABCDEF+Roboto-Bold", "Roboto"),
        ("SourceHanSerifCN-Regular", "SourceHanSerifCN"),
        ("ArialMT", "ArialMT"),
        ("TimesNewRomanPSMT", "TimesNewRomanPSMT"),
        ("Didot-Roman", "Didot"),
        ("Short", "Short"),
    ],
)
def test_simplify_basefont_strips_subset_and_random_prefixes(raw: str, expected: str):
    assert simplify_basefont(raw) == expected


def test_extract_basefont_from_xref_obj():
    obj = "<<\n  /BaseFont /JEMPKA+DplrwySourceHanSansCN-Light-Identity-H\n  /Subtype /Type0\n>>"
    assert extract_basefont_from_xref_obj(obj) == "JEMPKA+DplrwySourceHanSansCN-Light-Identity-H"
    assert extract_basefont_from_xref_obj("<< /Subtype /Type0 >>") is None


def test_parse_w_array_supports_list_and_range_forms():
    w_str = "[ 1 [ 225 ] 14 [ 333 298.5 ] 17 20 535 63039 [ 500 ] ]"
    widths = parse_w_array(w_str)
    assert widths[1] == 225.0
    assert widths[14] == 333.0
    assert widths[15] == 298.5
    assert widths[17] == 535.0
    assert widths[18] == 535.0
    assert widths[19] == 535.0
    assert widths[20] == 535.0
    assert widths[63039] == 500.0
    assert 16 not in widths


def _make_two_type0_fonts_doc(
    w1: str,
    w2: str,
    base1: str = "AAAAAA+AbcdefSourceHanSansCN-Light-Identity-H",
    base2: str = "BBBBBB+AbcdefSourceHanSansCN-LightItalic-Identity-H",
    indirect_w: bool = False,
) -> fitz.Document:
    doc = fitz.open()
    page = doc.new_page()

    def add_type0(base: str, w_str: str, subtype: str = "/CIDFontType0", cid2gid_stream: bool = False) -> int:
        desc_xref = doc.get_new_xref()
        w_part = ""
        if w_str:
            if indirect_w:
                w_xref = doc.get_new_xref()
                doc.update_object(w_xref, w_str)
                w_part = f"/W {w_xref} 0 R"
            else:
                w_part = f"/W {w_str}"
        cid2gid_part = ""
        if cid2gid_stream:
            m_xref = doc.get_new_xref()
            doc.update_object(m_xref, "<<>>")
            doc.update_stream(m_xref, b"\x00\x00\x00\x01")
            cid2gid_part = f"/CIDToGIDMap {m_xref} 0 R"
        doc.update_object(
            desc_xref,
            f"<< /Type /Font /Subtype {subtype} /BaseFont /{base} "
            f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            f"/DW 1000 {w_part} {cid2gid_part} >>",
        )
        t0_xref = doc.get_new_xref()
        doc.update_object(
            t0_xref,
            f"<< /Type /Font /Subtype /Type0 /BaseFont /{base} /Encoding /Identity-H "
            f"/DescendantFonts [ {desc_xref} 0 R ] >>",
        )
        return t0_xref

    f1 = add_type0(base1, w1)
    f2 = add_type0(base2, w2, subtype="/CIDFontType2", cid2gid_stream=True)
    res_xref = doc.get_new_xref()
    doc.update_object(res_xref, f"<< /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> >>")
    doc.xref_set_key(page.xref, "Resources", f"{res_xref} 0 R")
    return doc


def test_get_descendant_font_info_handles_indirect_w_and_cidtogidmap():
    doc = _make_two_type0_fonts_doc("[ 10 [ 400 ] ]", "[ 20 [ 600 ] ]", indirect_w=True)
    fonts = doc[0].get_fonts(full=True)
    by_name = {f[3]: f[0] for f in fonts}
    info1 = get_descendant_font_info(doc, by_name["AAAAAA+AbcdefSourceHanSansCN-Light-Identity-H"])
    assert info1.subtype == "CIDFontType0"
    assert info1.dw == 1000.0
    assert info1.widths == {10: 400.0}
    assert info1.cid_to_gid_map_is_stream is False

    info2 = get_descendant_font_info(doc, by_name["BBBBBB+AbcdefSourceHanSansCN-LightItalic-Identity-H"])
    assert info2.subtype == "CIDFontType2"
    assert info2.widths == {20: 600.0}
    assert info2.cid_to_gid_map_is_stream is True


def test_page_font_index_disambiguates_by_subset_prefix_and_returns_none_when_truly_ambiguous():
    # Two fonts with identical post-subset 24-char prefix and empty /W (all CJK /DW=1000)
    doc = _make_two_type0_fonts_doc("", "")
    idx = PageFontIndex(doc, doc[0])

    # With subset prefix present (set_subset_fontnames(True)): resolves unambiguously!
    xref, full, simp = idx.resolve("AAAAAA+AbcdefSourceHanSansCN-L", sample_cids={20000})
    assert xref is not None
    assert "AAAAAA+" in full
    assert simp == "SourceHanSansCN"

    # Without subset prefix and with identical empty /W -> genuinely ambiguous -> must return None xref!
    xref_amb, _, simp_amb = idx.resolve("AbcdefSourceHanSansCN-Li", sample_cids={20000})
    assert xref_amb is None
    assert simp_amb == "SourceHanSansCN"


def test_page_font_index_disambiguates_by_w_cids_when_narrow_cids_distinguish():
    doc = _make_two_type0_fonts_doc("[ 10 [ 400 ] 11 [ 420 ] ]", "[ 50 [ 600 ] ]")
    idx = PageFontIndex(doc, doc[0])
    xref, full, simp = idx.resolve("AbcdefSourceHanSansCN-Li", sample_cids={10, 11})
    assert xref is not None
    assert "AAAAAA+" in full
    assert simp == "SourceHanSansCN"


def test_is_standard_cjk_ordering_distinguishes_gb1_from_identity():
    from pdf_cmap_fix.fonts import is_standard_cjk_ordering

    doc = fitz.open()
    page = doc.new_page()

    def add_font(ordering: str) -> int:
        desc = doc.get_new_xref()
        doc.update_object(
            desc,
            f"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /SourceHanSerifCN-Regular "
            f"/CIDSystemInfo << /Registry (Adobe) /Ordering ({ordering}) /Supplement 5 >> /DW 1000 >>",
        )
        t0 = doc.get_new_xref()
        doc.update_object(
            t0,
            f"<< /Type /Font /Subtype /Type0 /BaseFont /SourceHanSerifCN-Regular-Identity-H "
            f"/Encoding /Identity-H /DescendantFonts [ {desc} 0 R ] >>",
        )
        return t0

    fx_id = add_font("Identity")
    fx_gb1 = add_font("GB1")
    fx_jp1 = add_font("Japan1")
    assert is_standard_cjk_ordering(doc, fx_id) is False
    assert is_standard_cjk_ordering(doc, fx_gb1) is True
    assert is_standard_cjk_ordering(doc, fx_jp1) is True
    doc.close()
