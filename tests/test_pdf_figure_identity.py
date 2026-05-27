from __future__ import annotations

import fitz

from backend.parser.pdf_parser import PDFParser, _CAPTION_RE


def test_parser_recognizes_chinese_figure_and_table_captions() -> None:
    assert _CAPTION_RE.search("图 ５ 2000-2015 年度分布")
    assert _CAPTION_RE.search("表 ２ 年度统计摘要")
    assert PDFParser._normalize_caption_text("图 ５２０００ ２０１５ 年度分布") == "图 ５ ２０００ ２０１５ 年度分布"


def test_parser_assigns_caption_above_table_region() -> None:
    parser = PDFParser()
    table = fitz.Rect(80, 180, 480, 420)
    captions = {
        "table": ("表 １ 卫星影像数据", fitz.Rect(160, 145, 360, 165)),
        "figure": ("图 ２ 面积变化", fitz.Rect(160, 500, 360, 520)),
    }

    assert parser._nearest_caption(table, captions) == "表 １ 卫星影像数据"


def test_parser_expands_sparse_long_figure_but_separates_stacked_figures() -> None:
    parser = PDFParser()
    page_rect = fitz.Rect(0, 0, 600, 800)
    header = {"text": "Journal header", "rect": fitz.Rect(40, 60, 500, 84), "char_count": 14}
    long_caption = fitz.Rect(100, 590, 350, 610)
    expanded = parser._normalize_caption_clip(
        fitz.Rect(70, 180, 500, 586),
        page_rect,
        long_caption,
        [header],
        {"long": ("图 ５ 年度频率分布", long_caption)},
    )
    assert expanded.y0 == 92
    assert expanded.x0 == 24
    assert expanded.x1 == 576

    upper_caption = fitz.Rect(100, 200, 350, 218)
    lower_caption = fitz.Rect(100, 450, 350, 468)
    separated = parser._normalize_caption_clip(
        fitz.Rect(70, 160, 500, 446),
        page_rect,
        lower_caption,
        [header],
        {
            "upper": ("图 ２ 面积变化", upper_caption),
            "lower": ("图 ３ 覆盖比例", lower_caption),
        },
    )
    assert separated.y0 >= upper_caption.y1 + 10
    assert separated.x0 == 24
    assert separated.x1 == 576
