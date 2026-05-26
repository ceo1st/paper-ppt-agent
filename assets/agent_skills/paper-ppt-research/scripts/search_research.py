from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PER_SOURCE_TIMEOUT = 20.0
DEFAULT_TOTAL_TIMEOUT = 90.0
DEFAULT_ABSTRACT_CHARS = 900
DEFAULT_BODY_CHARS = 1200
DEFAULT_MAX_TOTAL_RESULTS = 40


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Agent-selected external research queries for Paper PPT Agent."
    )
    parser.add_argument("--query", action="append", required=True, help="Agent-chosen query. Repeatable.")
    parser.add_argument(
        "--source",
        action="append",
        choices=["arxiv", "semantic_scholar", "tavily", "serpapi"],
        default=[],
        help="Source to query. Repeatable. Defaults to arxiv and semantic_scholar.",
    )
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--max-total-results", type=int, default=DEFAULT_MAX_TOTAL_RESULTS)
    parser.add_argument("--per-source-timeout", type=float, default=DEFAULT_PER_SOURCE_TIMEOUT)
    parser.add_argument("--total-timeout", type=float, default=DEFAULT_TOTAL_TIMEOUT)
    parser.add_argument("--abstract-chars", type=int, default=DEFAULT_ABSTRACT_CHARS)
    parser.add_argument("--body-chars", type=int, default=DEFAULT_BODY_CHARS)
    parser.add_argument(
        "--include-raw-content",
        action="store_true",
        help="Ask web providers for raw page content. Off by default to keep Agent context manageable.",
    )
    parser.add_argument("--out", default="research/raw_external_results.json")
    args = parser.parse_args()

    sources = args.source or ["arxiv", "semantic_scholar"]
    max_results = max(1, min(int(args.max_results or 5), 20))
    max_total_results = max(1, min(int(args.max_total_results or DEFAULT_MAX_TOTAL_RESULTS), 100))
    per_source_timeout = max(3.0, float(args.per_source_timeout or DEFAULT_PER_SOURCE_TIMEOUT))
    total_timeout = max(per_source_timeout, float(args.total_timeout or DEFAULT_TOTAL_TIMEOUT))
    abstract_chars = max(120, min(int(args.abstract_chars or DEFAULT_ABSTRACT_CHARS), 2500))
    body_chars = max(0, min(int(args.body_chars or DEFAULT_BODY_CHARS), 3000))
    socket.setdefaulttimeout(per_source_timeout)
    output: dict[str, Any] = {
        "queries": list(args.query),
        "sources_requested": sources,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "limits": {
            "max_results_per_query_source": max_results,
            "max_total_results": max_total_results,
            "per_source_timeout_seconds": per_source_timeout,
            "total_timeout_seconds": total_timeout,
            "abstract_chars": abstract_chars,
            "body_chars": body_chars,
            "include_raw_content": bool(args.include_raw_content),
        },
        "results": [],
        "errors": [],
        "source_status": [],
    }

    async def _run_all() -> None:
        seen: set[tuple[str, str]] = set()
        for query in args.query:
            for source in sources:
                if len(output["results"]) >= max_total_results:
                    output["source_status"].append(
                        {
                            "source": source,
                            "query": query,
                            "status": "skipped",
                            "reason": "max_total_results reached",
                        }
                    )
                    continue
                try:
                    rows = await asyncio.wait_for(
                        _search_source(
                            source,
                            query,
                            max_results,
                            abstract_chars=abstract_chars,
                            body_chars=body_chars,
                            include_raw_content=bool(args.include_raw_content),
                        ),
                        timeout=per_source_timeout,
                    )
                    added = 0
                    for row in rows:
                        title = str(row.get("title") or "").strip().lower()
                        url = str(row.get("url") or "").strip().lower()
                        key = (title, url)
                        if key in seen:
                            continue
                        seen.add(key)
                        output["results"].append(row)
                        added += 1
                        if len(output["results"]) >= max_total_results:
                            break
                    output["source_status"].append(
                        {
                            "source": source,
                            "query": query,
                            "status": "ok",
                            "results": added,
                        }
                    )
                except asyncio.TimeoutError:
                    message = f"Timed out after {per_source_timeout:g}s"
                    output["errors"].append({"source": source, "query": query, "error": message})
                    output["source_status"].append(
                        {"source": source, "query": query, "status": "timeout", "error": message}
                    )
                except Exception as exc:  # noqa: BLE001 - report per-source failure.
                    message = str(exc) or exc.__class__.__name__
                    output["errors"].append({"source": source, "query": query, "error": message})
                    output["source_status"].append(
                        {"source": source, "query": query, "status": "error", "error": message}
                    )

    try:
        await asyncio.wait_for(_run_all(), timeout=total_timeout)
        output["status"] = "partial" if output["errors"] else "complete"
    except asyncio.TimeoutError:
        output["status"] = "partial"
        output["errors"].append({"source": "all", "query": "*", "error": f"Total timeout after {total_timeout:g}s"})

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = out_path.with_name("external_search_summary.json")
    summary = {
        "status": output.get("status"),
        "queries": output["queries"],
        "sources_requested": sources,
        "result_count": len(output["results"]),
        "errors": output["errors"],
        "top_results": [
            {
                "source": row.get("source"),
                "query": row.get("query"),
                "title": row.get("title"),
                "url": row.get("url"),
                "year": row.get("year"),
                "read_depth": row.get("read_depth"),
            }
            for row in output["results"][:12]
        ],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(out_path))
    print(str(summary_path))
    return 0


