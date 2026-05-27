from backend.generator.svg_critic import CriticConfig
from backend.orchestrator.svg_executor import (
    _generation_static_report,
    _optimize_figure_choices,
    _validate_paper_figure_geometry,
)


def test_paper_figure_geometry_rejects_off_canvas_overcrop() -> None:
    svg = """
    <svg viewBox="0 0 1280 720">
      <defs>
        <clipPath id="leftPanelClip"><rect x="540" y="100" width="680" height="360"/></clipPath>
      </defs>
      <image href="../sources/images/fig_001_p1_vec.png" x="540" y="100"
             width="1361" height="724" clip-path="url(#leftPanelClip)" />
    </svg>
    """
    report = _validate_paper_figure_geometry(
        svg,
        allowed_figures=[
            {
                "path": "../sources/images/fig_001_p1_vec.png",
                "natural_width": 1361,
                "natural_height": 724,
            }
        ],
    )

    assert not report.passed
    rules = {violation.rule for violation in report.violations}
    assert "paper_figure_out_of_bounds" in rules
    assert "paper_figure_overcropped" in rules


def test_generation_static_report_runs_figure_safety_without_general_critic() -> None:
    svg = """
    <svg viewBox="0 0 1280 720">
      <image href="../sources/images/fig_001_p1_vec.png" x="40" y="330"
             width="1199" height="638" />
    </svg>
    """
    report = _generation_static_report(
        svg,
        critic_config=CriticConfig(),
        allowed_figures=[
            {
                "path": "../sources/images/fig_001_p1_vec.png",
                "natural_width": 1361,
                "natural_height": 724,
            }
        ],
        used_paper_figures={},
        required_icon=None,
        include_general_svg_checks=False,
    )

    assert not report.passed
    assert any(v.rule == "paper_figure_out_of_bounds" for v in report.violations)


def test_optimize_figure_choices_prefers_slide_friendly_same_caption_table() -> None:
    current = {
        "path": "../sources/images/fig_038_p58_table.png",
        "caption": "Table 14 | DeepSeek-V4-Pro vs. Claude-Opus-4.5",
        "page_number": 58,
        "natural_width": 1325,
        "natural_height": 588,
        "aspect_ratio": 2.25,
        "quality_score": 0.92,
    }
    compact = {
        "path": "../sources/images/fig_039_p58_table.png",
        "caption": "Table 14 | DeepSeek-V4-Pro vs. Claude-Opus-4.5",
        "page_number": 58,
        "natural_width": 1108,
        "natural_height": 240,
        "aspect_ratio": 4.62,
        "quality_score": 0.92,
    }

    optimized, notes = _optimize_figure_choices([current], [current, compact])

    assert optimized[0]["path"].endswith("fig_039_p58_table.png")
    assert notes
