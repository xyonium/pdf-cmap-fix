"""pdf-cmap-fix: Detect and repair PDFs with missing or broken ToUnicode CMaps."""

from __future__ import annotations

from pdf_cmap_fix.cache import CidEntry, MappingCache
from pdf_cmap_fix.cmap import (
    generate_tounicode_cmap,
    parse_tounicode_cmap,
    validate_tounicode_cmap,
)
from pdf_cmap_fix.detector import DetectionReport, FontIssue, inspect_pdf
from pdf_cmap_fix.ocr import OCRConfig, resolve_ocr_config
from pdf_cmap_fix.patcher import FixOptions, FixSummary, fix_pdf
from pdf_cmap_fix.verifier import VerifyOptions, VerifySummary, verify_cache_with_pdfs

__all__ = [
    "__version__",
    "CidEntry",
    "DetectionReport",
    "FixOptions",
    "FixSummary",
    "FontIssue",
    "MappingCache",
    "OCRConfig",
    "VerifyOptions",
    "VerifySummary",
    "fix_pdf",
    "generate_tounicode_cmap",
    "inspect_pdf",
    "parse_tounicode_cmap",
    "resolve_ocr_config",
    "validate_tounicode_cmap",
    "verify_cache_with_pdfs",
]
__version__ = "0.1.0"
