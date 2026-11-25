from __future__ import annotations

from pathlib import Path
import re

from conftest import make_embedded_type0_pdf
import fitz
from PIL import Image
import pytest

from pdf_cmap_fix.cache import MappingCache
from pdf_cmap_fix.cmap import generate_tounicode_cmap, validate_tounicode_cmap
from pdf_cmap_fix.detector import inspect_pdf
from pdf_cmap_fix.ocr import OCRAuthError, OCRConfig
from pdf_cmap_fix.patcher import FixOptions, fix_pdf


class FakeOCR:
    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls = 0

    def recognize(self, image: Image.Image) -> str:
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_fix_pdf_if_needed_skips_healthy_pdf(tmp_path: Path):
    _, cids = make_embedded_type0_pdf(tmp_path / "probe.pdf", text="AB", tounicode_mode="strip")
    cmap = generate_tounicode_cmap({cids[0]: "A", cids[1]: "B"}).encode("ascii")
    src, _ = make_embedded_type0_pdf(tmp_path / "ok.pdf", text="AB", tounicode_mode="custom", custom_tounicode=cmap)
    out = tmp_path / "out.pdf"

    res = fix_pdf(src, out, FixOptions(if_needed=True, no_ocr=True))
    assert res.status == "skipped"
    assert not out.exists()


def test_fix_pdf_ocr_injects_strict_adobe_cmap_with_clean_stream_dict(tmp_path: Path):
    src, cids = make_embedded_type0_pdf(tmp_path / "missing.pdf", text="HELLO", tounicode_mode="strip")
    out = tmp_path / "fixed.pdf"
    cache_path = tmp_path / "cache.json"
    fake = FakeOCR("HELLO")

    res = fix_pdf(
        src,
        out,
        FixOptions(cache_path=cache_path, if_needed=True),
        ocr_client_factory=lambda _: fake,
    )
    assert res.status == "fixed"
    assert res.patched_fonts == 1
    assert res.unmapped_cids == 0
    assert fake.calls == 1
    assert out.exists()

    # Verify extracted text is exact and inspect_pdf reports healthy
    doc = fitz.open(out)
    assert "HELLO" in doc[0].get_text("text")
    fx = doc[0].get_fonts(full=True)[0][0]
    tu_kind, tu_val = doc.xref_get_key(fx, "ToUnicode")
    assert tu_kind == "xref"
    tu_xref = int(re.search(r"(\d+)", tu_val).group(1))
    tu_obj = doc.xref_object(tu_xref)
    assert "/Type /Stream" not in tu_obj  # Clean stream dict!
    cmap_str = doc.xref_stream(tu_xref).decode("ascii")
    assert validate_tounicode_cmap(cmap_str) == []
    doc.close()

    assert inspect_pdf(out).needs_fix is False
    # Cache persisted with learned observations
    reloaded = MappingCache.load(cache_path)
    assert reloaded.resolve_cid("SourceHanSansCN", cids[0]) == "H"


def test_fix_pdf_auth_failure_refuses_to_write_partial_cmap(tmp_path: Path):
    src, _ = make_embedded_type0_pdf(tmp_path / "missing.pdf", text="AB", tounicode_mode="strip")
    out = tmp_path / "should_not_exist.pdf"
    fake = FakeOCR(OCRAuthError("401 unauthorized"))

    res = fix_pdf(
        src,
        out,
        FixOptions(if_needed=True),
        ocr_client_factory=lambda _: fake,
    )
    assert res.status == "failed"
    assert "401" in (res.error or "")
    assert not out.exists()


