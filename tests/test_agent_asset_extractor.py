from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

from PIL import Image


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "agent_skills"
    / "paper-ppt-generate"
    / "scripts"
    / "extract_paper_assets.py"
)


def _load_extractor():
    spec = importlib.util.spec_from_file_location("agent_asset_extractor", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_latex_zip_extractor_indexes_includegraphics(tmp_path: Path) -> None:
    extractor = _load_extractor()
    src = tmp_path / "src"
    figures = src / "figures"
    figures.mkdir(parents=True)
    Image.new("RGB", (240, 120), "white").save(figures / "overview.png")
    (src / "main.tex").write_text(
        r"""
\documentclass{article}
\graphicspath{{figures/}}
\title{TeX Asset Paper}
\begin{document}
\begin{abstract}We test archive figure indexing.\end{abstract}
\section{Method}
\begin{figure}
\includegraphics[width=.8\linewidth]{overview}
\caption{Architecture overview with two modules.}
\label{fig:overview}
\end{figure}
\end{document}
""",
        encoding="utf-8",
    )
    archive = tmp_path / "paper.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in src.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(src).as_posix())

    result = extractor.extract_latex_source(archive, tmp_path / "source_assets", tmp_path / "source_assets" / "images")
    records = [record for record in result["figures"] if record.available]

    assert len(records) == 1
    assert records[0].caption == "Architecture overview with two modules."
    assert records[0].href.startswith("../source_assets/images/")
    assert records[0].natural_width == 240
    assert records[0].natural_height == 120
    assert "[[FIG:" in result["paper_markdown"]


def test_pdf_extractor_records_caption_page_and_href(tmp_path: Path) -> None:
    try:
        import fitz
    except Exception:
        return

    extractor = _load_extractor()
    image_path = tmp_path / "chart.png"
    Image.new("RGB", (320, 180), "white").save(image_path)
    pdf_path = tmp_path / "paper.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "A Tiny Paper About Asset Extraction", fontsize=18)
    page.insert_image(fitz.Rect(120, 220, 492, 430), filename=str(image_path))
    page.insert_text(
        (120, 450),
        "Figure 1. A synthetic upward trend chart used for the method overview.",
        fontsize=10,
    )
    doc.save(pdf_path)
    doc.close()

    result = extractor.extract_pdf(pdf_path, tmp_path / "source_assets", tmp_path / "source_assets" / "images")
    records = [record for record in result["figures"] if record.available]

    assert len(records) >= 1
    assert records[0].page_number == 1
    assert "Figure 1" in records[0].caption
    assert records[0].href.startswith("../source_assets/images/")
    assert records[0].natural_width > 0
    assert "[[FIG:" in result["paper_markdown"]


def test_pdf_caption_crop_separates_stacked_figures() -> None:
    try:
        import fitz
    except Exception:
        return

    extractor = _load_extractor()
    captions = [
        {"text": "Figure 1. Upper trend.", "rect": fitz.Rect(100, 200, 300, 218)},
        {"text": "Figure 2. Lower trend.", "rect": fitz.Rect(100, 450, 300, 468)},
    ]
    clip = extractor._caption_region_clip(
        fitz.Rect(0, 0, 600, 800),
        captions[1]["rect"],
        [{"text": item["text"], "rect": item["rect"], "char_count": len(item["text"])} for item in captions],
        captions,
    )

    assert clip is not None
    assert clip.y0 >= captions[0]["rect"].y1 + 10


def test_pdf_caption_crop_expands_sparse_full_page_figure() -> None:
    try:
        import fitz
    except Exception:
        return

    extractor = _load_extractor()
    caption = {"text": "Figure 5. Annual distribution.", "rect": fitz.Rect(100, 590, 350, 610)}
    blocks = [
        {"text": "Journal header", "rect": fitz.Rect(40, 60, 500, 84), "char_count": 14},
        {"text": caption["text"], "rect": caption["rect"], "char_count": len(caption["text"])},
    ]
    clip = extractor._caption_region_clip(
        fitz.Rect(0, 0, 600, 800),
        caption["rect"],
        blocks,
        [caption],
    )

    assert clip is not None
    assert clip.y0 == 92
    assert clip.y0 < caption["rect"].y0 - extractor.PDF_CAPTION_MAX_GAP


def test_pdf_table_region_drops_caption_and_preceding_content() -> None:
    try:
        import fitz
    except Exception:
        return

    extractor = _load_extractor()

    class _Table:
        bbox = (50, 90, 500, 380)

    class _Finder:
        tables = [_Table()]

    class _Page:
        rect = fitz.Rect(0, 0, 600, 800)

        @staticmethod
        def find_tables():
            return _Finder()

    caption = {"text": "Table 1. Satellite imagery.", "rect": fitz.Rect(160, 120, 360, 140)}
    regions = extractor._pdf_table_regions(_Page(), [caption])

    assert len(regions) == 1
    clip, selected_caption = regions[0]
    assert selected_caption == caption["text"]
    assert clip.y0 >= caption["rect"].y1 + 12


def test_pdf_caption_normalizes_joined_chinese_index_and_year() -> None:
    extractor = _load_extractor()

    assert extractor._normalize_pdf_caption("图 ５２０００ ２０１５ 年频率分布") == "图 ５ ２０００ ２０１５ 年频率分布"
