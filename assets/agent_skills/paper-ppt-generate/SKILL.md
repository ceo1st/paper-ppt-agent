---
name: paper-ppt-generate
description: Generate an academic PowerPoint deck from a PDF or LaTeX source inside Paper PPT Agent's Agent-mode workspace. Use this when the task asks Claude Code or Codex to analyze a paper, plan a slide deck, author SVG slides, perform research/QA with subagents where useful, and write artifacts that the backend can preview and export.
---

# Paper PPT Generate

You are working inside a Paper PPT Agent generation workspace. The backend has
prepared `agent_task.json`, the uploaded paper source, template assets, icon
assets, and output folders. You own paper understanding, research, deck
planning, slide SVG authoring, and ordinary QA. The backend only watches files,
validates SVGs, finalizes them, and exports PPTX.

## Required Workflow

1. Read `agent_task.json`.
2. Read only the references you need:
   - `skills/paper-ppt-generate/references/output-contract.md` for required files and SVG rules.
   - `skills/paper-ppt-generate/references/slide-authoring.md` before writing SVG.
   - `skills/paper-ppt-research/SKILL.md` when external research is enabled.
   - `skills/paper-ppt-deep-research/SKILL.md` when deep research is enabled.
3. If `agent_task.json.presentation.template_id` is set, inspect the selected
   template directory in `agent_task.json.paths.selected_template` before
   planning the deck. Read its `design_spec.md`, page SVG skeletons, and
   `template_context.json`; preserve its page chrome, coordinate system,
   colors, typography, and structural conventions unless the user explicitly
   asks otherwise.
4. Inspect the paper source in `sources/`. Use commands/scripts as needed.
   For PDF sources, do not use the Read tool directly on `*.pdf`: it is a
   binary file and the tool may falsely report that the PDF is password
   protected. Use the Python interpreter from `agent_task.json.paths.python`
   or `PAPER_PPT_PYTHON` with a PDF library such as PyMuPDF (`fitz`) first,
   and only call the PDF password-protected if `doc.needs_pass` is true.
   Before drafting content, read `source_assets/paper.md` and
   `source_assets/figures.md`/`figures.json` if present. If they are missing,
   stale, or `source_assets/extraction_error.json` exists, run:
   `"<python>" skills/paper-ppt-generate/scripts/extract_paper_assets.py --task agent_task.json --out source_assets`
   using `agent_task.json.paths.python` or `PAPER_PPT_PYTHON`.
   During long work, periodically check `agent_feedback/` for user guidance.
5. Write `manuscript.md` as slide-structured content.
6. Write `design_spec.md` with deck-level visual rules and per-slide intent.
7. Write each slide as soon as it is usable:
   - `svg_output/slide_001.svg`
   - `svg_output/slide_002.svg`
   - etc.
8. Optionally write speaker notes in `notes/slide_001.md`, etc.
9. Write `agent_report.json` when finished.

## Agent Behavior

