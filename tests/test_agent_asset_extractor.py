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
