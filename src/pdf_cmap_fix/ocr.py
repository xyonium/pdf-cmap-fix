"""OpenAI-compatible VLM OCR engine, optional Tesseract backend, and anchor-aware CID alignment."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
import json
import os
import re
import time
from typing import Any, Callable, Literal, Protocol, Sequence
import unicodedata

from PIL import Image

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

_OCR_PROMPT = (
    "OCR mode: transcribe every character and punctuation mark visible in the image verbatim, "
    "preserving fullwidth/halfwidth punctuation and rare Chinese characters. "
    "Output ONLY the transcribed text with no explanations, markdown, or extra formatting. "
    "If nothing is readable, output an empty string."
)
_WHITESPACE_RE = re.compile(r"\s+")
_COMMON_LIGATURES = frozenset({"fi", "fl", "ff", "ffi", "ffl", "ft", "st", "tt", "ti", "ij", "IJ", "ae", "oe"})
_F_LIGATURES_ORDERED = ("ffi", "ffl", "ff", "fi", "fl")


class OCRConfigError(RuntimeError):
    """Raised when required OCR configuration (API key or model) is missing."""


class OCRAuthError(RuntimeError):
    """Raised on HTTP 401/403 authentication/authorization errors (non-retryable)."""


class OCRBackendError(RuntimeError):
    """Raised on unrecoverable OCR backend / transport failures."""


@dataclass(frozen=True)
class OCRConfig:
    backend: Literal["openai", "tesseract"] = "openai"
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    concurrency: int = 4
    max_retries: int = 6
    timeout: float = 90.0
    no_thinking: bool = False
    tesseract_lang: str = "chi_sim+eng"
    tesseract_cmd: str | None = None


def resolve_ocr_config(
    *,
    backend: Literal["openai", "tesseract"] = "openai",
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    concurrency: int = 4,
    max_retries: int = 6,
    timeout: float = 90.0,
    no_thinking: bool = False,
    tesseract_lang: str = "chi_sim+eng",
    tesseract_cmd: str | None = None,
) -> OCRConfig:
    """Resolve OCR config with CLI arguments taking precedence over environment variables."""
    resolved_key = (
        api_key
        or os.getenv("PDF_CMAP_API_KEY")
        or os.getenv("DASHSCOPE_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    resolved_base_url = (
        base_url
        or os.getenv("PDF_CMAP_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL
    )
    resolved_model = model or os.getenv("PDF_CMAP_MODEL")
    return OCRConfig(
        backend=backend,
        api_key=resolved_key.strip() if resolved_key else None,
        base_url=resolved_base_url.strip() if resolved_base_url else DEFAULT_BASE_URL,
        model=resolved_model.strip() if resolved_model else None,
        concurrency=max(1, concurrency),
        max_retries=max(1, max_retries),
        timeout=timeout,
        no_thinking=no_thinking,
        tesseract_lang=tesseract_lang,
        tesseract_cmd=tesseract_cmd,
    )


def validate_ocr_config(config: OCRConfig) -> None:
    """Validate OCR config lazily right before making OCR calls."""
    if config.backend == "tesseract":
        return
    if not config.api_key:
        raise OCRConfigError(
            "Missing OCR API key. Pass --api-key or set PDF_CMAP_API_KEY / DASHSCOPE_API_KEY / OPENAI_API_KEY "
            "(or pass --no-ocr to embed from cache only)."
        )
    if not config.model:
        raise OCRConfigError(
            "Missing OCR model name. Pass --model <model_id> or set PDF_CMAP_MODEL "
            "(e.g. a modern vision model on DashScope or any OpenAI-compatible VLM endpoint)."
        )


def _split_grapheme_clusters(text: str) -> list[str]:
    """Split NFC-normalized text into grapheme clusters (combining marks & variation selectors kept attached)."""
    clusters: list[str] = []
    for ch in text:
        cp = ord(ch)
        cat = unicodedata.category(ch)
        is_combining = cat in ("Mn", "Mc", "Me")
        is_vs = (0xFE00 <= cp <= 0xFE0F) or (0xE0100 <= cp <= 0xE01EF)
        is_zwj = cp == 0x200D
        if clusters and (is_combining or is_vs or is_zwj or clusters[-1].endswith("\u200d")):
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return clusters


def _normalize_for_anchor(text: str) -> str:
    """Fold fullwidth/compatibility forms ONLY for anchor matching (never mutates stored output)."""
    return unicodedata.normalize("NFKC", text)


def _is_plausible_single_cid_ligature(clusters: list[str]) -> bool:
    if len(clusters) == 1:
        return True
    return "".join(clusters) in _COMMON_LIGATURES


def _match_anchor_positions(
    real_cids: Sequence[int],
    clusters: list[str],
    known_anchors: dict[int, str],
) -> list[tuple[int, int, int]]:
    """Find strictly monotonic, unambiguous anchor matches `(cid_idx, ocr_start, ocr_end)`.

    Uses dynamic programming / unique-match filtering so repeated characters don't shift alignments.
    """
    n_cids = len(real_cids)
    n_clu = len(clusters)
    # Build anchor cluster sequences for CIDs present in known_anchors
    anchor_seqs: dict[int, list[str]] = {}
    for idx, cid in enumerate(real_cids):
        if cid in known_anchors and known_anchors[cid]:
            aseq = _split_grapheme_clusters(unicodedata.normalize("NFC", _WHITESPACE_RE.sub("", known_anchors[cid])))
            if aseq:
                anchor_seqs[idx] = aseq

    if not anchor_seqs:
        return []

    # Find candidate positions for each anchor CID in `clusters`
    norm_clusters = [_normalize_for_anchor(c) for c in clusters]
    candidates_per_cid: dict[int, list[tuple[int, int]]] = {}
    for cid_idx, aseq in anchor_seqs.items():
        norm_aseq = [_normalize_for_anchor(c) for c in aseq]
        k = len(norm_aseq)
        hits: list[tuple[int, int]] = []
        for pos in range(0, n_clu - k + 1):
            if norm_clusters[pos : pos + k] == norm_aseq:
                hits.append((pos, pos + k))
        if hits:
            candidates_per_cid[cid_idx] = hits

    if not candidates_per_cid:
        return []

    # Longest monotonic sequence of anchors, breaking ties by minimal positional drift from cid_idx
    anchor_indices = sorted(candidates_per_cid.keys())
    # Flatten nodes (cid_idx, start, end)
    nodes: list[tuple[int, int, int]] = []
    for ci in anchor_indices:
        # Only trust anchors that don't have excessive ambiguity, or score them via DP
        if len(candidates_per_cid[ci]) <= 4:
            for s, e in candidates_per_cid[ci]:
                nodes.append((ci, s, e))

    if not nodes:
        return []

    # DP maximizing matched anchors, keeping ALL maximum-length predecessor chains so
    # ambiguous repeated-anchor placements (e.g. 'A' matching pos 1 or 2 in 'XAAB') are
    # detected and intersected out rather than picked arbitrarily.
    m = len(nodes)
    best_len = [1] * m
    parents: list[list[int]] = [[] for _ in range(m)]

    for i in range(m):
        ci, si, ei = nodes[i]
        for j in range(i):
            cj, sj, ej = nodes[j]
            if cj < ci and ej <= si:
                cid_gap = ci - cj - 1
                ocr_gap = si - ej
                if ocr_gap > cid_gap * 4 + 2:
                    continue
                cand_len = best_len[j] + 1
                if cand_len > best_len[i]:
                    best_len[i] = cand_len
                    parents[i] = [j]
                elif cand_len == best_len[i]:
                    parents[i].append(j)

    global_best_len = max(best_len)
    optimal_ends = [i for i in range(m) if best_len[i] == global_best_len]

    # Collect up to a bounded set of optimal chains and intersect their anchor positions
    all_chains: list[list[tuple[int, int, int]]] = []
    max_chains = 16

    def _backtrack(idx: int, acc: list[tuple[int, int, int]]) -> None:
        if len(all_chains) >= max_chains:
            return
        acc.append(nodes[idx])
        if not parents[idx]:
            all_chains.append(list(reversed(acc)))
        else:
            for p in parents[idx]:
                _backtrack(p, acc)
        acc.pop()

    for end_idx in optimal_ends:
        _backtrack(end_idx, [])
        if len(all_chains) >= max_chains:
            break

    if not all_chains:
        return []
    if len(all_chains) == 1:
        return all_chains[0]

    # Keep ONLY anchor placements that appear identically in EVERY maximum-length chain
    common = set(all_chains[0])
    for ch in all_chains[1:]:
        common &= set(ch)
    return sorted(common, key=lambda t: (t[0], t[1]))


def _fast_path_conflicts_with_anchors(
    real_cids: Sequence[int],
    clusters: list[str],
    known_anchors: dict[int, str],
) -> bool:
    """Return True if a naive 1-to-1 zip would contradict any known anchor (e.g. ligature anchor)."""
    for idx, cid in enumerate(real_cids):
        if cid not in known_anchors or not known_anchors[cid]:
            continue
        aseq = _split_grapheme_clusters(
            unicodedata.normalize("NFC", _WHITESPACE_RE.sub("", known_anchors[cid]))
        )
        if not aseq:
            continue
        if len(aseq) != 1:
            return True
        if _normalize_for_anchor(aseq[0]) != _normalize_for_anchor(clusters[idx]):
            return True
    return False


def align_cids_with_ocr(
    real_cids: Sequence[int],
    ocr_text: str,
    known_anchors: dict[int, str] | None = None,
) -> list[tuple[int, str]]:
    """Align a sequence of real (non-space) CIDs with OCR text.

    - Preserves original NFC text (does NOT fold fullwidth characters to halfwidth).
    - Keeps combining marks and variation selectors attached as single grapheme clusters.
    - Exact length match takes fast path ONLY when consistent with any known anchors
      (never splitting a known ligature anchor like 'fi' across two CIDs).
    - Otherwise uses unambiguous maximum-length anchor chains to align gaps safely and
      skips ambiguous gaps without shifting surrounding text.
    """
    if not real_cids or not ocr_text:
        return []

    stripped = _WHITESPACE_RE.sub("", unicodedata.normalize("NFC", ocr_text))
    if not stripped:
        return []

    clusters = _split_grapheme_clusters(stripped)
    n_cids = len(real_cids)
    n_clu = len(clusters)

    # Fast path: exact 1-to-1 cluster length match (only if it does NOT contradict known anchors!)
    if n_cids == n_clu and (
        not known_anchors or not _fast_path_conflicts_with_anchors(real_cids, clusters, known_anchors)
    ):
        return [(real_cids[i], clusters[i]) for i in range(n_cids)]

    # Anchor-segmented alignment for ligatures and partial dropouts
    if known_anchors:
        anchors = _match_anchor_positions(real_cids, clusters, known_anchors)
        if anchors:
            aligned: list[tuple[int, str]] = []
            prev_cid_end = 0
            prev_ocr_end = 0

            # Append virtual sentinel at the end to flush trailing gap
            segments = anchors + [(n_cids, n_clu, n_clu)]
            for cid_idx, ocr_s, ocr_e in segments:
                gap_cids = real_cids[prev_cid_end:cid_idx]
                gap_clu = clusters[prev_ocr_end:ocr_s]
                if gap_cids:
                    if len(gap_cids) == len(gap_clu):
                        for gc, gt in zip(gap_cids, gap_clu):
                            aligned.append((gc, gt))
                    elif len(gap_cids) == 1 and gap_clu and _is_plausible_single_cid_ligature(gap_clu):
                        aligned.append((gap_cids[0], "".join(gap_clu)))
                    # Else: ambiguous gap -> skip without shifting!

                if cid_idx < n_cids:
                    aligned.append((real_cids[cid_idx], "".join(clusters[ocr_s:ocr_e])))
                prev_cid_end = cid_idx + 1
                prev_ocr_end = ocr_e

            if aligned:
                return aligned

    # Unanchored unique f-ligature folding (e.g. "June 2016, ScientificAmerican.com" where "fi" is 1 CID)
    if n_clu > n_cids and 1 <= (n_clu - n_cids) <= 4:
        folded: list[str] = []
        idx = 0
        while idx < n_clu:
            matched_lig = None
            for lig in _F_LIGATURES_ORDERED:
                k = len(lig)
                if idx + k <= n_clu and "".join(clusters[idx : idx + k]) == lig:
                    matched_lig = lig
                    break
            if matched_lig is not None:
                folded.append(matched_lig)
                idx += len(matched_lig)
            else:
                folded.append(clusters[idx])
                idx += 1
        # Only accept if folding all f-ligatures reaches the exact CID count (unambiguous!)
        if len(folded) == n_cids:
            return [(real_cids[i], folded[i]) for i in range(n_cids)]

    # Safe fallback ONLY when lengths still differ and both sides have leading runs of repeated tokens
    if n_cids >= 2 and n_clu >= 2 and real_cids[0] == real_cids[1] and clusters[0] == clusters[1]:
        first_cid = real_cids[0]
        first_clu = clusters[0]
        i = 0
        while i < n_cids and real_cids[i] == first_cid:
            i += 1
        j = 0
        while j < n_clu and clusters[j] == first_clu:
            j += 1
        # Keep 1 copy of the leading repeated character so it can still be learned
        rem_cids = [first_cid, *real_cids[i:]]
        rem_clu = [first_clu, *clusters[j:]]
        if len(rem_cids) == len(rem_clu):
            return list(zip(rem_cids, rem_clu))

    return []


def image_to_base64_png(img: Image.Image) -> str:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _clean_ocr_response(raw_content: str) -> str:
    text = raw_content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list) and parsed and all(isinstance(x, dict) and "text_content" in x for x in parsed):
                return "".join(str(x["text_content"]) for x in parsed)
            if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
                return parsed["text"]
        except json.JSONDecodeError:
            pass
    return text


def _extract_status_code(exc: Exception) -> int | None:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    resp = getattr(exc, "response", None)
    if resp is not None:
        rc = getattr(resp, "status_code", None)
        if isinstance(rc, int):
            return rc
    return None


class OCRClient(Protocol):
    def recognize(self, image: Image.Image) -> str: ...


class OpenAIVisionOCR:
    """OpenAI-compatible Vision LLM OCR client with fast-fail auth and exponential backoff."""

    def __init__(
        self,
        config: OCRConfig,
        *,
        client: Any | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        validate_ocr_config(config)
        self.config = config
        self._sleep = sleep_fn
        if client is not None:
            self._client = client
        else:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=config.timeout,
            )

    def recognize(self, image: Image.Image, *, prompt: str | None = None) -> str:
        b64 = image_to_base64_png(image)
        use_prompt = prompt or _OCR_PROMPT
        last_err: Exception | None = None

        for attempt in range(self.config.max_retries):
            try:
                req_kwargs: dict[str, Any] = {
                    "model": self.config.model,
                    "temperature": 0,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                                },
                                {"type": "text", "text": use_prompt},
                            ],
                        }
                    ],
                }
                if self.config.no_thinking:
                    req_kwargs["extra_body"] = {"enable_thinking": False}
                completion = self._client.chat.completions.create(**req_kwargs)
                if completion.choices and completion.choices[0].message.content is not None:
                    return _clean_ocr_response(str(completion.choices[0].message.content))
                return ""
            except Exception as exc:
                last_err = exc
                status = _extract_status_code(exc)
                if status in (401, 403):
                    raise OCRAuthError(f"OCR authentication failed (HTTP {status}): {exc}") from exc
                if status == 400:
                    lower_msg = str(exc).lower()
                    if any(k in lower_msg for k in ("access denied", "overdue", "arrearage", "quota", "insufficient")):
                        raise OCRAuthError(f"OCR account/quota rejected (HTTP 400): {exc}") from exc
                if status == 404:
                    raise OCRAuthError(
                        f"OCR endpoint/model not found (HTTP 404, check --model '{self.config.model}' and --base-url): {exc}"
                    ) from exc
                if status == 400:
                    raise OCRBackendError(
                        f"OCR request rejected (HTTP 400, check --model '{self.config.model}' and --base-url): {exc}"
                    ) from exc
                if attempt < self.config.max_retries - 1:
                    delay = min(30.0, 0.5 * (2**attempt))
                    self._sleep(delay)
                    continue
                raise OCRBackendError(
                    f"OCR failed after {self.config.max_retries} attempts: {last_err}"
                ) from exc

        raise OCRBackendError(f"OCR failed: {last_err}")


class TesseractOCR:
    """Optional local Tesseract OCR fallback."""

    def __init__(self, config: OCRConfig) -> None:
        try:
            import pytesseract
        except ImportError as exc:
            raise OCRConfigError(
                "Tesseract backend requires optional dependency: install with `uv pip install 'pdf-cmap-fix[tesseract]'`"
            ) from exc
        if config.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = config.tesseract_cmd
        self._pytesseract = pytesseract
        self._lang = config.tesseract_lang

    def recognize(self, image: Image.Image) -> str:
        try:
            return str(self._pytesseract.image_to_string(image, lang=self._lang)).strip()
        except Exception as exc:
            raise OCRBackendError(f"Tesseract OCR failed: {exc}") from exc


def build_ocr_client(config: OCRConfig) -> OCRClient:
    validate_ocr_config(config)
    if config.backend == "tesseract":
        return TesseractOCR(config)
    return OpenAIVisionOCR(config)
