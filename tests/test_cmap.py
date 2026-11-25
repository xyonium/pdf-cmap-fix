from __future__ import annotations

import pytest

from pdf_cmap_fix.cmap import (
    decode_unicode_hex,
    encode_unicode_hex,
    generate_tounicode_cmap,
    parse_tounicode_cmap,
    sanitize_cmap_name,
    validate_tounicode_cmap,
)


def test_encode_and_decode_unicode_hex_roundtrip():
    samples = ["A", "?", "中", "䴙", "\U00020BB7", "\U000285C8", "fi", "ffi", "e\u0301"]
    for s in samples:
        hx = encode_unicode_hex(s)
        assert len(hx) >= 4 and len(hx) % 4 == 0
        assert decode_unicode_hex(hx) == s

    assert encode_unicode_hex("\U00020BB7") == "D842DFB7"
    assert encode_unicode_hex("fi") == "00660069"
    assert encode_unicode_hex("ffi") == "006600660069"


def test_default_pure_bfchar_generation_valid_and_roundtrips():
    mapping = {
        0x0020: "?",
        0x4BFE: chr(0x635D),
        0x4BFF: chr(0x635E),
        0x4C00: chr(0x635F),
        0x2D08: chr(0x51FF),
        0x2D09: chr(0x5200),
        0x358A: "\U00020BB7",
        0x0010: "fi",
        0x0011: "ffi",
    }
    cmap = generate_tounicode_cmap(mapping)
    assert "beginbfrange" not in cmap
    assert "/Ordering (UCS) /Supplement 0" in cmap
    assert "/CMapName /Adobe-Identity-UCS def" in cmap
    assert "CMapName currentdict /CMap defineresource pop" in cmap
    assert validate_tounicode_cmap(cmap) == []
    assert parse_tounicode_cmap(cmap) == mapping


def test_bfrange_mode_splits_at_cid_high_byte_and_unicode_low_byte_and_surrogate_boundaries():
    mapping = {
        # CID high byte boundary: 0x4BFF -> 0x4C00
        0x4BFE: chr(0x635D),
        0x4BFF: chr(0x635E),
        0x4C00: chr(0x635F),
        0x4C01: chr(0x6360),
        # BMP target low byte 0xFF boundary: 0x51FF -> 0x5200
        0x2D08: chr(0x51FF),
        0x2D09: chr(0x5200),
        # Non-BMP low-surrogate low-byte boundary: U+100FF (D800 DCFF) -> U+10100 (D800 DD00)
        0x5001: "\U000100FF",
        0x5002: "\U00010100",
        # Non-BMP high-surrogate boundary: U+103FF (D800 DFFF) -> U+10400 (D801 DC00)
        0x5101: "\U000103FF",
        0x5102: "\U00010400",
        # Consecutive ligatures must NEVER use bfrange
        0x0010: "fi",
        0x0011: "fj",
    }
    cmap = generate_tounicode_cmap(mapping, use_bfrange=True)
    assert validate_tounicode_cmap(cmap) == []
    assert "<4BFE> <4BFF> <635D>" in cmap
    assert "<4C00> <4C01> <635F>" in cmap
    assert "<4BFE> <4C01>" not in cmap
    assert "<2D08> <2D09>" not in cmap
    assert "<5001> <5002>" not in cmap
    assert "<5101> <5102>" not in cmap
    assert "<0010> <0011>" not in cmap
    assert parse_tounicode_cmap(cmap) == mapping


def test_blocks_never_exceed_100_entries():
    mapping = {i + 1: chr(0x4E00 + i) for i in range(250)}
    cmap = generate_tounicode_cmap(mapping, use_bfrange=False)
    assert validate_tounicode_cmap(cmap) == []
    assert cmap.count("100 beginbfchar") == 2
    assert "50 beginbfchar" in cmap
    assert parse_tounicode_cmap(cmap) == mapping


def test_parse_bfrange_array_form():
    cmap = """/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
1 beginbfrange
<0010> <0012> [<0041> <00660069> <D842DFB7>]
endbfrange
endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""
    assert validate_tounicode_cmap(cmap) == []
    parsed = parse_tounicode_cmap(cmap)
    assert parsed == {0x0010: "A", 0x0011: "fi", 0x0012: "\U00020BB7"}


def test_validate_catches_legacy_adobe_reader_violations():
    bad_cmap = """/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (CNS1) /Supplement 7 >> def
/CMapName /SourceHanSansCN-ToUnicode def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
2 beginbfrange
<4BFF> <4C00> <635E>
<2D08> <2D09> <51FF>
endbfrange
endcmap
/SourceHanSansCN-ToUnicode currentdict /CMap defineresource pop
end
end
"""
    errors = validate_tounicode_cmap(bad_cmap)
    assert any("high-byte" in e or "first-byte" in e for e in errors)
    assert any("last-byte" in e or "0xFF" in e for e in errors)
    assert any("CMapName currentdict" in e for e in errors)
    # Parsing a legacy overflowed bfrange (<2D08> <2D09> <51FF>) recovers intended U+5200 (not wrapped U+5100)
    parsed = parse_tounicode_cmap(bad_cmap)
    assert parsed[0x2D08] == "\u51ff"
    assert parsed[0x2D09] == "\u5200"


def test_sanitize_cmap_name_strips_pdf_delimiters():
    assert sanitize_cmap_name("SourceHanSansCN /bad (name)") == "SourceHanSansCN_bad_name"
    assert sanitize_cmap_name("") == "Adobe-Identity-UCS"


def test_empty_mapping_raises_value_error():
    with pytest.raises(ValueError):
        generate_tounicode_cmap({})


def test_validate_accepts_multiline_bfrange_array_and_comments():
    cmap = """% Comment line from Adobe/InDesign
/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
2 beginbfchar
<0001> <0041> <0002> <0042>
endbfchar
1 beginbfrange
<0010> <0012> [
  <0043>
  <00660069>
  <D842DFB7>
]
endbfrange
endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""
    assert validate_tounicode_cmap(cmap) == []
    parsed = parse_tounicode_cmap(cmap)
    assert parsed == {0x0001: "A", 0x0002: "B", 0x0010: "C", 0x0011: "fi", 0x0012: "\U00020BB7"}
