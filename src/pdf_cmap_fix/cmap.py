"""Strict ToUnicode CMap generator, parser, and validator (ISO 32000-1 §9.10.3 & Adobe TN #5411)."""

from __future__ import annotations

import re

from pdf_cmap_fix.cache import validate_unicode_text

MAX_ENTRIES_PER_BLOCK = 100

_NAME_INVALID_RE = re.compile(r"[^A-Za-z0-9._+-]+")
_HEX_RE = re.compile(r"<([0-9A-Fa-f]+)>")
_TOKEN_RE = re.compile(r"<([0-9A-Fa-f]+)>|(\[)|(\])|([^\s<>\[\]]+)")
_COMMENT_RE = re.compile(r"%[^\r\n]*")
_BFCHAR_BLOCK_RE = re.compile(r"(\d+)\s+beginbfchar\s*(.*?)\s*endbfchar", re.S)
_BFRANGE_BLOCK_RE = re.compile(r"(\d+)\s+beginbfrange\s*(.*?)\s*endbfrange", re.S)
_CODESPACE_BLOCK_RE = re.compile(r"(\d+)\s+begincodespacerange\s*(.*?)\s*endcodespacerange", re.S)


def sanitize_cmap_name(name: str) -> str:
    """Produce a safe ASCII PDF Name token for `/CMapName`."""
    cleaned = _NAME_INVALID_RE.sub("_", (name or "").strip()).strip("_")
    return cleaned or "Adobe-Identity-UCS"


def encode_unicode_hex(text: str) -> str:
    """Encode a valid Unicode string to uppercase UTF-16BE hex (multiple of 4 hex chars)."""
    valid = validate_unicode_text(text)
    return valid.encode("utf-16-be").hex().upper()


def decode_unicode_hex(hex_str: str) -> str:
    """Decode a UTF-16BE hex string (multiple of 4 hex chars) to a validated Unicode string."""
    h = hex_str.strip()
    if len(h) < 4 or len(h) % 4 != 0:
        raise ValueError(f"Invalid UTF-16BE hex length ({len(h)}): <{h}>")
    raw = bytes.fromhex(h)
    text = raw.decode("utf-16-be")
    return validate_unicode_text(text)


def _can_extend_bfrange(prev_cid: int, prev_text: str, cid: int, text: str) -> bool:
    """Check ISO 32000-1 §9.10.3 + Adobe TN #5411 bfrange continuity constraints."""
    if cid != prev_cid + 1:
        return False
    # Never place multi-character ligatures / combining sequences in a bfrange
    if len(prev_text) != 1 or len(text) != 1:
        return False
    # TN #5411: CID range must never cross a first-byte (256-code) boundary
    if (prev_cid >> 8) != (cid >> 8):
        return False
    # ISO 32000-1 §9.10.3: destination increments ONLY the last UTF-16BE byte without carry
    prev_bytes = prev_text.encode("utf-16-be")
    cur_bytes = text.encode("utf-16-be")
    if len(prev_bytes) != len(cur_bytes):
        return False
    if prev_bytes[:-1] != cur_bytes[:-1]:
        return False
    return cur_bytes[-1] == prev_bytes[-1] + 1


