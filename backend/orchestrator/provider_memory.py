"""Provider-mode working memory for paper-to-deck generation.

The provider pipeline is intentionally stateless at the API boundary, but the
deck generation task is not. This module builds a small, file-backed working
memory from the parsed paper so later LLM calls can use scoped evidence instead
of repeatedly carrying the full paper text.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from backend.orchestrator.deck_plan import DeckPlan
from backend.orchestrator.manuscript import extract_page_type, page_title, split_manuscript_pages
from backend.parser.paper_model import ParsedPaper, PaperFigure, PaperSection

MAX_COMPACT_PAPER_CHARS = 36000
MAX_SECTION_BRIEF_CHARS = 1400
MAX_CARD_TEXT_CHARS = 520
MAX_EVIDENCE_CARDS = 180
SLIDE_CONTEXT_CARD_COUNT = 7

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,}|[\u4e00-\u9fff]{2,}")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？.!?])\s+|\n{2,}")
_EVIDENCE_RE = re.compile(
    r"(?i)(\d|%|table|figure|fig\.?|equation|formula|experiment|benchmark|"
    r"dataset|baseline|sota|accuracy|pass@|rating|flops|cache|latency|"
    r"ablation|parameter|token|training|inference|图|表|公式|实验|指标|"
    r"数据集|基线|消融|参数|训练|推理|精度|缓存|延迟)"
)
_METHOD_RE = re.compile(
    r"(?i)(method|architecture|framework|algorithm|module|attention|optimizer|"
    r"training|pipeline|mechanism|方法|架构|算法|模块|注意力|优化器|机制|流程)"
)


@dataclass(frozen=True)
class EvidenceCard:
    id: str
    section: str
    kind: str
    text: str
    score: float
    figure_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class FigureMemory:
    id: str
    path: str
    caption: str
    page_number: int | None
    natural_width: int
    natural_height: int
    quality_score: float


@dataclass(frozen=True)
class SectionMemory:
    title: str
    level: int
    brief: str
    card_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProviderMemory:
    title: str
    authors: tuple[str, ...]
    abstract: str
    compact_markdown: str
    sections: tuple[SectionMemory, ...]
    evidence_cards: tuple[EvidenceCard, ...]
    figures: tuple[FigureMemory, ...]


def build_provider_memory(paper: ParsedPaper) -> ProviderMemory:
    """Build deterministic, compact memory from a parsed paper."""
    cards: list[EvidenceCard] = []
    section_memories: list[SectionMemory] = []

    for section_index, section in enumerate(paper.sections, start=1):
        section_cards = _cards_for_section(section, section_index)
        cards.extend(section_cards)
        brief = _section_brief(section, section_cards)
        section_memories.append(
            SectionMemory(
                title=section.title,
                level=section.level,
                brief=brief,
                card_ids=tuple(card.id for card in section_cards),
            )
        )

    cards = sorted(cards, key=lambda card: card.score, reverse=True)[:MAX_EVIDENCE_CARDS]
    card_id_set = {card.id for card in cards}
    section_memories = [
        SectionMemory(
            title=section.title,
            level=section.level,
            brief=section.brief,
            card_ids=tuple(card_id for card_id in section.card_ids if card_id in card_id_set),
        )
        for section in section_memories
    ]

    figures = tuple(_figure_memory(fig) for fig in paper.all_figures() if paper._should_include_figure(fig))
    compact_markdown = _build_compact_markdown(
        paper,
        section_memories,
        cards,
        figures,
    )
    return ProviderMemory(
        title=paper.title,
        authors=tuple(paper.authors),
        abstract=_trim(paper.abstract, 2200),
        compact_markdown=compact_markdown,
        sections=tuple(section_memories),
        evidence_cards=tuple(cards),
        figures=figures,
    )


def save_provider_memory(memory: ProviderMemory, target_dir: Path) -> None:
    """Persist provider working memory for inspection and later reuse."""
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "compact_paper.md").write_text(memory.compact_markdown, encoding="utf-8")
    (target_dir / "evidence_cards.json").write_text(
        json.dumps([asdict(card) for card in memory.evidence_cards], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (target_dir / "figures.json").write_text(
        json.dumps([asdict(fig) for fig in memory.figures], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (target_dir / "working_state.md").write_text(_working_state(memory), encoding="utf-8")


def build_slide_contexts(
    manuscript: str,
    memory: ProviderMemory | None,
    deck_plan: DeckPlan | None = None,
) -> dict[int, str]:
    """Return compact evidence context for each manuscript page."""
    if memory is None:
        return {}
    pages = split_manuscript_pages(manuscript)
    contexts: dict[int, str] = {}
    plan_by_page = deck_plan.by_page if deck_plan is not None else {}
    for page_num, page in enumerate(pages, start=1):
        slide_plan = plan_by_page.get(page_num)
        query = "\n".join(
            part
            for part in (
                page_title(page),
                page,
                slide_plan.title if slide_plan else "",
                slide_plan.chapter_title if slide_plan else "",
            )
            if part
        )
        cards = retrieve_evidence_cards(query, memory, limit=SLIDE_CONTEXT_CARD_COUNT)
        figure_lines = _slide_figure_hints(page, memory)
        lines = [
            "## Provider Slide Working Memory",
            "",
            "Use this scoped memory as evidence grounding. Prefer these cards over re-inferring from the full paper.",
            f"- Slide title: {page_title(page)}",
            f"- Page type: {extract_page_type(page)}",
        ]
        if slide_plan:
            lines.extend(
                [
                    f"- Layout family: {slide_plan.layout_family}",
                    f"- Density target: {slide_plan.density}",
                ]
            )
        if cards:
            lines.append("")
            lines.append("### Relevant Evidence Cards")
            for card in cards:
                figure_note = f" figures={', '.join(card.figure_ids)}" if card.figure_ids else ""
                lines.append(f"- `{card.id}` [{card.kind}] {card.section}:{figure_note} {card.text}")
        if figure_lines:
            lines.append("")
            lines.append("### Figure Hints")
            lines.extend(figure_lines)
        contexts[page_num] = "\n".join(lines).strip()
    return contexts


def retrieve_evidence_cards(
    query: str,
    memory: ProviderMemory,
    *,
    limit: int = SLIDE_CONTEXT_CARD_COUNT,
) -> list[EvidenceCard]:
    query_terms = _terms(query)
    if not query_terms:
        return list(memory.evidence_cards[:limit])
    ranked: list[tuple[float, EvidenceCard]] = []
    for card in memory.evidence_cards:
        card_terms = _terms(f"{card.section} {card.kind} {card.text}")
        overlap = len(query_terms.intersection(card_terms))
        if overlap <= 0:
            score = card.score * 0.05
        else:
            score = overlap * 4.0 + min(card.score, 8.0)
        ranked.append((score, card))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [card for score, card in ranked[:limit] if score > 0]


def _cards_for_section(section: PaperSection, section_index: int) -> list[EvidenceCard]:
    figure_ids = tuple(fig.fig_id for fig in section.figures if fig.available)
    candidates: list[EvidenceCard] = []
    for sentence_index, sentence in enumerate(_candidate_sentences(section.content), start=1):
        score = _sentence_score(sentence)
        if score < 1.0 and sentence_index > 4:
            continue
        kind = _sentence_kind(sentence)
        candidates.append(
            EvidenceCard(
                id=f"s{section_index:02d}c{sentence_index:03d}",
                section=section.title,
                kind=kind,
                text=_trim(sentence, MAX_CARD_TEXT_CHARS),
                score=score,
                figure_ids=figure_ids[:4],
            )
        )
    for table_index, table in enumerate(section.tables, start=1):
        text = " ".join(part for part in (table.caption, table.markdown) if part).strip()
        if text:
            candidates.append(
                EvidenceCard(
                    id=f"s{section_index:02d}t{table_index:02d}",
                    section=section.title,
                    kind="table",
                    text=_trim(text, MAX_CARD_TEXT_CHARS),
                    score=9.0,
                    figure_ids=(),
                )
            )
    for eq_index, equation in enumerate(section.equations, start=1):
        if equation.strip():
            candidates.append(
                EvidenceCard(
                    id=f"s{section_index:02d}e{eq_index:02d}",
                    section=section.title,
                    kind="equation",
                    text=_trim(equation.strip(), MAX_CARD_TEXT_CHARS),
                    score=8.0,
                    figure_ids=(),
                )
            )
    return sorted(candidates, key=lambda card: card.score, reverse=True)[:10]


def _candidate_sentences(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned:
        return []
    parts = [part.strip() for part in _SENTENCE_SPLIT_RE.split(cleaned) if part.strip()]
    if len(parts) <= 1:
        parts = [cleaned[i : i + 420].strip() for i in range(0, len(cleaned), 420)]
    return [part for part in parts if len(part) >= 35][:60]


def _sentence_score(sentence: str) -> float:
    score = 0.0
    if _EVIDENCE_RE.search(sentence):
        score += 4.0
    if _METHOD_RE.search(sentence):
        score += 2.0
    score += min(len(re.findall(r"\d+(?:\.\d+)?%?", sentence)), 6) * 0.7
    if len(sentence) > 260:
        score += 0.6
    return score


def _sentence_kind(sentence: str) -> str:
    if re.search(r"(?i)(result|benchmark|accuracy|pass@|rating|experiment|实验|结果|指标)", sentence):
        return "result"
    if _METHOD_RE.search(sentence):
        return "method"
    if re.search(r"(?i)(limitation|future|assumption|局限|未来|假设)", sentence):
        return "limitation"
    if _EVIDENCE_RE.search(sentence):
        return "evidence"
    return "context"


def _section_brief(section: PaperSection, cards: Iterable[EvidenceCard]) -> str:
    card_texts = [card.text for card in cards][:4]
    if card_texts:
        return _trim(" ".join(card_texts), MAX_SECTION_BRIEF_CHARS)
    return _trim(section.content or "", MAX_SECTION_BRIEF_CHARS)


def _figure_memory(fig: PaperFigure) -> FigureMemory:
    caption = (fig.caption or "").replace("\n", " ").strip()
    return FigureMemory(
        id=fig.fig_id,
        path=str(fig.path),
        caption=_trim(caption, 360),
        page_number=fig.page_number,
        natural_width=fig.natural_width,
        natural_height=fig.natural_height,
        quality_score=float(fig.quality_score or 0.0),
    )


def _build_compact_markdown(
    paper: ParsedPaper,
    sections: list[SectionMemory],
    cards: list[EvidenceCard],
    figures: tuple[FigureMemory, ...],
) -> str:
    parts = [f"# {paper.title}", ""]
    if paper.authors:
        parts.extend([f"**Authors:** {', '.join(paper.authors)}", ""])
    if paper.abstract:
        parts.extend(["## Abstract", "", _trim(paper.abstract, 2200), ""])
    parts.extend(["## Section Working Memory", ""])
    for section in sections:
        prefix = "#" * min(max(section.level + 1, 2), 5)
        parts.extend([f"{prefix} {section.title}", "", section.brief, ""])
    parts.extend(["## High-Value Evidence Cards", ""])
    for card in cards[:80]:
        figure_note = f" figures={', '.join(card.figure_ids)}" if card.figure_ids else ""
        parts.append(f"- `{card.id}` [{card.kind}] {card.section}:{figure_note} {card.text}")
    if figures:
        parts.extend(["", "## Figure Manifest", ""])
        for fig in figures[:60]:
            size = f"{fig.natural_width}x{fig.natural_height}" if fig.natural_width and fig.natural_height else "unknown-size"
            page = f"p{fig.page_number}" if fig.page_number is not None else "page?"
            parts.append(f"- `{fig.id}` ({page}, {size}, q={fig.quality_score:.2f}) {fig.caption}")
    compact = "\n".join(parts).strip()
    if len(compact) > MAX_COMPACT_PAPER_CHARS:
        compact = compact[:MAX_COMPACT_PAPER_CHARS].rstrip() + "\n\n[Compact paper memory truncated here.]"
    return compact


def _working_state(memory: ProviderMemory) -> str:
    return (
        "# Provider Working State\n\n"
        f"- Paper: {memory.title}\n"
        f"- Sections indexed: {len(memory.sections)}\n"
        f"- Evidence cards: {len(memory.evidence_cards)}\n"
        f"- Figures indexed: {len(memory.figures)}\n\n"
        "Use `compact_paper.md` for manuscript planning and `evidence_cards.json` "
        "for slide-scoped retrieval."
    )


def _slide_figure_hints(page_content: str, memory: ProviderMemory) -> list[str]:
    figure_by_id = {fig.id: fig for fig in memory.figures}
    hints: list[str] = []
    for fig_id in re.findall(r"\[\[FIG:([A-Za-z0-9_\-]+)\]\]", page_content):
        fig = figure_by_id.get(fig_id)
        if fig:
            hints.append(f"- `{fig.id}` href={fig.path} caption={fig.caption}")
    return hints[:5]


def _terms(text: str) -> set[str]:
    return {token.lower() for token in _WORD_RE.findall(text or "") if len(token.strip()) >= 2}


def _trim(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"
