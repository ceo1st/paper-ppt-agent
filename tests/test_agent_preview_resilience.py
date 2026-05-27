from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.schemas import ResearchConfig
from backend.orchestrator import agent_pipeline


@pytest.mark.asyncio
async def test_agent_start_waits_for_runtime_without_blocking_loop(tmp_path: Path, monkeypatch) -> None:
    session = agent_pipeline.PersistentAgentSession(tmp_path, "claude_code", {})

    def delayed_worker() -> None:
        time.sleep(0.15)
        session._ready.set()

    monkeypatch.setattr(session, "_worker", delayed_worker)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        deadline = asyncio.get_running_loop().time() + 0.12
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
            ticks += 1

    await asyncio.gather(session.start("generate"), ticker())

    assert ticks >= 3


def test_agent_svg_preview_repairs_and_embeds_project_relative_svg_asset(tmp_path: Path) -> None:
    project_dir = tmp_path
    svg_dir = project_dir / "svg_output"
    icon_dir = project_dir / "icons"
    svg_dir.mkdir()
    icon_dir.mkdir()
    (icon_dir / "status.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
        '<circle cx="8" cy="8" r="6" fill="#2563eb"/></svg>',
        encoding="utf-8",
    )
    slide = svg_dir / "slide_001.svg"
    slide.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
        '<text x="40" y="80">A & B</text>'
        '<image href="icons/status.svg" x="40" y="100" width="32" height="32"/>'
        "</svg>",
        encoding="utf-8",
    )

    _, preview = agent_pipeline._prepare_agent_svg_preview(slide, project_dir)

    assert "A &amp; B" in preview
    assert "data:image/svg+xml;base64," in preview


def test_agent_icon_policy_requires_purposeful_consistent_icons() -> None:
    policy = agent_pipeline._agent_icon_policy()

    assert policy["use_icons_only_when_they_clarify"] is True
    assert policy["density_limits"]["maximum_icons_per_slide_unless_diagram"] == 4
    assert any("Reuse the same icon" in item for item in policy["consistency_rules"])
    assert any("Avoid per-icon rainbow colors" in item for item in policy["color_rules"])


def test_agent_research_policy_keeps_query_choice_with_agent() -> None:
    policy = agent_pipeline._agent_research_policy(
        {"web_search_provider": "tavily", "max_results_per_source": 8}
    )

    assert policy["query_owner"] == "agent"
    assert "backend does not preselect" in policy["query_rule"]


def test_empty_agent_output_message_reports_stray_svg(tmp_path: Path) -> None:
    stray_dir = tmp_path / "nested"
    stray_dir.mkdir()
    (stray_dir / "slide_001.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        encoding="utf-8",
    )

    message = agent_pipeline._empty_agent_output_message(tmp_path)

    assert "completed without producing SVG slides" in message
    assert "nested/slide_001.svg" in message


def _write_research_gated_task(project_dir: Path) -> None:
    (project_dir / "agent_task.json").write_text(
        json.dumps(
            {
                "agent_policy": {
                    "allow_external_research": True,
                    "allow_deep_research": True,
                    "research_gate": {
                        "enforced": True,
                        "external_required_outputs": [
                            "research/raw_external_results.json",
                            "research/sources.json",
                            "research/brief.md",
                        ],
                        "deep_required_outputs": [
                            "research/deep/notes_index.json",
                            "research/deep/brief.md",
                        ],
                    },
                }
            }
        ),
        encoding="utf-8",
    )


def test_agent_research_gate_blocks_authoring_before_skill_outputs(tmp_path: Path) -> None:
    _write_research_gated_task(tmp_path)

    reason = agent_pipeline._agent_research_gate_block_reason(
        tmp_path,
        "Write",
        {"file_path": str(tmp_path / "design_spec.md")},
    )
    assert reason is not None
    assert "research/raw_external_results.json" in reason
    assert agent_pipeline._agent_research_gate_block_reason(
        tmp_path,
        "Write",
        {"file_path": str(tmp_path / "research" / "brief.md")},
    ) is None
    assert agent_pipeline._agent_research_gate_block_reason(
        tmp_path,
        "Write",
        {"file_path": str(tmp_path / "research" / "deep" / "notes" / "method.md")},
    ) is None
    assert agent_pipeline._agent_research_gate_block_reason(
        tmp_path,
        "Bash",
        {"command": "python make_slides.py --out svg_output"},
    ) is not None


