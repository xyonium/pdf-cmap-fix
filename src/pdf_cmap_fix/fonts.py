"""Font name normalization, CIDFont descendant inspection, and page font_xref resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
import re
from typing import Iterable

import fitz

_BASEFONT_RE = re.compile(r"/BaseFont\s+/([^\s/<>\[\]()]+)")
_XREF_REF_RE = re.compile(r"(\d+)\s+0\s+R")
_W_TOKEN_RE = re.compile(r"\[|\]|-?\d+(?:\.\d+)?")
_SUBSET_PREFIX_RE = re.compile(r"^[A-Z]{6}\+")
_REGISTRY_RE = re.compile(r"/Registry\s*\(([^)]+)\)")
_ORDERING_RE = re.compile(r"/Ordering\s*\(([^)]+)\)")
_STANDARD_ADOBE_ORDERINGS = frozenset({"GB1", "CNS1", "Japan1", "Korea1"})

_FZ_ERROR_BASE = getattr(getattr(fitz, "mupdf", None), "FzErrorBase", RuntimeError)
MUPDF_XREF_ERRORS: tuple[type[BaseException], ...] = (RuntimeError, _FZ_ERROR_BASE)


@lru_cache(maxsize=1)
def load_six_char_fonts() -> frozenset[str]:
    """Load the whitelist of legitimate 6-character TitleCase font family prefixes."""
    data_file = resources.files("pdf_cmap_fix.data").joinpath("six_char_fonts.txt")
    words = [
        line.strip()
        for line in data_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return frozenset(words)


def strip_subset_prefix(basefont: str) -> str:
    """Remove standard 6-uppercase-letter PDF subset prefix like 'JEMPKA+' if present."""
    if "+" in basefont:
        prefix, rest = basefont.split("+", 1)
        if len(prefix) == 6 and prefix.isalpha() and prefix.isupper():
            return rest
    return basefont


def simplify_basefont(basefont: str) -> str:
    """Normalize a PDF BaseFont name to its stable font family prefix.

    1. Strips standard 6-letter uppercase subset tag (`ABCDEF+`).
    2. Strips weight/encoding suffix after the first `-` (e.g. `-Light-Identity-H`).
    3. Iteratively strips one or more 6-letter TitleCase obfuscation prefixes
       added by SVG-to-PDF converters (e.g. `CtsnbwBywnmpSourceHanSansCN`),
       unless the leading 6 characters match a known real font family name.
    """
    name = basefont.lstrip("/")
    name = strip_subset_prefix(name)
    if "-" in name:
        name = name.split("-", 1)[0]

    if len(name) < 7:
        return name

    six_char_fonts = load_six_char_fonts()
    while len(name) >= 7 and name[6].isupper():
        has_inner_upper = any(c.isupper() for c in name[1:6])
        if not has_inner_upper:
            if name[:6] in six_char_fonts:
                break
            name = name[6:]
        else:
            break
    return name


def extract_basefont_from_xref_obj(obj_str: str) -> str | None:
    """Extract the raw `/BaseFont` name (without leading `/`) from a PDF object dict string."""
    match = _BASEFONT_RE.search(obj_str)
    if not match:
        return None
    return match.group(1)


def parse_w_array(w_str: str) -> dict[int, float]:
    """Parse a PDF CIDFont `/W` array into a `{cid: width}` mapping (ISO 32000-1 §9.7.4.3)."""
    tokens = _W_TOKEN_RE.findall(w_str)
    if not tokens:
        return {}
    idx = 0
    if tokens[0] == "[":
        idx = 1
    end = len(tokens)
    if end > idx and tokens[-1] == "]":
        end -= 1

    widths: dict[int, float] = {}
    while idx < end:
        tok = tokens[idx]
        if tok in ("[", "]"):
            idx += 1
            continue
        try:
            cid_start = int(float(tok))
        except ValueError:
            idx += 1
            continue
        idx += 1
        if idx >= end:
            break
        if tokens[idx] == "[":
            idx += 1
            cur = cid_start
            while idx < end and tokens[idx] != "]":
                try:
                    widths[cur] = float(tokens[idx])
                except ValueError:
                    pass
                cur += 1
                idx += 1
            if idx < end and tokens[idx] == "]":
                idx += 1
        else:
            if idx + 1 >= end:
                break
            try:
                cid_end = int(float(tokens[idx]))
                width = float(tokens[idx + 1])
            except ValueError:
                idx += 1
                continue
            if cid_end >= cid_start and (cid_end - cid_start) <= 65536:
                for c in range(cid_start, cid_end + 1):
                    widths[c] = width
            idx += 2
    return widths


@dataclass(frozen=True)
class DescendantFontInfo:
    descendant_xref: int | None = None
    subtype: str = ""
    dw: float = 1000.0
    widths: dict[int, float] = field(default_factory=dict)
    cid_to_gid_map_is_stream: bool = False
    cid_system_info: str = ""

    def width_for(self, cid: int) -> float:
        return self.widths.get(cid, self.dw)


def _safe_xref_get_key(doc: fitz.Document, xref: int, key: str) -> tuple[str, str]:
    try:
        return doc.xref_get_key(xref, key)
    except MUPDF_XREF_ERRORS:
        return ("null", "null")


def _safe_xref_object(doc: fitz.Document, xref: int) -> str:
    try:
        return doc.xref_object(xref)
    except MUPDF_XREF_ERRORS:
        return ""


def get_descendant_font_info(doc: fitz.Document, font_xref: int) -> DescendantFontInfo:
    """Inspect a Type0 font's DescendantFont for subtype, `/DW`, `/W`, and `/CIDToGIDMap`."""
    kind, val = _safe_xref_get_key(doc, font_xref, "DescendantFonts")
    if kind == "xref":
        m = _XREF_REF_RE.search(val)
        if m:
            val = _safe_xref_object(doc, int(m.group(1)))
    elif kind == "null":
        return DescendantFontInfo()

    m = _XREF_REF_RE.search(val)
    if not m:
        return DescendantFontInfo()
    desc_xref = int(m.group(1))

    _, st_val = _safe_xref_get_key(doc, desc_xref, "Subtype")
    subtype = st_val.lstrip("/") if st_val and st_val != "null" else ""

    dw = 1000.0
    dw_kind, dw_val = _safe_xref_get_key(doc, desc_xref, "DW")
    if dw_kind in ("int", "float"):
        try:
            dw = float(dw_val)
        except ValueError:
            pass

    widths: dict[int, float] = {}
    w_kind, w_val = _safe_xref_get_key(doc, desc_xref, "W")
    if w_kind == "xref":
        wm = _XREF_REF_RE.search(w_val)
        if wm:
            w_val = _safe_xref_object(doc, int(wm.group(1)))
            widths = parse_w_array(w_val)
    elif w_kind == "array":
        widths = parse_w_array(w_val)

    c2g_kind, c2g_val = _safe_xref_get_key(doc, desc_xref, "CIDToGIDMap")
    cid_to_gid_map_is_stream = False
    if c2g_kind == "xref":
        cm = _XREF_REF_RE.search(c2g_val)
        if cm:
            try:
                cid_to_gid_map_is_stream = bool(doc.xref_is_stream(int(cm.group(1))))
            except MUPDF_XREF_ERRORS:
                cid_to_gid_map_is_stream = False

    ci_kind, ci_val = _safe_xref_get_key(doc, desc_xref, "CIDSystemInfo")
    if ci_kind == "xref":
        cim = _XREF_REF_RE.search(ci_val)
        if cim:
            ci_val = _safe_xref_object(doc, int(cim.group(1)))
    cid_system_info = ci_val if ci_kind != "null" else ""

    return DescendantFontInfo(
        descendant_xref=desc_xref,
        subtype=subtype,
        dw=dw,
        widths=widths,
        cid_to_gid_map_is_stream=cid_to_gid_map_is_stream,
        cid_system_info=cid_system_info,
    )


