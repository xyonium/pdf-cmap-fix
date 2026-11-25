"""Command-line interface for pdf-cmap-fix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from pdf_cmap_fix import __version__
from pdf_cmap_fix.cache import MappingCache
from pdf_cmap_fix.cmap import generate_tounicode_cmap, sanitize_cmap_name
from pdf_cmap_fix.detector import inspect_pdf
from pdf_cmap_fix.ocr import resolve_ocr_config
from pdf_cmap_fix.patcher import FixOptions, fix_pdf


def _collect_pdfs(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(p for p in target.rglob("*.pdf") if p.is_file())
    return []


def _parse_cid(raw: str) -> int:
    s = raw.strip()
    if s.lower().startswith("0x"):
        return int(s, 16)
    return int(s, 10)


def _cmd_check(args: argparse.Namespace) -> int:
    target = Path(args.input)
    if not target.exists():
        print(f"Error: path not found: {target}", file=sys.stderr)
        return 1

    pdfs = _collect_pdfs(target)
    if not pdfs:
        print(f"Error: no PDF files found at: {target}", file=sys.stderr)
        return 1

    cache = MappingCache.load(args.cache) if args.cache else None
    reports = []
    exit_code = 0

    for pdf in pdfs:
        try:
            rep = inspect_pdf(
                pdf,
                cache=cache,
                strict_text=args.strict_text,
                strict_cmap=args.strict_cmap,
                verify_coverage=not args.fast,
            )
            reports.append(rep.to_dict())
            if rep.needs_fix and exit_code != 1:
                exit_code = 2
            if not args.json:
                status_tag = "NEEDS_FIX" if rep.needs_fix else "OK"
                print(
                    f"[{status_tag}] {pdf.name} "
                    f"(pages={rep.page_count}, identity_type0={rep.identity_type0_fonts}, "
                    f"problem_fonts={len(rep.problem_fonts)})"
                )
                if rep.cmap_warnings:
                    print(f"  CMap warnings ({len(rep.cmap_warnings)}): {rep.cmap_warnings[0]}")
                if rep.cache_coverage:
                    cov = rep.cache_coverage
                    print(
                        f"  Cache coverage: {cov['cached']}/{cov['total_unique_cids']} "
                        f"({cov['hit_rate'] * 100:.1f}% hit, {cov['missing']} missing)"
                    )
                if rep.text_anomaly_ratio is not None:
                    print(f"  Text anomaly ratio: {rep.text_anomaly_ratio * 100:.2f}%")
        except Exception as exc:
            exit_code = 1
            if args.json:
                reports.append({"pdf_path": str(pdf), "error": str(exc)})
            else:
                print(f"[ERROR] {pdf}: {exc}", file=sys.stderr)

    if args.json:
        out_obj = reports[0] if target.is_file() and len(reports) == 1 else reports
        print(json.dumps(out_obj, ensure_ascii=False, indent=2))

    return exit_code


def _cmd_fix(args: argparse.Namespace) -> int:
    target = Path(args.input)
    if not target.exists():
        print(f"Error: input path not found: {target}", file=sys.stderr)
        return 1

    if not args.in_place and not args.output:
        print("Error: specify either -o/--output <path> or --in-place", file=sys.stderr)
        return 1

    if args.in_place and args.output:
        print("Error: --in-place and -o/--output are mutually exclusive", file=sys.stderr)
        return 1

    pdfs = _collect_pdfs(target)
    if not pdfs:
        print(f"Error: no PDF files found at: {target}", file=sys.stderr)
        return 1

    if target.is_dir() and args.output:
        out_root = Path(args.output)
        if out_root.exists() and out_root.is_file():
            print("Error: output must be a directory when input is a directory", file=sys.stderr)
            return 1

    ocr_cfg = resolve_ocr_config(
        backend=args.ocr,
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        concurrency=args.concurrency,
        max_retries=args.max_retries,
        tesseract_lang=args.tesseract_lang,
        tesseract_cmd=args.tesseract_cmd,
    )

    space_cids = frozenset(_parse_cid(x) for x in args.space_cid) if args.space_cid else frozenset()
    opts = FixOptions(
        cache_path=Path(args.cache) if args.cache else None,
        cmap_dir=Path(args.cmap_dir) if args.cmap_dir else None,
        if_needed=args.if_needed,
        force=args.force,
        no_ocr=args.no_ocr,
        strict=args.strict,
        strict_cmap=args.strict_cmap,
        use_bfrange=args.bfrange,
        min_count=args.min_count,
        dominance_ratio=args.dominance_ratio,
        max_jobs_per_cid=args.max_jobs_per_cid,
        concurrency=args.concurrency,
        ocr_config=ocr_cfg,
        debug_dir=Path(args.debug_dir) if args.debug_dir else None,
        space_cids=space_cids,
        verbose=args.verbose,
    )

    exit_code = 0
    for pdf in pdfs:
        if args.in_place:
            out_path = pdf
        else:
            out_arg = Path(args.output)
            if target.is_dir():
                rel = pdf.relative_to(target)
                out_path = out_arg / rel
            elif out_arg.is_dir() or str(args.output).endswith(("/", "\\")):
                out_path = out_arg / pdf.name
            else:
                out_path = out_arg

        summary = fix_pdf(pdf, out_path, opts)
        for w in summary.warnings:
            print(f"  [warn] {w}", file=sys.stderr)

        if summary.status == "skipped":
            print(f"[SKIP] {pdf.name} (already has valid ToUnicode)")
        elif summary.status == "fixed":
            print(
                f"[FIXED] {pdf.name} -> {summary.output_path} "
                f"(fonts={summary.patched_fonts}, ocr_calls={summary.ocr_calls}, "
                f"resolved={summary.resolved_cids}, unmapped=0)"
            )
        elif summary.status == "partial":
            print(
                f"[PARTIAL] {pdf.name} -> {summary.output_path} "
                f"(fonts={summary.patched_fonts}, ocr_calls={summary.ocr_calls}, "
                f"resolved={summary.resolved_cids}, unmapped={summary.unmapped_cids})"
            )
            if exit_code == 0:
                exit_code = 3
        else:
            print(f"[FAILED] {pdf.name}: {summary.error}", file=sys.stderr)
            exit_code = 1

    return exit_code


def _cmd_cache(args: argparse.Namespace) -> int:
    sub = args.cache_command
    if sub == "stats":
        cache = MappingCache.load(args.cache)
        st = cache.stats(min_count=args.min_count)
        if args.json:
            print(json.dumps(st, ensure_ascii=False, indent=2))
        else:
            print(
                f"Cache v{st['version']}: {st['font_count']} fonts, {st['total_cids']} CIDs, "
                f"{st['total_stable']} stable, {st['total_locked']} locked, "
                f"{st['total_multi_candidate']} multi-candidate, {st['total_observations']} observations"
            )
            for fn, fst in st["fonts"].items():
                print(
                    f"  {fn:24s} cids={fst['cids']:5d} stable={fst['stable']:5d} "
                    f"locked={fst['locked']:5d} conflict_rate={fst['conflict_rate']*100:5.1f}%"
                )
        return 0

    if sub == "migrate":
        src = Path(args.input)
        dst = Path(args.output)
        if not src.exists():
            print(f"Error: source cache not found: {src}", file=sys.stderr)
            return 1
        cache = MappingCache.load(src)
        if args.clean_sourcehan_cid32:
            for fn in ("SourceHanSansCN", "SourceHanSerifCN"):
                entry = cache.get_entry(fn, 32)
                if entry and entry.top() and entry.top()[0] == " ":
                    cache.set_mapping(fn, 32, "?", lock=True)
        cache.save(dst)
        st = cache.stats()
        print(f"Migrated {src} -> {dst} ({st['font_count']} fonts, {st['total_cids']} CIDs, {st['total_observations']} obs)")
        return 0

    if sub == "compact":
        p = Path(args.cache)
        if not p.exists():
            print(f"Error: cache not found: {p}", file=sys.stderr)
            return 1
        cache = MappingCache.load(p)
        res = cache.compact(
            promote_min=args.promote_min,
            dominance_ratio=args.ratio,
            cap=args.cap,
            drop_noise_ratio=args.drop_noise_ratio,
        )
        cache.save(p)
        print(
            f"Compacted {p}: promoted={res['promoted']}, capped={res['capped']}, "
            f"dropped_candidates={res['dropped_candidates']}"
        )
        return 0

    if sub == "conflicts":
        cache = MappingCache.load(args.cache)
        items = cache.conflicts(
            min_secondary=args.min_secondary,
            max_ratio=args.max_ratio,
            font=args.font,
        )
        if args.json:
            print(json.dumps(items[: args.limit], ensure_ascii=False, indent=2))
        else:
            print(f"Found {len(items)} conflict(s) (showing up to {args.limit}):")
            for item in items[: args.limit]:
                cand_str = ", ".join(f"{c['text']!r}:{c['count']}" for c in item["candidates"][:4])
                lock_tag = " [LOCKED]" if item["locked"] else ""
                print(f"  {item['font']} CID {item['cid']:5d} ({item['cid_hex']}){lock_tag} ratio={item['ratio']}: {cand_str}")
        return 0

    if sub == "set":
        p = Path(args.cache)
        cache = MappingCache.load(p) if p.exists() else MappingCache()
        cid = _parse_cid(args.cid)
        cache.set_mapping(args.font, cid, args.text, lock=not args.unlock)
        cache.save(p)
        print(f"Set {args.font} CID {cid} (0x{cid:04X}) -> {args.text!r} (locked={not args.unlock})")
        return 0

    if sub == "export-cmap":
        cache = MappingCache.load(args.cache)
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for fn in sorted(cache.fonts.keys()):
            fmap = cache.resolve_font_map(fn)
            if not fmap:
                continue
            cmap = generate_tounicode_cmap(
                fmap,
                cmap_name=sanitize_cmap_name(f"Adobe-Identity-UCS-{fn}"),
                use_bfrange=args.bfrange,
            )
            (out_dir / f"{fn}.cmap").write_text(cmap, encoding="ascii")
            count += 1
        print(f"Exported {count} ToUnicode CMap file(s) to {out_dir}")
        return 0

    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf-cmap-fix",
        description="Detect and repair PDFs with missing or broken ToUnicode CMaps.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # check
    p_check = subparsers.add_parser("check", help="Inspect PDF(s) for missing or incomplete ToUnicode CMaps")
    p_check.add_argument("input", help="Input PDF file or directory")
    p_check.add_argument("--cache", help="Optional cache JSON to estimate cache hit rate")
    p_check.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_check.add_argument("--strict-text", action="store_true", help="Also compute PUA/replacement text anomaly ratio")
    p_check.add_argument("--strict-cmap", action="store_true", help="Flag legacy Adobe-incompatible bfrange violations")
    p_check.add_argument("--fast", action="store_true", help="Skip page CID coverage verification (xref check only)")

    # fix
    p_fix = subparsers.add_parser("fix", help="Repair missing/broken ToUnicode CMaps in PDF(s)")
    p_fix.add_argument("input", help="Input PDF file or directory")
    p_fix.add_argument("-o", "--output", help="Output PDF file or directory")
    p_fix.add_argument("--in-place", action="store_true", help="Atomically replace input PDF(s) in place")
    p_fix.add_argument("--cache", help="Path to v2 (or legacy v1) CID->Unicode cache JSON")
    p_fix.add_argument("--cmap-dir", help="Use pre-generated .cmap files from directory (offline, skips OCR)")
    p_fix.add_argument("--if-needed", action="store_true", help="Skip PDFs that already have complete ToUnicode CMaps")
    p_fix.add_argument("--force", action="store_true", help="Rebuild ToUnicode even on fonts that already have one")
    p_fix.add_argument("--no-ocr", action="store_true", help="Offline mode: embed from cache only without calling OCR")
    p_fix.add_argument("--strict", action="store_true", help="Abort without writing if any CID remains unmapped")
    p_fix.add_argument("--strict-cmap", action="store_true", help="Also re-embed fonts with legacy bfrange violations")
    p_fix.add_argument("--bfrange", action="store_true", help="Use validated bfrange compression (default: pure bfchar)")
    p_fix.add_argument("--min-count", type=int, default=5, help="Observations required to treat a CID as stable (default: 5)")
    p_fix.add_argument("--dominance-ratio", type=float, default=4.0, help="Top/second ratio required for stability (default: 4.0)")
    p_fix.add_argument("--max-jobs-per-cid", type=int, default=3, help="Max OCR calls per unstabilized CID in a single run (default: 3; 0 = unlimited)")
    p_fix.add_argument("--concurrency", type=int, default=4, help="Concurrent OCR API requests (default: 4)")
    p_fix.add_argument("--max-retries", type=int, default=6, help="Max retries per OCR call (default: 6)")
    p_fix.add_argument("--ocr", choices=["openai", "tesseract"], default="openai", help="OCR backend (default: openai)")
    p_fix.add_argument("--api-key", help="OCR API key (overrides PDF_CMAP_API_KEY / DASHSCOPE_API_KEY / OPENAI_API_KEY)")
    p_fix.add_argument("--base-url", help="OpenAI-compatible base URL (overrides PDF_CMAP_BASE_URL / OPENAI_BASE_URL)")
    p_fix.add_argument("--model", help="Vision model ID (overrides PDF_CMAP_MODEL)")
    p_fix.add_argument("--tesseract-lang", default="chi_sim+eng", help="Tesseract language code")
    p_fix.add_argument("--tesseract-cmd", help="Explicit path to tesseract executable")
    p_fix.add_argument("--space-cid", action="append", help="Force specific CID(s) (dec or 0xHEX) to map to space")
    p_fix.add_argument("--debug-dir", help="Optional directory to dump rendered PNGs and generated .cmap files")
    p_fix.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    # cache
    p_cache = subparsers.add_parser("cache", help="Inspect, migrate, compact, and export CID->Unicode caches")
    cache_sub = p_cache.add_subparsers(dest="cache_command", required=True)

    pc_stats = cache_sub.add_parser("stats", help="Show cache statistics")
    pc_stats.add_argument("cache", help="Cache JSON path")
    pc_stats.add_argument("--min-count", type=int, default=5)
    pc_stats.add_argument("--json", action="store_true")

    pc_mig = cache_sub.add_parser("migrate", help="Losslessly migrate legacy v1 cache JSON to v2")
    pc_mig.add_argument("input", help="Legacy v1 JSON path")
    pc_mig.add_argument("output", help="Output v2 JSON path")
    pc_mig.add_argument(
        "--clean-sourcehan-cid32",
        action="store_true",
        help="Fix legacy polluted SourceHanSansCN/SourceHanSerifCN CID 32 (' ' -> '?')",
    )

    pc_comp = cache_sub.add_parser("compact", help="Promote high-confidence entries to locked and cap counts")
    pc_comp.add_argument("cache", help="Cache JSON path (updated atomically in place)")
    pc_comp.add_argument("--promote-min", type=int, default=7)
    pc_comp.add_argument("--ratio", type=float, default=4.0)
    pc_comp.add_argument("--cap", type=int, default=100)
    pc_comp.add_argument("--drop-noise-ratio", type=float, default=None)

    pc_conf = cache_sub.add_parser("conflicts", help="List CIDs with multiple candidate mappings")
    pc_conf.add_argument("cache", help="Cache JSON path")
    pc_conf.add_argument("--font", help="Filter to a single simplified font family")
    pc_conf.add_argument("--min-secondary", type=int, default=1)
    pc_conf.add_argument("--max-ratio", type=float, default=None)
    pc_conf.add_argument("--limit", type=int, default=50)
    pc_conf.add_argument("--json", action="store_true")

    pc_set = cache_sub.add_parser("set", help="Manually set and lock a single CID->Unicode mapping")
    pc_set.add_argument("cache", help="Cache JSON path")
    pc_set.add_argument("font", help="Simplified font name (e.g. SourceHanSansCN)")
    pc_set.add_argument("cid", help="CID (decimal or 0xHEX)")
    pc_set.add_argument("text", help="Target Unicode text (single char or ligature like 'fi')")
    pc_set.add_argument("--unlock", action="store_true", help="Do not lock this entry")

    pc_exp = cache_sub.add_parser("export-cmap", help="Export standard ToUnicode .cmap files per font")
    pc_exp.add_argument("cache", help="Cache JSON path")
    pc_exp.add_argument("-o", "--output", required=True, help="Output directory")
    pc_exp.add_argument("--bfrange", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        return _cmd_check(args)
    if args.command == "fix":
        return _cmd_fix(args)
    if args.command == "cache":
        return _cmd_cache(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