async def _search_source(
    source: str,
    query: str,
    max_results: int,
    *,
    abstract_chars: int,
    body_chars: int,
    include_raw_content: bool,
) -> list[dict[str, Any]]:
    if source == "arxiv":
        return await asyncio.to_thread(_search_arxiv, query, max_results, abstract_chars)
    if source == "semantic_scholar":
        return await _search_semantic_scholar(query, max_results, abstract_chars)
    if source == "tavily":
        return await _search_tavily(query, max_results, body_chars, include_raw_content)
    if source == "serpapi":
        return await _search_serpapi(query, max_results)
    raise ValueError(f"Unsupported source: {source}")


def _search_arxiv(query: str, max_results: int, abstract_chars: int) -> list[dict[str, Any]]:
    import arxiv  # type: ignore[import-not-found]

    client = arxiv.Client(page_size=max_results)
    search = arxiv.Search(query=query, max_results=max_results)
    rows: list[dict[str, Any]] = []
    for item in client.results(search):
        rows.append(
            {
                "source": "arxiv",
                "query": query,
                "title": str(item.title or ""),
                "url": str(item.entry_id or item.pdf_url or ""),
                "abstract": str(item.summary or "")[:abstract_chars],
                "authors": [author.name for author in item.authors][:8],
                "year": item.published.year if item.published else None,
                "read_depth": "metadata",
            }
        )
    return rows


async def _search_semantic_scholar(query: str, max_results: int, abstract_chars: int) -> list[dict[str, Any]]:
    from semanticscholar import AsyncSemanticScholar  # type: ignore[import-not-found]

    client = AsyncSemanticScholar(
        api_key=os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or None,
        timeout=8,
    )
    results = await client.search_paper(
        query,
        limit=max_results,
        fields=["title", "authors", "abstract", "citationCount", "year", "url", "externalIds"],
    )
    rows: list[dict[str, Any]] = []
    for paper in results:
        authors = getattr(paper, "authors", []) or []
        external = getattr(paper, "externalIds", None) or {}
        arxiv_id = external.get("ArXiv", "") if isinstance(external, dict) else ""
        rows.append(
            {
                "source": "semantic_scholar",
                "query": query,
                "title": str(getattr(paper, "title", "") or ""),
                "url": str(getattr(paper, "url", "") or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "")),
                "abstract": str(getattr(paper, "abstract", "") or "")[:abstract_chars],
                "authors": [str(getattr(author, "name", "") or "") for author in authors][:8],
                "year": getattr(paper, "year", None),
                "citation_count": getattr(paper, "citationCount", None),
                "read_depth": "metadata",
            }
        )
    return rows


async def _search_tavily(
    query: str,
    max_results: int,
    body_chars: int,
    include_raw_content: bool,
) -> list[dict[str, Any]]:
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY is not set")
    import httpx  # type: ignore[import-not-found]

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            "https://api.tavily.com/search",
            json={
                "query": query,
                "api_key": api_key,
                "max_results": max_results,
                "include_images": False,
                "search_depth": "advanced",
                "include_raw_content": include_raw_content,
            },
        )
        response.raise_for_status()
        data = response.json()
    rows: list[dict[str, Any]] = []
    for item in data.get("results", []):
        if not isinstance(item, dict):
            continue
        raw = str(item.get("raw_content") or "").strip()
        rows.append(
            {
                "source": "tavily",
                "query": query,
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("content") or "")[:1000],
                "body_excerpt": raw[:body_chars] if body_chars else "",
                "read_depth": "opened" if raw else "search_result",
            }
        )
    return rows


async def _search_serpapi(query: str, max_results: int) -> list[dict[str, Any]]:
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        raise RuntimeError("SERPAPI_KEY is not set")
    import httpx  # type: ignore[import-not-found]

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            "https://serpapi.com/search",
            params={"engine": "google", "q": query, "api_key": api_key, "num": max_results},
        )
        response.raise_for_status()
        data = response.json()
    rows: list[dict[str, Any]] = []
    for item in data.get("organic_results", []):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "source": "serpapi",
                "query": query,
                "title": str(item.get("title") or ""),
                "url": str(item.get("link") or ""),
                "snippet": str(item.get("snippet") or "")[:1000],
                "read_depth": "search_result",
            }
        )
    return rows


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
