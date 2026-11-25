"""Batch visual verification of CID->Unicode cache entries via numbered glyph contact sheets."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Sequence
import unicodedata

import fitz
from PIL import Image, ImageDraw, ImageFont

from pdf_cmap_fix.cache import MappingCache, validate_unicode_text
from pdf_cmap_fix.fonts import MUPDF_XREF_ERRORS, PageFontIndex, is_standard_cjk_ordering
from pdf_cmap_fix.ocr import (
    OCRAuthError,
    OCRBackendError,
    OCRClient,
    OCRConfig,
    OCRConfigError,
    _split_grapheme_clusters,
    build_ocr_client,
)
from pdf_cmap_fix.renderer import SyntheticReplayRenderer, build_tj_content_stream

_ROW_LINE_RE = re.compile(r"^\s*(\d+)\s*[:：.\)]\s*(.*)$")


@dataclass
class VerifyOptions:
    fonts: tuple[str, ...] = ()  # Empty = all fonts in cache that appear in PDFs
    rows: int = 10
    cols: int = 10
    cell_size: int = 84
    reverify_locked: bool = False
    conflicts_only: bool = False
    auto_fix: bool = False
    concurrency: int = 4
    ocr_config: OCRConfig = field(default_factory=OCRConfig)
    referee_config: OCRConfig | None = None
    report_path: Path | None = None
    debug_dir: Path | None = None
    max_sheets: int = 0  # 0 = unlimited
    verbose: bool = False


@dataclass
class VerifySummary:
    total_candidates: int = 0
    skipped_already_locked: int = 0
    skipped_not_in_pdfs: int = 0
    blank_verified: int = 0
    verified_locked: int = 0
    referee_confirmed_cache: int = 0
    auto_fixed: int = 0
    unresolved_mismatches: int = 0
    sheets_processed: int = 0
    referee_sheets_processed: int = 0
    error: str | None = None
    corrections: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_candidates": self.total_candidates,
            "skipped_already_locked": self.skipped_already_locked,
            "skipped_not_in_pdfs": self.skipped_not_in_pdfs,
            "blank_verified": self.blank_verified,
            "verified_locked": self.verified_locked,
            "referee_confirmed_cache": self.referee_confirmed_cache,
            "auto_fixed": self.auto_fixed,
            "unresolved_mismatches": self.unresolved_mismatches,
            "sheets_processed": self.sheets_processed,
            "referee_sheets_processed": self.referee_sheets_processed,
            "error": self.error,
            "corrections": self.corrections,
            "unresolved": self.unresolved,
        }


@dataclass(frozen=True)
class _GlyphItem:
    font: str
    cid: int
    cached_text: str
    pdf_path: Path
    font_xref: int
    cell_image: Image.Image


@dataclass
class _ContactSheetJob:
    sheet_index: int
    font: str
    items: list[_GlyphItem]
    rows: int
    cols: int
    image: Image.Image
    prompt: str


def _load_label_font(size: int = 20) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def render_single_glyph_baseline(
    renderer: SyntheticReplayRenderer,
    font_xref: int,
    cid: int,
    *,
    fontsize: float = 48.0,
    dpi: int = 108,
) -> Image.Image | None:
    """Render a single CID preserving its true vertical baseline position.

    Unlike tight-ink cropping, preserving the vertical baseline ensures that glyphs
    distinguished by vertical position (`.` vs `·`, `'` vs `,`, `-` vs `_`) stay unambiguous.
    Returns `None` when the glyph draws zero ink (true blank/space glyph).
    """
    info = renderer.desc_info(font_xref)
    w = info.width_for(cid)
    if w <= 0 or w > 4000:
        w = 1000.0
    adv_pt = max(w, 400.0) / 1000.0 * fontsize
    margin_x = fontsize * 0.45
    width_pt = max(fontsize * 1.4, adv_pt + margin_x * 2.0)
    height_pt = fontsize * 1.65
    baseline_y = fontsize * 0.42

    stream = build_tj_content_stream([cid], fontsize=fontsize, margin_x=margin_x, baseline_y=baseline_y)
    if not stream:
        return None
    img = renderer._render_scratch(font_xref, stream, width_pt=width_pt, height_pt=height_pt, dpi=dpi)
    if img is None:
        return None

    # Check if there is visible ink
    gray = img.convert("L")
    mask = gray.point(lambda p: 0 if p >= 245 else 255)
    bbox = mask.getbbox()
    if bbox is None:
        return None

    # Crop horizontally centered around ink with generous side margin, keeping FULL vertical baseline frame!
    ink_x0, ink_y0, ink_x1, ink_y1 = bbox
    ink_w = ink_x1 - ink_x0
    target_w = max(img.height, ink_w + 16)
    cx = (ink_x0 + ink_x1) // 2
    crop_x0 = cx - target_w // 2
    crop_x1 = crop_x0 + target_w

    # Create clean white square/wide canvas preserving exact y coordinates
    canvas = Image.new("RGB", (target_w, img.height), (255, 255, 255))
    paste_x = max(0, -crop_x0)
    src_x0 = max(0, crop_x0)
    src_x1 = min(img.width, crop_x1)
    if src_x1 > src_x0:
        canvas.paste(img.crop((src_x0, 0, src_x1, img.height)), (paste_x, 0))
    return canvas


def render_glyph_contact_sheet(
    cell_images: Sequence[Image.Image],
    *,
    cols: int = 10,
    cell_size: int = 84,
) -> Image.Image:
    """Compose baseline-aligned glyph cell images into a crisp numbered grid contact sheet."""
    n = len(cell_images)
    if n == 0:
        raise ValueError("Cannot render an empty contact sheet")
    rows = (n + cols - 1) // cols
    pad = 16
    label_w = 58
    width = pad * 2 + label_w + cols * cell_size
    height = pad * 2 + rows * cell_size

    sheet = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    lbl_font = _load_label_font(max(18, cell_size // 4))

    inner_max = cell_size - 10
    for idx, gimg in enumerate(cell_images):
        r = idx // cols
        c = idx % cols
        x0 = pad + label_w + c * cell_size
        y0 = pad + r * cell_size
        if c == 0:
            draw.text((pad + 6, y0 + cell_size // 2 - 11), f"{r + 1:02d}", fill=(70, 110, 165), font=lbl_font)
        draw.rectangle([x0, y0, x0 + cell_size - 1, y0 + cell_size - 1], outline=(218, 218, 218), width=1)
        tile = gimg.copy()
        if tile.width > inner_max or tile.height > inner_max:
            tile.thumbnail((inner_max, inner_max), Image.Resampling.LANCZOS)
        px = x0 + (cell_size - tile.width) // 2
        py = y0 + (cell_size - tile.height) // 2
        sheet.paste(tile, (px, py))

    return sheet


def build_contact_sheet_prompt(total_items: int, cols: int) -> str:
    rows = (total_items + cols - 1) // cols
    last_row_len = total_items - (rows - 1) * cols
    if rows == 1:
        row_spec = f"There is 1 row labeled 01 containing exactly {last_row_len} cell(s) from left to right."
    elif last_row_len == cols:
        row_spec = f"There are {rows} rows labeled 01 to {rows:02d}. Each row has exactly {cols} cells from left to right."
    else:
        row_spec = (
            f"There are {rows} rows labeled 01 to {rows:02d}. "
            f"Rows 01 to {rows - 1:02d} each have exactly {cols} cells; "
            f"row {rows:02d} has exactly {last_row_len} cell(s) from left to right."
        )
    return (
        f"This image is a numbered character grid. {row_spec} "
        "Blindly transcribe every single cell verbatim (preserving exact simplified vs traditional Chinese characters, "
        "exact fullwidth vs halfwidth punctuation, symbols, Greek letters, digits, and Latin letters). "
        "Do NOT skip or merge cells. Do NOT add spaces between characters within a row. "
        f"Output ONLY {rows} lines in the exact format:\n"
        "01: <characters>\n"
        + (f"...\n{rows:02d}: <characters>" if rows > 1 else "")
    )


def parse_contact_sheet_response(raw_text: str) -> dict[int, list[str]]:
    """Parse `01: <chars>` lines into `{1-based row_index: list[grapheme_cluster]}`."""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()

    parsed: dict[int, list[str]] = {}
    for line in text.splitlines():
        m = _ROW_LINE_RE.match(line)
        if not m:
            continue
        row_num = int(m.group(1))
        body = re.sub(r"\s+", "", unicodedata.normalize("NFC", m.group(2)))
        parsed[row_num] = _split_grapheme_clusters(body)
    return parsed


def _call_ocr(client: OCRClient, image: Image.Image, prompt: str) -> str:
    try:
        return client.recognize(image, prompt=prompt)  # type: ignore[call-arg]
    except TypeError:
        return client.recognize(image)


def _index_renderable_glyphs(
    pdf_paths: Sequence[Path],
    target_keys: set[tuple[str, int]],
) -> dict[tuple[str, int], tuple[Path, int]]:
    """Scan PDFs to locate a real embedded `font_xref` for each requested `(simplified_font, cid)`."""
    found: dict[tuple[str, int], tuple[Path, int]] = {}
    remaining = set(target_keys)
    if not remaining:
        return found

    prev_subset = fitz.TOOLS.set_subset_fontnames(True)
    try:
        for pdf_path in pdf_paths:
            if not remaining:
                break
            doc = fitz.open(pdf_path)
            try:
                # Strip ToUnicode on disposable doc BEFORE any TextPage is built so we read raw CIDs
                # Skip standard Adobe-GB1/CNS1/Japan1/Korea1 fonts (MuPDF decodes them to Unicode, not raw Identity CIDs)
                std_cjk_xrefs: set[int] = set()
                for page in doc:
                    for fi in page.get_fonts(full=True):
                        fx = int(fi[0])
                        try:
                            if doc.xref_get_key(fx, "Subtype") == ("name", "/Type0"):
                                if is_standard_cjk_ordering(doc, fx):
                                    std_cjk_xrefs.add(fx)
                                    continue
                                enc_k, enc_v = doc.xref_get_key(fx, "Encoding")
                                if enc_k == "name" and enc_v == "/Identity-H":
                                    doc.xref_set_key(fx, "ToUnicode", "null")
                        except MUPDF_XREF_ERRORS:
                            pass

                for page in doc:
                    if not remaining:
                        break
                    font_index = PageFontIndex(doc, page)
                    raw = page.get_text("rawdict")
                    for block in raw.get("blocks", []):
                        if block.get("type") != 0:
                            continue
                        for line in block.get("lines", []):
                            for span in line.get("spans", []):
                                cids = [
                                    ord(ch["c"])
                                    for ch in span.get("chars", [])
                                    if not ch.get("synthetic") and ch.get("c")
                                ]
                                if not cids:
                                    continue
                                fx, _, simp = font_index.resolve(span.get("font", ""), sample_cids=cids)
                                if fx is None or fx in std_cjk_xrefs:
                                    continue
                                for cid in cids:
                                    key = (simp, cid)
                                    if key in remaining:
                                        found[key] = (pdf_path, fx)
                                        remaining.remove(key)
            finally:
                doc.close()
    finally:
        fitz.TOOLS.set_subset_fontnames(prev_subset)

    return found


def verify_cache_with_pdfs(
    cache_path: Path | str,
    pdf_paths: Sequence[Path | str],
    options: VerifyOptions | None = None,
    *,
    ocr_client_factory: Callable[[OCRConfig], OCRClient] = build_ocr_client,
) -> VerifySummary:
    """Visually verify cached CID->Unicode mappings by rendering embedded vector glyphs into contact sheets.

    Only entries that pass blind VLM transcription (or unanimous primary + referee agreement when
    `auto_fix=True`) are promoted to `locked=True`.
    """
    opts = options or VerifyOptions()
    cpath = Path(cache_path)
    cache = MappingCache.load(cpath)
    pdfs = [Path(p) for p in pdf_paths if Path(p).is_file()]
    summary = VerifySummary()

    # Step 1: Select candidate (font, cid) entries from cache
    target_fonts = set(opts.fonts) if opts.fonts else set(cache.fonts.keys())
    candidates: dict[tuple[str, int], str] = {}

    for fn in sorted(target_fonts):
        fmap = cache.fonts.get(fn, {})
        for cid, entry in sorted(fmap.items()):
            top = entry.top()
            if top is None:
                continue
            if entry.locked and not opts.reverify_locked:
                summary.skipped_already_locked += 1
                continue
            if opts.conflicts_only and entry.second_count() == 0:
                continue
            candidates[(fn, cid)] = top[0]

    summary.total_candidates = len(candidates)
    if not candidates:
        return summary

    # Step 2: Locate an embedded Identity-H font_xref for each candidate CID across the PDFs
    glyph_sources = _index_renderable_glyphs(pdfs, set(candidates.keys()))
    summary.skipped_not_in_pdfs = len(candidates) - len(glyph_sources)
    if not glyph_sources:
        return summary

    # Step 3: Render individual baseline-preserving glyph cells (grouped by PDF so each PDF opens once!)
    by_pdf: dict[Path, list[tuple[str, int, int]]] = {}
    for (fn, cid), (pdf_path, fx) in sorted(glyph_sources.items()):
        by_pdf.setdefault(pdf_path, []).append((fn, cid, fx))

    rendered_items_by_font: dict[str, list[_GlyphItem]] = {}
    cache_dirty = False

    for pdf_path, items in sorted(by_pdf.items()):
        with SyntheticReplayRenderer(pdf_path) as renderer:
            for fn, cid, fx in items:
                cached_text = candidates[(fn, cid)]
                cell_img = render_single_glyph_baseline(renderer, fx, cid)
                if cell_img is None:
                    # Zero-ink blank glyph!
                    if cached_text == " ":
                        entry = cache.get_entry(fn, cid)
                        if entry is not None:
                            entry.locked = True
                            cache_dirty = True
                            summary.blank_verified += 1
                    elif opts.auto_fix:
                        old_text = cached_text
                        cache.set_mapping(fn, cid, " ", lock=True)
                        cache_dirty = True
                        summary.auto_fixed += 1
                        summary.corrections.append(
                            {
                                "font": fn,
                                "cid": cid,
                                "cid_hex": f"0x{cid:04X}",
                                "old": old_text,
                                "new": " ",
                                "reason": "zero_ink_blank_glyph",
                            }
                        )
                    else:
                        summary.unresolved_mismatches += 1
                        summary.unresolved.append(
                            {
                                "font": fn,
                                "cid": cid,
                                "cid_hex": f"0x{cid:04X}",
                                "cached": cached_text,
                                "primary": " ",
                                "referee": " ",
                                "reason": "zero_ink_blank_glyph",
                            }
                        )
                    continue
                rendered_items_by_font.setdefault(fn, []).append(
                    _GlyphItem(
                        font=fn,
                        cid=cid,
                        cached_text=cached_text,
                        pdf_path=pdf_path,
                        font_xref=fx,
                        cell_image=cell_img,
                    )
                )

    # Step 4: Build Stage 1 contact sheets (grouped per font, sorted by CID)
    per_sheet = max(1, opts.rows * opts.cols)
    sheet_jobs: list[_ContactSheetJob] = []
    sheet_idx = 0
    for fn in sorted(rendered_items_by_font.keys()):
        font_items = sorted(rendered_items_by_font[fn], key=lambda x: x.cid)
        for i in range(0, len(font_items), per_sheet):
            if opts.max_sheets > 0 and len(sheet_jobs) >= opts.max_sheets:
                break
            batch = font_items[i : i + per_sheet]
            sheet_idx += 1
            img = render_glyph_contact_sheet([it.cell_image for it in batch], cols=opts.cols, cell_size=opts.cell_size)
            prompt = build_contact_sheet_prompt(len(batch), opts.cols)
            if opts.debug_dir is not None:
                opts.debug_dir.mkdir(parents=True, exist_ok=True)
                img.save(opts.debug_dir / f"sheet_{sheet_idx:03d}_{fn}.png")
            sheet_jobs.append(
                _ContactSheetJob(
                    sheet_index=sheet_idx,
                    font=fn,
                    items=batch,
                    rows=(len(batch) + opts.cols - 1) // opts.cols,
                    cols=opts.cols,
                    image=img,
                    prompt=prompt,
                )
            )

    if not sheet_jobs:
        if cache_dirty:
            cache.save(cpath)
        return summary

    # Step 5: Execute Stage 1 blind verification concurrently
    try:
        primary_client = ocr_client_factory(opts.ocr_config)
    except (OCRConfigError, OCRAuthError, OCRBackendError) as exc:
        summary.error = str(exc)
        if cache_dirty:
            cache.save(cpath)
        return summary

    # Track stage 2 items: (GlyphItem, stage1_ocr_or_None, reason)
    stage2_queue: list[tuple[_GlyphItem, str | None, str]] = []
    completed_since_save = 0

    try:
        max_workers = max(1, min(opts.concurrency, len(sheet_jobs)))
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            fut_to_job = {
                executor.submit(_call_ocr, primary_client, job.image, job.prompt): job
                for job in sheet_jobs
            }
            for fut in as_completed(fut_to_job):
                job = fut_to_job[fut]
                summary.sheets_processed += 1
                try:
                    raw_out = fut.result()
                except OCRAuthError as exc:
                    summary.error = str(exc)
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                except OCRBackendError as exc:
                    print(f"  [verify-warn] sheet {job.sheet_index} backend error: {exc}", file=sys.stderr)
                    for it in job.items:
                        stage2_queue.append((it, None, "stage1_backend_error"))
                    continue

                row_map = parse_contact_sheet_response(raw_out)
                matched_this_sheet = 0
                mismatch_this_sheet = 0

                for r in range(job.rows):
                    row_items = job.items[r * job.cols : (r + 1) * job.cols]
                    got_row = row_map.get(r + 1)
                    if got_row is None or len(got_row) != len(row_items):
                        for it in row_items:
                            stage2_queue.append((it, None, "stage1_row_length_mismatch"))
                            mismatch_this_sheet += 1
                        continue
                    for it, got_ch in zip(row_items, got_row):
                        if got_ch == it.cached_text:
                            entry = cache.get_entry(it.font, it.cid)
                            if entry is not None:
                                entry.locked = True
                                cache_dirty = True
                                summary.verified_locked += 1
                                matched_this_sheet += 1
                        else:
                            stage2_queue.append((it, got_ch, "mismatch"))
                            mismatch_this_sheet += 1

                if opts.verbose:
                    print(
                        f"  [sheet {job.sheet_index}/{len(sheet_jobs)} {job.font}] "
                        f"locked={matched_this_sheet}/{len(job.items)} flagged={mismatch_this_sheet}",
                        file=sys.stderr,
                    )

                completed_since_save += 1
                if cache_dirty and completed_since_save >= 5:
                    cache.save(cpath)
                    completed_since_save = 0
                    cache_dirty = False
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    finally:
        if cache_dirty:
            cache.save(cpath)
            cache_dirty = False

    # Step 6: Stage 2 focused referee pass on mismatches & row-length retries (smaller 5-col high-zoom sheets!)
    if stage2_queue and summary.error is None:
        ref_cfg = opts.referee_config or opts.ocr_config
        try:
            referee_client = ocr_client_factory(ref_cfg)
        except (OCRConfigError, OCRAuthError, OCRBackendError) as exc:
            summary.error = f"Referee init failed: {exc}"
            referee_client = None

        if referee_client is not None:
            ref_cols = min(5, opts.cols)
            ref_rows = 5
            ref_per_sheet = ref_cols * ref_rows
            ref_jobs: list[tuple[int, list[tuple[_GlyphItem, str | None, str]], Image.Image, str]] = []

            for i in range(0, len(stage2_queue), ref_per_sheet):
                batch = stage2_queue[i : i + ref_per_sheet]
                r_idx = i // ref_per_sheet + 1
                img = render_glyph_contact_sheet(
                    [it.cell_image for it, _, _ in batch],
                    cols=ref_cols,
                    cell_size=max(108, opts.cell_size + 20),
                )
                prompt = build_contact_sheet_prompt(len(batch), ref_cols)
                if opts.debug_dir is not None:
                    opts.debug_dir.mkdir(parents=True, exist_ok=True)
                    img.save(opts.debug_dir / f"referee_{r_idx:03d}.png")
                ref_jobs.append((r_idx, batch, img, prompt))

            max_workers = max(1, min(opts.concurrency, len(ref_jobs)))
            executor = ThreadPoolExecutor(max_workers=max_workers)
            try:
                fut_to_ref = {
                    executor.submit(_call_ocr, referee_client, img, prompt): (r_idx, batch)
                    for r_idx, batch, img, prompt in ref_jobs
                }
                for fut in as_completed(fut_to_ref):
                    r_idx, batch = fut_to_ref[fut]
                    summary.referee_sheets_processed += 1
                    if opts.verbose:
                        print(f"  [referee {summary.referee_sheets_processed}/{len(ref_jobs)}] processed {len(batch)} glyph(s)", file=sys.stderr)
                    try:
                        raw_out = fut.result()
                    except OCRAuthError as exc:
                        summary.error = f"Referee auth failed: {exc}"
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    except OCRBackendError as exc:
                        for it, s1_text, reason in batch:
                            summary.unresolved_mismatches += 1
                            summary.unresolved.append(
                                {
                                    "font": it.font,
                                    "cid": it.cid,
                                    "cid_hex": f"0x{it.cid:04X}",
                                    "cached": it.cached_text,
                                    "primary": s1_text,
                                    "referee": None,
                                    "reason": f"referee_error:{exc}",
                                }
                            )
                        continue

                    row_map = parse_contact_sheet_response(raw_out)
                    n_rows = (len(batch) + ref_cols - 1) // ref_cols
                    for r in range(n_rows):
                        row_entries = batch[r * ref_cols : (r + 1) * ref_cols]
                        got_row = row_map.get(r + 1)
                        if got_row is None or len(got_row) != len(row_entries):
                            for it, s1_text, reason in row_entries:
                                summary.unresolved_mismatches += 1
                                summary.unresolved.append(
                                    {
                                        "font": it.font,
                                        "cid": it.cid,
                                        "cid_hex": f"0x{it.cid:04X}",
                                        "cached": it.cached_text,
                                        "primary": s1_text,
                                        "referee": None,
                                        "reason": "referee_row_length_mismatch",
                                    }
                                )
                            continue

                        for (it, s1_text, reason), ref_ch in zip(row_entries, got_row):
                            if ref_ch == it.cached_text:
                                # Referee confirmed original cached value!
                                entry = cache.get_entry(it.font, it.cid)
                                if entry is not None:
                                    entry.locked = True
                                    cache_dirty = True
                                    summary.referee_confirmed_cache += 1
                            elif opts.auto_fix and s1_text is not None and ref_ch == s1_text:
                                # Primary + Referee unanimously agree on new value -> safe auto-fix & lock!
                                try:
                                    valid_new = validate_unicode_text(ref_ch)
                                except ValueError:
                                    summary.unresolved_mismatches += 1
                                    continue
                                cache.set_mapping(it.font, it.cid, valid_new, lock=True)
                                cache_dirty = True
                                summary.auto_fixed += 1
                                summary.corrections.append(
                                    {
                                        "font": it.font,
                                        "cid": it.cid,
                                        "cid_hex": f"0x{it.cid:04X}",
                                        "old": it.cached_text,
                                        "new": valid_new,
                                        "reason": "unanimous_primary_and_referee",
                                    }
                                )
                            else:
                                summary.unresolved_mismatches += 1
                                summary.unresolved.append(
                                    {
                                        "font": it.font,
                                        "cid": it.cid,
                                        "cid_hex": f"0x{it.cid:04X}",
                                        "cached": it.cached_text,
                                        "primary": s1_text,
                                        "referee": ref_ch,
                                        "reason": reason if reason != "mismatch" else "disagreement",
                                    }
                                )
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
                if cache_dirty:
                    cache.save(cpath)
                    cache_dirty = False

    if opts.report_path is not None:
        opts.report_path.parent.mkdir(parents=True, exist_ok=True)
        opts.report_path.write_text(
            json.dumps(summary.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return summary
