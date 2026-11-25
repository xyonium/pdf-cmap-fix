"""v2 CID-to-Unicode frequency-count cache with lossless v1 migration and compaction."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unicodedata

CACHE_VERSION = 2


def validate_unicode_text(text: str) -> str:
    """Validate that `text` is a non-empty valid Unicode string (no NUL or lone surrogates)."""
    if not isinstance(text, str) or not text:
        raise ValueError("Unicode mapping target must be a non-empty string")
    for ch in text:
        cp = ord(ch)
        if cp == 0:
            raise ValueError("Unicode mapping target must not contain NUL (U+0000)")
        if 0xD800 <= cp <= 0xDFFF:
            raise ValueError(f"Unicode mapping target contains lone surrogate U+{cp:04X}")
    # Ensure clean UTF-16BE encodability
    text.encode("utf-16-be")
    return text


def decode_legacy_unicode_int(val: int) -> str | None:
    """Decode a legacy v1 integer observation into a valid Unicode string.

    - `1 <= val <= 0x10FFFF` (excluding UTF-16 surrogates): single Unicode scalar `chr(val)`.
    - `val > 0x10FFFF`: legacy packed UTF-16BE ligature integer (e.g. `0x00660069` -> `"fi"`,
      `0x006600660069` -> `"ffi"`), left-padded to a 4-hex-digit boundary and decoded as UTF-16BE.
    """
    if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
        return None
    if val <= 0x10FFFF:
        if 0xD800 <= val <= 0xDFFF:
            return None
        return chr(val)
    hex_str = f"{val:X}"
    padded_len = ((len(hex_str) + 3) // 4) * 4
    hex_str = hex_str.zfill(padded_len)
    try:
        decoded = bytes.fromhex(hex_str).decode("utf-16-be")
        return validate_unicode_text(decoded)
    except (ValueError, UnicodeDecodeError):
        return None


@dataclass
class CidEntry:
    counts: dict[str, int] = field(default_factory=dict)
    locked: bool = False

    def top(self) -> tuple[str, int] | None:
        if not self.counts:
            return None
        # Deterministic tie-breaking: highest count first, then lexicographically smallest string
        best_text, best_count = min(self.counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return (best_text, best_count)

    def second_count(self) -> int:
        if len(self.counts) < 2:
            return 0
        ordered = sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return ordered[1][1]

    def is_stable(self, min_count: int = 5, dominance_ratio: float = 4.0) -> bool:
        top_item = self.top()
        if top_item is None:
            return False
        if self.locked:
            return True
        top_text, top_count = top_item
        if top_count < min_count:
            return False
        if len(self.counts) < 2:
            return True
        ordered = sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0]))
        sec_text, sec_count = ordered[1]
        if sec_count == 0 or top_count >= dominance_ratio * sec_count:
            return True
        # Fullwidth/halfwidth NFKC equivalents (e.g. '，' vs ',', '（' vs '(') stabilize once >= 1.5x
        if unicodedata.normalize("NFKC", top_text) == unicodedata.normalize("NFKC", sec_text):
            if top_count >= 1.5 * sec_count:
                return True
        # High-volume statistical consensus (e.g. >= 20 top observations with >= 2x margin)
        if top_count >= max(20, min_count * 4) and top_count >= 2.0 * sec_count:
            return True
        return False


class MappingCache:
    """Per-font CID -> Unicode frequency-count cache (v2 schema, v1 auto-migration)."""

    def __init__(self, fonts: dict[str, dict[int, CidEntry]] | None = None) -> None:
        self.version: int = CACHE_VERSION
        self.fonts: dict[str, dict[int, CidEntry]] = fonts if fonts is not None else {}

    @classmethod
    def load(cls, path: Path | str) -> MappingCache:
        p = Path(path)
        if not p.exists():
            return cls()
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid cache JSON root in {p}: expected object")
        if raw.get("version") == CACHE_VERSION and isinstance(raw.get("fonts"), dict):
            return cls._from_v2_dict(raw)
        return cls._from_v1_dict(raw)

    @classmethod
    def _from_v2_dict(cls, raw: dict[str, Any]) -> MappingCache:
        fonts: dict[str, dict[int, CidEntry]] = {}
        for font_name, cid_map in raw.get("fonts", {}).items():
            if not isinstance(cid_map, dict):
                continue
            parsed_font: dict[int, CidEntry] = {}
            for cid_str, entry_raw in cid_map.items():
                try:
                    cid = int(cid_str)
                except ValueError:
                    continue
                if not (0 <= cid <= 0xFFFF):
                    continue
                if not isinstance(entry_raw, dict):
                    continue
                raw_counts = entry_raw.get("counts", {})
                counts: dict[str, int] = {}
                if isinstance(raw_counts, dict):
                    for text_key, cnt in raw_counts.items():
                        if not isinstance(cnt, int) or cnt <= 0:
                            continue
                        try:
                            valid = validate_unicode_text(str(text_key))
                        except ValueError:
                            continue
                        counts[valid] = counts.get(valid, 0) + cnt
                locked = bool(entry_raw.get("locked", False))
                if counts:
                    parsed_font[cid] = CidEntry(counts=counts, locked=locked)
            if parsed_font:
                fonts[str(font_name)] = parsed_font
        return cls(fonts=fonts)

    @classmethod
    def _from_v1_dict(cls, raw: dict[str, Any]) -> MappingCache:
        fonts: dict[str, dict[int, CidEntry]] = {}
        for font_name, cid_map in raw.items():
            if not isinstance(cid_map, dict):
                continue
            parsed_font: dict[int, CidEntry] = {}
            for cid_str, obs_list in cid_map.items():
                try:
                    cid = int(cid_str)
                except ValueError:
                    continue
                if not (0 <= cid <= 0xFFFF) or not isinstance(obs_list, list):
                    continue
                counter: Counter[str] = Counter()
                for val in obs_list:
                    if isinstance(val, int) and not isinstance(val, bool):
                        decoded = decode_legacy_unicode_int(val)
                        if decoded is not None:
                            counter[decoded] += 1
                    elif isinstance(val, str):
                        try:
                            counter[validate_unicode_text(val)] += 1
                        except ValueError:
                            pass
                if counter:
                    parsed_font[cid] = CidEntry(counts=dict(counter), locked=False)
            if parsed_font:
                fonts[str(font_name)] = parsed_font
        return cls(fonts=fonts)

    def to_dict(self) -> dict[str, Any]:
        serialized_fonts: dict[str, dict[str, Any]] = {}
        for font_name in sorted(self.fonts.keys()):
            cid_map = self.fonts[font_name]
            if not cid_map:
                continue
            serialized_cids: dict[str, Any] = {}
            for cid in sorted(cid_map.keys()):
                entry = cid_map[cid]
                if not entry.counts:
                    continue
                ordered_counts = dict(sorted(entry.counts.items(), key=lambda kv: (-kv[1], kv[0])))
                obj: dict[str, Any] = {"counts": ordered_counts}
                if entry.locked:
                    obj["locked"] = True
                serialized_cids[str(cid)] = obj
            if serialized_cids:
                serialized_fonts[font_name] = serialized_cids
        return {
            "version": CACHE_VERSION,
            "fonts": serialized_fonts,
        }

    def save(self, path: Path | str) -> None:
        """Atomically persist the v2 cache to disk (Windows-safe close before replace)."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def get_entry(self, font: str, cid: int) -> CidEntry | None:
        return self.fonts.get(font, {}).get(cid)

    def add_observation(self, font: str, cid: int, text: str, count: int = 1) -> None:
        if not (0 <= cid <= 0xFFFF):
            raise ValueError(f"CID out of 16-bit range: {cid}")
        if count <= 0:
            return
        valid = validate_unicode_text(text)
        font_map = self.fonts.setdefault(font, {})
        entry = font_map.get(cid)
        if entry is None:
            font_map[cid] = CidEntry(counts={valid: count}, locked=False)
            return
        if entry.locked:
            # Protect locked entries from noisy OCR drift
            return
        entry.counts[valid] = entry.counts.get(valid, 0) + count

    def set_mapping(self, font: str, cid: int, text: str, lock: bool = True) -> None:
        if not (0 <= cid <= 0xFFFF):
            raise ValueError(f"CID out of 16-bit range: {cid}")
        valid = validate_unicode_text(text)
        font_map = self.fonts.setdefault(font, {})
        font_map[cid] = CidEntry(counts={valid: 1}, locked=lock)

    def delete_entry(self, font: str, cid: int) -> bool:
        font_map = self.fonts.get(font)
        if not font_map or cid not in font_map:
            return False
        del font_map[cid]
        if not font_map:
            del self.fonts[font]
        return True

    def is_cid_stable(
        self,
        font: str,
        cid: int,
        min_count: int = 5,
        dominance_ratio: float = 4.0,
    ) -> bool:
        entry = self.get_entry(font, cid)
        if entry is None:
            return False
        return entry.is_stable(min_count=min_count, dominance_ratio=dominance_ratio)

    def resolve_cid(self, font: str, cid: int) -> str | None:
        entry = self.get_entry(font, cid)
        if entry is None:
            return None
        top = entry.top()
        return top[0] if top is not None else None

    def resolve_font_map(self, font: str) -> dict[int, str]:
        font_map = self.fonts.get(font, {})
        resolved: dict[int, str] = {}
        for cid, entry in font_map.items():
            top = entry.top()
            if top is not None:
                resolved[cid] = top[0]
        return resolved

    def font_conflict_rate(self, font: str, dominance_ratio: float = 4.0) -> float:
        font_map = self.fonts.get(font, {})
        if not font_map:
            return 0.0
        conflicted = 0
        for entry in font_map.values():
            top = entry.top()
            if top is None:
                continue
            sec = entry.second_count()
            if sec > 0 and top[1] < dominance_ratio * sec:
                conflicted += 1
        return conflicted / len(font_map)

    def stats(self, min_count: int = 5, dominance_ratio: float = 4.0) -> dict[str, Any]:
        per_font: dict[str, dict[str, Any]] = {}
        total_cids = 0
        total_stable = 0
        total_locked = 0
        total_conflicts = 0
        total_observations = 0

        for font_name in sorted(self.fonts.keys()):
            font_map = self.fonts[font_name]
            n_cids = len(font_map)
            n_stable = sum(
                1 for e in font_map.values() if e.is_stable(min_count=min_count, dominance_ratio=dominance_ratio)
            )
            n_locked = sum(1 for e in font_map.values() if e.locked)
            n_conf = sum(1 for e in font_map.values() if e.second_count() > 0)
            n_obs = sum(sum(e.counts.values()) for e in font_map.values())

            per_font[font_name] = {
                "cids": n_cids,
                "stable": n_stable,
                "locked": n_locked,
                "multi_candidate": n_conf,
                "observations": n_obs,
                "conflict_rate": round(self.font_conflict_rate(font_name, dominance_ratio), 4),
            }
            total_cids += n_cids
            total_stable += n_stable
            total_locked += n_locked
            total_conflicts += n_conf
            total_observations += n_obs

        return {
            "version": self.version,
            "font_count": len(per_font),
            "total_cids": total_cids,
            "total_stable": total_stable,
            "total_locked": total_locked,
            "total_multi_candidate": total_conflicts,
            "total_observations": total_observations,
            "fonts": per_font,
        }

    def conflicts(
        self,
        min_secondary: int = 1,
        max_ratio: float | None = None,
        font: str | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        font_names = [font] if font else sorted(self.fonts.keys())
        for fn in font_names:
            font_map = self.fonts.get(fn, {})
            for cid in sorted(font_map.keys()):
                entry = font_map[cid]
                if len(entry.counts) < 2:
                    continue
                ordered = sorted(entry.counts.items(), key=lambda kv: (-kv[1], kv[0]))
                top_text, top_cnt = ordered[0]
                sec_text, sec_cnt = ordered[1]
                if sec_cnt < min_secondary:
                    continue
                ratio = top_cnt / sec_cnt if sec_cnt > 0 else float("inf")
                if max_ratio is not None and ratio > max_ratio:
                    continue
                results.append(
                    {
                        "font": fn,
                        "cid": cid,
                        "cid_hex": f"0x{cid:04X}",
                        "locked": entry.locked,
                        "top": {"text": top_text, "count": top_cnt},
                        "second": {"text": sec_text, "count": sec_cnt},
                        "ratio": round(ratio, 2),
                        "candidates": [{"text": t, "count": c} for t, c in ordered],
                    }
                )
        results.sort(key=lambda r: (r["ratio"], -r["second"]["count"], r["font"], r["cid"]))
        return results

    def compact(
        self,
        promote_min: int = 7,
        dominance_ratio: float = 4.0,
        cap: int = 100,
        drop_noise_ratio: float | None = None,
    ) -> dict[str, int]:
        """Promote high-confidence entries to `locked=True` and cap oversized counts proportionally.

        Low-frequency candidates are preserved with a minimum count of `1` unless
        `drop_noise_ratio` is explicitly provided.
        """
        promoted = 0
        capped = 0
        dropped_candidates = 0

        for font_map in self.fonts.values():
            for entry in font_map.values():
                top = entry.top()
                if top is None:
                    continue
                top_text, top_cnt = top
                sec_cnt = entry.second_count()

                if not entry.locked and top_cnt >= promote_min and (sec_cnt == 0 or top_cnt >= dominance_ratio * sec_cnt):
                    entry.locked = True
                    promoted += 1

                if drop_noise_ratio is not None and top_cnt > 0:
                    threshold = top_cnt * drop_noise_ratio
                    to_drop = [t for t, c in entry.counts.items() if t != top_text and c < threshold]
                    for t in to_drop:
                        del entry.counts[t]
                        dropped_candidates += 1

                if cap > 0 and top_cnt > cap:
                    scale = cap / top_cnt
                    new_counts: dict[str, int] = {}
                    for t, c in entry.counts.items():
                        new_counts[t] = max(1, round(c * scale))
                    new_counts[top_text] = cap
                    entry.counts = new_counts
                    capped += 1

        return {
            "promoted": promoted,
            "capped": capped,
            "dropped_candidates": dropped_candidates,
        }