def test_fix_pdf_lazy_ocr_succeeds_without_api_key_when_cache_covers_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    for var in ("PDF_CMAP_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY", "PDF_CMAP_MODEL"):
        monkeypatch.delenv(var, raising=False)

    src, cids = make_embedded_type0_pdf(tmp_path / "missing.pdf", text="AB", tounicode_mode="strip")
    cache_path = tmp_path / "cache.json"
    c = MappingCache()
    c.set_mapping("SourceHanSansCN", cids[0], "A", lock=True)
    c.set_mapping("SourceHanSansCN", cids[1], "B", lock=True)
    c.save(cache_path)

    out = tmp_path / "fixed.pdf"
    res = fix_pdf(src, out, FixOptions(cache_path=cache_path, ocr_config=OCRConfig()))
    assert res.status == "fixed"
    assert res.ocr_calls == 0
    assert inspect_pdf(out).needs_fix is False


def test_fix_pdf_no_ocr_partial_returns_partial_status_and_check_still_flags_needs_fix(tmp_path: Path):
    src, cids = make_embedded_type0_pdf(tmp_path / "missing.pdf", text="AB", tounicode_mode="strip")
    cache_path = tmp_path / "cache.json"
    c = MappingCache()
    c.set_mapping("SourceHanSansCN", cids[0], "A", lock=True)
    c.save(cache_path)

    out = tmp_path / "partial.pdf"
    res = fix_pdf(src, out, FixOptions(cache_path=cache_path, no_ocr=True))
    assert res.status == "partial"
    assert res.unmapped_cids == 1
    assert out.exists()
    # Coverage-aware detector MUST still flag partial PDF as needing fix!
    assert inspect_pdf(out).needs_fix is True


def test_fix_pdf_force_strips_existing_corrupted_tounicode_before_extracting_cids(tmp_path: Path):
    _, cids = make_embedded_type0_pdf(tmp_path / "probe.pdf", text="AB", tounicode_mode="strip")
    # Intentionally wrong existing ToUnicode mapping A->X, B->Y
    wrong_cmap = generate_tounicode_cmap({cids[0]: "X", cids[1]: "Y"}).encode("ascii")
    src, _ = make_embedded_type0_pdf(
        tmp_path / "corrupt.pdf", text="AB", tounicode_mode="custom", custom_tounicode=wrong_cmap
    )
    out = tmp_path / "rebuilt.pdf"
    cache_path = tmp_path / "cache.json"
    fake = FakeOCR("AB")

    res = fix_pdf(
        src,
        out,
        FixOptions(cache_path=cache_path, force=True),
        ocr_client_factory=lambda _: fake,
    )
    assert res.status == "fixed"
    doc = fitz.open(out)
    assert "AB" in doc[0].get_text("text")
    assert "XY" not in doc[0].get_text("text")
    doc.close()


