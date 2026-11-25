"""Two-tier span renderer: embedded-font Synthetic Vector Replay + rawdict page-clip fallback."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Callable, Literal, Sequence

import fitz
from PIL import Image, ImageOps

from pdf_cmap_fix.fonts import DescendantFontInfo, MUPDF_XREF_ERRORS, get_descendant_font_info

SYNTHETIC_FONTSIZE = 48.0
SYNTHETIC_DPI = 144
SYNTHETIC_SPACE_KERNING = -500  # Negative TJ adjustment advances right by +0.5em
SYNTHETIC_PAD_PX = 24
MAX_CANVAS_WIDTH_PX = 3600
MAX_CANVAS_PIXELS = 18_000_000
MAX_CHUNK_CIDS = 28
MAX_CHUNK_WIDTH_PT = 1400.0
CHUNK_OVERLAP_CIDS = 2


@dataclass(frozen=True)
class SpanTask:
    page_index: int
    block_index: int
    line_index: int
    span_index: int
    font_xref: int | None
    simplified_font: str
    encoding: str
    tokens: tuple[int | None, ...]  # int = real CID, None = virtual/blank space separator
    bbox: tuple[float, float, float, float]
    char_bboxes: tuple[tuple[float, float, float, float], ...]
    font_size: float
    line_dir: tuple[float, float]
    wmode: int = 0

    @property
    def real_cids(self) -> tuple[int, ...]:
        return tuple(t for t in self.tokens if t is not None)


@dataclass(frozen=True)
class TokenChunk:
    tokens: tuple[int | None, ...]
    start_cid_index: int  # Index into the parent span's real_cids

    @property
    def real_cids(self) -> tuple[int, ...]:
        return tuple(t for t in self.tokens if t is not None)


@dataclass(frozen=True)
class RenderResult:
    image: Image.Image
    method: Literal["synthetic", "clip_fallback"]


def build_tj_content_stream(
    tokens: Sequence[int | None],
    fontsize: float = SYNTHETIC_FONTSIZE,
    margin_x: float = 24.0,
    baseline_y: float = 48.0,
    space_kerning: int = SYNTHETIC_SPACE_KERNING,
) -> bytes:
    """Build a valid PDF `TJ` content stream using 16-bit big-endian hex strings.

    `None` entries become negative kerning numbers (`-500`, advancing right by 0.5em).
    Hex strings use `<XXXX...>` syntax directly inside `[...] TJ` (never parenthesized).
    """
    elements: list[str] = []
    buf: list[str] = []
    pending_space = False

    for tok in tokens:
        if tok is None:
            if buf:
                elements.append("<" + "".join(buf) + ">")
                buf = []
                pending_space = True
            elif elements:
                pending_space = True
        else:
            if pending_space and elements:
                elements.append(str(space_kerning))
                pending_space = False
            buf.append(f"{tok & 0xFFFF:04X}")

    if buf:
        elements.append("<" + "".join(buf) + ">")

    if not elements:
        return b""

    tj_body = " ".join(elements)
    stream = f"q 0 0 0 rg BT /F0 {fontsize:.2f} Tf {margin_x:.2f} {baseline_y:.2f} Td [{tj_body}] TJ ET Q"
    return stream.encode("ascii")


def split_tokens_into_chunks(
    tokens: Sequence[int | None],
    width_fn: Callable[[int], float],
    fontsize: float = SYNTHETIC_FONTSIZE,
    max_cids: int = MAX_CHUNK_CIDS,
    max_width_pt: float = MAX_CHUNK_WIDTH_PT,
    overlap: int = CHUNK_OVERLAP_CIDS,
) -> list[TokenChunk]:
    """Split a long token sequence on CID boundaries with small context overlap."""
    # Map each real CID to its token index & preserve whether a space preceded it
    indexed_cids: list[tuple[int, bool]] = []
    saw_space = False
    for tok in tokens:
        if tok is None:
            if indexed_cids:
                saw_space = True
        else:
            indexed_cids.append((tok, saw_space))
            saw_space = False

    n = len(indexed_cids)
    if n == 0:
        return []

    def estimate_chunk(start: int, end: int) -> tuple[list[int | None], float]:
        out: list[int | None] = []
        width_pt = 0.0
        for i in range(start, end):
            cid, has_space = indexed_cids[i]
            if i > start and has_space:
                out.append(None)
                width_pt += abs(SYNTHETIC_SPACE_KERNING) / 1000.0 * fontsize
            out.append(cid)
            w = width_fn(cid)
            if not (0.0 <= w <= 4000.0):
                w = 1000.0
            width_pt += max(w, 250.0) / 1000.0 * fontsize
        return out, width_pt

    # Fast path: fits in a single chunk
    whole_tokens, whole_w = estimate_chunk(0, n)
    if n <= max_cids and whole_w <= max_width_pt:
        return [TokenChunk(tokens=tuple(whole_tokens), start_cid_index=0)]

    chunks: list[TokenChunk] = []
    start = 0
    safe_overlap = max(0, min(overlap, max_cids // 4))
    while start < n:
        end = start
        cur_w = 0.0
        while end < n and (end - start) < max_cids:
            cid, has_space = indexed_cids[end]
            step_w = max(min(width_fn(cid), 4000.0), 250.0) / 1000.0 * fontsize
            if end > start and has_space:
                step_w += abs(SYNTHETIC_SPACE_KERNING) / 1000.0 * fontsize
            if end > start and (cur_w + step_w) > max_width_pt:
                break
            cur_w += step_w
            end += 1
        if end == start:
            end = start + 1
        ch_tokens, _ = estimate_chunk(start, end)
        chunks.append(TokenChunk(tokens=tuple(ch_tokens), start_cid_index=start))
        if end >= n:
            break
        next_start = end - safe_overlap
        start = next_start if next_start > start else end

    return chunks


def calculate_rotation(line_dir: tuple[float, float]) -> float:
    """Compute clockwise correction angle in degrees from MuPDF line direction vector."""
    dx, dy = line_dir
    angle_deg = math.degrees(math.atan2(dy, dx))
    return angle_deg % 360.0


def _crop_and_pad_white(img: Image.Image, pad: int = SYNTHETIC_PAD_PX, white_threshold: int = 245) -> Image.Image | None:
    """Crop tight non-white ink bounding box and add a uniform white border.

    Returns `None` if the image contains no visible ink pixels.
    """
    gray = img.convert("L")
    # Threshold near-white pixels to pure 255 before inverting so subtle anti-alias noise is preserved
    mask = gray.point(lambda p: 0 if p >= white_threshold else 255)
    bbox = mask.getbbox()
    if bbox is None:
        return None
    cropped = img.crop(bbox)
    return ImageOps.expand(cropped, border=pad, fill=(255, 255, 255))


class SyntheticReplayRenderer:
    """Renders spans using an in-memory disposable PDF handle with O(1) scratch page reuse.

    Never saves or modifies the caller's output document. All MuPDF calls must happen
    on a single thread; parallelize only network OCR calls after rendering PNG bytes.
    """

    def __init__(self, pdf_path: Path | str) -> None:
        self._pdf_path = Path(pdf_path)
        self.doc: fitz.Document = fitz.open(self._pdf_path)
        self._desc_cache: dict[int, DescendantFontInfo] = {}
        self._blank_glyph_cache: dict[tuple[int, int], bool] = {}

        # Allocate ONE scratch page, ONE resource dict xref, and ONE content stream xref (O(1) reuse)
        self._scratch_page: fitz.Page = self.doc.new_page(width=400, height=150)
        self._res_xref: int = self.doc.get_new_xref()
        self._content_xref: int = self.doc.get_new_xref()
        self.doc.update_object(self._res_xref, "<<>>")
        self.doc.update_object(self._content_xref, "<<>>")
        self.doc.update_stream(self._content_xref, b"")
        self.doc.xref_set_key(self._scratch_page.xref, "Resources", f"{self._res_xref} 0 R")
        self.doc.xref_set_key(self._scratch_page.xref, "Contents", f"{self._content_xref} 0 R")

    def __enter__(self) -> SyntheticReplayRenderer:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self.doc is not None and not self.doc.is_closed:
            self.doc.close()

    def desc_info(self, font_xref: int) -> DescendantFontInfo:
        if font_xref not in self._desc_cache:
            self._desc_cache[font_xref] = get_descendant_font_info(self.doc, font_xref)
        return self._desc_cache[font_xref]

    def _render_scratch(
        self,
        font_xref: int,
        stream_bytes: bytes,
        width_pt: float,
        height_pt: float,
        dpi: int = SYNTHETIC_DPI,
    ) -> Image.Image | None:
        est_w_px = int(math.ceil(width_pt * dpi / 72.0))
        est_h_px = int(math.ceil(height_pt * dpi / 72.0))
        if est_w_px <= 0 or est_h_px <= 0:
            return None
        if est_w_px > MAX_CANVAS_WIDTH_PX or (est_w_px * est_h_px) > MAX_CANVAS_PIXELS:
            return None
        try:
            self._scratch_page.set_mediabox(fitz.Rect(0, 0, width_pt, height_pt))
            self.doc.update_object(self._res_xref, f"<< /Font << /F0 {font_xref} 0 R >> >>")
            self.doc.update_object(self._content_xref, "<<>>")
            self.doc.update_stream(self._content_xref, stream_bytes)
            pix = self._scratch_page.get_pixmap(dpi=dpi, alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        except MUPDF_XREF_ERRORS:
            return None

    def is_blank_glyph(self, font_xref: int, cid: int) -> bool:
        """Dynamically detect whether `(font_xref, cid)` renders zero ink (true whitespace glyph)."""
        key = (font_xref, cid)
        if key in self._blank_glyph_cache:
            return self._blank_glyph_cache[key]
        stream = build_tj_content_stream([cid], fontsize=36.0, margin_x=18.0, baseline_y=36.0)
        img = self._render_scratch(font_xref, stream, width_pt=120.0, height_pt=108.0, dpi=72)
        if img is None:
            self._blank_glyph_cache[key] = False
            return False
        blank = _crop_and_pad_white(img, pad=4) is None
        self._blank_glyph_cache[key] = blank
        return blank

    def render_tokens_synthetic(
        self,
        font_xref: int,
        tokens: Sequence[int | None],
        fontsize: float = SYNTHETIC_FONTSIZE,
        dpi: int = SYNTHETIC_DPI,
    ) -> Image.Image | None:
        """Render a single chunk of tokens on the clean white O(1) scratch page."""
        info = self.desc_info(font_xref)
        total_adv = 0.0
        for tok in tokens:
            if tok is None:
                total_adv += abs(SYNTHETIC_SPACE_KERNING) / 1000.0 * fontsize
            else:
                w = info.width_for(tok)
                if w < 0 or w > 4000:
                    return None
                total_adv += max(w, 250.0) / 1000.0 * fontsize

        margin_x = fontsize * 1.25
        baseline_y = fontsize * 1.0
        width_pt = max(200.0, total_adv + margin_x * 2.5)
        height_pt = fontsize * 3.0

        stream = build_tj_content_stream(
            tokens,
            fontsize=fontsize,
            margin_x=margin_x,
            baseline_y=baseline_y,
        )
        if not stream:
            return None

        img = self._render_scratch(font_xref, stream, width_pt=width_pt, height_pt=height_pt, dpi=dpi)
        if img is None:
            return None
        return _crop_and_pad_white(img, pad=SYNTHETIC_PAD_PX)

    def render_span_fallback(self, task: SpanTask) -> RenderResult | None:
        """Improved rawdict char-bbox page-clip fallback with pre-clip padding and wmode-safe rotation."""
        if not (0 <= task.page_index < len(self.doc) - 1):  # exclude trailing scratch page
            return None
        page = self.doc[task.page_index]

        prev_small = fitz.TOOLS.set_small_glyph_heights(True)
        try:
            if task.char_bboxes:
                x0 = min(b[0] for b in task.char_bboxes)
                y0 = min(b[1] for b in task.char_bboxes)
                x1 = max(b[2] for b in task.char_bboxes)
                y1 = max(b[3] for b in task.char_bboxes)
                rect = fitz.Rect(x0, y0, x1, y1)
            else:
                rect = fitz.Rect(task.bbox)

            pad = max(task.font_size * 0.15, 1.5)
            rect = fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)
            rect = rect & page.rect
            if rect.is_empty or rect.width <= 0.5 or rect.height <= 0.5:
                return None

            scale = max(2.0, min(16.0, 56.0 / max(task.font_size, 1.0)))
            pix = page.get_pixmap(clip=rect, matrix=fitz.Matrix(scale, scale), alpha=False)
            if pix.width <= 1 or pix.height <= 1:
                return None
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

            # Only rotate horizontal-mode rotated text; vertical wmode=1 glyphs are already upright!
            if task.wmode == 0:
                rot = calculate_rotation(task.line_dir)
                if abs(rot) > 1e-2 and abs(rot - 360.0) > 1e-2:
                    img = img.rotate(
                        rot,
                        expand=True,
                        resample=Image.Resampling.BICUBIC,
                        fillcolor=(255, 255, 255),
                    )
            img = ImageOps.expand(img, border=16, fill=(255, 255, 255))
            return RenderResult(image=img, method="clip_fallback")
        finally:
            fitz.TOOLS.set_small_glyph_heights(prev_small)

    def render_span(self, task: SpanTask) -> RenderResult | None:
        """Render a span (single chunk), preferring Synthetic Vector Replay for Identity-H."""
        if task.font_xref is not None and task.encoding == "Identity-H":
            img = self.render_tokens_synthetic(task.font_xref, task.tokens)
            if img is not None:
                return RenderResult(image=img, method="synthetic")
        return self.render_span_fallback(task)

    def render_span_chunks(
        self,
        task: SpanTask,
        max_cids: int = MAX_CHUNK_CIDS,
        max_width_pt: float = MAX_CHUNK_WIDTH_PT,
    ) -> list[tuple[TokenChunk, RenderResult]]:
        """Render a potentially long span as one or more clean chunks."""
        if task.font_xref is not None and task.encoding == "Identity-H":
            info = self.desc_info(task.font_xref)
            chunks = split_tokens_into_chunks(
                task.tokens,
                width_fn=info.width_for,
                fontsize=SYNTHETIC_FONTSIZE,
                max_cids=max_cids,
                max_width_pt=max_width_pt,
            )
            results: list[tuple[TokenChunk, RenderResult]] = []
            all_ok = True
            for ch in chunks:
                img = self.render_tokens_synthetic(task.font_xref, ch.tokens)
                if img is None:
                    all_ok = False
                    break
                results.append((ch, RenderResult(image=img, method="synthetic")))
            if all_ok and results:
                return results

        # Fall back to single clip render covering the full span
        fb = self.render_span_fallback(task)
        if fb is None:
            return []
        return [(TokenChunk(tokens=task.tokens, start_cid_index=0), fb)]
