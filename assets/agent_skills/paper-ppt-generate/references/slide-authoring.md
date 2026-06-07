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
- Author one explicitly named slide SVG at a time. Do not build a deck generator, loop over slide definitions, or emit pages through shared renderer functions. Each page's Region Plan and final SVG markup must be written for that page.
- Shared colors, typography, margins, and recurring chrome may be repeated directly. They are not permission to reuse a universal page skeleton or card grid for unrelated content.
- Put styling directly on SVG elements. Do not use `<style>` blocks or `class=` attributes in final slide SVGs; inline `fill`, `stroke`, `font-family`, `font-size`, opacity, and line attributes instead.
- Use local icons from the configured icons path; verify files exist before referencing them.
- Treat the per-slide Region Plan in `design_spec.md` as the layout contract. Instantiate the planned x/y/w/h boxes first, then place figures, text, cards, charts, and callouts inside those boxes.
- For ordinary content slides, aim for 65-85% meaningful occupancy of the content area. Whitespace is useful for hierarchy, but do not leave an empty quadrant, a short bullet list floating in a large blank field, or a bottom callout visually detached from the main grid.
- Use 24-40px gutters between major regions and align edges across repeated layout families.
- For figure slides, reserve the figure frame first and fit the image inside that frame preserving aspect ratio. Do not stretch the paper figure to fill a convenient box.
- If content is sparse, fill space with a supported visual explanation: enlarge the paper figure, split bullets into labeled callouts, add a mechanism/process diagram grounded in the paper, or use a comparison strip. Do not invent data, axes, or decorative mini charts.
- Before writing markup, calculate text capacity for each box. At minimum, check `text_right <= box_right - padding` and `last_baseline + font_size*0.25 <= box_bottom - padding`.
- Reserve vertical space explicitly: heading, heading-to-body gap, `line_count * line_height`, caption/source, and bottom padding. Do not use an 80px card for a heading plus two body lines merely because the first baseline fits.
- For tables, measure the longest visible cell in each column, assign column widths first, and right-align numeric columns. Do not position rows by guessed x coordinates that let labels and values overlap.

Icons:

- Search the configured local icon path before placing an icon. Useful commands include `rg --files <icons_path>`, `rg -i "<concept>" <icons_path>`, or a recursive SVG listing.
- Allowed icons are existing local SVG/icon files or template-provided vector assets that actually exist.
- Do not use letters, initials, ASCII characters, Unicode symbols, dingbats, stars, checkmarks, arrows, pictographs, or emoji as icon substitutes.
- If no suitable local icon exists, omit the icon or use a neutral non-icon shape/accent. Never fake an icon with text such as `P`, `F`, `A`, or a star glyph.
- Keep `design_spec.md` or `agent_report.json` aware of the icon choices so QA can check missing assets and policy violations.

Text:

- Use normal SVG `<text>` elements.
- Avoid excessive nested `tspan` complexity.
- Do not use `foreignObject`, `<style>`, CSS classes, masks, filters, clip paths, scripts, event handlers, or remote URLs.
- Keep line lengths short enough for the slide.
- For Chinese body copy, target about 24-36 CJK characters per line in ordinary paragraphs and 18-30 in cards. Avoid several consecutive visible lines under 10 CJK characters except labels, legends, or final paragraph lines.
- If wrapping looks ragged, fix the layout or copy before finishing: widen the text box, reduce font size slightly, split the sentence into callouts, or rewrite more concisely. Do not accept huge blank bands caused by narrow text boxes.
- Preserve exact source numbers and units. Do not use K/M/B shorthand, reduce decimal precision, or manufacture chart ticks unless the source itself does so. Put transparent derivations and their inputs on the same slide.

Images:

- Use local relative paths.
- Prefer figures that directly support the slide's argument.
- Avoid remote URLs.
- For paper figures, read `source_assets/figures.json` or `source_assets/figures.md`.
- Use the exact manifest `href` in `<image href="...">`; from `svg_output/` it normally starts with `../source_assets/images/`.
- Preserve the listed natural aspect ratio. If you need a different visual box, letterbox or crop deliberately with SVG clipping; do not stretch the image.
- Choose figures by caption, page number, section/context, and slide claim. Do not choose a figure only because the filename looks convenient.

Consistency:

- Write `design_spec.md` before drafting many slides.
- For parallel slide authoring, treat `design_spec.md` as the single source of visual truth.
- Structural slides (cover, TOC, chapter, ending) should be sparse.
- Content slides should prioritize method, evidence, results, and takeaways.
- Consistency means common visual rules, not identical page construction. Select each content slide's layout from its own evidence shape: figure, process, comparison, table, equation, or argument.
