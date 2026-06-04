from __future__ import annotations

from pathlib import Path

from backend.orchestrator.manuscript import normalize_manuscript_slide_delimiters
from backend.orchestrator.provider_memory import build_provider_memory, build_slide_contexts, retrieve_evidence_cards
from backend.parser.paper_model import ParsedPaper, PaperFigure, PaperSection


def _paper(tmp_path: Path) -> ParsedPaper:
    fig = PaperFigure(
        path=tmp_path / "fig_001_p1.png",
        caption="Figure 1: benchmark accuracy improves with sparse attention.",
        page_number=1,
        bbox=(80.0, 120.0, 480.0, 320.0),
        extraction_method="caption_region",
        natural_width=800,
        natural_height=400,
        quality_score=0.92,
    )
    return ParsedPaper(
        title="Fast Long Context Models",
        authors=["A. Researcher"],
        abstract="We study long-context inference and reduce KV cache while improving benchmark accuracy.",
        sections=[
            PaperSection(
                title="Method",
                level=1,
                content=(
                    "The architecture combines sparse attention with cache compression. "
                    "It reduces KV cache by 60% and lowers FLOPs by 35% on 128K-token inference. "
                    "A routing module selects high-value tokens for exact attention."
                ),
                figures=[fig],
            ),
            PaperSection(
                title="Experiments",
                level=1,
                content=(
                    "On LongBench, the model reaches 74.2 accuracy compared with a 68.1 baseline. "
                    "Ablation experiments show the routing module contributes 3.4 points."
                ),
            ),
        ],
    )


def test_provider_memory_compacts_full_paper_and_keeps_evidence(tmp_path: Path) -> None:
    memory = build_provider_memory(_paper(tmp_path))

    assert "High-Value Evidence Cards" in memory.compact_markdown
    assert len(memory.compact_markdown) < len(_paper(tmp_path).to_markdown()) + 2000
    assert any("60%" in card.text for card in memory.evidence_cards)
    assert memory.figures[0].id == "fig_001_p1"
    assert memory.figures[0].bbox == (80.0, 120.0, 480.0, 320.0)
    assert memory.figures[0].extraction_method == "caption_region"
    assert "caption_region" in memory.compact_markdown


def test_slide_context_retrieves_relevant_cards(tmp_path: Path) -> None:
    memory = build_provider_memory(_paper(tmp_path))
    manuscript = normalize_manuscript_slide_delimiters(
        """
<!-- page_type: content -->
## KV Cache Efficiency

- Explain sparse attention and KV cache reduction.
[[FIG:fig_001_p1]]
        """
    )

    contexts = build_slide_contexts(manuscript, memory)

    assert 1 in contexts
    assert "Provider Slide Working Memory" in contexts[1]
    assert "KV cache" in contexts[1] or "cache" in contexts[1]
    assert "fig_001_p1" in contexts[1]
    assert "source=caption_region" in contexts[1]


def test_evidence_retrieval_handles_non_cs_academic_language() -> None:
    paper = ParsedPaper(
        title="Classroom Belonging and Relational Security",
        authors=["B. Scholar"],
        abstract="We examine how relational security shapes classroom belonging.",
        sections=[
            PaperSection(
                title="Theoretical Framework",
                level=1,
                content=(
                    "Attachment theory describes how caregiver responsiveness shapes internal working models "
                    "of security and exploration in early childhood. The framework connects relational "
                    "expectations with later emotion regulation."
                ),
            ),
            PaperSection(
                title="Qualitative Findings",
                level=1,
                content=(
                    "In a qualitative interview study with 42 teachers and caregivers, participants described "
                    "three recurring themes: trust-building routines, classroom transitions, and family communication. "
                    "Survey responses showed perceived belonging increased by 18% after the intervention."
                ),
            ),
        ],
    )
    memory = build_provider_memory(paper)

    cards = retrieve_evidence_cards(
        "teacher interview themes and caregiver communication",
        memory,
        limit=3,
    )

    assert cards
    assert cards[0].section == "Qualitative Findings"
    assert "recurring themes" in cards[0].text
