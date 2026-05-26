---
name: paper-ppt-deep-research
description: Deep paper analysis for Paper PPT Agent decks. Use when Agent mode has deep research enabled or when a long/technical paper needs focused reading passes, SubAgent/task decomposition, evidence extraction, limitation analysis, and a slide-ready synthesis before manuscript and SVG generation.
---

# Paper PPT Deep Research

## Overview

Use this skill after reading `agent_task.json` and the extracted paper assets. It organizes the uploaded paper into focused research passes so the final deck is faithful, detailed, and coherent.

## Workflow

1. Read `source_assets/paper.md` and `source_assets/figures.json` or `figures.md`.
2. Create `research/deep/plan.md` with focused passes. Typical passes:
   - problem, motivation, and research gap
   - method/architecture and algorithm details
   - data, experiments, metrics, baselines, and ablations
   - figures/tables/equations worth showing
   - limitations, assumptions, failure modes, and implications
   - slide narrative and audience framing
3. If the runtime provides Task/SubAgent tools, assign focused readers when deep research is enabled. Use separate readers for the paper's background/related work, method, experiments, and critique when the paper is complex. Skip only when the tool is unavailable, fails, or the paper is too short/simple for meaningful decomposition; record the concrete reason in `agent_report.json.subagents`.
4. Store pass notes under `research/deep/notes/`. Keep notes factual, with section/page/figure anchors where available.
5. Run `scripts/compile_deep_notes.py` to write `research/deep/notes_index.json`, even when a failed/unavailable SubAgent leaves the index empty and the limitation must be described.
6. Write `research/deep/brief.md` with slide-ready synthesis and conflicts/uncertainties.

When this skill is enabled, the backend blocks `manuscript.md`, `design_spec.md`, notes, `agent_report.json`, and slide SVG authoring until `research/deep/notes_index.json` and `research/deep/brief.md` exist.
Merge the synthesis into `manuscript.md`; do not paste independent reader styles into the deck.

## Script

Use the Python interpreter from `agent_task.json.paths.python` or `PAPER_PPT_PYTHON`.

```bash
"<python>" skills/paper-ppt-deep-research/scripts/compile_deep_notes.py \
  --notes-dir research/deep/notes \
  --out research/deep/notes_index.json
```

The script only indexes notes you or SubAgents already wrote. It must not replace paper reading or decide what matters.

## Quality Rules

- Tie findings back to the uploaded paper. Prefer concrete paper facts, metrics, figures, tables, equations, and named components.
- Separate what the paper proves from your interpretation or external context.
- Record SubAgent usage or skip reasons in `agent_report.json.subagents`.
- Keep the final deck narrative unified; the main Agent owns synthesis and style consistency.
