---
name: paper-ppt-research
description: Agent-driven external research for Paper PPT Agent decks. Use when Agent mode is allowed to use external knowledge, web search, arXiv, Semantic Scholar, Tavily, or SerpAPI to gather slide-relevant context after reading the uploaded paper; the Agent must choose search keywords from its own paper understanding and then write research/brief.md and research/sources.json.
---

# Paper PPT Research

## Overview

Use this skill only after reading `agent_task.json`, `source_assets/paper.md`, and the relevant paper sections. The backend does not choose search keywords for Agent mode; you choose queries from the paper's title, authors, claims, methods, datasets, baselines, limitations, and likely follow-up discussions.

## Workflow

1. Create 2-6 intentional queries. Cover different purposes rather than many variants of the title:
   - exact paper title plus authors or venue
   - core method/component names
   - datasets, benchmarks, or baseline methods
   - limitations, critique, follow-up work, project/repository, or related surveys
2. Run `scripts/search_research.py` with your chosen queries and enabled sources. Do not ask the script to invent queries.
3. Read `research/external_search_summary.json` first, then inspect focused parts of `research/raw_external_results.json` only as needed. Do not load the full raw result file when it is large.
4. Open/read the most relevant sources when web tooling is available. Search-result titles, snippets, and abstracts are leads, not evidence.
5. Stop after one reasonable search pass unless a query is obviously malformed, a required source failed before producing any usable result, or the paper itself reveals a missing key term. Limited or zero directly relevant external results are acceptable when you record the limitation.
6. Preserve the script result as `research/raw_external_results.json`; it is required proof that Agent-selected queries were executed.
7. Write `research/sources.json` with normalized sources, read depth, relevance, and limitations.
8. Write `research/brief.md` with only deck-relevant synthesis. Do not dump raw search results.

When this skill is enabled, the backend blocks `manuscript.md`, `design_spec.md`, notes, `agent_report.json`, and slide SVG authoring until all three research files above exist.
Before writing `manuscript.md` or slides, send a short progress update summarizing usable evidence, source limitations, and how the deck plan changes.

## Script

Use the Python interpreter from `agent_task.json.paths.python` or `PAPER_PPT_PYTHON`.

```bash
"<python>" skills/paper-ppt-research/scripts/search_research.py \
  --query "Exact paper title author" \
  --query "method name benchmark limitation" \
  --source arxiv --source semantic_scholar --source tavily \
  --max-results 5 \
  --per-source-timeout 20 \
  --total-timeout 90 \
  --out research/raw_external_results.json
```

The script supports `arxiv`, `semantic_scholar`, `tavily`, and `serpapi`. It reads API keys from `SEMANTIC_SCHOLAR_API_KEY`, `TAVILY_API_KEY`, and `SERPAPI_KEY`. It writes the raw result file plus `research/external_search_summary.json`. If a source is disabled, missing a key, rate-limited, timed out, unreachable, or yields little directly relevant evidence, record that limitation in both output files and continue to deep research or deck authoring once the required research files exist.

## Evidence Rules

- Prefer primary sources, official repositories/project pages, arXiv, Semantic Scholar metadata, and reputable discussion/critique.
- Keep external material subordinate to the uploaded paper. Use it to clarify context, comparisons, impact, limitations, or follow-up work.
- Do not use a source in slides unless you can explain why it matters to this deck.
- Keep citations/source URLs in `agent_report.json` when external research affects slide content.
- Do not rerun external search merely to increase result counts for the gate. The gate checks that Agent-selected research was attempted and summarized, not that the web contains enough relevant papers.