- Do not call Paper PPT Agent's provider model pipeline or request provider API keys.
- Follow `agent_task.json.presentation.detail_profile` quantitatively. Treat its slide count, bullets per content slide, evidence points, visuals per content slide, and paper coverage fields as the content contract for the requested detail level.
- Use `paper-ppt-deep-research` for deep work. When `allow_deep_research` is true, run that skill and produce `research/deep/notes_index.json` and `research/deep/brief.md` before authoring any deck output. Launch focused research SubAgents with the Task/SubAgent tool if it is available; skip only if the runtime has no such tool or it fails, and record the concrete reason in `agent_report.json.subagents`. When Agent review is enabled, start one focused reviewer after the first full deck draft; review the whole deck for layout overflow, missing assets, icon-policy violations, and narrative gaps instead of checking every page one by one. The main Agent remains responsible for the final story, visual consistency, and artifact integration.
- Generate slides in parallel when useful; keep consistency through `design_spec.md`.
- Icons are enabled but optional. Use them only when they clarify repeated semantic labels, process steps, comparison dimensions, legend keys, compact section markers, or actual diagram nodes. Do not add icons as filler decoration to ordinary bullet lists or dense evidence slides.
- Before using any icon, search local icon assets under the path listed in `agent_task.json.paths.icons` and verify the file exists.
- Before drafting slide SVGs, define a small icon vocabulary in `design_spec.md`: concept, local icon file, size, stroke/fill style, and color. Reuse the same icon for the same concept across slides; avoid mixing filled and outline libraries unless the selected template already does so.
- Keep icon density restrained: most content slides should use 0-3 icons, with more only when every icon is a node in a structured flow, taxonomy, or legend.
- Color icons from the template/deck palette. Usually use primary, muted foreground, or one accent color. Avoid per-icon rainbow colors unless color encodes a repeated category or status.
- Never use letters, initials, ASCII characters, Unicode symbols, dingbats, stars, checkmarks, arrows, pictographs, or emoji as icon substitutes. If no suitable local icon exists, omit the icon or use a neutral non-icon shape/accent.
- Never use punctuation or single-character text such as `!`, `?`, `+`, `*`, `#`, `>`, `<`, `/`, `→`, `←`, `↑`, `↓`, `★`, `✓`, or warning marks as badges, icons, status marks, or diagram connectors.
- Flow connectors must be real SVG geometry (`line`, `path`, `polyline`, marker definitions, or simple geometric arrowheads), not text glyph arrows.
- Do not invent missing icon names, use remote icon URLs, or rely on font glyphs as icons.
- Use external research only when `agent_task.json` allows it. Run `paper-ppt-research` after reading the paper; choose search queries yourself from paper understanding, then produce `research/raw_external_results.json`, `research/sources.json`, and `research/brief.md` before authoring any deck output. Read `research/external_search_summary.json` before sampling raw search output, and treat documented sparse/no relevant results as a valid completion state.
- Treat `agent_task.json.agent_policy.research_gate` as mandatory. When enabled, the backend will reject writes to manuscript, design, notes, report, and slide SVG files until the required research artifacts exist.
- If deep research is enabled, run `paper-ppt-deep-research`, split work across focused readers/SubAgents where supported, and merge the synthesis into the manuscript.
- If Agent review is enabled, perform a whole-deck review after the first complete draft. Use multimodal inspection only when the runtime supports it; otherwise perform XML/static review and record the limitation.
- For any Python command, prefer `agent_task.json.paths.python` or `PAPER_PPT_PYTHON` over system `python`; this keeps PDF parsing, SVG checks, and export helpers in the backend environment where required packages are installed.
- Treat `source_assets/figures.json` as the paper-figure contract. It gives each figure's `id`, exact SVG `href`, caption, PDF page/bbox when available, surrounding context, and natural dimensions. Select figures by caption/page/context, not by filename alone.
- Use paper figures in content slides whenever they directly support the slide argument. In SVG, reference the exact manifest `href` such as `../source_assets/images/pdf_fig_001_p3_abcd.png`; preserve the listed aspect ratio.
- For TeX archives, use the source graphics discovered from `\includegraphics`; do not screenshot every archive image. PDF graphics referenced by TeX may already be rendered into `source_assets/images/`.

## Live Preview

The backend watches `svg_output/`. Save each completed or repaired slide SVG
there immediately. Rewriting an existing `slide_###.svg` updates the app's live
preview.

## Quality Bar

- Every SVG must parse as XML and define a correct canvas `viewBox`.
- Keep all needed assets local or embedded by relative path.
- Avoid external image URLs in final SVGs.
- Audit the final SVGs for icon-policy violations: icons are purposeful rather than decorative, the icon vocabulary is consistent, colors follow the deck palette, and there are no fake icon letters/symbols/emoji, made-up icon files, or missing local icon references.
- Audit for text glyph symbols used visually, including exclamation marks, stars, checkmarks, and arrow characters. Replace them with real local icon SVGs or SVG geometry before finishing.
- Preserve template chrome when a template is provided.
- Prefer clear academic storytelling over dense paper transcription.
- End only when all slides are present and `agent_report.json` explains what was produced, what research was used, what icon assets were used or why icons were omitted, which SubAgent tasks were used/skipped, and whether QA passed.
- Include `paper_figures_used` in `agent_report.json` with figure ids/hrefs/captions used in slides, or explain why no extracted paper figure was suitable.
