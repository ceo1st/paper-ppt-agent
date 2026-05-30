from pathlib import Path

from backend.api.endpoints import preview


def test_renumber_svg_slide_assets_keeps_sidecars_aligned(tmp_path: Path) -> None:
    project_dir = tmp_path
    svg_dir = project_dir / "svg_final"
    doc_dir = project_dir / "slide_documents"
    notes_dir = project_dir / "notes"
    svg_dir.mkdir()
    doc_dir.mkdir()
    notes_dir.mkdir()

    first = svg_dir / "01_title.svg"
    third = svg_dir / "03_summary.svg"
    first.write_text("<svg></svg>", encoding="utf-8")
    third.write_text("<svg></svg>", encoding="utf-8")
    (doc_dir / "03_summary.json").write_text('{"title":"summary"}', encoding="utf-8")
    (notes_dir / "03_summary.md").write_text("speaker notes", encoding="utf-8")

    preview._renumber_svg_slide_assets(project_dir, [first, third])

    assert first.exists()
    assert not third.exists()
    assert (svg_dir / "02_summary.svg").exists()
    assert not (doc_dir / "03_summary.json").exists()
    assert (doc_dir / "02_summary.json").read_text(encoding="utf-8") == '{"title":"summary"}'
    assert not (notes_dir / "03_summary.md").exists()
    assert (notes_dir / "02_summary.md").read_text(encoding="utf-8") == "speaker notes"