def generate_tounicode_cmap(
    cid_to_text: dict[int, str],
    cmap_name: str = "Adobe-Identity-UCS",
    use_bfrange: bool = False,
) -> str:
    """Generate a strictly compliant ToUnicode CMap stream.

    By default (`use_bfrange=False`), emits only `bfchar` blocks (<=100 entries each),
    eliminating any `bfrange` carry edge cases across PDF viewers (especially Adobe Reader).
    When `use_bfrange=True`, splits ranges strictly at both CID high-byte and destination
    UTF-16BE last-byte boundaries and emits `bfchar` blocks before `bfrange` blocks.
    """
    if not cid_to_text:
        raise ValueError("Cannot generate ToUnicode CMap from empty mapping")

    validated_pairs: list[tuple[int, str]] = []
    for cid, text in sorted(cid_to_text.items()):
        if not isinstance(cid, int) or not (0 <= cid <= 0xFFFF):
            raise ValueError(f"CID out of 16-bit range: {cid!r}")
        validated_pairs.append((cid, validate_unicode_text(text)))

    safe_name = sanitize_cmap_name(cmap_name)
    lines: list[str] = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        f"/CMapName /{safe_name} def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]

    bfchars: list[str] = []
    bfranges: list[str] = []

    if not use_bfrange:
        for cid, text in validated_pairs:
            bfchars.append(f"<{cid:04X}> <{encode_unicode_hex(text)}>")
    else:
        groups: list[list[tuple[int, str]]] = []
        current: list[tuple[int, str]] = []
        for cid, text in validated_pairs:
            if not current:
                current = [(cid, text)]
            else:
                prev_cid, prev_text = current[-1]
                if _can_extend_bfrange(prev_cid, prev_text, cid, text):
                    current.append((cid, text))
                else:
                    groups.append(current)
                    current = [(cid, text)]
        if current:
            groups.append(current)

        for grp in groups:
            if len(grp) == 1:
                cid, text = grp[0]
                bfchars.append(f"<{cid:04X}> <{encode_unicode_hex(text)}>")
            else:
                start_cid, start_text = grp[0]
                end_cid, _ = grp[-1]
                bfranges.append(f"<{start_cid:04X}> <{end_cid:04X}> <{encode_unicode_hex(start_text)}>")

    # TN #5411 §1.4: emit bfchar blocks before bfrange blocks, <= 100 entries per block
    for i in range(0, len(bfchars), MAX_ENTRIES_PER_BLOCK):
        chunk = bfchars[i : i + MAX_ENTRIES_PER_BLOCK]
        lines.append(f"{len(chunk)} beginbfchar")
        lines.extend(chunk)
        lines.append("endbfchar")

    for i in range(0, len(bfranges), MAX_ENTRIES_PER_BLOCK):
        chunk = bfranges[i : i + MAX_ENTRIES_PER_BLOCK]
        lines.append(f"{len(chunk)} beginbfrange")
        lines.extend(chunk)
        lines.append("endbfrange")

    lines.extend(
        [
            "endcmap",
            "CMapName currentdict /CMap defineresource pop",
            "end",
            "end",
            "",
        ]
    )
    return "\n".join(lines)


def _expand_bfrange_target(start_cid: int, end_cid: int, dst_hex: str) -> dict[int, str]:
    """Expand `<start> <end> <dst>` following ISO 32000-1 §9.10.3 last-byte-only increment."""
    raw = bytearray(bytes.fromhex(dst_hex))
    if len(raw) < 2 or len(raw) % 2 != 0:
        raise ValueError(f"Invalid UTF-16BE bfrange destination: <{dst_hex}>")
    result: dict[int, str] = {}
    base_last = raw[-1]
    for offset, cid in enumerate(range(start_cid, end_cid + 1)):
        cur_last = (base_last + offset) & 0xFF
        cur_bytes = bytes(raw[:-1]) + bytes([cur_last])
        text = cur_bytes.decode("utf-16-be")
        result[cid] = validate_unicode_text(text)
    return result


def parse_tounicode_cmap(cmap_text: str) -> dict[int, str]:
    """Parse a ToUnicode CMap string into a `{cid: unicode_text}` dictionary."""
    mapping: dict[int, str] = {}

    for _, body in _BFCHAR_BLOCK_RE.findall(cmap_text):
        tokens = _HEX_RE.findall(body)
        for i in range(0, len(tokens) - 1, 2):
            cid = int(tokens[i], 16)
            try:
                mapping[cid] = decode_unicode_hex(tokens[i + 1])
            except (ValueError, UnicodeDecodeError):
                continue

    for _, body in _BFRANGE_BLOCK_RE.findall(cmap_text):
        # Tokenize line-by-line or expression-by-expression
        idx = 0
        n = len(body)
        while idx < n:
            m1 = _HEX_RE.search(body, idx)
            if not m1:
                break
            m2 = _HEX_RE.search(body, m1.end())
            if not m2:
                break
            start_cid = int(m1.group(1), 16)
            end_cid = int(m2.group(1), 16)
            pos = m2.end()
            while pos < n and body[pos].isspace():
                pos += 1
            if pos >= n:
                break
            if body[pos] == "[":
                close_pos = body.find("]", pos)
                if close_pos == -1:
                    break
                arr_tokens = _HEX_RE.findall(body[pos + 1 : close_pos])
                for offset, h in enumerate(arr_tokens):
                    cid = start_cid + offset
                    if cid > end_cid:
                        break
                    try:
                        mapping[cid] = decode_unicode_hex(h)
                    except (ValueError, UnicodeDecodeError):
                        continue
                idx = close_pos + 1
            else:
                m3 = _HEX_RE.search(body, pos)
                if not m3:
                    break
                if end_cid >= start_cid:
                    try:
                        mapping.update(_expand_bfrange_target(start_cid, end_cid, m3.group(1)))
                    except (ValueError, UnicodeDecodeError):
                        pass
                idx = m3.end()

    return mapping


