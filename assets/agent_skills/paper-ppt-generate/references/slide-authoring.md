# Slide SVG Authoring

Author clean SVG for PowerPoint conversion.

Canvas:

- Use the exact `viewBox` from `agent_task.json.presentation.viewbox`.
- Common 16:9 canvas is `0 0 1280 720`.
- Common 4:3 canvas is `0 0 1024 768`.

Layout:

- Keep margins stable across the deck.
- Avoid tiny text. Prefer fewer, clearer points.
- Use template assets in `templates/` when available.
- Use local icons from the configured icons path; verify files exist before referencing them.

Icons:

- Search the configured local icon path before placing an icon. Useful commands include `rg --files <icons_path>`, `rg -i "<concept>" <icons_path>`, or a recursive SVG listing.
- Allowed icons are existing local SVG/icon files or template-provided vector assets that actually exist.
- Do not use letters, initials, ASCII characters, Unicode symbols, dingbats, stars, checkmarks, arrows, pictographs, or emoji as icon substitutes.
- If no suitable local icon exists, omit the icon or use a neutral non-icon shape/accent. Never fake an icon with text such as `P`, `F`, `A`, or a star glyph.
- Keep `design_spec.md` or `agent_report.json` aware of the icon choices so QA can check missing assets and policy violations.

Text:

- Use normal SVG `<text>` elements.
- Avoid excessive nested `tspan` complexity.
- Do not use `foreignObject`.
- Keep line lengths short enough for the slide.

Images:

- Use local relative paths.
- Prefer figures that directly support the slide's argument.
- Avoid remote URLs.

Consistency:

- Write `design_spec.md` before drafting many slides.
- For parallel slide authoring, treat `design_spec.md` as the single source of visual truth.
- Structural slides (cover, TOC, chapter, ending) should be sparse.
- Content slides should prioritize method, evidence, results, and takeaways.