def extract_font_w_cids(doc: fitz.Document, font_xref: int) -> set[int]:
    """Return the set of CIDs explicitly listed in the font's `/W` width table."""
    return set(get_descendant_font_info(doc, font_xref).widths.keys())


def is_subset_renumbered_font(doc: fitz.Document, font_xref: int) -> bool:
    """Return True when the font uses a stream `/CIDToGIDMap` (typically subset-renumbered TTF)."""
    info = get_descendant_font_info(doc, font_xref)
    return info.subtype == "CIDFontType2" and info.cid_to_gid_map_is_stream


def is_standard_cjk_ordering(doc: fitz.Document, font_xref: int) -> bool:
    """Return True if the font's DescendantFont uses a standard Adobe CJK character collection
    (Adobe-GB1, Adobe-CNS1, Adobe-Japan1, Adobe-Korea1) that PDF viewers already map natively
    without needing a custom ToUnicode CMap (Adobe TN #5411 §1.2).
    """
    info = get_descendant_font_info(doc, font_xref)
    if not info.cid_system_info:
        return False
    reg_m = _REGISTRY_RE.search(info.cid_system_info)
    ord_m = _ORDERING_RE.search(info.cid_system_info)
    if not reg_m or not ord_m:
        return False
    return reg_m.group(1).strip() == "Adobe" and ord_m.group(1).strip() in _STANDARD_ADOBE_ORDERINGS


