from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from conftest import make_embedded_type0_pdf
from PIL import Image
import pytest

from pdf_cmap_fix.cache import MappingCache
from pdf_cmap_fix.ocr import OCRAuthError, OCRConfig
from pdf_cmap_fix.verifier import (
    VerifyOptions,
    parse_contact_sheet_response,
    render_glyph_contact_sheet,
    verify_cache_with_pdfs,
)


def test_parse_contact_sheet_response_handles_numbered_rows_and_graphemes():
    raw = """```
01: 邮局订阅代号：80-
02: e\u0301汉\U00020BB7
03: [封面]
```"""
    parsed = parse_contact_sheet_response(raw)
    assert parsed[1] == ["邮", "局", "订", "阅", "代", "号", "：", "8", "0", "-"]
    assert parsed[2] == ["é", "汉", "\U00020BB7"]
    assert parsed[3] == ["[", "封", "面", "]"]


def test_render_glyph_contact_sheet_preserves_baseline_and_dimensions():
    g1 = Image.new("RGB", (60, 80), (255, 255, 255))
    g2 = Image.new("RGB", (60, 80), (255, 255, 255))
    sheet = render_glyph_contact_sheet([g1, g2, g1], cols=2, cell_size=84)
    # 2 rows x 2 cols
    assert sheet.width > 2 * 84
    assert sheet.height > 2 * 84


def test_verify_cache_locks_exact_matches_and_auto_fixes_with_referee(tmp_path: Path):
    pdf_path, cids = make_embedded_type0_pdf(tmp_path / "sample.pdf", text="ABC", tounicode_mode="strip")
    assert len(cids) == 3

    cache_path = tmp_path / "cache.json"
    c = MappingCache()
    c.add_observation("SourceHanSansCN", cids[0], "A", count=10)
    c.add_observation("SourceHanSansCN", cids[1], "Z", count=2)  # WRONG cached value! Real glyph is 'B'
    c.add_observation("SourceHanSansCN", cids[2], "C", count=10)
    c.save(cache_path)

    # Primary model transcribes "ABC" on sheet, referee confirms "B" on mismatch strip
    class FakePrimary:
        def recognize(self, image: Image.Image) -> str:
            return "01: ABC"

    class FakeReferee:
        def recognize(self, image: Image.Image) -> str:
            return "01: B"

    clients = iter([FakePrimary(), FakeReferee()])
    report_path = tmp_path / "report.json"

    summary = verify_cache_with_pdfs(
        cache_path=cache_path,
        pdf_paths=[pdf_path],
        options=VerifyOptions(
            fonts=("SourceHanSansCN",),
            cols=3,
            rows=1,
            auto_fix=True,
            referee_config=OCRConfig(api_key="k", base_url="https://x/v1", model="ref"),
            report_path=report_path,
            concurrency=1,
        ),
        ocr_client_factory=lambda _: next(clients),
    )

    assert summary.verified_locked == 2  # A and C matched blindly
    assert summary.auto_fixed == 1  # B corrected by unanimous primary + referee
    assert summary.unresolved_mismatches == 0
    assert report_path.exists()

    reloaded = MappingCache.load(cache_path)
    assert reloaded.get_entry("SourceHanSansCN", cids[0]).locked is True
    assert reloaded.resolve_cid("SourceHanSansCN", cids[1]) == "B"
    assert reloaded.get_entry("SourceHanSansCN", cids[1]).locked is True
    assert reloaded.get_entry("SourceHanSansCN", cids[2]).locked is True


def test_verify_cache_does_not_overwrite_when_referee_disagrees(tmp_path: Path):
    pdf_path, cids = make_embedded_type0_pdf(tmp_path / "sample.pdf", text="AB", tounicode_mode="strip")
    cache_path = tmp_path / "cache.json"
    c = MappingCache()
    c.add_observation("SourceHanSansCN", cids[0], "A", count=5)
    c.add_observation("SourceHanSansCN", cids[1], "Z", count=5)
    c.save(cache_path)

    class FakePrimary:
        def recognize(self, image: Image.Image) -> str:
            return "01: AB"

    class FakeRefereeDisagree:
        def recognize(self, image: Image.Image) -> str:
            return "01: Q"  # disagrees with both Z and B!

    clients = iter([FakePrimary(), FakeRefereeDisagree()])

    summary = verify_cache_with_pdfs(
        cache_path=cache_path,
        pdf_paths=[pdf_path],
        options=VerifyOptions(
            fonts=("SourceHanSansCN",),
            cols=2,
            rows=1,
            auto_fix=True,
            referee_config=OCRConfig(api_key="k", base_url="https://x/v1", model="ref"),
            concurrency=1,
        ),
        ocr_client_factory=lambda _: next(clients),
    )

    assert summary.verified_locked == 1
    assert summary.auto_fixed == 0
    assert summary.unresolved_mismatches == 1

    reloaded = MappingCache.load(cache_path)
    assert reloaded.get_entry("SourceHanSansCN", cids[0]).locked is True
    # Must remain untouched & unlocked when models disagree
    assert reloaded.resolve_cid("SourceHanSansCN", cids[1]) == "Z"
    assert reloaded.get_entry("SourceHanSansCN", cids[1]).locked is False


def test_verify_cache_skips_already_locked_entries_by_default(tmp_path: Path):
    pdf_path, cids = make_embedded_type0_pdf(tmp_path / "sample.pdf", text="AB", tounicode_mode="strip")
    cache_path = tmp_path / "cache.json"
    c = MappingCache()
    c.set_mapping("SourceHanSansCN", cids[0], "A", lock=True)
    c.set_mapping("SourceHanSansCN", cids[1], "B", lock=True)
    c.save(cache_path)

    called = {"n": 0}

    def fail_if_called(_):
        called["n"] += 1
        raise AssertionError("OCR should not be called when all renderable CIDs are already locked")

    summary = verify_cache_with_pdfs(
        cache_path=cache_path,
        pdf_paths=[pdf_path],
        options=VerifyOptions(fonts=("SourceHanSansCN",)),
        ocr_client_factory=fail_if_called,
    )
    assert summary.skipped_already_locked == 2
    assert called["n"] == 0
