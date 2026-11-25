"""PDF missing/invalid/incomplete ToUnicode CMap detector."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

import fitz

from pdf_cmap_fix.cache import MappingCache
from pdf_cmap_fix.cmap import parse_tounicode_cmap, validate_tounicode_cmap
from pdf_cmap_fix.fonts import (
    MUPDF_XREF_ERRORS,
    PageFontIndex,
    extract_basefont_from_xref_obj,
    is_standard_cjk_ordering,
    simplify_basefont,
)

_XREF_REF_RE = re.compile(r"(\d+)\s+0\s+R")
_TARGET_ENCODINGS = ("Identity-H", "Identity-V")
_FATAL_CMAP_MARKERS = (
    "first-byte",
    "high-byte",
    "last-byte",
    "0xFF",
    "out of range 1..100",
    "Invalid bfchar",
    "Invalid bfrange",
    "Overlapping",
    "Duplicate CID",
    "Malformed",
)


@dataclass
class FontIssue:
    xref: int
    basefont: str
    simplified_name: str
    encoding: str
    reason: str  # "missing" | "invalid" | "incomplete" | "invalid_cmap"
    unmapped_cids: list[int] = field(default_factory=list)
    cmap_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "xref": self.xref,
            "basefont": self.basefont,
            "simplified_name": self.simplified_name,
            "encoding": self.encoding,
            "reason": self.reason,
            "unmapped_cid_count": len(self.unmapped_cids),
            "unmapped_cids_sample": [f"0x{c:04X}" for c in self.unmapped_cids[:15]],
            "cmap_errors": self.cmap_errors[:10],
        }


@dataclass
class DetectionReport:
    pdf_path: str
    page_count: int
    total_type0_fonts: int
    identity_type0_fonts: int
    problem_fonts: list[FontIssue] = field(default_factory=list)
    cmap_warnings: list[str] = field(default_factory=list)
    needs_fix: bool = False
    text_anomaly_ratio: float | None = None
    cache_coverage: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdf_path": self.pdf_path,
            "page_count": self.page_count,
            "total_type0_fonts": self.total_type0_fonts,
            "identity_type0_fonts": self.identity_type0_fonts,
            "problem_font_count": len(self.problem_fonts),
            "needs_fix": self.needs_fix,
            "cmap_warnings": self.cmap_warnings[:20],
            "text_anomaly_ratio": self.text_anomaly_ratio,
            "cache_coverage": self.cache_coverage,
            "problem_fonts": [f.to_dict() for f in self.problem_fonts],
        }


def _safe_get_key(doc: fitz.Document, xref: int, key: str) -> tuple[str, str]:
    try:
        return doc.xref_get_key(xref, key)
    except MUPDF_XREF_ERRORS:
        return ("null", "null")


def _safe_get_obj(doc: fitz.Document, xref: int) -> str:
    try:
        return doc.xref_object(xref)
    except MUPDF_XREF_ERRORS:
        return ""


def _read_tounicode_stream(doc: fitz.Document, font_xref: int) -> tuple[str, str | None]:
    """Return `(status, cmap_text)` where status is 'missing', 'invalid', or 'present'."""
    kind, val = _safe_get_key(doc, font_xref, "ToUnicode")
    if kind in ("none", "null") or val == "null":
        return ("missing", None)
    if kind != "xref":
        return ("invalid", None)
    m = _XREF_REF_RE.search(val)
    if not m:
        return ("invalid", None)
    tu_xref = int(m.group(1))
    try:
        raw = doc.xref_stream(tu_xref)
    except MUPDF_XREF_ERRORS:
        return ("invalid", None)
    if not raw:
        return ("invalid", None)
    text = raw.decode("latin-1", errors="replace")
    return ("present", text)


def _compute_text_anomaly_ratio(doc: fitz.Document, max_pages: int = 20) -> float:
    total = 0
    anomalous = 0
    step = max(1, len(doc) // max_pages) if len(doc) > max_pages else 1
    for i in range(0, len(doc), step):
        txt = doc[i].get_text("text")
        for ch in txt:
            if ch.isspace():
                continue
            total += 1
            cp = ord(ch)
            if cp == 0xFFFD or (0xE000 <= cp <= 0xF8FF) or (cp < 0x20):
                anomalous += 1
    return round(anomalous / total, 4) if total > 0 else 0.0


def _collect_live_type0_fonts(doc: fitz.Document) -> dict[int, tuple[str, str]]:
    """Return `{font_xref: (basefont, encoding)}` for all Type0 fonts referenced on any page."""
    found: dict[int, tuple[str, str]] = {}
    for page in doc:
        for fi in page.get_fonts(full=True):
            xref = int(fi[0])
            if xref in found:
                continue
            st_kind, st_val = _safe_get_key(doc, xref, "Subtype")
            if st_kind != "name" or st_val != "/Type0":
                continue
            enc_kind, enc_val = _safe_get_key(doc, xref, "Encoding")
            enc = enc_val.lstrip("/") if enc_kind == "name" else ""
            raw_obj = _safe_get_obj(doc, xref)
            basefont = extract_basefont_from_xref_obj(raw_obj) or str(fi[3] or "").lstrip("/")
            found[xref] = (basefont, enc)
    return found


def _extract_raw_cids_by_font_xref(
    doc: fitz.Document,
    target_xrefs: set[int],
) -> tuple[dict[int, set[int]], dict[str, set[int]]]:
    """Extract raw non-synthetic CIDs per font_xref (and per simplified font family) from a read-only doc.

    Caller must have already stripped `/ToUnicode` on `target_xrefs` in this disposable doc.
    """
    cids_by_xref: dict[int, set[int]] = {x: set() for x in target_xrefs}
    cids_by_family: dict[str, set[int]] = {}
    prev_subset = fitz.TOOLS.set_subset_fontnames(True)
    try:
        for page in doc:
            page_font_xrefs = {int(fi[0]) for fi in page.get_fonts(full=True)}
            if not (page_font_xrefs & target_xrefs):
                continue
            font_index = PageFontIndex(doc, page)
            raw = page.get_text("rawdict")
            for block in raw.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        chars = span.get("chars", [])
                        real_cids = [
                            ord(ch["c"])
                            for ch in chars
                            if not ch.get("synthetic") and ch.get("c")
                        ]
                        if not real_cids:
                            continue
                        fx, cand_xrefs, _, simp = font_index.resolve_candidates(
                            span.get("font", ""), sample_cids=real_cids
                        )
                        if fx is not None and fx in target_xrefs:
                            cids_by_xref[fx].update(real_cids)
                            cids_by_family.setdefault(simp, set()).update(real_cids)
                        elif fx is None and cand_xrefs:
                            # Conservative coverage attribution: attribute to all matching candidate xrefs
                            # so ambiguous same-name fonts never produce false-negative "healthy" results.
                            matched_targets = [cx for cx in cand_xrefs if cx in target_xrefs]
                            for cx in matched_targets:
                                cids_by_xref[cx].update(real_cids)
                            if matched_targets:
                                cids_by_family.setdefault(simp, set()).update(real_cids)
    finally:
        fitz.TOOLS.set_subset_fontnames(prev_subset)
    return cids_by_xref, cids_by_family


def inspect_pdf(
    pdf_path: Path | str,
    cache: MappingCache | None = None,
    strict_text: bool = False,
    strict_cmap: bool = False,
    verify_coverage: bool = True,
) -> DetectionReport:
    """Inspect a PDF for missing, invalid, or incomplete ToUnicode CMaps on Identity-H/V Type0 fonts."""
    path = Path(pdf_path)
    doc = fitz.open(path)
    try:
        page_count = len(doc)
        text_anomaly = _compute_text_anomaly_ratio(doc) if strict_text else None

        live_type0 = _collect_live_type0_fonts(doc)
        identity_fonts = {
            xref: (bf, enc)
            for xref, (bf, enc) in live_type0.items()
            if enc in _TARGET_ENCODINGS
        }

        problem_fonts: list[FontIssue] = []
        cmap_warnings: list[str] = []
        parsed_cmaps: dict[int, dict[int, str]] = {}
        present_xrefs: set[int] = set()
        missing_or_invalid_xrefs: set[int] = set()
        seen_stream_hashes: set[int] = set()

        for xref, (bf, enc) in sorted(identity_fonts.items()):
            simp = simplify_basefont(bf)
            std_cjk = is_standard_cjk_ordering(doc, xref)
            status, cmap_text = _read_tounicode_stream(doc, xref)
            if status == "missing":
                if std_cjk:
                    # Standard Adobe-GB1/CNS1/Japan1/Korea1 maps natively without ToUnicode
                    continue
                missing_or_invalid_xrefs.add(xref)
                problem_fonts.append(
                    FontIssue(xref=xref, basefont=bf, simplified_name=simp, encoding=enc, reason="missing")
                )
                continue
            if status == "invalid" or cmap_text is None:
                missing_or_invalid_xrefs.add(xref)
                problem_fonts.append(
                    FontIssue(xref=xref, basefont=bf, simplified_name=simp, encoding=enc, reason="invalid")
                )
                continue

            parsed = parse_tounicode_cmap(cmap_text)
            if not parsed:
                missing_or_invalid_xrefs.add(xref)
                problem_fonts.append(
                    FontIssue(xref=xref, basefont=bf, simplified_name=simp, encoding=enc, reason="invalid")
                )
                continue

            # Check for fatal Adobe Reader CMap violations (deduplicated per stream content)
            h = hash(cmap_text)
            all_errs = validate_tounicode_cmap(cmap_text)
            fatal_errs = [e for e in all_errs if any(m in e for m in _FATAL_CMAP_MARKERS)]
            if fatal_errs and h not in seen_stream_hashes:
                seen_stream_hashes.add(h)
                for e in fatal_errs[:5]:
                    cmap_warnings.append(f"{simp} (xref={xref}): {e}")

            if strict_cmap and fatal_errs:
                missing_or_invalid_xrefs.add(xref)
                problem_fonts.append(
                    FontIssue(
                        xref=xref,
                        basefont=bf,
                        simplified_name=simp,
                        encoding=enc,
                        reason="invalid_cmap",
                        cmap_errors=fatal_errs,
                    )
                )
                continue

            if std_cjk:
                # Standard Adobe CJK ordering with valid ToUnicode does not need raw-CID coverage scan
                continue

            parsed_cmaps[xref] = parsed
            present_xrefs.add(xref)

        # Strip ToUnicode on a disposable read-only handle when we need raw CIDs for coverage or cache stats
        need_raw_scan_xrefs: set[int] = set()
        if verify_coverage and present_xrefs:
            need_raw_scan_xrefs.update(present_xrefs)
        if cache is not None and missing_or_invalid_xrefs:
            need_raw_scan_xrefs.update(missing_or_invalid_xrefs)

        cids_by_xref: dict[int, set[int]] = {}
        cids_by_family: dict[str, set[int]] = {}
        if need_raw_scan_xrefs:
            # Open fresh disposable handle before any TextPage exists, strip ToUnicode on ALL
            # scanned xrefs (including invalid_cmap), then extract raw CIDs.
            scan_doc = fitz.open(path)
            try:
                for x in need_raw_scan_xrefs:
                    try:
                        scan_doc.xref_set_key(x, "ToUnicode", "null")
                    except MUPDF_XREF_ERRORS:
                        pass
                cids_by_xref, cids_by_family = _extract_raw_cids_by_font_xref(scan_doc, need_raw_scan_xrefs)
            finally:
                scan_doc.close()

        if verify_coverage and present_xrefs:
            for xref in sorted(present_xrefs):
                used = {c for c in cids_by_xref.get(xref, set()) if c != 0}
                if not used:
                    continue
                cmap_map = parsed_cmaps[xref]
                unmapped = sorted(
                    c for c in used if c not in cmap_map or cmap_map[c] == "\ufffd"
                )
                if unmapped:
                    bf, enc = identity_fonts[xref]
                    problem_fonts.append(
                        FontIssue(
                            xref=xref,
                            basefont=bf,
                            simplified_name=simplify_basefont(bf),
                            encoding=enc,
                            reason="incomplete",
                            unmapped_cids=unmapped,
                        )
                    )

        cache_coverage: dict[str, Any] | None = None
        if cache is not None and problem_fonts:
            problem_xrefs = {f.xref for f in problem_fonts}
            total_unique = 0
            hits = 0
            misses = 0
            per_fam: dict[str, dict[str, int]] = {}
            fam_cids: dict[str, set[int]] = {}
            for xref in problem_xrefs:
                bf, _ = identity_fonts[xref]
                simp = simplify_basefont(bf)
                fam_cids.setdefault(simp, set()).update(c for c in cids_by_xref.get(xref, set()) if c != 0)
            for fam, cset in sorted(fam_cids.items()):
                known = set(cache.resolve_font_map(fam).keys())
                h_cnt = len(cset & known)
                m_cnt = len(cset - known)
                total_unique += len(cset)
                hits += h_cnt
                misses += m_cnt
                per_fam[fam] = {"total": len(cset), "cached": h_cnt, "missing": m_cnt}
            cache_coverage = {
                "total_unique_cids": total_unique,
                "cached": hits,
                "missing": misses,
                "hit_rate": round(hits / total_unique, 4) if total_unique > 0 else 1.0,
                "by_font": per_fam,
            }

        return DetectionReport(
            pdf_path=str(path),
            page_count=page_count,
            total_type0_fonts=len(live_type0),
            identity_type0_fonts=len(identity_fonts),
            problem_fonts=problem_fonts,
            cmap_warnings=cmap_warnings,
            needs_fix=bool(problem_fonts),
            text_anomaly_ratio=text_anomaly,
            cache_coverage=cache_coverage,
        )
    finally:
        doc.close()