@dataclass
class _PageFontEntry:
    xref: int
    font_type: str
    full_name: str
    no_subset_name: str
    simplified_name: str
    aliases: tuple[str, ...] = ()


def _name_matches_span(full_name: str, span_font: str) -> bool:
    if not span_font:
        return False
    if full_name == span_font or full_name.startswith(span_font):
        return True
    # Handle PDF/MuPDF span font truncation (commonly 24 chars, or shorter when full_name < len)
    if len(full_name) < len(span_font) and span_font.startswith(full_name):
        return True
    return False


class PageFontIndex:
    """Per-page font lookup resolving truncated span font names to exact `font_xref`s."""

    def __init__(self, doc: fitz.Document, page: fitz.Page) -> None:
        self._doc = doc
        self._entries: list[_PageFontEntry] = []
        self._desc_cache: dict[int, DescendantFontInfo] = {}
        self._internal_name_cache: dict[int, tuple[str, ...]] = {}
        for fi in page.get_fonts(full=True):
            xref = int(fi[0])
            ftype = str(fi[2] or "")
            raw_name = str(fi[3] or "").lstrip("/")
            no_sub = strip_subset_prefix(raw_name)
            aliases: list[str] = []
            if ftype == "Type0":
                desc = self._desc_info(xref)
                if desc.descendant_xref is not None:
                    dk, dv = _safe_xref_get_key(doc, desc.descendant_xref, "BaseFont")
                    if dk == "name" and dv:
                        dname = dv.lstrip("/")
                        if dname and dname != raw_name:
                            aliases.append(dname)
                            dno = strip_subset_prefix(dname)
                            if dno != dname:
                                aliases.append(dno)
            self._entries.append(
                _PageFontEntry(
                    xref=xref,
                    font_type=ftype,
                    full_name=raw_name,
                    no_subset_name=no_sub,
                    simplified_name=simplify_basefont(raw_name),
                    aliases=tuple(dict.fromkeys(aliases)),
                )
            )

    def _internal_names(self, xref: int) -> tuple[str, ...]:
        if xref not in self._internal_name_cache:
            names: list[str] = []
            try:
                ext_name, _, _, buf = self._doc.extract_font(xref)
                if ext_name:
                    names.append(str(ext_name).lstrip("/"))
                if buf:
                    internal = fitz.Font(fontbuffer=buf).name
                    if internal:
                        names.append(str(internal).lstrip("/"))
            except Exception:
                pass
            self._internal_name_cache[xref] = tuple(dict.fromkeys(names))
        return self._internal_name_cache[xref]

    def _desc_info(self, xref: int) -> DescendantFontInfo:
        if xref not in self._desc_cache:
            self._desc_cache[xref] = get_descendant_font_info(self._doc, xref)
        return self._desc_cache[xref]

    def resolve(
        self,
        span_font: str,
        sample_cids: Iterable[int] | None = None,
    ) -> tuple[int | None, str, str]:
        """Resolve `(font_xref, full_basefont, simplified_font_name)` for a span's font string.

        Returns `font_xref=None` if multiple candidates remain genuinely ambiguous,
        signaling the renderer to fall back to page-clip rendering rather than
        blindly selecting the wrong subset font.
        """
        xref, _, full_name, simplified = self.resolve_candidates(span_font, sample_cids=sample_cids)
        return (xref, full_name, simplified)

    def resolve_candidates(
        self,
        span_font: str,
        sample_cids: Iterable[int] | None = None,
    ) -> tuple[int | None, tuple[int, ...], str, str]:
        """Resolve `(unique_xref_or_None, candidate_xrefs, full_basefont, simplified_font_name)`."""
        raw_span = (span_font or "").lstrip("/")
        if not raw_span:
            return (None, (), "", "")

        has_subset_tag = bool(_SUBSET_PREFIX_RE.match(raw_span))

        # Stage 1: Exact/prefix match preserving subset prefix when present.
        candidates: list[_PageFontEntry] = []
        if has_subset_tag:
            candidates = [e for e in self._entries if _name_matches_span(e.full_name, raw_span)]
        if not candidates:
            span_no_sub = strip_subset_prefix(raw_span)
            candidates = [e for e in self._entries if _name_matches_span(e.no_subset_name, span_no_sub)]
        if not candidates:
            span_no_sub = strip_subset_prefix(raw_span)
            candidates = [
                e
                for e in self._entries
                if any(
                    _name_matches_span(a, raw_span) or _name_matches_span(strip_subset_prefix(a), span_no_sub)
                    for a in e.aliases
                )
            ]
        if not candidates:
            span_no_sub = strip_subset_prefix(raw_span)
            candidates = [
                e
                for e in self._entries
                if e.font_type == "Type0"
                and any(
                    _name_matches_span(a, raw_span) or _name_matches_span(strip_subset_prefix(a), span_no_sub)
                    for a in self._internal_names(e.xref)
                )
            ]

        if not candidates:
            return (None, (), raw_span, simplify_basefont(raw_span))

        # Deduplicate identical xrefs on the same page
        unique: dict[int, _PageFontEntry] = {e.xref: e for e in candidates}
        cand_list = list(unique.values())

        if len(cand_list) == 1:
            e = cand_list[0]
            return (e.xref, (e.xref,), e.full_name, e.simplified_name)

        # Prefer Type0 candidates if mixed with Type1/TrueType
        type0_cands = [e for e in cand_list if e.font_type == "Type0"]
        if len(type0_cands) == 1:
            e = type0_cands[0]
            return (e.xref, (e.xref,), e.full_name, e.simplified_name)
        if type0_cands:
            cand_list = type0_cands

        # Stage 2: Disambiguate using /W coverage ONLY when it strictly separates candidates
        # (noting that default-width CJK glyphs are absent from /W, so 0 hits is inconclusive).
        cids = set(sample_cids) if sample_cids else set()
        if cids:
            scores: list[tuple[int, _PageFontEntry]] = []
            for e in cand_list:
                w_cids = self._desc_info(e.xref).widths
                hit_count = sum(1 for c in cids if c in w_cids)
                scores.append((hit_count, e))
            max_hits = max(s for s, _ in scores)
            if max_hits > 0:
                winners = [e for s, e in scores if s == max_hits]
                if len(winners) == 1:
                    e = winners[0]
                    return (e.xref, (e.xref,), e.full_name, e.simplified_name)

        # Stage 3: Check embedded font glyph count upper bound when available.
        if cids:
            max_cid = max(cids)
            feasible: list[_PageFontEntry] = []
            all_checked = True
            for e in cand_list:
                try:
                    _, _, _, buf = self._doc.extract_font(e.xref)
                    if buf:
                        fobj = fitz.Font(fontbuffer=buf)
                        if max_cid < fobj.glyph_count:
                            feasible.append(e)
                    else:
                        all_checked = False
                except Exception:
                    all_checked = False
            if all_checked and len(feasible) == 1:
                e = feasible[0]
                return (e.xref, (e.xref,), e.full_name, e.simplified_name)

        # Genuinely ambiguous: return None unique xref PLUS all candidate xrefs for coverage checks
        first = cand_list[0]
        return (None, tuple(e.xref for e in cand_list), first.full_name, first.simplified_name)
