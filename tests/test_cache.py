from __future__ import annotations

import json
from pathlib import Path

import pytest

from pdf_cmap_fix.cache import (
    CidEntry,
    MappingCache,
    decode_legacy_unicode_int,
    validate_unicode_text,
)


def test_validate_unicode_text_accepts_bmp_non_bmp_ligature_and_combining():
    assert validate_unicode_text("责") == "责"
    assert validate_unicode_text("䴙") == "䴙"
    assert validate_unicode_text("\U00020BB7") == "\U00020BB7"
    assert validate_unicode_text("fi") == "fi"
    assert validate_unicode_text("ffi") == "ffi"
    assert validate_unicode_text("e\u0301") == "e\u0301"


@pytest.mark.parametrize("bad", ["", "\x00", "a\x00b", "\ud800", "\udfff", "x\udc00"])
def test_validate_unicode_text_rejects_empty_nul_and_lone_surrogates(bad: str):
    with pytest.raises(ValueError):
        validate_unicode_text(bad)


def test_decode_legacy_unicode_int_handles_bmp_non_bmp_and_packed_ligatures():
    assert decode_legacy_unicode_int(ord("责")) == "责"
    assert decode_legacy_unicode_int(0x20BB7) == "\U00020BB7"
    assert decode_legacy_unicode_int(0x00660069) == "fi"
    assert decode_legacy_unicode_int(0x006600660069) == "ffi"
    assert decode_legacy_unicode_int(0) is None
    assert decode_legacy_unicode_int(0xD800) is None


def test_v1_lossless_migration_preserves_all_candidate_counts(tmp_path: Path):
    v1_path = tmp_path / "legacy.json"
    v1_data = {
        "SourceHanSansCN": {
            "39187": [36131, 36131, 36132],
            "100": [0x00660069, 0x00660069],
            "200": [0x20BB7],
        }
    }
    v1_path.write_text(json.dumps(v1_data), encoding="utf-8")

    cache = MappingCache.load(v1_path)
    entry = cache.get_entry("SourceHanSansCN", 39187)
    assert entry is not None
    assert entry.counts == {chr(36131): 2, chr(36132): 1}
    assert entry.locked is False

    lig = cache.get_entry("SourceHanSansCN", 100)
    assert lig is not None and lig.counts == {"fi": 2}

    non_bmp = cache.get_entry("SourceHanSansCN", 200)
    assert non_bmp is not None and non_bmp.counts == {"\U00020BB7": 1}

    v2_path = tmp_path / "v2.json"
    cache.save(v2_path)
    raw = json.loads(v2_path.read_text(encoding="utf-8"))
    assert raw["version"] == 2
    assert raw["fonts"]["SourceHanSansCN"]["39187"]["counts"] == {chr(36131): 2, chr(36132): 1}

    reloaded = MappingCache.load(v2_path)
    assert reloaded.resolve_font_map("SourceHanSansCN") == {
        39187: chr(36131),
        100: "fi",
        200: "\U00020BB7",
    }


def test_stability_rules_locked_empty_and_dominance():
    empty_locked = CidEntry(counts={}, locked=True)
    assert empty_locked.top() is None
    assert empty_locked.is_stable() is False

    locked_one = CidEntry(counts={"中": 1}, locked=True)
    assert locked_one.is_stable(min_count=10) is True

    weak = CidEntry(counts={"中": 4}, locked=False)
    assert weak.is_stable(min_count=5) is False

    contested = CidEntry(counts={"中": 6, "申": 2}, locked=False)
    assert contested.is_stable(min_count=5, dominance_ratio=4.0) is False

    dominant = CidEntry(counts={"中": 8, "申": 2}, locked=False)
    assert dominant.is_stable(min_count=5, dominance_ratio=4.0) is True

    # Fullwidth vs halfwidth punctuation with massive evidence (e.g. '，':1070 vs ',':395) is stable
    fw_punct = CidEntry(counts={"，": 1070, ",": 395}, locked=False)
    assert fw_punct.is_stable(min_count=5, dominance_ratio=4.0) is True

    # High-volume statistical majority (25 vs 8 -> >3x with >=20 top hits) is stable
    high_vol = CidEntry(counts={"秀": 25, "青": 8}, locked=False)
    assert high_vol.is_stable(min_count=5, dominance_ratio=4.0) is True


def test_add_observation_protects_locked_entry():
    cache = MappingCache()
    cache.set_mapping("SourceHanSansCN", 32, "?", lock=True)
    for _ in range(10):
        cache.add_observation("SourceHanSansCN", 32, " ")
    entry = cache.get_entry("SourceHanSansCN", 32)
    assert entry is not None
    assert entry.top() == ("?", 1)
    assert " " not in entry.counts


def test_compact_promotes_and_caps_without_zeroing_low_freq_candidates():
    cache = MappingCache()
    cache.add_observation("SourceHanSansCN", 10, "的", count=500)
    cache.add_observation("SourceHanSansCN", 10, "白", count=1)
    cache.add_observation("SourceHanSansCN", 11, "一", count=8)
    cache.add_observation("SourceHanSansCN", 11, "二", count=2)
    cache.add_observation("SourceHanSansCN", 12, "三", count=4)
    cache.add_observation("SourceHanSansCN", 12, "四", count=2)

    summary = cache.compact(promote_min=7, dominance_ratio=4.0, cap=100)
    assert summary["promoted"] == 2
    e10 = cache.get_entry("SourceHanSansCN", 10)
    assert e10 is not None
    assert e10.locked is True
    assert e10.counts["的"] == 100
    assert e10.counts["白"] == 1  # clamped >= 1, never zeroed out!

    e11 = cache.get_entry("SourceHanSansCN", 11)
    assert e11 is not None and e11.locked is True

    e12 = cache.get_entry("SourceHanSansCN", 12)
    assert e12 is not None and e12.locked is False


def test_conflicts_and_font_conflict_rate():
    cache = MappingCache()
    cache.add_observation("ArialMT", 3, "A", count=5)
    cache.add_observation("ArialMT", 3, "Ó", count=3)
    cache.add_observation("ArialMT", 4, "E", count=5)
    cache.add_observation("ArialMT", 4, "i", count=2)
    assert cache.font_conflict_rate("ArialMT") == 1.0
    conflicts = cache.conflicts()
    assert len(conflicts) == 2
    assert conflicts[0]["font"] == "ArialMT"
