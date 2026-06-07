"""Search icon metadata without enumerating or reading SVG files."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in TOKEN_RE.findall(value.lower().replace("-", " "))
        if len(token) >= 3
    }


def _score(query: str, icon: dict) -> float:
    query_tokens = _tokens(query)
    metadata = " ".join(
        [
            str(icon.get("name") or ""),
            str(icon.get("category") or ""),
            " ".join(str(tag) for tag in icon.get("tags", []) or []),
        ]
    )
    overlap = query_tokens & _tokens(metadata)
    if not overlap:
        return 0.0
    score = len(overlap) / max(1, len(query_tokens))
    if overlap & _tokens(str(icon.get("name") or "")):
        score += 0.35
    return score


def _resolve_icons_dir(task_path: Path, task: dict) -> Path:
    configured = Path(str(task["paths"]["icons"]))
    if configured.is_absolute():
        return configured
    return (task_path.parent / configured).resolve()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="agent_task.json")
    parser.add_argument("--query", required=True)
    parser.add_argument("--library")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()

    task_path = Path(args.task).resolve()
    task = json.loads(task_path.read_text(encoding="utf-8"))
    icons_dir = _resolve_icons_dir(task_path, task)
    metadata_path = icons_dir / "index_meta.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    library = args.library or task.get("presentation", {}).get("icon_library")

    ranked = []
    for icon in metadata.get("icons", []):
        if library and icon.get("lib") != library:
            continue
        score = _score(args.query, icon)
        if score <= 0:
            continue
        ranked.append((score, icon))
    ranked.sort(key=lambda item: (item[0], str(item[1].get("name") or "")), reverse=True)

    results = [
        {
            "path": icon.get("path"),
            "name": icon.get("name"),
            "library": icon.get("lib"),
            "category": icon.get("category", ""),
            "tags": icon.get("tags", []),
            "score": round(score, 4),
        }
        for score, icon in ranked[: max(0, args.limit)]
    ]
    print(json.dumps({"query": args.query, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
