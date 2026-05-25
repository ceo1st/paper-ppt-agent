# Output Contract

Required files:

- `manuscript.md`: slide-by-slide manuscript.
- `design_spec.md`: visual system, template usage, slide inventory, and per-slide design intent.
- `svg_output/slide_001.svg`, `svg_output/slide_002.svg`, ...: final authoring SVGs.
- `agent_report.json`: machine-readable summary.

Optional files:

- `notes/slide_001.md`, ...: speaker notes matched by SVG stem.
- `research/brief.md` and `research/sources.json`: research summary and sources.
- `qa/*.md` or `qa/*.json`: review results.

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
    "assets_used": ["relative/path/icon.svg"],
    "substitutes_used": false,
    "notes": "No letters/symbols/emoji were used as icon substitutes."
  },
  "subagents": [
    {
      "name": "paper-extraction",
      "used": true,
      "purpose": "Extract method/results facts",
      "skip_reason": null
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
- Keep all visible content inside the canvas.
- Do not include script tags, event handlers, remote network URLs, or `foreignObject`.
- Do not use letters, initials, ASCII/Unicode symbols, dingbats, stars, pictographs, or emoji as fake icons. Use existing local icon assets or omit the icon.
