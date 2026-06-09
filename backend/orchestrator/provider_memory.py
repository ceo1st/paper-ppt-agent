"""Provider-mode working memory for paper-to-deck generation.

The provider pipeline is intentionally stateless at the API boundary, but the
deck generation task is not. This module preserves the parsed paper, builds a
local full-text chunk index, and prepares scoped evidence so later per-slide
LLM calls can retrieve the relevant source passages instead of relying on a
lossy summary.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from rank_bm25 import BM25Okapi

from backend.orchestrator.deck_plan import DeckPlan
from backend.orchestrator.manuscript import extract_page_type, page_title, split_manuscript_pages
from backend.parser.paper_model import ParsedPaper, PaperFigure, PaperSection

MAX_COMPACT_PAPER_CHARS = 36000
MAX_SECTION_BRIEF_CHARS = 1400
MAX_CARD_TEXT_CHARS = 520
MAX_PAPER_CHUNK_CHARS = 1400
MAX_EVIDENCE_CARDS = 180
SLIDE_CONTEXT_CARD_COUNT = 5
SLIDE_CONTEXT_BUDGETS = {
    "normal": {
        "chunks": 3,
        "cards": 3,
        "sections": 1,
        "chunk_chars": 780,
        "card_chars": 420,
        "section_chars": 420,
    },
    "high": {
        "chunks": 4,
        "cards": 4,
        "sections": 2,
        "chunk_chars": 980,
        "card_chars": 480,
        "section_chars": 520,
    },
    "very_high": {
        "chunks": 5,
        "cards": 5,
        "sections": 2,
        "chunk_chars": 1180,
        "card_chars": 520,
        "section_chars": 620,
    },
}
BM25_K1 = 1.5
BM25_B = 0.75

_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?%?|[A-Za-z][A-Za-z0-9_\-]*|[\u4e00-\u9fff]+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？.!?])\s+|\n{2,}")
_EVIDENCE_RE = re.compile(
    r"(?i)(\d|%|table|figure|fig\.?|equation|formula|experiment|benchmark|"
    r"dataset|baseline|sota|accuracy|pass@|rating|flops|cache|latency|"
    r"ablation|parameter|token|training|inference|图|表|公式|实验|指标|"
    r"数据集|基线|消融|参数|训练|推理|精度|缓存|延迟|"
    r"study|survey|interview|participant|sample|cohort|trial|case|finding|"
    r"theme|theory|hypothesis|measure|scale|regression|correlation|"
    r"significant|confidence interval|effect size|qualitative|quantitative|"
    r"研究|调查|访谈|参与者|样本|队列|试验|案例|发现|主题|理论|假设|"
    r"量表|回归|相关|显著|置信区间|效应|质性|定量|定性)"
)
_METHOD_RE = re.compile(
    r"(?i)(method|architecture|framework|algorithm|module|attention|optimizer|"
    r"training|pipeline|mechanism|approach|design|protocol|procedure|model|"
    r"construct|coding|方法|架构|算法|模块|注意力|优化器|机制|流程|"
    r"路径|设计|方案|模型|构念|编码)"
)
_STOPWORDS = frozenset(
    {
        "about",
        "above",
        "after",
        "again",
        "against",
        "also",
        "among",
        "and",
        "are",
        "because",
        "between",
        "both",
        "but",
        "can",
        "could",
        "does",
        "during",
        "each",
        "from",
        "had",
        "has",
        "have",
        "how",
        "into",
        "its",
        "may",
        "more",
        "most",
        "not",
        "our",
        "out",
        "over",
        "paper",
        "page",
        "result",
        "results",
        "section",
        "show",
        "shows",
        "slide",
        "study",
        "such",
        "than",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "this",
        "through",
        "using",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "with",
    }
)
_QUERY_INTENTS = {
    "result": re.compile(
        r"(?i)(result|finding|outcome|effect|impact|evidence|analysis|"
        r"结果|发现|影响|效果|证据|分析)"
    ),
    "method": re.compile(
        r"(?i)(method|approach|design|framework|model|theory|construct|procedure|sample|participant|"
        r"方法|路径|设计|框架|模型|理论|构念|流程|样本|参与者)"
    ),
    "limitation": re.compile(
        r"(?i)(limitation|future|assumption|threat|bias|constraint|"
        r"局限|未来|假设|偏差|限制)"
    ),
}
_CONCEPT_GROUPS = (
    frozenset(
        {
            "participant",
            "participants",
            "sample",
            "samples",
            "respondent",
            "respondents",
            "cohort",
            "population",
            "subject",
            "subjects",
            "参与者",
            "样本",
            "受试者",
            "人群",
        }
    ),
    frozenset(
        {
            "interview",
            "interviews",
            "qualitative",
            "theme",
            "themes",
            "coding",
            "coded",
            "访谈",
            "质性",
            "主题",
            "编码",
        }
    ),
    frozenset({"survey", "questionnaire", "scale", "measure", "measurement", "调查", "问卷", "量表", "测量"}),
    frozenset(
        {
            "theory",
            "framework",
            "model",
            "construct",
            "mechanism",
            "理论",
            "框架",
            "模型",
            "构念",
            "机制",
        }
    ),
    frozenset(
        {
            "finding",
            "findings",
            "outcome",
            "outcomes",
            "effect",
            "effects",
            "impact",
            "发现",
            "结果",
            "效果",
            "影响",
        }
    ),
    frozenset({"limitation", "limitations", "future", "assumption", "bias", "局限", "未来", "假设", "偏差"}),
)
_REFERENCE_SECTION_RE = re.compile(
    r"(?i)^\s*(references|bibliography|works cited|参考文献|引用文献)\s*$"
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
    bbox: tuple[float, float, float, float] | None
    extraction_method: str
    natural_width: int
    natural_height: int
    aspect_ratio: float
    quality_score: float
    review_flags: tuple[str, ...]


@dataclass(frozen=True)
class PaperChunk:
    id: str
    section: str
    kind: str
    text: str
    page_number: int | None = None
    figure_ids: tuple[str, ...] = ()


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
    full_markdown: str
    compact_markdown: str
    sections: tuple[SectionMemory, ...]
    paper_chunks: tuple[PaperChunk, ...]
    evidence_cards: tuple[EvidenceCard, ...]
    figures: tuple[FigureMemory, ...]


def build_provider_memory(paper: ParsedPaper) -> ProviderMemory:
    """Build deterministic, compact memory from a parsed paper."""
    cards: list[EvidenceCard] = []
    section_memories: list[SectionMemory] = []

    for section_index, section in enumerate(paper.sections, start=1):
        if _REFERENCE_SECTION_RE.match(section.title):
            continue
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
    paper_chunks = tuple(_paper_chunks(paper))
    full_markdown = paper.to_markdown()
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
        full_markdown=full_markdown,
        compact_markdown=compact_markdown,
        sections=tuple(section_memories),
        paper_chunks=paper_chunks,
        evidence_cards=tuple(cards),
        figures=figures,
    )


def save_provider_memory(memory: ProviderMemory, target_dir: Path) -> None:
    """Persist provider working memory for inspection and later reuse."""
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "full_paper.md").write_text(memory.full_markdown, encoding="utf-8")
    (target_dir / "compact_paper.md").write_text(memory.compact_markdown, encoding="utf-8")
    (target_dir / "paper_chunks.jsonl").write_text(
        "\n".join(
            json.dumps(asdict(chunk), ensure_ascii=False)
            for chunk in memory.paper_chunks
        )
        + ("\n" if memory.paper_chunks else ""),
        encoding="utf-8",
    )
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
    detail_level: str = "normal",
) -> dict[int, str]:
    """Return compact evidence context for each manuscript page."""
    if memory is None:
        return {}
    pages = split_manuscript_pages(manuscript)
    contexts: dict[int, str] = {}
    plan_by_page = deck_plan.by_page if deck_plan is not None else {}
    budget = SLIDE_CONTEXT_BUDGETS.get(
        detail_level,
        SLIDE_CONTEXT_BUDGETS["normal"],
    )
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
        page_type = extract_page_type(page)
        is_structural = page_type in {"cover", "chapter", "toc", "ending"}
        chunks = (
            []
            if is_structural
            else retrieve_paper_chunks(
                query,
                memory,
                limit=budget["chunks"],
            )
        )
        cards = (
            []
            if is_structural
            else retrieve_evidence_cards(
                query,
                memory,
                limit=budget["cards"],
            )
        )
        relevant_sections = (
            []
            if is_structural
            else _relevant_section_memories(
                query,
                cards,
                memory,
                limit=budget["sections"],
            )
        )
        figure_lines = [] if is_structural else _slide_figure_hints(page, memory)
        lines = [
            "## Provider Slide Working Memory",
            "",
            "This is private grounding, not slide copy. Use the retrieved source passages and evidence anchors to preserve exact facts; do not print chunk IDs, card IDs, section labels, or this memory verbatim.",
            f"- Slide title: {page_title(page)}",
            f"- Page type: {page_type}",
            f"- Paper title: {memory.title}",
        ]
        if memory.authors:
            lines.append(f"- Authors: {', '.join(memory.authors)}")
        if chunks:
            lines.extend(["", "### Retrieved Source Passages"])
            for chunk in chunks:
                page_note = f", p{chunk.page_number}" if chunk.page_number is not None else ""
                figure_note = f", figures={', '.join(chunk.figure_ids)}" if chunk.figure_ids else ""
                lines.append(
                    f"- `{chunk.id}` [{chunk.kind}{page_note}{figure_note}] {chunk.section}: "
                    f"{_trim(chunk.text, budget['chunk_chars'])}"
                )
        if relevant_sections:
            lines.extend(["", "### Relevant Section Summaries"])
            for section in relevant_sections:
                lines.append(
                    f"- {section.title}: {_trim(section.brief, budget['section_chars'])}"
                )
        if cards:
            lines.append("")
            lines.append("### Evidence Anchors")
            for card in cards:
                figure_note = f" figures={', '.join(card.figure_ids)}" if card.figure_ids else ""
                lines.append(
                    f"- `{card.id}` [{card.kind}] {card.section}:{figure_note} "
                    f"{_trim(card.text, budget['card_chars'])}"
                )
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
    query_tokens = _tokens(query)
    if not query_tokens:
        return list(memory.evidence_cards[:limit])
    ranked = _rank_cards(query, query_tokens, memory.evidence_cards)
    return _select_diverse_cards(ranked, limit)


def retrieve_paper_chunks(
    query: str,
    memory: ProviderMemory,
    *,
    limit: int,
) -> list[PaperChunk]:
    query_tokens = _tokens(query)
    if not query_tokens:
        return list(memory.paper_chunks[:limit])
    ranked = _rank_chunks(query, query_tokens, memory.paper_chunks)
    selected: list[PaperChunk] = []
    seen_sections: dict[str, int] = {}
    seen_kinds: dict[str, int] = {}
    for score, chunk in ranked:
        if score <= 0:
            continue
        if seen_sections.get(chunk.section, 0) >= 3:
            continue
        if chunk.kind == "table" and seen_kinds.get("table", 0) >= 2:
            continue
        selected.append(chunk)
        seen_sections[chunk.section] = seen_sections.get(chunk.section, 0) + 1
        seen_kinds[chunk.kind] = seen_kinds.get(chunk.kind, 0) + 1
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        selected_ids = {chunk.id for chunk in selected}
        for score, chunk in ranked:
            if score <= 0 or chunk.id in selected_ids:
                continue
            selected.append(chunk)
            selected_ids.add(chunk.id)
            if len(selected) >= limit:
                break
    return selected


def _select_diverse_cards(
    ranked: list[tuple[float, EvidenceCard]],
    limit: int,
) -> list[EvidenceCard]:
    selected: list[EvidenceCard] = []
    section_counts: dict[str, int] = {}
    kind_counts: dict[str, int] = {}
    for score, card in ranked:
        if score <= 0:
            continue
        if section_counts.get(card.section, 0) >= 3:
            continue
        if card.kind == "table" and kind_counts.get("table", 0) >= 2:
            continue
        selected.append(card)
        section_counts[card.section] = section_counts.get(card.section, 0) + 1
        kind_counts[card.kind] = kind_counts.get(card.kind, 0) + 1
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        selected_ids = {card.id for card in selected}
        for score, card in ranked:
            if score <= 0 or card.id in selected_ids:
                continue
            selected.append(card)
            selected_ids.add(card.id)
            if len(selected) >= limit:
                break
    return selected


def _relevant_section_memories(
    query: str,
    cards: list[EvidenceCard],
    memory: ProviderMemory,
    *,
    limit: int,
) -> list[SectionMemory]:
    by_title = {section.title: section for section in memory.sections}
    selected: list[SectionMemory] = []
    seen: set[str] = set()
    for card in cards:
        section = by_title.get(card.section)
        if section is None or section.title in seen:
            continue
        selected.append(section)
        seen.add(section.title)
        if len(selected) >= limit:
            return selected
    query_terms = set(_tokens(query))
    ranked = sorted(
        memory.sections,
        key=lambda section: len(query_terms.intersection(_tokens(section.title))),
        reverse=True,
    )
    for section in ranked:
        if section.title in seen:
            continue
        selected.append(section)
        seen.add(section.title)
        if len(selected) >= limit:
            break
    return selected


def _rank_cards(
    query: str,
    query_tokens: list[str],
    cards: Iterable[EvidenceCard],
) -> list[tuple[float, EvidenceCard]]:
    card_list = list(cards)
    doc_tokens = [_tokens(_card_search_text(card)) for card in card_list]
    bm25_scores = _bm25_scores(query_tokens, doc_tokens)

    query_terms = set(query_tokens)
    query_intents = _query_intents(query)
    query_concepts = _concept_matches(query_terms)
    query_phrases = _token_phrases(query_tokens)

    ranked: list[tuple[float, EvidenceCard]] = []
    for card, tokens, bm25_score in zip(card_list, doc_tokens, bm25_scores, strict=True):
        token_terms = set(tokens)
        score = max(0.0, bm25_score)

        # Keep the original extraction score as a weak prior while BM25 and
        # lexical relevance drive the ordering.
        score += min(card.score, 10.0) * 0.08

        section_terms = set(_tokens(card.section))
        score += min(len(query_terms.intersection(section_terms)), 3) * 0.7

        phrase_hits = sum(1 for phrase in query_phrases if phrase in _normalized_text(card.text))
        score += min(phrase_hits, 3) * 0.8

        if card.kind in query_intents:
            score += 1.2
        elif "result" in query_intents and card.kind in {"evidence", "table"}:
            score += 0.8
        elif "method" in query_intents and card.kind in {"equation", "context"}:
            score += 0.4

        shared_concepts = sum(1 for concept in query_concepts if token_terms.intersection(concept))
        score += min(shared_concepts, 3) * 0.65

        if token_terms.intersection(query_terms):
            score += min(len(token_terms.intersection(query_terms)), 5) * 0.18

        ranked.append((score, card))

    ranked.sort(key=lambda item: (item[0], item[1].score), reverse=True)
    return ranked


def _rank_chunks(
    query: str,
    query_tokens: list[str],
    chunks: Iterable[PaperChunk],
) -> list[tuple[float, PaperChunk]]:
    chunk_list = list(chunks)
    doc_tokens = [_tokens(_chunk_search_text(chunk)) for chunk in chunk_list]
    bm25_scores = _bm25_scores(query_tokens, doc_tokens)

    query_terms = set(query_tokens)
    query_intents = _query_intents(query)
    query_concepts = _concept_matches(query_terms)
    query_phrases = _token_phrases(query_tokens)

    ranked: list[tuple[float, PaperChunk]] = []
    for chunk, tokens, bm25_score in zip(chunk_list, doc_tokens, bm25_scores, strict=True):
        token_terms = set(tokens)
        score = max(0.0, bm25_score)

        section_terms = set(_tokens(chunk.section))
        score += min(len(query_terms.intersection(section_terms)), 4) * 0.7

        phrase_hits = sum(1 for phrase in query_phrases if phrase in _normalized_text(chunk.text))
        score += min(phrase_hits, 4) * 0.85

        if chunk.kind in query_intents:
            score += 1.1
        elif "result" in query_intents and chunk.kind in {"table", "figure", "paragraph"}:
            score += 0.6
        elif "method" in query_intents and chunk.kind in {"equation", "paragraph"}:
            score += 0.5

        shared_concepts = sum(1 for concept in query_concepts if token_terms.intersection(concept))
        score += min(shared_concepts, 3) * 0.65

        if token_terms.intersection(query_terms):
            score += min(len(token_terms.intersection(query_terms)), 6) * 0.16

        if chunk.figure_ids and any(fig_id.lower() in query.lower() for fig_id in chunk.figure_ids):
            score += 2.0

        ranked.append((score, chunk))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def _bm25_scores(query_tokens: list[str], docs: list[list[str]]) -> list[float]:
    if not docs:
        return []
    if not any(docs):
        return [0.0 for _ in docs]
    bm25 = BM25Okapi(docs, k1=BM25_K1, b=BM25_B)
    return [float(score) for score in bm25.get_scores(query_tokens)]


def _card_search_text(card: EvidenceCard) -> str:
    return f"{card.section} {card.section} {card.kind} {card.text}"


def _chunk_search_text(chunk: PaperChunk) -> str:
    figures = " ".join(chunk.figure_ids)
    return f"{chunk.section} {chunk.section} {chunk.kind} {figures} {chunk.text}"


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
            table_parts = _split_table_card_text(text)
            for part_index, part in enumerate(table_parts[:8], start=1):
                card_id = (
                    f"s{section_index:02d}t{table_index:02d}"
                    if len(table_parts) == 1
                    else f"s{section_index:02d}t{table_index:02d}p{part_index:02d}"
                )
                candidates.append(
                    EvidenceCard(
                        id=card_id,
                        section=section.title,
                        kind="table",
                        text=_trim(part, MAX_CARD_TEXT_CHARS),
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
    if re.search(
        r"(?i)(result|finding|outcome|effect|impact|benchmark|accuracy|pass@|rating|experiment|"
        r"结果|发现|效果|影响|指标|实验)",
        sentence,
    ):
        return "result"
    if _METHOD_RE.search(sentence):
        return "method"
    if re.search(r"(?i)(limitation|future|assumption|局限|未来|假设)", sentence):
        return "limitation"
    if _EVIDENCE_RE.search(sentence):
        return "evidence"
    return "context"


def _split_table_card_text(text: str) -> list[str]:
    if len(text) <= MAX_CARD_TEXT_CHARS:
        return [text]
    overlap = min(260, MAX_CARD_TEXT_CHARS // 2)
    step = max(1, MAX_CARD_TEXT_CHARS - overlap)
    parts: list[str] = []
    for start in range(0, len(text), step):
        part = text[start : start + MAX_CARD_TEXT_CHARS].strip()
        if part:
            parts.append(part)
        if start + MAX_CARD_TEXT_CHARS >= len(text):
            break
    return parts


def _section_brief(section: PaperSection, cards: Iterable[EvidenceCard]) -> str:
    card_texts = [card.text for card in cards][:4]
    if card_texts:
        return _trim(" ".join(card_texts), MAX_SECTION_BRIEF_CHARS)
    return _trim(section.content or "", MAX_SECTION_BRIEF_CHARS)


def _paper_chunks(paper: ParsedPaper) -> list[PaperChunk]:
    chunks: list[PaperChunk] = []
    if paper.abstract.strip():
        chunks.extend(
            _split_chunk_text(
                "abs",
                "Abstract",
                "abstract",
                paper.abstract,
                page_number=None,
                figure_ids=(),
            )
        )

    for section_index, section in enumerate(paper.sections, start=1):
        if _REFERENCE_SECTION_RE.match(section.title):
            continue
        prefix = f"s{section_index:02d}"
        chunks.extend(
            _split_chunk_text(
                f"{prefix}p",
                section.title,
                "paragraph",
                section.content,
                page_number=None,
                figure_ids=tuple(fig.fig_id for fig in section.figures if fig.available)[:4],
            )
        )
        for fig_index, fig in enumerate(section.figures, start=1):
            if not paper._should_include_figure(fig):
                continue
            caption = (fig.caption or "").replace("\n", " ").strip()
            text = f"Figure token [[FIG:{fig.fig_id}]]. {caption}".strip()
            if text:
                chunks.extend(
                    _split_chunk_text(
                        f"{prefix}f{fig_index:02d}",
                        section.title,
                        "figure",
                        text,
                        page_number=fig.page_number,
                        figure_ids=(fig.fig_id,),
                    )
                )
        for table_index, table in enumerate(section.tables, start=1):
            text = "\n".join(part for part in (table.caption, table.markdown) if part).strip()
            if text:
                chunks.extend(
                    _split_chunk_text(
                        f"{prefix}t{table_index:02d}",
                        section.title,
                        "table",
                        text,
                        page_number=None,
                        figure_ids=(),
                    )
                )
        for eq_index, equation in enumerate(section.equations, start=1):
            if equation.strip():
                chunks.extend(
                    _split_chunk_text(
                        f"{prefix}e{eq_index:02d}",
                        section.title,
                        "equation",
                        equation.strip(),
                        page_number=None,
                        figure_ids=(),
                    )
                )
    return chunks


def _split_chunk_text(
    id_prefix: str,
    section: str,
    kind: str,
    text: str,
    *,
    page_number: int | None,
    figure_ids: tuple[str, ...],
) -> list[PaperChunk]:
    normalized = re.sub(r"[ \t]+", " ", text or "").strip()
    if not normalized:
        return []

    parts: list[str] = []
    for paragraph in re.split(r"\n{2,}", normalized):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= MAX_PAPER_CHUNK_CHARS:
            parts.append(paragraph)
            continue
        parts.extend(_split_long_chunk(paragraph, MAX_PAPER_CHUNK_CHARS))

    chunks: list[PaperChunk] = []
    for idx, part in enumerate(parts, start=1):
        if len(part) < 20:
            continue
        chunks.append(
            PaperChunk(
                id=f"{id_prefix}{idx:03d}",
                section=section,
                kind=kind,
                text=part,
                page_number=page_number,
                figure_ids=figure_ids,
            )
        )
    return chunks


def _split_long_chunk(text: str, limit: int) -> list[str]:
    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
    if len(sentences) <= 1:
        return [text[i : i + limit].strip() for i in range(0, len(text), limit)]

    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if not current:
            current = sentence
            continue
        if len(current) + 1 + len(sentence) <= limit:
            current = f"{current} {sentence}"
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    split_chunks: list[str] = []
    for chunk in chunks:
        if len(chunk) <= limit:
            split_chunks.append(chunk)
        else:
            split_chunks.extend(chunk[i : i + limit].strip() for i in range(0, len(chunk), limit))
    return split_chunks


def _figure_memory(fig: PaperFigure) -> FigureMemory:
    caption = (fig.caption or "").replace("\n", " ").strip()
    return FigureMemory(
        id=fig.fig_id,
        path=str(fig.path),
        caption=_trim(caption, 360),
        page_number=fig.page_number,
        bbox=fig.bbox,
        extraction_method=fig.extraction_method,
        natural_width=fig.natural_width,
        natural_height=fig.natural_height,
        aspect_ratio=fig.aspect_ratio,
        quality_score=float(fig.quality_score or 0.0),
        review_flags=tuple(fig.review_flags or []),
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
            method = f", {fig.extraction_method}" if fig.extraction_method else ""
            flags = f", flags={','.join(fig.review_flags)}" if fig.review_flags else ""
            parts.append(f"- `{fig.id}` ({page}, {size}, q={fig.quality_score:.2f}{method}{flags}) {fig.caption}")
    compact = "\n".join(parts).strip()
    if len(compact) > MAX_COMPACT_PAPER_CHARS:
        compact = compact[:MAX_COMPACT_PAPER_CHARS].rstrip() + "\n\n[Compact paper memory truncated here.]"
    return compact


def _working_state(memory: ProviderMemory) -> str:
    return (
        "# Provider Working State\n\n"
        f"- Paper: {memory.title}\n"
        f"- Sections indexed: {len(memory.sections)}\n"
        f"- Full-text chunks: {len(memory.paper_chunks)}\n"
        f"- Evidence cards: {len(memory.evidence_cards)}\n"
        f"- Figures indexed: {len(memory.figures)}\n\n"
        "`full_paper.md` is the preserved source text. `paper_chunks.jsonl`, "
        "`evidence_cards.json`, and `figures.json` provide slide-scoped "
        "retrieval grounding. `compact_paper.md` is retained only as a "
        "fallback/debug summary."
    )


def _slide_figure_hints(page_content: str, memory: ProviderMemory) -> list[str]:
    figure_by_id = {fig.id: fig for fig in memory.figures}
    hints: list[str] = []
    for fig_id in re.findall(r"\[\[FIG:([A-Za-z0-9_\-]+)\]\]", page_content):
        fig = figure_by_id.get(fig_id)
        if fig:
            hints.append(
                f"- `{fig.id}` href={fig.path} caption={fig.caption} "
                f"source={fig.extraction_method or 'unknown'} quality={fig.quality_score:.2f}"
            )
    return hints[:5]


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text or ""):
        raw = match.group(0)
        if not raw:
            continue
        if _is_cjk(raw):
            tokens.extend(_cjk_tokens(raw))
            continue
        normalized = raw.lower().strip("-_")
        parts = [part for part in re.split(r"[-_]+", normalized) if part]
        candidates = [normalized, *parts] if len(parts) > 1 else [normalized]
        for candidate in candidates:
            token = _normalize_token(candidate)
            if len(token) >= 2 and token not in _STOPWORDS:
                tokens.append(token)
    return tokens


def _normalize_token(token: str) -> str:
    token = token.lower().strip()
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _is_cjk(value: str) -> bool:
    return all("\u4e00" <= char <= "\u9fff" for char in value)


def _cjk_tokens(value: str) -> list[str]:
    if len(value) <= 2:
        return [value]
    tokens = [value]
    tokens.extend(value[i : i + 2] for i in range(0, len(value) - 1))
    return tokens


def _query_intents(query: str) -> set[str]:
    return {intent for intent, pattern in _QUERY_INTENTS.items() if pattern.search(query or "")}


def _concept_matches(tokens: set[str]) -> tuple[frozenset[str], ...]:
    return tuple(group for group in _CONCEPT_GROUPS if tokens.intersection(group))


def _token_phrases(tokens: list[str]) -> tuple[str, ...]:
    phrases: list[str] = []
    for size in (3, 2):
        for idx in range(0, max(len(tokens) - size + 1, 0)):
            phrase = " ".join(tokens[idx : idx + size])
            if len(phrase) >= 7:
                phrases.append(phrase)
    return tuple(dict.fromkeys(phrases[:12]))


def _normalized_text(text: str) -> str:
    return " ".join(_tokens(text))


def _trim(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"
