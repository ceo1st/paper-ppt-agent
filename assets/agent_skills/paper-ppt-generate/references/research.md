# Research Guidance

Use this only when `agent_task.json.agent_policy.allow_external_research` or
`allow_deep_research` is true.

External research:

- Search for the paper title, authors, venue, related work, and known follow-up discussions.
- Selectively open sources. Do not dump raw search results into the manuscript.
- Do not use search result titles, snippets, or abstracts alone as evidence. Open relevant pages and read enough body content to understand the claim, method/evidence, publication date, and reliability.
- Prefer primary sources, paper pages, arXiv, Semantic Scholar, project pages, and official repositories.
- Use API keys from the environment when present: `SEMANTIC_SCHOLAR_API_KEY`, `TAVILY_API_KEY`, and `SERPAPI_KEY`. If a configured provider is unavailable, use other accessible search/read methods and record the limitation.
- Write `research/sources.json` with title, URL, source type, and why it matters.
- Write `research/brief.md` with only slide-relevant context.
- Before drafting the manuscript or SVGs, report back to the user with your own short synthesis of usable sources, key slide-relevant findings, source limitations/failures, and any resulting deck-plan adjustment. Do not reply with only query counts.

Deep research:

- Split reading into focused passes: problem, method, experiments, limitations, and presentation storyline.
- When the Task/SubAgent tool is available, use focused SubAgents for deep research. Only skip SubAgents if unavailable or failed, and record the reason in `agent_report.json.subagents`.
- Merge findings into `manuscript.md`; do not leave unresolved contradictions.
- Make sure the deck remains faithful to the uploaded paper.