def _tokenize_cmap_body(body: str) -> list[tuple[str, str]]:
    """Tokenize a CMap section body into `('hex'|'lbrack'|'rbrack'|'other', value)` pairs."""
    tokens: list[tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(body):
        if m.group(1) is not None:
            tokens.append(("hex", m.group(1)))
        elif m.group(2) is not None:
            tokens.append(("lbrack", "["))
        elif m.group(3) is not None:
            tokens.append(("rbrack", "]"))
        else:
            tokens.append(("other", m.group(4)))
    return tokens


def validate_tounicode_cmap(cmap_text: str) -> list[str]:
    """Validate a ToUnicode CMap string against ISO 32000-1 §9.10.3 & Adobe TN #5411.

    Uses token-based scanning (stripping `%` comments and supporting multi-line arrays and
    multi-entry lines) so compliant third-party PDFs do not produce false positives.
    Returns an empty list if strictly valid; otherwise a list of human-readable violation strings.
    """
    errors: list[str] = []

    try:
        cmap_text.encode("ascii")
    except UnicodeEncodeError:
        errors.append("CMap stream must be pure ASCII")

    stripped = _COMMENT_RE.sub("", cmap_text)

    if "begincmap" not in stripped or "endcmap" not in stripped:
        errors.append("Missing begincmap/endcmap markers")

    if "/CMapType 2 def" not in stripped:
        errors.append("Missing '/CMapType 2 def'")

    if "CMapName currentdict /CMap defineresource pop" not in stripped:
        errors.append("Footer must use 'CMapName currentdict /CMap defineresource pop'")

    cs_blocks = _CODESPACE_BLOCK_RE.findall(stripped)
    if len(cs_blocks) != 1:
        errors.append(f"Expected exactly 1 codespacerange block, found {len(cs_blocks)}")
    else:
        cs_count_str, cs_body = cs_blocks[0]
        cs_hex = [h.upper() for h in _HEX_RE.findall(cs_body)]
        if int(cs_count_str) != 1 or cs_hex != ["0000", "FFFF"]:
            errors.append(f"Codespacerange must be exactly 1 entry <0000> <FFFF>, got {cs_count_str}: {cs_hex}")

    seen_cids: set[int] = set()
    first_bfrange_pos = stripped.find("beginbfrange")
    last_bfchar_pos = stripped.rfind("beginbfchar")
    if first_bfrange_pos != -1 and last_bfchar_pos > first_bfrange_pos:
        errors.append("All beginbfchar blocks must precede beginbfrange blocks (Adobe TN #5411)")

    for m in _BFCHAR_BLOCK_RE.finditer(stripped):
        declared = int(m.group(1))
        if not (1 <= declared <= MAX_ENTRIES_PER_BLOCK):
            errors.append(f"beginbfchar declared count {declared} out of range 1..{MAX_ENTRIES_PER_BLOCK}")
        tokens = _tokenize_cmap_body(m.group(2))
        entry_count = 0
        idx = 0
        while idx < len(tokens):
            if idx + 1 >= len(tokens) or tokens[idx][0] != "hex" or tokens[idx + 1][0] != "hex":
                errors.append(f"Malformed bfchar entry near token {tokens[idx][1]!r}")
                break
            cid_hex = tokens[idx][1]
            dst_hex = tokens[idx + 1][1]
            idx += 2
            entry_count += 1
            if len(cid_hex) != 4:
                errors.append(f"bfchar CID must be 4 hex digits: <{cid_hex}>")
            cid = int(cid_hex, 16)
            if cid in seen_cids:
                errors.append(f"Duplicate CID mapping for <{cid_hex}>")
            seen_cids.add(cid)
            try:
                decode_unicode_hex(dst_hex)
            except (ValueError, UnicodeDecodeError) as exc:
                errors.append(f"Invalid bfchar UTF-16BE destination for <{cid_hex}>: {exc}")
        if declared != entry_count:
            errors.append(f"beginbfchar declared {declared} entries but found {entry_count}")

    for m in _BFRANGE_BLOCK_RE.finditer(stripped):
        declared = int(m.group(1))
        if not (1 <= declared <= MAX_ENTRIES_PER_BLOCK):
            errors.append(f"beginbfrange declared count {declared} out of range 1..{MAX_ENTRIES_PER_BLOCK}")
        tokens = _tokenize_cmap_body(m.group(2))
        entry_count = 0
        idx = 0
        while idx < len(tokens):
            if idx + 2 >= len(tokens) or tokens[idx][0] != "hex" or tokens[idx + 1][0] != "hex":
                errors.append(f"Malformed bfrange entry near token {tokens[idx][1]!r}")
                break
            c1_hex = tokens[idx][1]
            c2_hex = tokens[idx + 1][1]
            idx += 2
            entry_count += 1

            if len(c1_hex) != 4 or len(c2_hex) != 4:
                errors.append(f"bfrange CIDs must be 4 hex digits: <{c1_hex}> <{c2_hex}>")
            c1, c2 = int(c1_hex, 16), int(c2_hex, 16)
            if c2 < c1:
                errors.append(f"bfrange end < start: <{c1_hex}> <{c2_hex}>")
            if (c1 >> 8) != (c2 >> 8):
                errors.append(f"bfrange crosses CID first-byte (high-byte) boundary: <{c1_hex}> <{c2_hex}>")

            if tokens[idx][0] == "hex":
                dst_hex = tokens[idx][1]
                idx += 1
                try:
                    start_text = decode_unicode_hex(dst_hex)
                except (ValueError, UnicodeDecodeError) as exc:
                    errors.append(f"Invalid bfrange UTF-16BE destination <{dst_hex}>: {exc}")
                    continue
                if len(start_text) != 1 and c2 > c1:
                    errors.append(f"bfrange destination must not be a multi-char ligature: <{c1_hex}> <{c2_hex}> <{dst_hex}>")
                dst_bytes = bytes.fromhex(dst_hex)
                if c2 >= c1 and dst_bytes[-1] + (c2 - c1) > 0xFF:
                    errors.append(f"bfrange destination overflows last-byte 0xFF boundary: <{c1_hex}> <{c2_hex}> <{dst_hex}>")
                if c2 >= c1:
                    for cid in range(c1, c2 + 1):
                        if cid in seen_cids:
                            errors.append(f"Overlapping/duplicate CID mapping <{cid:04X}> in <{c1_hex}> <{c2_hex}>")
                            break
                        seen_cids.add(cid)
            elif tokens[idx][0] == "lbrack":
                idx += 1
                arr_hex: list[str] = []
                closed = False
                while idx < len(tokens):
                    if tokens[idx][0] == "rbrack":
                        idx += 1
                        closed = True
                        break
                    if tokens[idx][0] != "hex":
                        errors.append(f"Malformed token inside bfrange array: {tokens[idx][1]!r}")
                        idx += 1
                        continue
                    arr_hex.append(tokens[idx][1])
                    idx += 1
                if not closed:
                    errors.append(f"Unclosed bfrange array for <{c1_hex}> <{c2_hex}>")
                    break
                if c2 >= c1 and len(arr_hex) != (c2 - c1 + 1):
                    errors.append(f"bfrange array length mismatch for <{c1_hex}> <{c2_hex}>")
                for offset, h in enumerate(arr_hex):
                    cid = c1 + offset
                    if cid in seen_cids:
                        errors.append(f"Overlapping/duplicate CID mapping <{cid:04X}> in <{c1_hex}> <{c2_hex}>")
                        break
                    seen_cids.add(cid)
                    try:
                        decode_unicode_hex(h)
                    except (ValueError, UnicodeDecodeError) as exc:
                        errors.append(f"Invalid bfrange array UTF-16BE entry <{h}>: {exc}")
            else:
                errors.append(f"Malformed bfrange target token {tokens[idx][1]!r} after <{c1_hex}> <{c2_hex}>")
                break
        if declared != entry_count:
            errors.append(f"beginbfrange declared {declared} entries but found {entry_count}")

    if not seen_cids:
        errors.append("CMap contains zero character mappings")

    return errors
