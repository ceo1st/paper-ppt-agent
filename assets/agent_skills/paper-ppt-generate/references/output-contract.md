# Output Contract

Required files:

- `manuscript.md`: slide-by-slide manuscript.
- `design_spec.md`: visual system, template usage, slide inventory, and per-slide design intent.
- `svg_output/slide_001.svg`, `svg_output/slide_002.svg`, ...: final authoring SVGs.
- `agent_report.json`: machine-readable summary.

Optional files:

- `notes/slide_001.md`, ...: speaker notes matched by SVG stem.
- `qa/*.md` or `qa/*.json`: review results.

Conditionally required before authoring any deck output:

- When external research is enabled: `research/raw_external_results.json`, `research/sources.json`, and `research/brief.md`. The search helper also writes `research/external_search_summary.json`; read that before sampling raw results.
- When deep research is enabled: `research/deep/notes_index.json` and `research/deep/brief.md`.
- The backend research gate rejects manuscript, design, notes, report, and slide SVG writes until the enabled research requirements are complete.

`agent_report.json` should be valid JSON:

```json
{
  "status": "complete",
  "slides": 12,
  "language": "zh",
  "external_research_used": false,
  "deep_research_used": false,
  "detail_level": "normal|high|very_high",
  "detail_profile_followed": true,
  "icon_policy": {
    "local_icons_searched": true,
    "purposeful_usage": true,
    "consistent_vocabulary": true,
    "assets_used": ["relative/path/icon.svg"],
    "substitutes_used": false,
    "notes": "Icons clarify repeated concepts and follow the deck palette."
  },
  "subagents": [
    {
      "name": "paper-extraction",
      "used": true,
      "purpose": "Extract method/results facts",
      "skip_reason": null
    }
  ],
  "paper_figures_used": [
    {
      "slide": 4,
      "id": "pdf_fig_001_p3_abcd",
      "href": "../source_assets/images/pdf_fig_001_p3_abcd.png",
      "caption": "Figure 1. Method overview"
    }
  ],
  "visual_qa": "passed|skipped|failed",
  "notes": ["short summary"]
}
```

SVG requirements:

- Root element must be `<svg>`.
- `viewBox` must match `agent_task.json.presentation.viewbox`.
- Use filenames `slide_001.svg`, `slide_002.svg`, etc.
- Use UTF-8 text.
- Use local relative image/icon references only.
- Paper figure references must use exact `href` values from `source_assets/figures.json` or `source_assets/figures.md`.
- Keep all visible content inside the canvas.
- Do not include script tags, event handlers, remote network URLs, or `foreignObject`.
- Do not use letters, initials, ASCII/Unicode symbols, dingbats, stars, pictographs, or emoji as fake icons. Use existing local icon assets or omit the icon.
