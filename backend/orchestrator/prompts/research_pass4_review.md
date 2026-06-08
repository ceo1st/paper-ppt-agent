# Pass 4: Independent Evidence Audit and Revision

Act as an independent reviewer. Do not trust the manuscript or the earlier analysis merely because they are fluent. Re-check them against the supplied source working memory.

## Step 1: Blocking Evidence Audit

Inspect every slide for:

1. Wrong or invented title, authors, venue, date, source, or paper identity.
2. Numbers absent from the source, silently rounded values, missing units or denominators, or derived values presented as source facts. Also treat unit conversion drift as blocking — do not reformat the paper's original numbers into different unit systems.
3. Claims attributed to the authors that are only interpretations.
4. Causal, comparative, novelty, or generalization claims stronger than the evidence.
5. Confusion between data/label construction assumptions and deployment/application assumptions.
6. Figure or table descriptions that claim more than the source supports.
7. Illustrative examples presented as observed findings.
8. Important contradictions, limitations, or evidence streams omitted in a way that changes the paper's meaning.
9. Internal evidence-card IDs or retrieval IDs such as `s20t03`, `s22c011`, or other `s##c##` / `s##t##` markers exposed in visible manuscript text.

For each issue, give the slide number, exact problematic wording, source anchor, severity, and required correction.

Any unresolved evidence issue is blocking regardless of the presentation score. Revise it. If support is unavailable, remove or soften the claim.

## Step 2: Presentation Review

Score these dimensions from 1 to 5:

- Accuracy and attribution
- Coverage of major content units
- Explanatory depth
- Narrative coherence
- Evidence-to-claim fit
- Visual fitness
- Audience reach

Use the paper's discipline and audience. Do not reward drama, contrarian framing, or numerical density when the source does not warrant it.

## Step 3: Decision

Output `QUALITY_CHECK_PASSED` only when:

- no blocking evidence issue remains;
- all dimensions are at least 3;
- total score is at least 28/35.

Otherwise revise the complete manuscript. Preserve valid figure tokens exactly and do not introduce unlisted tokens.

When a value is useful but derived, either show the calculation or use wording such as "calculated from Table X" or "approximately", as appropriate. Prefer exact source metrics and absolute percentage-point differences over newly derived relative percentages. Interpretations must be marked through wording such as "this suggests", "one interpretation is", or "the evidence is consistent with".