def test_fix_pdf_caps_ocr_jobs_per_cid_on_repeated_unknown_char(tmp_path: Path):
    # Create a multi-line PDF where the same unknown glyph 'A' appears in 8 distinct lines
    doc = fitz.open()
    page = doc.new_page(width=400, height=500)
    f = fitz.Font("helv")
    page.insert_font(fontname="F0", fontbuffer=f.buffer)
    lines = ["AB", "AC", "AD", "AE", "AF", "AG", "AH", "AI"]
    for idx, txt in enumerate(lines):
        page.insert_text((40, 40 + idx * 30), txt, fontname="F0", fontsize=14)
    raw_bytes = doc.tobytes()
    doc.close()

    d2 = fitz.open(stream=raw_bytes, filetype="pdf")
    fx = d2[0].get_fonts(full=True)[0][0]
    d2.xref_set_key(fx, "BaseFont", "/AAAAAA+DplrwySourceHanSansCN-Regular-Identity-H")
    obj = d2.xref_object(fx)
    d2.update_object(fx, "\n".join(ln for ln in obj.splitlines() if "/ToUnicode" not in ln))
    src = tmp_path / "multi_lines.pdf"
    d2.save(src)
    d2.close()

    # Pre-seed B..I as locked so ONLY 'A' is unknown across all 8 distinct lines!
    probe = fitz.open(src)
    cids_by_line = []
    for b in probe[0].get_text("rawdict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b["lines"]:
            for sp in ln["spans"]:
                cids_by_line.append([ord(c["c"]) for c in sp["chars"] if not c.get("synthetic")])
    probe.close()

    cache_path = tmp_path / "cache.json"
    c = MappingCache()
    for txt, lcids in zip(lines, cids_by_line):
        c.set_mapping("SourceHanSansCN", lcids[1], txt[1], lock=True)
    c.save(cache_path)

    fake = FakeOCR("AB")
    out = tmp_path / "out.pdf"
    res = fix_pdf(
        src,
        out,
        FixOptions(cache_path=cache_path, max_jobs_per_cid=2),
        ocr_client_factory=lambda _: fake,
    )
    assert res.status == "fixed"
    assert fake.calls <= 2  # Capped at max_jobs_per_cid=2 instead of OCRing all 8 lines!


def test_single_ocr_noise_across_two_xrefs_does_not_drop_cached_family_mappings(tmp_path: Path):
    # Two Type0 xrefs of the same stable family on two pages:
    # 'A' is already cached/locked; 'B' is unknown and OCR returns slightly noisy 'B' vs '8' across the two xrefs.
    doc = fitz.open()
    f = fitz.Font("helv")
    for _ in range(2):
        p = doc.new_page(width=400, height=200)
        p.insert_font(fontname="F0", fontbuffer=f.buffer)
        p.insert_text((40, 100), "AB", fontname="F0", fontsize=14)
    raw = doc.tobytes()
    doc.close()

    d2 = fitz.open(stream=raw, filetype="pdf")
    xrefs = []
    for idx, p in enumerate(d2):
        fx = p.get_fonts(full=True)[0][0]
        if fx not in xrefs:
            xrefs.append(fx)
        d2.xref_set_key(fx, "BaseFont", f"/AAAAA{chr(65+idx)}+DplrwySourceHanSansCN-Regular-Identity-H")
        obj = d2.xref_object(fx)
        d2.update_object(fx, "\n".join(ln for ln in obj.splitlines() if "/ToUnicode" not in ln))
    src = tmp_path / "two_xrefs.pdf"
    d2.save(src)
    d2.close()

    # Read raw CIDs for 'A' and 'B'
    probe = fitz.open(src)
    cids = [ord(c["c"]) for c in probe[0].get_text("rawdict")["blocks"][0]["lines"][0]["spans"][0]["chars"] if not c.get("synthetic")]
    probe.close()

    cache_path = tmp_path / "cache.json"
    c = MappingCache()
    c.set_mapping("SourceHanSansCN", cids[0], "A", lock=True)
    c.save(cache_path)

    responses = iter(["AB", "A8"])

    class NoisyOCR:
        def recognize(self, image: Image.Image) -> str:
            return next(responses, "AB")

    out = tmp_path / "out.pdf"
    res = fix_pdf(
        src,
        out,
        FixOptions(cache_path=cache_path, concurrency=1),
        ocr_client_factory=lambda _: NoisyOCR(),
    )
    assert res.status == "fixed"
    assert res.unmapped_cids == 0
    # Cached 'A' must NEVER be dropped on either page!
    out_doc = fitz.open(out)
    assert "A" in out_doc[0].get_text("text")
    assert "A" in out_doc[1].get_text("text")
    out_doc.close()


def test_patching_incomplete_cmap_preserves_existing_valid_mappings(tmp_path: Path):
    _, cids = make_embedded_type0_pdf(tmp_path / "probe.pdf", text="AB", tounicode_mode="strip")
    existing_cmap = generate_tounicode_cmap({cids[0]: "A"}).encode("ascii")
    src, _ = make_embedded_type0_pdf(
        tmp_path / "partial_src.pdf", text="AB", tounicode_mode="custom", custom_tounicode=existing_cmap
    )
    # Cache only knows cids[1] -> "B" (does NOT have cids[0])
    cache_path = tmp_path / "cache.json"
    c = MappingCache()
    c.set_mapping("SourceHanSansCN", cids[1], "B", lock=True)
    c.save(cache_path)

    out = tmp_path / "merged_out.pdf"
    res = fix_pdf(src, out, FixOptions(cache_path=cache_path, no_ocr=True))
    assert res.status == "fixed"
    assert res.unmapped_cids == 0
    doc = fitz.open(out)
    assert "AB" in doc[0].get_text("text")
    doc.close()


def test_explicit_space_cid_overrides_locked_cache_entry(tmp_path: Path):
    src, cids = make_embedded_type0_pdf(tmp_path / "src.pdf", text="AB", tounicode_mode="strip")
    cache_path = tmp_path / "cache.json"
    c = MappingCache()
    c.set_mapping("SourceHanSansCN", cids[0], "X", lock=True)
    c.set_mapping("SourceHanSansCN", cids[1], "B", lock=True)
    c.save(cache_path)

    out = tmp_path / "space_out.pdf"
    res = fix_pdf(
        src,
        out,
        FixOptions(cache_path=cache_path, no_ocr=True, space_cids=frozenset({cids[0]})),
    )
    assert res.status == "fixed"
    doc = fitz.open(out)
    txt = doc[0].get_text("text")
    assert "B" in txt
    assert "X" not in txt
    doc.close()


def test_true_two_xref_subset_conflict_does_not_pollute_shared_cache(tmp_path: Path):
    # Build two genuinely separate Type0 font xrefs (two separate embedded font instances) of the same family
    doc = fitz.open()
    f = fitz.Font("helv")
    p1 = doc.new_page(width=400, height=200)
    p1.insert_font(fontname="F1", fontbuffer=f.buffer)
    p1.insert_text((40, 100), "ABC", fontname="F1", fontsize=14)
    p2 = doc.new_page(width=400, height=200)
    p2.insert_font(fontname="F2", fontbuffer=f.buffer)
    p2.insert_text((40, 100), "ABC", fontname="F2", fontsize=14)
    raw = doc.tobytes()
    doc.close()

    d2 = fitz.open(stream=raw, filetype="pdf")
    fx1 = d2[0].get_fonts(full=True)[0][0]
    # Clone fx1 to a brand-new xref on page 2 so p1 and p2 have truly distinct font xrefs!
    fx2 = d2.get_new_xref()
    d2.update_object(fx2, d2.xref_object(fx1))
    res_k, res_v = d2.xref_get_key(d2[1].xref, "Resources")
    if res_k == "xref":
        res_xref = int(re.search(r"(\d+)", res_v).group(1))
        d2.xref_set_key(res_xref, "Font/F2", f"{fx2} 0 R")
    else:
        d2.xref_set_key(d2[1].xref, "Resources/Font/F2", f"{fx2} 0 R")
    for idx, fx in enumerate((fx1, fx2)):
        d2.xref_set_key(fx, "BaseFont", f"/AAAAA{chr(65+idx)}+ArialMT-Identity-H")
        obj = d2.xref_object(fx)
        d2.update_object(fx, "\n".join(ln for ln in obj.splitlines() if "/ToUnicode" not in ln))
    src = tmp_path / "conflict_two_xrefs.pdf"
    d2.save(src)
    d2.close()
    assert fx1 != fx2

    cache_path = tmp_path / "cache.json"
    responses = iter(["ABC", "XYZ"])

    class ConflictOCR:
        def recognize(self, image: Image.Image) -> str:
            return next(responses)

    out = tmp_path / "out.pdf"
    res = fix_pdf(
        src,
        out,
        FixOptions(cache_path=cache_path, concurrency=1),
        ocr_client_factory=lambda _: ConflictOCR(),
    )
    assert res.status == "fixed"
    out_doc = fitz.open(out)
    assert "ABC" in out_doc[0].get_text("text")
    assert "XYZ" in out_doc[1].get_text("text")
    out_doc.close()

    # Shared cache MUST NOT be polluted by the conflicting subset-renumbered family!
    reloaded = MappingCache.load(cache_path)
    assert "ArialMT" not in reloaded.fonts
