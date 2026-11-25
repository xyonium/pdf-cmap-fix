"""End-to-end PDF ToUnicode CMap repair orchestration, OCR execution, and atomic compressed patching."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import sys
import tempfile
from typing import Callable, Literal
import unicodedata

import fitz
from PIL import Image

from pdf_cmap_fix.cache import MappingCache
from pdf_cmap_fix.cmap import (
    generate_tounicode_cmap,
    parse_tounicode_cmap,
    sanitize_cmap_name,
    validate_tounicode_cmap,
)
from pdf_cmap_fix.detector import _collect_live_type0_fonts, _read_tounicode_stream, inspect_pdf
from pdf_cmap_fix.fonts import (
    MUPDF_XREF_ERRORS,
    PageFontIndex,
    is_subset_renumbered_font,
    simplify_basefont,
)
from pdf_cmap_fix.ocr import (
    OCRAuthError,
    OCRBackendError,
    OCRClient,
    OCRConfig,
    OCRConfigError,
    align_cids_with_ocr,
    build_ocr_client,
)
from pdf_cmap_fix.renderer import (
    SpanTask,
    SyntheticReplayRenderer,
)

_TARGET_ENCODINGS = ("Identity-H", "Identity-V")
_SUBSET_CONFLICT_RATE_THRESHOLD = 0.15


@dataclass
class FixOptions:
    cache_path: Path | None = None
    cmap_dir: Path | None = None
    if_needed: bool = False
    force: bool = False
    no_ocr: bool = False
    strict: bool = False
    strict_cmap: bool = False
    use_bfrange: bool = False
    min_count: int = 5
    dominance_ratio: float = 4.0
    max_jobs_per_cid: int = 3
    concurrency: int = 4
    ocr_config: OCRConfig = field(default_factory=OCRConfig)
    debug_dir: Path | None = None
    space_cids: frozenset[int] = field(default_factory=frozenset)
    checkpoint_every: int = 20
    verbose: bool = False


@dataclass
class FixSummary:
    status: Literal["fixed", "partial", "skipped", "failed"]
    pdf_path: str
    output_path: str | None = None
    patched_fonts: int = 0
    ocr_calls: int = 0
    resolved_cids: int = 0
    unmapped_cids: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class _RenderOCRJob:
    font_xref: int
    simplified_font: str
    isolated: bool
    real_cids: tuple[int, ...]
    image: Image.Image
    render_method: str
    debug_label: str


def _load_cmap_dir(cmap_dir: Path) -> dict[str, dict[int, str]]:
    """Load existing `.cmap` files from a directory, keyed by simplified font name."""
    loaded: dict[str, dict[int, str]] = {}
    for p in sorted(cmap_dir.glob("*.cmap")):
        stem = p.stem
        if stem.startswith("debug_to_unicode_"):
            font_name = stem[len("debug_to_unicode_") :]
        elif stem.endswith("-ToUnicode"):
            font_name = stem[: -len("-ToUnicode")]
        else:
            font_name = stem
        parsed = parse_tounicode_cmap(p.read_text(encoding="utf-8", errors="replace"))
        if parsed:
            loaded[simplify_basefont(font_name)] = parsed
    return loaded


def _save_pdf_cleanly(doc: fitz.Document, output_pdf: Path) -> None:
    """Atomically save `doc` with FlateDecode compression and xref cleanup.

    Falls back to rebuilding into a clean `fitz.open()` via `insert_pdf()` when the source
    PDF contains sparse/dangling xref slots (common in pdfcpu-merged PDFs) that cause
    PyMuPDF `doc.save()` to raise `FzErrorFormat`.
    """
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{output_pdf.name}.", suffix=".tmp.pdf", dir=output_pdf.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)

    try:
        try:
            doc.save(tmp_path, deflate=True, garbage=3)
        except MUPDF_XREF_ERRORS:
            clean_doc = fitz.open()
            try:
                clean_doc.insert_pdf(doc, links=True, annots=True)
                if doc.metadata:
                    clean_doc.set_metadata(doc.metadata)
                try:
                    toc = doc.get_toc(simple=False)
                    if toc:
                        clean_doc.set_toc(toc)
                except Exception:
                    pass
                try:
                    labels = doc.get_page_labels()
                    if labels:
                        clean_doc.set_page_labels(labels)
                except Exception:
                    pass
                clean_doc.save(tmp_path, deflate=True, garbage=3)
            finally:
                clean_doc.close()
        # Caller must close source `doc` BEFORE atomic replace if saving in-place on Windows;
        # close here before os.replace so even Windows in-place replace succeeds cleanly.
        if not doc.is_closed:
            doc.close()
        os.replace(tmp_path, output_pdf)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def fix_pdf(
    input_pdf: Path | str,
    output_pdf: Path | str,
    options: FixOptions | None = None,
    *,
    ocr_client_factory: Callable[[OCRConfig], OCRClient] = build_ocr_client,
) -> FixSummary:
    """Detect, optionally OCR missing glyphs, build Adobe-compliant CMaps, and patch `input_pdf`."""
    opts = options or FixOptions()
    src = Path(input_pdf)
    dst = Path(output_pdf)

    if not src.exists() or not src.is_file():
        return FixSummary(status="failed", pdf_path=str(src), error=f"Input PDF not found: {src}")

    cache = MappingCache.load(opts.cache_path) if opts.cache_path else MappingCache()
    warnings: list[str] = []

    # Step 1: Decide which live Identity-H/V Type0 fonts need patching
    if opts.if_needed and not opts.force:
        report = inspect_pdf(src, strict_cmap=opts.strict_cmap)
        if not report.needs_fix:
            return FixSummary(status="skipped", pdf_path=str(src), output_path=str(dst))
        target_xrefs = {f.xref for f in report.problem_fonts}
        font_meta: dict[int, tuple[str, str, str]] = {
            f.xref: (f.basefont, f.simplified_name, f.encoding) for f in report.problem_fonts
        }
    else:
        probe_doc = fitz.open(src)
        try:
            live_type0 = _collect_live_type0_fonts(probe_doc)
        finally:
            probe_doc.close()
        identity_live = {
            x: (bf, enc) for x, (bf, enc) in live_type0.items() if enc in _TARGET_ENCODINGS
        }
        if opts.force:
            target_xrefs = set(identity_live.keys())
            font_meta = {
                x: (bf, simplify_basefont(bf), enc) for x, (bf, enc) in identity_live.items()
            }
        else:
            report = inspect_pdf(src, strict_cmap=opts.strict_cmap)
            target_xrefs = {f.xref for f in report.problem_fonts}
            font_meta = {
                f.xref: (f.basefont, f.simplified_name, f.encoding) for f in report.problem_fonts
            }

    if not target_xrefs:
        return FixSummary(status="skipped", pdf_path=str(src), output_path=str(dst))

    # Optional pre-built CMap directory mode (offline, no OCR)
    prebuilt_cmaps: dict[str, dict[int, str]] | None = None
    if opts.cmap_dir is not None:
        if not opts.cmap_dir.exists() or not opts.cmap_dir.is_dir():
            return FixSummary(status="failed", pdf_path=str(src), error=f"CMap directory not found: {opts.cmap_dir}")
        prebuilt_cmaps = _load_cmap_dir(opts.cmap_dir)

    # Step 2: Scan raw spans & dynamically detect blank glyphs on a fresh disposable handle
    used_cids_by_xref: dict[int, set[int]] = {x: set() for x in target_xrefs}
    blank_cids_by_xref: dict[int, set[int]] = {x: set() for x in target_xrefs}
    existing_tounicode_by_xref: dict[int, dict[int, str]] = {}
    isolated_xrefs: set[int] = set()
    probe_per_xref_families: set[str] = set()
    render_jobs: list[_RenderOCRJob] = []
    seen_job_keys: set[tuple[str | int, tuple[int, ...]]] = set()
    cid_job_counts: Counter[tuple[str | int, int]] = Counter()

    # Count how many target xrefs share each simplified family (to probe uncached multi-xref families per xref)
    family_target_counts: Counter[str] = Counter(font_meta[fx][1] for fx in target_xrefs)

    with SyntheticReplayRenderer(src) as renderer:
        # Preserve existing valid ToUnicode mappings when NOT --force so partial repairs never lose valid entries
        if not opts.force:
            for fx in target_xrefs:
                status_tu, cmap_txt = _read_tounicode_stream(renderer.doc, fx)
                if status_tu == "present" and cmap_txt:
                    parsed_existing = parse_tounicode_cmap(cmap_txt)
                    if parsed_existing:
                        existing_tounicode_by_xref[fx] = parsed_existing

        # Determine which target fonts are subset-renumbered and should stay isolated per font_xref
        for fx in target_xrefs:
            _, simp, _ = font_meta[fx]
            if is_subset_renumbered_font(renderer.doc, fx) or cache.font_conflict_rate(simp) >= _SUBSET_CONFLICT_RATE_THRESHOLD:
                isolated_xrefs.add(fx)
                warnings.append(f"Isolated subset-renumbered/high-conflict font xref={fx} ({simp})")
            elif family_target_counts[simp] > 1 and not cache.fonts.get(simp):
                # Multiple xrefs of an uncached family: probe each xref separately so cross-xref dedup
                # does not mask subset-renumbered TTF/CFF fonts (e.g. /CIDToGIDMap /Identity ArialMT).
                probe_per_xref_families.add(simp)

        # Strip existing /ToUnicode on all target xrefs BEFORE any TextPage is created (--force safe!)
        for fx in target_xrefs:
            try:
                renderer.doc.xref_set_key(fx, "ToUnicode", "null")
            except MUPDF_XREF_ERRORS:
                pass

        prev_subset = fitz.TOOLS.set_subset_fontnames(True)
        try:
            real_page_count = len(renderer.doc) - 1  # exclude trailing scratch page
            for page_idx in range(real_page_count):
                page = renderer.doc[page_idx]
                page_xrefs = {int(fi[0]) for fi in page.get_fonts(full=True)}
                if not (page_xrefs & target_xrefs):
                    continue
                font_index = PageFontIndex(renderer.doc, page)
                raw = page.get_text("rawdict")
                for b_idx, block in enumerate(raw.get("blocks", [])):
                    if block.get("type") != 0:
                        continue
                    for l_idx, line in enumerate(block.get("lines", [])):
                        line_dir_raw = line.get("dir", (1.0, 0.0))
                        line_dir = (float(line_dir_raw[0]), float(line_dir_raw[1]))
                        wmode = int(line.get("wmode", 0))
                        for s_idx, span in enumerate(line.get("spans", [])):
                            chars = span.get("chars", [])
                            if not chars:
                                continue
                            raw_real_cids = [
                                ord(ch["c"])
                                for ch in chars
                                if not ch.get("synthetic") and ch.get("c")
                            ]
                            if not raw_real_cids:
                                continue
                            fx, _, resolved_simp = font_index.resolve(
                                span.get("font", ""), sample_cids=raw_real_cids
                            )
                            if fx is None or fx not in target_xrefs:
                                continue

                            _, simp, enc = font_meta[fx]
                            is_isolated = fx in isolated_xrefs
                            existing_map = existing_tounicode_by_xref.get(fx, {})
                            tokens: list[int | None] = []
                            char_bboxes: list[tuple[float, float, float, float]] = []

                            for ch in chars:
                                c_str = ch.get("c", "")
                                if not c_str:
                                    continue
                                if ch.get("synthetic"):
                                    tokens.append(None)
                                    continue
                                cid = ord(c_str)
                                if cid == 0:
                                    continue
                                used_cids_by_xref[fx].add(cid)

                                # Explicit user space CID override or dynamic zero-ink detection
                                is_blank = False
                                if cid in opts.space_cids:
                                    is_blank = True
                                elif enc == "Identity-H":
                                    if is_isolated or not cache.is_cid_stable(
                                        simp, cid, min_count=opts.min_count, dominance_ratio=opts.dominance_ratio
                                    ) or cache.resolve_cid(simp, cid) == " ":
                                        is_blank = renderer.is_blank_glyph(fx, cid)

                                if is_blank:
                                    blank_cids_by_xref[fx].add(cid)
                                    tokens.append(None)
                                else:
                                    tokens.append(cid)
                                    cb = ch.get("bbox")
                                    if cb and len(cb) == 4:
                                        char_bboxes.append((float(cb[0]), float(cb[1]), float(cb[2]), float(cb[3])))

                            visible_cids = [t for t in tokens if t is not None]
                            if not visible_cids:
                                continue

                            if prebuilt_cmaps is not None or opts.no_ocr:
                                continue

                            # Check whether this span needs OCR (respecting per-CID job budget)
                            dedup_bucket: str | int = fx if (is_isolated or simp in probe_per_xref_families) else simp
                            if is_isolated:
                                needed_cids = [
                                    cid
                                    for cid in visible_cids
                                    if cid not in existing_map
                                    and (
                                        opts.max_jobs_per_cid <= 0
                                        or cid_job_counts[(dedup_bucket, cid)] < opts.max_jobs_per_cid
                                    )
                                ]
                            else:
                                needed_cids = [
                                    cid
                                    for cid in visible_cids
                                    if cid not in existing_map
                                    and not cache.is_cid_stable(
                                        simp, cid, min_count=opts.min_count, dominance_ratio=opts.dominance_ratio
                                    )
                                    and (
                                        opts.max_jobs_per_cid <= 0
                                        or cid_job_counts[(dedup_bucket, cid)] < opts.max_jobs_per_cid
                                    )
                                ]
                            if not needed_cids:
                                continue

                            task = SpanTask(
                                page_index=page_idx,
                                block_index=b_idx,
                                line_index=l_idx,
                                span_index=s_idx,
                                font_xref=fx,
                                simplified_font=simp,
                                encoding=enc,
                                tokens=tuple(tokens),
                                bbox=tuple(float(v) for v in span["bbox"]),
                                char_bboxes=tuple(char_bboxes),
                                font_size=float(span.get("size", 10.0)),
                                line_dir=line_dir,
                                wmode=wmode,
                            )

                            # Render all chunks on the main thread (MuPDF thread safety!)
                            rendered_chunks = renderer.render_span_chunks(task)
                            for ch_idx, (chunk, render_res) in enumerate(rendered_chunks):
                                ch_cids = chunk.real_cids
                                if not ch_cids:
                                    continue
                                ch_needed = [
                                    cid
                                    for cid in set(ch_cids)
                                    if cid not in existing_map
                                    and (
                                        is_isolated
                                        or not cache.is_cid_stable(
                                            simp, cid, min_count=opts.min_count, dominance_ratio=opts.dominance_ratio
                                        )
                                    )
                                    and (
                                        opts.max_jobs_per_cid <= 0
                                        or cid_job_counts[(dedup_bucket, cid)] < opts.max_jobs_per_cid
                                    )
                                ]
                                if not ch_needed:
                                    continue
                                dedup_key = (dedup_bucket, ch_cids)
                                if dedup_key in seen_job_keys:
                                    continue
                                seen_job_keys.add(dedup_key)
                                for cid in ch_needed:
                                    cid_job_counts[(dedup_bucket, cid)] += 1
                                label = f"p{page_idx + 1}_b{b_idx + 1}_l{l_idx + 1}_s{s_idx + 1}_c{ch_idx + 1}_{render_res.method}"
                                if opts.debug_dir is not None:
                                    opts.debug_dir.mkdir(parents=True, exist_ok=True)
                                    render_res.image.save(opts.debug_dir / f"{label}.png")
                                render_jobs.append(
                                    _RenderOCRJob(
                                        font_xref=fx,
                                        simplified_font=simp,
                                        isolated=is_isolated,
                                        real_cids=ch_cids,
                                        image=render_res.image,
                                        render_method=render_res.method,
                                        debug_label=label,
                                    )
                                )
        finally:
            fitz.TOOLS.set_subset_fontnames(prev_subset)

    # Step 3: Lazy OCR client init + concurrent network OCR (buffered in per_xref_obs BEFORE cache commit!)
    per_xref_obs: dict[int, dict[int, Counter[str]]] = {}
    ocr_calls = 0
    fatal_ocr_error: str | None = None

    if render_jobs and prebuilt_cmaps is None and not opts.no_ocr:
        try:
            ocr_client = ocr_client_factory(opts.ocr_config)
        except (OCRConfigError, OCRAuthError, OCRBackendError) as exc:
            fatal_ocr_error = str(exc)
            ocr_client = None

        if ocr_client is not None:
            completed_since_save = 0
            try:
                max_workers = max(1, min(opts.concurrency, len(render_jobs)))
                executor = ThreadPoolExecutor(max_workers=max_workers)
                try:
                    future_to_job = {
                        executor.submit(ocr_client.recognize, job.image): job
                        for job in render_jobs
                    }
                    for fut in as_completed(future_to_job):
                        job = future_to_job[fut]
                        ocr_calls += 1
                        try:
                            ocr_text = fut.result()
                        except OCRAuthError as exc:
                            fatal_ocr_error = str(exc)
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                        except OCRBackendError as exc:
                            fatal_ocr_error = str(exc)
                            warnings.append(f"OCR backend error on {job.debug_label}: {exc}")
                            continue

                        if not ocr_text:
                            continue

                        # Build known anchors for anchor-segmented alignment
                        anchors: dict[int, str] = {}
                        existing_fx_map = existing_tounicode_by_xref.get(job.font_xref, {})
                        for cid in job.real_cids:
                            if cid in existing_fx_map:
                                anchors[cid] = existing_fx_map[cid]
                            elif not job.isolated and cache.is_cid_stable(
                                job.simplified_font,
                                cid,
                                min_count=opts.min_count,
                                dominance_ratio=opts.dominance_ratio,
                            ):
                                resolved = cache.resolve_cid(job.simplified_font, cid)
                                if resolved:
                                    anchors[cid] = resolved

                        pairs = align_cids_with_ocr(job.real_cids, ocr_text, known_anchors=anchors or None)
                        if not pairs:
                            if opts.verbose:
                                print(
                                    f"  [align-skip] {job.debug_label}: len(cids)={len(job.real_cids)} ocr={ocr_text!r}",
                                    file=sys.stderr,
                                )
                            continue

                        xref_bucket = per_xref_obs.setdefault(job.font_xref, {})
                        for cid, text in pairs:
                            xref_bucket.setdefault(cid, Counter())[text] += 1

                        # Only checkpoint directly to shared cache for families NOT being conflict-probed
                        if (
                            not job.isolated
                            and job.simplified_font not in probe_per_xref_families
                            and family_target_counts[job.simplified_font] <= 1
                        ):
                            for cid, text in pairs:
                                cache.add_observation(job.simplified_font, cid, text)
                            completed_since_save += 1
                            if opts.cache_path and completed_since_save >= opts.checkpoint_every:
                                cache.save(opts.cache_path)
                                completed_since_save = 0
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)
            except KeyboardInterrupt:
                if opts.cache_path and ocr_calls > 0:
                    cache.save(opts.cache_path)
                raise

    # Step 4: Detect genuine cross-font_xref subset-renumbering conflicts BEFORE committing to shared cache
    conflicting_families: set[str] = set()
    post_ocr_override_xrefs: set[int] = set()
    family_xref_winners: dict[str, dict[int, dict[int, str]]] = {}
    for fx, cid_counters in per_xref_obs.items():
        if fx in isolated_xrefs:
            continue
        _, simp, _ = font_meta[fx]
        for cid, ctr in cid_counters.items():
            if ctr:
                top_text = min(ctr.items(), key=lambda kv: (-kv[1], kv[0]))[0]
                family_xref_winners.setdefault(simp, {}).setdefault(fx, {})[cid] = top_text

    for simp, xref_maps in family_xref_winners.items():
        xlist = sorted(xref_maps.keys())
        for i in range(len(xlist)):
            for j in range(i + 1, len(xlist)):
                xa, xb = xlist[i], xlist[j]
                shared = set(xref_maps[xa].keys()) & set(xref_maps[xb].keys())
                if len(shared) < 2:
                    continue
                disagree = sum(
                    1
                    for c in shared
                    if unicodedata.normalize("NFKC", xref_maps[xa][c])
                    != unicodedata.normalize("NFKC", xref_maps[xb][c])
                )
                if disagree >= 2 and (disagree / len(shared)) >= 0.25:
                    conflicting_families.add(simp)
                    post_ocr_override_xrefs.add(xa)
                    post_ocr_override_xrefs.add(xb)
                    warnings.append(
                        f"In-document subset conflict for {simp} between xrefs {xa} and {xb} "
                        f"({disagree}/{len(shared)} CIDs disagree); isolating per font_xref without polluting shared cache"
                    )

    # Commit ONLY non-conflicting, non-isolated observations and blank glyphs into shared cache
    cache_dirty = False
    for fx, blanks in blank_cids_by_xref.items():
        _, simp, _ = font_meta[fx]
        if fx in isolated_xrefs or simp in conflicting_families:
            continue
        for cid in blanks:
            if cid in opts.space_cids:
                continue  # User CLI --space-cid is a per-run override; do not silently mutate shared cache
            entry = cache.get_entry(simp, cid)
            if entry is None or not entry.is_stable(min_count=opts.min_count, dominance_ratio=opts.dominance_ratio):
                cache.set_mapping(simp, cid, " ", lock=True)
                cache_dirty = True

    for fx, cid_counters in per_xref_obs.items():
        _, simp, _ = font_meta[fx]
        if fx in isolated_xrefs or simp in conflicting_families:
            continue
        # Avoid double-counting single-xref families already committed during streaming checkpoint
        if simp not in probe_per_xref_families and family_target_counts[simp] <= 1:
            cache_dirty = True
            continue
        for cid, ctr in cid_counters.items():
            for text, cnt in ctr.items():
                cache.add_observation(simp, cid, text, count=cnt)
                cache_dirty = True

    if opts.cache_path and cache_dirty:
        cache.save(opts.cache_path)

    # Step 5: Build final CID->Unicode maps per target font_xref and count unmapped CIDs
    per_xref_final_maps: dict[int, dict[int, str]] = {}
    total_resolved = 0
    total_unmapped = 0

    for fx in sorted(target_xrefs):
        _, simp, _ = font_meta[fx]
        used = {c for c in used_cids_by_xref.get(fx, set()) if c != 0}
        blanks = blank_cids_by_xref.get(fx, set())

        # Start from existing valid ToUnicode mappings when NOT --force (never lose valid existing entries!)
        base_existing = dict(existing_tounicode_by_xref.get(fx, {})) if not opts.force else {}

        if prebuilt_cmaps is not None:
            source_map = {**base_existing, **prebuilt_cmaps.get(simp, {})}
        elif fx in isolated_xrefs or simp in conflicting_families:
            # Isolated or conflicted subset-renumbered font: NEVER pull from shared family cache!
            source_map = dict(base_existing)
            for cid, ctr in per_xref_obs.get(fx, {}).items():
                if ctr:
                    source_map[cid] = min(ctr.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        else:
            source_map = {**base_existing, **cache.resolve_font_map(simp)}

        # Blank glyphs and explicit --space-cid overrides ALWAYS take precedence over stale/locked cache entries
        for bc in blanks:
            source_map[bc] = " "
        for sc in opts.space_cids:
            if sc in used:
                source_map[sc] = " "

        if prebuilt_cmaps is not None or (fx not in isolated_xrefs and simp not in conflicting_families):
            final_map = dict(source_map)
        else:
            final_map = {c: source_map[c] for c in used if c in source_map}

        for c in used:
            if c in final_map:
                total_resolved += 1
            else:
                total_unmapped += 1

        if final_map:
            per_xref_final_maps[fx] = final_map

    # Step 6: Write-gating semantics
    if fatal_ocr_error is not None and total_unmapped > 0:
        return FixSummary(
            status="failed",
            pdf_path=str(src),
            output_path=None,
            patched_fonts=0,
            ocr_calls=ocr_calls,
            resolved_cids=total_resolved,
            unmapped_cids=total_unmapped,
            error=fatal_ocr_error,
            warnings=warnings,
        )

    if opts.strict and total_unmapped > 0:
        return FixSummary(
            status="failed",
            pdf_path=str(src),
            output_path=None,
            patched_fonts=0,
            ocr_calls=ocr_calls,
            resolved_cids=total_resolved,
            unmapped_cids=total_unmapped,
            error=f"Strict mode aborted: {total_unmapped} CID(s) remain unmapped",
            warnings=warnings,
        )

    if not per_xref_final_maps:
        return FixSummary(
            status="failed",
            pdf_path=str(src),
            output_path=None,
            patched_fonts=0,
            ocr_calls=ocr_calls,
            resolved_cids=total_resolved,
            unmapped_cids=total_unmapped,
            error=fatal_ocr_error or "No valid ToUnicode mappings available to inject",
            warnings=warnings,
        )

    # Step 7: Generate validated Adobe-compliant CMap streams, deduplicated by exact SHA-256 content
    out_doc = fitz.open(src)
    stream_xref_by_sha: dict[str, int] = {}
    patched_fonts = 0
    try:
        for fx, fmap in sorted(per_xref_final_maps.items()):
            _, simp, _ = font_meta[fx]
            cmap_name = sanitize_cmap_name(f"Adobe-Identity-UCS-{simp}")
            cmap_str = generate_tounicode_cmap(fmap, cmap_name=cmap_name, use_bfrange=opts.use_bfrange)
            v_errors = validate_tounicode_cmap(cmap_str)
            if v_errors:
                raise RuntimeError(f"Internal CMap validation failed for {simp}: {v_errors[:3]}")
            stream_bytes = cmap_str.encode("ascii")
            digest = hashlib.sha256(stream_bytes).hexdigest()

            if opts.debug_dir is not None and digest not in stream_xref_by_sha:
                opts.debug_dir.mkdir(parents=True, exist_ok=True)
                (opts.debug_dir / f"{simp}_{digest[:8]}.cmap").write_text(cmap_str, encoding="ascii")

            if digest not in stream_xref_by_sha:
                sx = out_doc.get_new_xref()
                out_doc.update_object(sx, "<<>>")  # Clean stream dict — NO non-standard /Type /Stream
                out_doc.update_stream(sx, stream_bytes)
                stream_xref_by_sha[digest] = sx

            out_doc.xref_set_key(fx, "ToUnicode", f"{stream_xref_by_sha[digest]} 0 R")
            patched_fonts += 1

        _save_pdf_cleanly(out_doc, dst)
    finally:
        if not out_doc.is_closed:
            out_doc.close()

    status: Literal["fixed", "partial"] = "fixed" if total_unmapped == 0 else "partial"
    return FixSummary(
        status=status,
        pdf_path=str(src),
        output_path=str(dst),
        patched_fonts=patched_fonts,
        ocr_calls=ocr_calls,
        resolved_cids=total_resolved,
        unmapped_cids=total_unmapped,
        warnings=warnings,
    )