def test_agent_research_gate_points_to_remaining_phase(tmp_path: Path) -> None:
    _write_research_gated_task(tmp_path)
    for relative, content in (
        ("research/raw_external_results.json", '{"queries": ["paper method"], "results": [], "errors": []}'),
        ("research/sources.json", '{"sources": [], "limitations": ["No relevant external result found."]}'),
        ("research/brief.md", "External search attempted; no directly relevant sources found."),
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    reason = agent_pipeline._agent_research_gate_block_reason(
        tmp_path,
        "Write",
        {"file_path": str(tmp_path / "design_spec.md")},
    )

    assert reason is not None
    assert "External research: complete" in reason
    assert "do not repeat searches just to increase result counts" in reason
    assert "Deep research: missing" in reason
    assert "research/deep/notes_index.json" in reason


def test_agent_research_gate_rejects_outputs_written_before_research(tmp_path: Path) -> None:
    _write_research_gated_task(tmp_path)
    (tmp_path / "manuscript.md").write_text("too early", encoding="utf-8")
    time.sleep(0.01)
    for relative, content in (
        ("research/raw_external_results.json", '{"queries": ["paper method"], "results": [], "errors": []}'),
        ("research/sources.json", '{"sources": [], "limitations": ["No relevant external result found."]}'),
        ("research/brief.md", "brief"),
        ("research/deep/notes_index.json", '{"notes": []}'),
        ("research/deep/brief.md", "deep brief"),
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    with pytest.raises(RuntimeError, match="before required research finished"):
        agent_pipeline._validate_agent_research_gate(tmp_path)


def test_agent_report_accepts_documented_external_research_limitation(tmp_path: Path) -> None:
    _write_research_gated_task(tmp_path)
    for relative, content in (
        ("research/raw_external_results.json", '{"queries": ["new paper"], "results": [], "errors": []}'),
        ("research/sources.json", '{"sources": [], "limitations": ["The paper is too new for direct external matches."]}'),
        ("research/brief.md", "External research completed with no directly relevant sources."),
        ("research/deep/notes_index.json", '{"notes": []}'),
        ("research/deep/brief.md", "Deep research completed."),
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    report_path = tmp_path / "agent_report.json"
    report_path.write_text(
        json.dumps(
            {
                "status": "complete",
                "slides": 1,
                "external_research_used": False,
                "deep_research_used": True,
                "subagents": [{"name": "deep-reader", "used": False, "skip_reason": "Task tool unavailable"}],
            }
        ),
        encoding="utf-8",
    )

    agent_pipeline._validate_agent_report(tmp_path, report_path)


def test_agent_task_does_not_persist_research_api_keys(tmp_path: Path) -> None:
    request = SimpleNamespace(
        canvas_format="ppt169",
        source_type="pdf",
        language="zh",
        num_pages=None,
        detail_level="normal",
        style="academic",
        template_id=None,
        instruction="",
        style_overrides=None,
        research_config=ResearchConfig(
            semantic_scholar_api_key="semantic-secret",
            tavily_api_key="tavily-secret",
            serpapi_key="serp-secret",
        ),
    )

    task = agent_pipeline._build_agent_task(
        tmp_path,
        tmp_path / "sources" / "paper.pdf",
        request,
        {"allow_external_research": True},
    )
    serialized = json.dumps(task)

    assert "semantic-secret" not in serialized
    assert "tavily-secret" not in serialized
    assert "serp-secret" not in serialized
    assert agent_pipeline._agent_research_env_from_config(request.research_config) == {
        "SEMANTIC_SCHOLAR_API_KEY": "semantic-secret",
        "TAVILY_API_KEY": "tavily-secret",
        "SERPAPI_KEY": "serp-secret",
    }
