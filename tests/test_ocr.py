from __future__ import annotations

from types import SimpleNamespace

from PIL import Image
import pytest

from pdf_cmap_fix.ocr import (
    OCRAuthError,
    OCRConfig,
    OCRConfigError,
    OpenAIVisionOCR,
    align_cids_with_ocr,
    resolve_ocr_config,
    validate_ocr_config,
)


def test_resolve_ocr_config_cli_overrides_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PDF_CMAP_API_KEY", "env-primary")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "env-dash")
    monkeypatch.setenv("PDF_CMAP_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("PDF_CMAP_MODEL", "qwen4-max")

    cfg_env = resolve_ocr_config()
    assert cfg_env.api_key == "env-primary"
    assert cfg_env.base_url == "https://env.example/v1"
    assert cfg_env.model == "qwen4-max"

    cfg_cli = resolve_ocr_config(api_key="cli-key", base_url="https://cli.example/v1", model="qwen3.8-max")
    assert cfg_cli.api_key == "cli-key"
    assert cfg_cli.base_url == "https://cli.example/v1"
    assert cfg_cli.model == "qwen3.8-max"

    monkeypatch.delenv("PDF_CMAP_API_KEY")
    cfg_fallback = resolve_ocr_config()
    assert cfg_fallback.api_key == "env-dash"


def test_validate_ocr_config_raises_clear_error_when_missing_key_or_model(monkeypatch: pytest.MonkeyPatch):
    for var in ("PDF_CMAP_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY", "PDF_CMAP_MODEL"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(OCRConfigError, match="API key"):
        validate_ocr_config(resolve_ocr_config(model="qwen4-max"))

    with pytest.raises(OCRConfigError, match="model"):
        validate_ocr_config(resolve_ocr_config(api_key="test-key"))


def test_align_cids_exact_match_preserves_fullwidth_and_doubled_chars():
    # Doubled leading chars ("22世纪") must NOT be stripped when lengths match!
    pairs = align_cids_with_ocr([10, 10, 20, 30], "22 世 纪\n")
    assert pairs == [(10, "2"), (10, "2"), (20, "世"), (30, "纪")]

    # Fullwidth characters preserved verbatim (NOT destructively NFKC-folded to ASCII)
    pairs_fw = align_cids_with_ocr([1, 2, 3], "Ａ：（）"[:3])
    assert pairs_fw == [(1, "Ａ"), (2, "："), (3, "（")]


def test_align_cids_handles_combining_marks_and_non_bmp_variation_selector():
    # NFD 'e' + U+0301 -> NFC 'é'; base + combining mark without precomposed form stays 1 grapheme cluster
    pairs_nfd = align_cids_with_ocr([1, 2], "e\u0301x")
    assert pairs_nfd == [(1, "é"), (2, "x")]

    # Non-precomposed combining sequence ('x' + combining acute accent U+0301) stays 1 cluster
    pairs_comb = align_cids_with_ocr([1, 2], "x\u0301b")
    assert pairs_comb == [(1, "x\u0301"), (2, "b")]

    # Non-BMP + variation selector U+FE00 stays 1 cluster
    pairs_vs = align_cids_with_ocr([1, 2], "\U00020BB7\uFE00汉")
    assert pairs_vs == [(1, "\U00020BB7\uFE00"), (2, "汉")]


def test_align_cids_anchor_recovers_ligatures_and_skips_ambiguous_gaps():
    # Known anchors: 10='o', 30='c', 40='e'; unknown CID 20 is 'ffi' ligature!
    anchors = {10: "o", 30: "c", 40: "e"}
    pairs = align_cids_with_ocr([10, 20, 30, 40], "office", known_anchors=anchors)
    assert (20, "ffi") in pairs
    assert (10, "o") in pairs
    assert (30, "c") in pairs

    # Ambiguous gap: 2 unknown CIDs (21, 22) vs 3 OCR chars ("xyz") between anchors 10='中' and 30='国'
    # Must NOT shift the trailing anchor or fabricate wrong mappings for 21/22!
    anchors2 = {10: "中", 30: "国"}
    pairs2 = align_cids_with_ocr([10, 21, 22, 30], "中xyz国", known_anchors=anchors2)
    mapped_cids = {c for c, _ in pairs2}
    assert 21 not in mapped_cids and 22 not in mapped_cids
    assert (10, "中") in pairs2 and (30, "国") in pairs2


def test_align_cids_mismatch_without_anchors_returns_empty_not_shifted():
    assert align_cids_with_ocr([1, 2, 3], "ab") == []


def test_align_cids_equal_length_respects_multichar_anchor_and_skips_ambiguous_chains():
    # Even though len([10, 20, 30]) == len("fiX") == 3, anchor 10 -> "fi" MUST NOT be split into ('f','i')!
    pairs = align_cids_with_ocr([10, 20, 30], "fiX", known_anchors={10: "fi", 30: "X"})
    as_dict = dict(pairs)
    assert as_dict.get(10) == "fi"
    assert as_dict.get(30) == "X"
    assert 20 not in as_dict  # CID 20 has 0 OCR chars between 'fi' and 'X' -> never mislearned as 'i'!

    # Ambiguous repeated anchor: CID 20='A' could match either first or second 'A' in "XAAB"
    pairs_amb = align_cids_with_ocr([10, 20, 30], "XAAB", known_anchors={10: "X", 20: "A", 30: "B"})
    amb_dict = dict(pairs_amb)
    assert amb_dict.get(10) == "X"
    assert amb_dict.get(30) == "B"
    assert 20 not in amb_dict  # Non-unique anchor position -> skipped instead of arbitrary pick!


def test_openai_ocr_retries_429_and_fails_fast_on_401():
    img = Image.new("RGB", (64, 32), (255, 255, 255))

    class RateLimitErr(Exception):
        status_code = 429

    class AuthErr(Exception):
        status_code = 401

    calls = {"n": 0}

    def fake_create_retry(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimitErr("rate limit")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="小䴙䴘"))])

    fake_client_ok = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create_retry)))
    ocr_ok = OpenAIVisionOCR(
        OCRConfig(api_key="k", base_url="https://x/v1", model="m", max_retries=4),
        client=fake_client_ok,
        sleep_fn=lambda _: None,
    )
    assert ocr_ok.recognize(img) == "小䴙䴘"
    assert calls["n"] == 3

    auth_calls = {"n": 0}

    def fake_create_401(**kwargs):
        auth_calls["n"] += 1
        raise AuthErr("unauthorized")

    fake_client_401 = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create_401)))
    ocr_401 = OpenAIVisionOCR(
        OCRConfig(api_key="bad", base_url="https://x/v1", model="m", max_retries=5),
        client=fake_client_401,
        sleep_fn=lambda _: None,
    )
    with pytest.raises(OCRAuthError):
        ocr_401.recognize(img)
    assert auth_calls["n"] == 1  # Fails fast without retrying 401!

    # DashScope returns HTTP 400 for overdue-payment / Access denied -> must also raise OCRAuthError!
    class Overdue400Err(Exception):
        status_code = 400

    def fake_create_400_overdue(**kwargs):
        raise Overdue400Err("Error code: 400 - Access denied, overdue-payment")

    fake_client_400 = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create_400_overdue)))
    ocr_400 = OpenAIVisionOCR(
        OCRConfig(api_key="k", base_url="https://x/v1", model="m", max_retries=5),
        client=fake_client_400,
        sleep_fn=lambda _: None,
    )
    with pytest.raises(OCRAuthError):
        ocr_400.recognize(img)
