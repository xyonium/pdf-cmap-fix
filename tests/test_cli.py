from __future__ import annotations

import json
from pathlib import Path

from conftest import make_embedded_type0_pdf

from pdf_cmap_fix.cache import MappingCache
from pdf_cmap_fix.cli import main
from pdf_cmap_fix.cmap import generate_tounicode_cmap


def test_cli_check_exit_codes_0_2_1(tmp_path: Path, capsys):
    missing_pdf, cids = make_embedded_type0_pdf(tmp_path / "missing.pdf", text="AB", tounicode_mode="strip")
    cmap = generate_tounicode_cmap({cids[0]: "A", cids[1]: "B"}).encode("ascii")
    ok_pdf, _ = make_embedded_type0_pdf(tmp_path / "ok.pdf", text="AB", tounicode_mode="custom", custom_tounicode=cmap)

    assert main(["check", str(ok_pdf)]) == 0
    capsys.readouterr()  # clear previous stdout
    assert main(["check", str(missing_pdf), "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["needs_fix"] is True
    assert main(["check", str(tmp_path / "nonexistent.pdf")]) == 1

    # Directory precedence: mixed ok + missing -> 2
    assert main(["check", str(tmp_path)]) == 2


def test_cli_fix_requires_output_or_in_place(tmp_path: Path):
    missing_pdf, _ = make_embedded_type0_pdf(tmp_path / "missing.pdf", text="AB", tounicode_mode="strip")
    assert main(["fix", str(missing_pdf), "--no-ocr"]) == 1


def test_cli_fix_in_place_and_cache_subcommands(tmp_path: Path, capsys):
    missing_pdf, cids = make_embedded_type0_pdf(tmp_path / "missing.pdf", text="AB", tounicode_mode="strip")
    v1_path = tmp_path / "v1.json"
    v1_path.write_text(
        json.dumps({"SourceHanSansCN": {str(cids[0]): [ord("A")] * 8, str(cids[1]): [ord("B")] * 8}}),
        encoding="utf-8",
    )
    v2_path = tmp_path / "v2.json"

    assert main(["cache", "migrate", str(v1_path), str(v2_path)]) == 0
    assert main(["cache", "stats", str(v2_path)]) == 0
    assert main(["cache", "compact", str(v2_path)]) == 0
    assert main(["cache", "set", str(v2_path), "SourceHanSansCN", str(cids[0]), "A"]) == 0

    cmap_dir = tmp_path / "cmaps"
    assert main(["cache", "export-cmap", str(v2_path), "-o", str(cmap_dir)]) == 0
    assert (cmap_dir / "SourceHanSansCN.cmap").exists()

    # In-place fix using migrated cache (zero OCR needed)
    assert main(["fix", str(missing_pdf), "--in-place", "--cache", str(v2_path), "--if-needed"]) == 0
    assert main(["check", str(missing_pdf)]) == 0


def test_cli_cache_verify_help_and_missing_path_handling(tmp_path: Path):
    assert main(["cache", "verify", str(tmp_path / "missing.json"), "--pdfs", str(tmp_path)]) == 1
    cache_path = tmp_path / "c.json"
    c = MappingCache()
    c.set_mapping("SourceHanSansCN", 10, "A", lock=True)
    c.save(cache_path)
    pdf_path, _ = make_embedded_type0_pdf(tmp_path / "sample.pdf", text="AB", tounicode_mode="strip")
    # All cached entries already locked -> exits 0 cleanly without needing API key!
    assert main(["cache", "verify", str(cache_path), "--pdfs", str(pdf_path)]) == 0
