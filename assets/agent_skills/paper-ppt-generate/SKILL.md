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
   - `skills/paper-ppt-generate/references/research.md` when external or deep research is enabled.
   - `skills/paper-ppt-generate/references/slide-authoring.md` before writing SVG.
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
- Use subagents/parallel tasks for deep work. When `allow_deep_research` is true, you must launch focused research SubAgents with the Task/SubAgent tool if it is available; skip only if the runtime has no such tool or it fails, and record the concrete reason in `agent_report.json.subagents`. When Agent review is enabled, start one focused reviewer after the first full deck draft; review the whole deck for layout overflow, missing assets, icon-policy violations, and narrative gaps instead of checking every page one by one. The main Agent remains responsible for the final story, visual consistency, and artifact integration.
- Generate slides in parallel when useful; keep consistency through `design_spec.md`.
- Icons are enabled by default. Before using any icon, search local icon assets under the path listed in `agent_task.json.paths.icons` and verify the file exists.
- Never use letters, initials, ASCII characters, Unicode symbols, dingbats, stars, checkmarks, arrows, pictographs, or emoji as icon substitutes. If no suitable local icon exists, omit the icon or use a neutral non-icon shape/accent.
- Never use punctuation or single-character text such as `!`, `?`, `+`, `*`, `#`, `>`, `<`, `/`, `→`, `←`, `↑`, `↓`, `★`, `✓`, or warning marks as badges, icons, status marks, or diagram connectors.
- Flow connectors must be real SVG geometry (`line`, `path`, `polyline`, marker definitions, or simple geometric arrowheads), not text glyph arrows.
- Do not invent missing icon names, use remote icon URLs, or rely on font glyphs as icons.
- Use external research only when `agent_task.json` allows it. Use API keys from environment variables named in `agent_task.json.agent_policy.research_policy.credential_env` when present. You may also use other accessible search methods/tools if available.
- For external web research, do not rely on search result titles/snippets. Open relevant pages and read enough body content to understand the claim, evidence, date, and reliability before using it. Summarize verified findings into `research/brief.md` and cite `research/sources.json`.
- When external research is enabled, read any prefetched `research/brief.md` and `research/sources.json`, then send a user-visible research update before writing the manuscript or slides. Summarize usable evidence and limitations in your own words; include 2-4 slide-relevant findings when present and explain their effect on the deck rather than reporting only API/result counts.
- If deep research is enabled, split work across focused readers/SubAgents and merge into the manuscript.
- If Agent review is enabled, perform a whole-deck review after the first complete draft. Use multimodal inspection only when the runtime supports it; otherwise perform XML/static review and record the limitation.
- For any Python command, prefer `agent_task.json.paths.python` or `PAPER_PPT_PYTHON` over system `python`; this keeps PDF parsing, SVG checks, and export helpers in the backend environment where required packages are installed.

## Live Preview

The backend watches `svg_output/`. Save each completed or repaired slide SVG
there immediately. Rewriting an existing `slide_###.svg` updates the app's live
preview.

## Quality Bar

- Every SVG must parse as XML and define a correct canvas `viewBox`.
- Keep all needed assets local or embedded by relative path.
- Avoid external image URLs in final SVGs.
- Audit the final SVGs for icon-policy violations: no fake icon letters/symbols/emoji, no made-up icon files, no missing local icon references.
- Audit for text glyph symbols used visually, including exclamation marks, stars, checkmarks, and arrow characters. Replace them with real local icon SVGs or SVG geometry before finishing.
- Preserve template chrome when a template is provided.
- Prefer clear academic storytelling over dense paper transcription.
- End only when all slides are present and `agent_report.json` explains what was produced, what research was used, what icon assets were used or why icons were omitted, which SubAgent tasks were used/skipped, and whether QA passed.
