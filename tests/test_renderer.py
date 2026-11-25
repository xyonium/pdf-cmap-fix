from __future__ import annotations

from pathlib import Path

from conftest import make_embedded_type0_pdf
import fitz
import pytest

from pdf_cmap_fix.renderer import (
    SpanTask,
    SyntheticReplayRenderer,
    build_tj_content_stream,
    split_tokens_into_chunks,
)


def test_build_tj_content_stream_uses_valid_hex_strings_without_parentheses():
    stream = build_tj_content_stream([0x9F71, None, 0x3F77, 0x96E7], fontsize=48.0)
    text = stream.decode("ascii")
    # Must be [<9F71> -500 <3F7796E7>] TJ — NEVER [(<9F71>) ...]
    assert "[<9F71> -500 <3F7796E7>] TJ" in text
    assert "(<" not in text
    assert ">)" not in text


def test_split_tokens_into_chunks_respects_max_cids_and_overlap():
    tokens = list(range(100, 175))  # 75 CIDs
    chunks = split_tokens_into_chunks(tokens, width_fn=lambda _: 1000.0, fontsize=48.0, max_cids=28, max_width_pt=1500.0, overlap=2)
    assert len(chunks) >= 3
    for ch in chunks:
        real_cids = [t for t in ch.tokens if t is not None]
        assert len(real_cids) <= 28
    # Every original CID index is covered
    covered = set()
    for ch in chunks:
        for offset in range(len([t for t in ch.tokens if t is not None])):
            covered.add(ch.start_cid_index + offset)
    assert covered == set(range(75))


def test_synthetic_replay_renders_clean_horizontal_image_and_roundtrips_cids(tmp_path: Path):
    # Create a 90-degree rotated tiny-font PDF and verify synthetic replay renders upright horizontal image!
    pdf_path, cids = make_embedded_type0_pdf(
        tmp_path / "rotated_tiny.pdf",
        text="HELLO",
        tounicode_mode="strip",
        rotate_deg=90,
        fontsize=6.0,
    )
    assert len(cids) == 5

    with SyntheticReplayRenderer(pdf_path) as renderer:
        fonts = renderer.doc[0].get_fonts(full=True)
        font_xref = fonts[0][0]
        tokens: list[int | None] = [cids[0], cids[1], None, cids[2], cids[3], cids[4]]
        task = SpanTask(
            page_index=0,
            block_index=0,
            line_index=0,
            span_index=0,
            font_xref=font_xref,
            simplified_font="SourceHanSansCN",
            encoding="Identity-H",
            tokens=tuple(tokens),
            bbox=(100.0, 100.0, 110.0, 140.0),
            char_bboxes=((100.0, 100.0, 110.0, 140.0),),
            font_size=6.0,
            line_dir=(0.0, -1.0),
            wmode=0,
        )
        res = renderer.render_span(task)
        assert res is not None
        assert res.method == "synthetic"
        # Upright horizontal multi-char span: wider than tall and high-res (font rendered at 48pt @ 144dpi)
        assert res.image.width > res.image.height
        assert res.image.height >= 80

        # Verify MuPDF extracts the exact same 5 raw CIDs back from the scratch page!
        raw = renderer._scratch_page.get_text("rawdict")
        extracted = [
            ord(ch["c"])
            for b in raw.get("blocks", [])
            if b.get("type") == 0
            for ln in b.get("lines", [])
            for sp in ln.get("spans", [])
            for ch in sp.get("chars", [])
            if not ch.get("synthetic") and ch.get("c")
        ]
        assert extracted == cids

        # Verify O(1) xref reuse across repeated renders
        xref_count_before = renderer.doc.xref_length()
        for _ in range(5):
            res2 = renderer.render_span(task)
            assert res2 is not None and res2.method == "synthetic"
        assert renderer.doc.xref_length() == xref_count_before


def test_fallback_clip_renderer_for_missing_xref_and_identity_v_no_rotation(tmp_path: Path):
    pdf_path, cids = make_embedded_type0_pdf(tmp_path / "normal.pdf", text="ABCDEF", tounicode_mode="strip", fontsize=10.0)
    with SyntheticReplayRenderer(pdf_path) as renderer:
        raw = renderer.doc[0].get_text("rawdict")
        span = raw["blocks"][0]["lines"][0]["spans"][0]
        cbboxes = tuple(tuple(ch["bbox"]) for ch in span["chars"])

        # Missing font_xref -> clip_fallback
        task_no_xref = SpanTask(
            page_index=0,
            block_index=0,
            line_index=0,
            span_index=0,
            font_xref=None,
            simplified_font="SourceHanSansCN",
            encoding="Identity-H",
            tokens=tuple(cids),
            bbox=tuple(span["bbox"]),
            char_bboxes=cbboxes,
            font_size=10.0,
            line_dir=(1.0, 0.0),
            wmode=0,
        )
        res = renderer.render_span(task_no_xref)
        assert res is not None
        assert res.method == "clip_fallback"
        assert res.image.height >= 40
        assert res.image.width > res.image.height * 1.5

        # Identity-V with wmode=1 and line_dir=(0, 1) -> must NOT rotate 90 degrees!
        font_xref = renderer.doc[0].get_fonts(full=True)[0][0]
        task_v = SpanTask(
            page_index=0,
            block_index=0,
            line_index=0,
            span_index=0,
            font_xref=font_xref,
            simplified_font="SourceHanSansCN",
            encoding="Identity-V",
            tokens=tuple(cids),
            bbox=tuple(span["bbox"]),
            char_bboxes=cbboxes,
            font_size=10.0,
            line_dir=(0.0, 1.0),
            wmode=1,
        )
        res_v = renderer.render_span(task_v)
        assert res_v is not None
        assert res_v.method == "clip_fallback"
        # Since original "ABCDEF" is horizontal and wmode=1 suppresses rotation, aspect remains wide
        assert res_v.image.width > res_v.image.height * 1.5

        # Dynamic zero-ink detection: real visible glyph 'A' is NOT blank
        assert renderer.is_blank_glyph(font_xref, cids[0]) is False
